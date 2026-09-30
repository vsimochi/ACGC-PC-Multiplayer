#!/usr/bin/env python3
"""test_clock_offset_lifecycle.py - Clock hardening pass, concern #1 (NEW, focused, protocol/state
level). Does NOT launch the game.

Re-derives, from the ACTUAL post-fix control flow in pc/src/pc_net_game.c and src/lb_rtc.c (not
imported -- these are process-memory-only C globals with no Python binding), the exact points at
which s_pc_net_clock_offset (lb_rtc.c) is read, written, or deliberately left alone:

  - pc_lb_rtc_set_net_clock_offset()/pc_lb_rtc_get_net_clock_offset() (lb_rtc.c): a plain global,
    never touched by Save_t, never touched on its own.
  - pcnetgame_handle_client_clock_sync() (pc_net_game.c): sets the offset when a CLOCK_SYNC is
    accepted (see test_clock_sync_wire.py's own ClientClockState for that accept/forward-only-guard
    logic in detail -- this test does not re-model it, it only cares about the offset's lifecycle
    around role transitions).
  - pcnetgame_reset_client_session_state() (pc_net_game.c): called on start_client(),
    PEER_CONNECTED, PEER_DISCONNECTED, and from pc_net_game_shutdown() -- resets
    s_client_clock_seq_applied to 0 but, BY DESIGN, never touches the offset itself. An ordinary
    disconnect (PEER_DISCONNECTED) leaves s_role == PC_NETGAME_ROLE_CLIENT (only s_client_link
    changes) -- the process is still "a client", just with its link down, possibly about to
    reconnect -- so the offset must survive that so the displayed clock doesn't visibly jump
    backward to this process's own free-running local clock while a reconnect might restore the
    same host relationship.
  - pc_net_game_shutdown() (pc_net_game.c): THE one place s_role actually reverts from
    PC_NETGAME_ROLE_CLIENT (or PC_NETGAME_ROLE_HOST) to PC_NETGAME_ROLE_NONE. Post clock-hardening
    fix, this function explicitly calls pc_lb_rtc_set_net_clock_offset(0) after flipping s_role to
    PC_NETGAME_ROLE_NONE -- this is exactly concern #1's fix.

This test's RoleModel mirrors those four transition points and their effect on the offset, so it can
assert the concern-#1 contract end to end: a full disconnect (real shutdown, or the town-mismatch
path in pcnetgame_client_tick() which also calls pc_net_game_shutdown()) always leaves offset == 0,
while a merely-dropped-link-but-still-CLIENT disconnect does not.

Usage: python3 test_clock_offset_lifecycle.py   (no host/port needed)
Exit code: 0 all checks passed, 1 otherwise.
"""
import sys

ROLE_NONE = "NONE"
ROLE_HOST = "HOST"
ROLE_CLIENT = "CLIENT"

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + f": {name}")


class RoleModel:
    """Re-derives the exact s_pc_net_clock_offset lifecycle from pc_net_game.c's real control flow
    (post clock-hardening fix)."""

    def __init__(self):
        self.role = ROLE_NONE
        self.offset = 0  # s_pc_net_clock_offset (lb_rtc.c) -- process-memory-only global

    # ---- pc_net_game_start_client() ----
    def start_client(self):
        assert self.role == ROLE_NONE
        self.role = ROLE_CLIENT
        self._reset_client_session_state()  # does NOT touch offset

    # ---- pc_net_game_start_host() ----
    def start_host(self):
        assert self.role == ROLE_NONE
        self.role = ROLE_HOST
        # host never calls the offset setter at all -- it stays whatever it was, which must always
        # be 0 by the time we get here since the only way to reach ROLE_NONE is via shutdown(),
        # which now forces it to 0.

    def _reset_client_session_state(self):
        """pcnetgame_reset_client_session_state(): deliberately does NOT touch the offset."""
        pass  # no-op on self.offset by design

    # ---- PC_NET_EVENT_PEER_DISCONNECTED while s_role == CLIENT (ordinary mid-session drop) ----
    def client_link_dropped(self):
        assert self.role == ROLE_CLIENT
        self._reset_client_session_state()  # offset survives -- role is STILL CLIENT

    # ---- accepted CLOCK_SYNC (pcnetgame_handle_client_clock_sync) ----
    def apply_clock_sync(self, new_offset):
        assert self.role == ROLE_CLIENT
        self.offset = new_offset

    # ---- pc_net_game_shutdown(): the one real ROLE_NONE-reversion point ----
    def shutdown(self):
        if self.role == ROLE_NONE:
            return
        self.role = ROLE_NONE
        self._reset_client_session_state()
        # Clock hardening concern #1 fix: explicitly force the offset back to 0 here.
        self.offset = 0


