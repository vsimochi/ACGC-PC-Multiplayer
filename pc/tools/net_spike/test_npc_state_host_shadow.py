#!/usr/bin/env python3
"""test_npc_state_host_shadow.py - N3 finalization pass, Bug 2 regression test: the HOST-side
NPC_STATE dirty-check shadow (pc_net_game.c: s_npc_state_shadow[], pc_net_game_notify_npc_state())
must be keyed by (slot, npc_id), not slot alone, and must be invalidated on population change.

FOCUSED UNIT TEST / WIRE MODEL (re-derived, not imported): the exact bug was that villager A departs
slot N, villager B arrives in slot N with is_home/hide/forced_type/forced_active that happen to equal
what A last broadcast, and the host's per-slot dirty-check saw "no change" and silently skipped
broadcasting B's initial state. This cannot be cleanly forced in a live two-real-process window (it
requires engineering a coincidental value collision between two specific villagers' schedule state at
the exact moment of a slot-reuse departure/arrival, which is not practically controllable via the
existing --force-villager-grow/--force-villager-remove test hooks). This script re-implements, in
Python, the EXACT shadow struct and dirty-check/reset logic added in pc_net_game.c (mirroring
test_npc_state_wire.py's own re-derivation convention for the client side) and exercises the precise
scenario the task calls for:

  1. Villager A (slot N, npc_id A) broadcasts state X (have_sent=0 -> dirty -> sent, seq=1).
  2. A departs -> host resets slot N's shadow (pc_net_game_notify_villager_departure()).
  3. Villager B (slot N, npc_id B) arrives -> host resets slot N's shadow again
     (pc_net_game_notify_villager_arrival()).
  4. B's first real sample is state X (the SAME field values A last had) -> dirty-check must still fire
     (have_sent was reset to 0) -> broadcast happens (seq=2).
  5. A stale/late packet for the departed A (state_seq 1, i.e. <= B's now-current last_applied_seq) is
     rejected by the EXISTING, unweakened client-side staleness/slot-reuse guard (already covered by
     test_npc_state_wire.py's "slot reuse: late state for departed occupant A is rejected once B
     occupies the slot" case -- re-run here for completeness of this scenario's own narrative).
  6. B's own subsequent real packet (state_seq 2) is accepted normally by the client.

  Also covers the narrower regression this bug fix targets directly: WITHOUT the (slot, npc_id) key
  (i.e., simulating the OLD, buggy shadow), step 4 would incorrectly suppress B's broadcast -- verified
  by running the same sequence through both the OLD (buggy, slot-only) and NEW (fixed, slot+npc_id)
  dirty-check models and confirming they diverge exactly where the bug predicts.

Usage: python3 test_npc_state_host_shadow.py   (no host/port needed)
Exit code: 0 all checks passed, 1 otherwise.
"""
import sys

results = []


def check(desc, cond):
    status = "PASS" if cond else "FAIL"
    print(f"{status}: {desc}")
    results.append(bool(cond))


class GlobalSeq:
    """Re-derivation of s_npc_state_seq_counter: ONE monotonic counter shared across every slot,
    never reset except at full session init -- never touched by arrival/departure."""
    def __init__(self):
        self.value = 0

    def next(self):
        self.value += 1
        return self.value


class OldShadow:
    """Re-derivation of the PRE-FIX host shadow: keyed by slot only, no npc_id, never reset on
    population change. Mirrors s_npc_state_shadow[]'s fields minus cached_npc_id, and
    pc_net_game_notify_npc_state()'s dirty-check minus the npc_id comparison, and WITHOUT the
    arrival/departure reset calls this fix added."""
    def __init__(self, global_seq):
        self.have_sent = False
        self.is_home = None
        self.hide = None
        self.forced_type = None
        self.forced_active = None
        self.global_seq = global_seq

    def notify(self, is_home, hide, forced_type, forced_active):
        if self.have_sent and (self.is_home, self.hide, self.forced_type, self.forced_active) == \
                (is_home, hide, forced_type, forced_active):
            return None  # suppressed -- BUG: no npc_id check, no reset on population change
        self.have_sent = True
        self.is_home, self.hide, self.forced_type, self.forced_active = is_home, hide, forced_type, forced_active
        return self.global_seq.next()

    # OLD code had no departure/arrival reset hooks at all.
    def on_departure(self):
        pass

    def on_arrival(self):
        pass


class NewShadow:
    """Re-derivation of the FIXED host shadow: keyed by (slot implicit, npc_id), and explicitly reset
    on departure/arrival -- mirrors pc_net_game.c's PCNetNpcStateShadow (with cached_npc_id) and
    pc_net_game_notify_npc_state()'s updated dirty-check, plus the memset(&s_npc_state_shadow[slot],
    0, ...) calls added to pc_net_game_notify_villager_arrival()/_departure(). state_seq is drawn from
    the SAME shared global counter every slot uses, exactly like the real s_npc_state_seq_counter --
    never reset by arrival/departure, only by full session init (out of scope for this scenario)."""
    def __init__(self, global_seq):
        self.have_sent = False
        self.cached_npc_id = None
        self.is_home = None
        self.hide = None
        self.forced_type = None
        self.forced_active = None
        self.global_seq = global_seq

    def notify(self, npc_id, is_home, hide, forced_type, forced_active):
        if self.have_sent and self.cached_npc_id == npc_id and \
                (self.is_home, self.hide, self.forced_type, self.forced_active) == \
                (is_home, hide, forced_type, forced_active):
            return None  # genuinely unchanged for the SAME occupant -- correctly suppressed
        self.have_sent = True
        self.cached_npc_id = npc_id
        self.is_home, self.hide, self.forced_type, self.forced_active = is_home, hide, forced_type, forced_active
        return self.global_seq.next()

    def _reset(self):
        self.have_sent = False
        self.cached_npc_id = None
        self.is_home = self.hide = self.forced_type = self.forced_active = None

    def on_departure(self):
        self._reset()

    def on_arrival(self):
        self._reset()