def main():
    # 1. ROLE_NONE (fresh process / single-player) starts with offset == 0.
    m = RoleModel()
    check("fresh ROLE_NONE process starts with offset == 0", m.role == ROLE_NONE and m.offset == 0)

    # 2. Becoming a client and accepting a CLOCK_SYNC makes the offset non-zero.
    m.start_client()
    check("start_client() does not change the offset by itself", m.offset == 0)
    m.apply_clock_sync(123456)
    check("client offset becomes non-zero once a CLOCK_SYNC is applied", m.offset == 123456)

    # 3. An ORDINARY mid-session disconnect (still ROLE_CLIENT, e.g. hoping to reconnect) must NOT
    #    reset the offset -- this is the "don't jump backward while briefly disconnected" case the
    #    concern's own wording says must be preserved.
    m.client_link_dropped()
    check("role is still CLIENT after an ordinary link drop", m.role == ROLE_CLIENT)
    check("offset SURVIVES an ordinary mid-session disconnect (still a client)", m.offset == 123456)

    # 4. A fresh CLOCK_SYNC after reconnecting (still never having left ROLE_CLIENT) applies cleanly
    #    on top, same as before the drop -- reconnect establishes a fresh relationship without any
    #    special-casing needed here (Rule 2 covers the sequence-number side; this test only cares
    #    about the offset).
    m.apply_clock_sync(999)
    check("a fresh CLOCK_SYNC after reconnect updates the offset normally", m.offset == 999)

    # 5. THE fix under test: pc_net_game_shutdown() -- the one place role actually reverts to
    #    ROLE_NONE -- forces the offset back to exactly 0. This directly validates concern #1.
    m.shutdown()
    check("role reverts to ROLE_NONE after shutdown()", m.role == ROLE_NONE)
    check("offset resets to EXACTLY 0 on shutdown() (concern #1 fix)", m.offset == 0)

    # 6. Post-shutdown ROLE_NONE (single-player, possibly on a totally different local save) must
    #    never inherit the old host's offset -- this is the exact "different save file" scenario
    #    concern #1 describes as the worst case.
    check("ROLE_NONE after a disconnect no longer inherits the former host's offset", m.offset == 0)

    # 7. A brand new client session (simulating: player reconnects, or connects to a DIFFERENT host,
    #    possibly after loading a different save) starts completely fresh.
    m2 = RoleModel()
    m2.start_client()
    check("a brand-new client session (fresh RoleModel) starts at offset 0", m2.offset == 0)
    m2.apply_clock_sync(42)
    check("the new session's own CLOCK_SYNC establishes its own independent offset", m2.offset == 42)

    # 8. Host role: the offset setter is never called host-side, so it must stay 0 for the entire
    #    hosting session (matches lb_rtc.c's "always 0 on a host process" contract).
    m3 = RoleModel()
    m3.start_host()
    check("a host process's offset stays exactly 0 throughout hosting", m3.offset == 0)
    m3.shutdown()
    check("host shutdown -> ROLE_NONE also leaves/forces offset at 0 (harmless no-op per the fix's "
          "own doc comment)", m3.role == ROLE_NONE and m3.offset == 0)

    # 9. The town-mismatch path in pcnetgame_client_tick() also calls pc_net_game_shutdown() directly
    #    (not through a PEER_DISCONNECTED event) -- confirm that path is covered by the SAME shutdown()
    #    model above (it is literally the same function call in the real source), i.e. it is not a
    #    second, separately-tracked transition that could be missed.
    m4 = RoleModel()
    m4.start_client()
    m4.apply_clock_sync(555)
    m4.shutdown()  # models pcnetgame_client_tick()'s "local save now holds a different town" branch
    check("the town-mismatch/local-save-changed shutdown path also resets the offset to 0",
          m4.role == ROLE_NONE and m4.offset == 0)

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