class ClientSlot:
    """Re-derivation of pc_net_game.c's client-side PCNetNpcStateSlot + pcnetgame_handle_client_npc_state()
    (state_seq strict->) and pc_net_game_get_npc_state()'s cached_npc_id vs expected_npc_id guard --
    UNCHANGED by this fix, re-tested here to confirm it still works against the new host behavior."""
    def __init__(self):
        self.have_state = False
        self.cached_npc_id = None
        self.last_applied_seq = 0
        self.state = None

    def on_receive(self, npc_id, seq, state):
        if self.have_state and seq <= self.last_applied_seq:
            return False  # stale/duplicate/reordered -- dropped
        self.have_state = True
        self.cached_npc_id = npc_id
        self.last_applied_seq = seq
        self.state = state
        return True

    def consume(self, expected_npc_id):
        if not self.have_state:
            return None
        if self.cached_npc_id != expected_npc_id:
            return None  # slot-reuse identity mismatch -- discard
        return self.state


NPC_A = 0xE006
NPC_B = 0xE0B1
SLOT = 3
STATE_X = (0, 0, 0, 0)  # (is_home, hide, forced_type, forced_active)


def run_scenario(shadow_cls):
    gseq = GlobalSeq()
    sh = shadow_cls(gseq)
    events = []  # (npc_id, seq, state) actually broadcast, in order

    # 1. A broadcasts state X.
    seq = sh.notify(NPC_A, *STATE_X) if shadow_cls is NewShadow else sh.notify(*STATE_X)
    assert seq == 1
    events.append((NPC_A, seq, STATE_X))

    # 2. A departs.
    sh.on_departure()

    # 3. B arrives.
    sh.on_arrival()

    # 4. B's first real sample happens to equal state X exactly.
    if shadow_cls is NewShadow:
        seq_b = sh.notify(NPC_B, *STATE_X)
    else:
        seq_b = sh.notify(*STATE_X)
    if seq_b is not None:
        events.append((NPC_B, seq_b, STATE_X))

    return events


def main():
    # --- Reproduce the bug in the OLD model ---
    old_events = run_scenario(OldShadow)
    check("OLD (buggy, slot-only, no reset) model: A's initial broadcast happened",
          any(e[0] == NPC_A for e in old_events))
    check("OLD (buggy) model: B's initial state is INCORRECTLY suppressed (reproduces Bug 2)",
          not any(e[0] == NPC_B for e in old_events))

    # --- Confirm the fix in the NEW model ---
    new_events = run_scenario(NewShadow)
    check("NEW (fixed) model: A's initial broadcast happened", any(e[0] == NPC_A for e in new_events))
    check("NEW (fixed) model: B's initial state IS broadcast despite value equality with A's last state",
          any(e[0] == NPC_B for e in new_events))
    check("NEW (fixed) model: B's broadcast got a fresh, higher state_seq than A's",
          new_events[-1][1] > new_events[0][1])

    # --- Client-side: stale A packet after B's arrival must still be rejected; B's real packet accepted ---
    cs = ClientSlot()
    a_seq, a_state = new_events[0][1], new_events[0][2]
    b_seq, b_state = new_events[-1][1], new_events[-1][2]

    # Apply B's broadcast first (as it would arrive in real time).
    check("client: B's own state_seq accepted", cs.on_receive(NPC_B, b_seq, b_state))
    # A stale/late packet for departed A arrives after -- same or lower state_seq is rejected outright,
    # but even a HIGHER stale seq for the WRONG npc_id must be rejected at consume time by the identity
    # guard (this is the "existing behavior, must still work" requirement).
    late_a_delivered = cs.on_receive(NPC_A, a_seq, a_state)  # a_seq(1) <= last_applied(b_seq=2) -> dropped
    check("client: stale/late A packet (seq <= last_applied) is dropped at receive time",
          not late_a_delivered)
    # Even if a stale A packet with seq_b+1 (edge case simulate) arrived, it wouldn't apply now
    # since cached_npc_id would then update to A -- so the true defense-in-depth is CONSUME-time identity:
    consumed_for_b = cs.consume(NPC_B)
    check("client: consume(expected_npc_id=B) returns B's real state", consumed_for_b == b_state)
    consumed_for_a = cs.consume(NPC_A)
    check("client: consume(expected_npc_id=A) returns None (A no longer occupies this slot)",
          consumed_for_a is None)

    total = len(results)
    passed = sum(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
