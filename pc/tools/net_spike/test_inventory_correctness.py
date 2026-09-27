#!/usr/bin/env python3
"""test_inventory_correctness.py - host-observable tests of the TWO-PHASE shared-world pickup/drop protocol
(final hardening pass: reserve -> confirm -> commit) and of the finite-position hardening.

Why: the old single-phase host mutated the field at REQUEST time and the client then did its inventory step on
the RESULT without re-checking anything -> a drop whose pocket slot had been rearranged destroyed/duplicated
the wrong item, a pickup with full pockets removed the item from the world for nobody, and NaN peer positions
made the reach checks fail open. The contract now is:

  REQUEST  (client -> host)  PICKUP_REQUEST / DROP_REQUEST, validated entirely by the host
  RESULT   (host -> client)  accepted=1 is PROVISIONAL: the tile is RESERVED for the requester, the FIELD HAS NOT
                             CHANGED; accepted=0 is a final rejection (no reservation)
  CONFIRM  (client -> host)  INTERACT_CONFIRM (type 17, 8 bytes: kind, outcome COMMIT/ABORT, reason, request_id)
  COMMIT   the host mutates the field ONLY on a matching CONFIRM(COMMIT) and then broadcasts FIELD_UPDATE
  release  ABORT, a newer request from the same peer, peer disconnect/reset/timeout, expiry after
           PC_NETGAME_CONFIRM_TIMEOUT_MS (20 s). Expiry/abort never mutate the field.

Everything here is observable from fake clients over UDP (the client-side inventory logic -- slot-changed /
pockets-full / owner-stamp -- runs inside the game process and is verified by review + the real two-process
test). Every "the field has NOT changed" assertion uses TWO independent observations: no FIELD_UPDATE reached
any observer, AND a fresh client's authoritative world snapshot still shows the old value. Each check must FAIL
on the old single-phase behaviour (proved in loopback against `net_transport_model.py --buggy-single-phase`).

Groups (select with --only, comma separated; default = all):
  A        INV-A*        drop, pocket slot changed before the result: REQUEST -> provisional RESULT ->
                         ABORT(SLOT_CHANGED): no FIELD_UPDATE, tile still empty, retry of the request_id
                         rejected, COMMIT after the abort ignored, a later drop on the tile succeeds
  B        INV-B*        pickup with full pockets: ... ABORT(POCKETS_FULL): tile still holds the item, a second
                         client can still pick it up
  ci       INV-Ci*       (i)  CONFIRM(COMMIT) with an unknown / old / wrong-kind / another peer's request_id
  civ      INV-Civ*      (iv) the same tile cannot be reserved by two peers (pickup and drop)
  cvi      INV-Cvi*      (vi) a new request from a peer replaces (aborts) its own pending one
  cv       INV-Cv*       (v)  reconnect into the same slot with a colliding request_id (clean DISCONNECT and
                         same-address nonce restart): the old reservation cannot be confirmed
  ciii     INV-Ciii*     (iii) peer disconnects while pending (pickup and drop): reservation released
  timeout  INV-Ctimeout* (iii') peer vanishes (transport timeout ~5 s) while pending: reservation released
  d        INV-D*        NaN / Inf / absurd MOVE positions and non-finite speed (= d-nomove + d-far + d-relay + d-rest):
                         d-nomove: a connection with only invalid samples has no position -> rejected;
                         d-far:    valid FAR position + invalid sample at the target -> rejected;
                         d-relay:  the invalid samples are not relayed to other clients, the host logs the drops;
                         d-rest:   the last VALID position is retained, valid-position controls are accepted
  cross    INV-X*        one pending interaction PER KIND per peer: a pending pickup and a pending drop of the
                         same peer coexist and are independent; a new request of the same kind replaces only
                         that kind's pending one
  expiry   INV-C-expiry* (ii) COMMIT after the reservation expired (~22 s on the real host): no mutation, the
                         tile is available again, a retry of the expired request is rejected
Selectors: --skip-expiry drops the slow group; --expiry-s N tells the test the host's confirm timeout
(default 20; loopback runs the model with a short one).

Usage: python test_inventory_correctness.py <host_ip> <port> [--expiry-s 20] [--only a,b,...] [--skip-expiry]
                                            [--host-log PATH]
       (host must be launched with --pickup-test-seed; exit 0 all pass, 1 any failure, 2 pending/no fixtures)
"""
import argparse
import math
import re
import sys
import time

import net_spike_lib as L

ITM_FOOD_APPLE = 0x2800   # the --pickup-test-seed item
ITM_FOOD_CHERRY = 0x2801
# Fixture tile on which the host's ordinary drop validation refuses a drop even after the apple is picked up
# (probed: 19 of the 20 town fixture tiles accept one, this is the only refusal). It stays usable for pickups,
# but must never be chosen as a DROP target by take_empty().
NO_DROP_TILE = (72, 56)

PICKUP_RES = L.PC_NETGAME_MSG_PICKUP_RESULT
DROP_RES = L.PC_NETGAME_MSG_DROP_RESULT
K_PICKUP = L.CONFIRM_KIND_PICKUP
K_DROP = L.CONFIRM_KIND_DROP
COMMIT = L.CONFIRM_OUTCOME_COMMIT
ABORT = L.CONFIRM_OUTCOME_ABORT
R_POCKETS_FULL = L.CONFIRM_REASON_POCKETS_FULL
R_SLOT_CHANGED = L.CONFIRM_REASON_SLOT_CHANGED
R_CANCELLED = L.CONFIRM_REASON_CANCELLED
R_STALE = L.CONFIRM_REASON_STALE

results = []
pend = []


def ck(desc, cond):
    L.check(desc, bool(cond), results)


class OutOfTiles(Exception):
    pass


# --- host log evidence (only when --host-log is given; the model host prints none) ---------------------------------
# Verbose host lines (pc_net_game.c), one per reserve / commit / release; `%s` is PICKUP or DROP:
#   [NET][%s] host: peer %d request %u reserved tile (%d,%d) item=0x%04X (field unchanged until CONFIRM)
#   [NET][%s] host: peer %d request %u reservation released: <aborted by client (reason=%u)|expired (...)|
#                                     replaced by a newer request from the same peer|peer reset/disconnect> (tile ..)
#   [NET][%s] host: peer %d request %u committed tile (%d,%d) 0x%04X -> 0x%04X
#   [NET] host: peer %d: dropped invalid MOVE sample (frame %u pos=(%g,%g,%g) speed=%g) [%u dropped so far]
TAG = {K_PICKUP: "PICKUP", K_DROP: "DROP"}


def rx_reserved(kind, rid):
    return rf"\[NET\]\[{TAG[kind]}\] host: peer \d+ request {rid}\b reserved tile"


def rx_committed(kind, rid):
    return rf"\[NET\]\[{TAG[kind]}\] host: peer \d+ request {rid}\b committed tile"


def rx_released(kind, rid, how):
    """how: 'abort:<reason>' | 'expired' | 'replaced' | 'disconnect'"""
    if how.startswith("abort:"):
        what = rf"aborted by client \(reason={how.split(':')[1]}\)"
    else:
        what = {"expired": r"expired", "replaced": r"replaced by a newer request", "disconnect": r"peer reset/disconnect"}[how]
    return rf"\[NET\]\[{TAG[kind]}\] host: peer \d+ request {rid}\b reservation released: {what}"


RX_DROPPED_MOVE = r"\[NET\] host: peer (\d+): dropped invalid MOVE sample \(frame (\d+) (.*?)\) \[(\d+) dropped so far\]"


def evaluate_evidence(log_text, evidence):
    """[(desc, ok)] -- every `must` regex present and no `must_not` regex present in the host log."""
    out = []
    for desc, must, must_not in evidence:
        ok = all(re.search(rx, log_text) for rx in must) and not any(re.search(rx, log_text) for rx in must_not)
        out.append((desc, ok))
    return out


class Env:
    """Three long-lived fake clients (A requester, B second client, C observer/third peer) plus helpers."""

    def __init__(self, host_ip, port, expiry_s):
        self.host_ip, self.port, self.expiry_s = host_ip, port, expiry_s
        self.rid_gen = L.make_request_id_counter(9000)
        self.a, self.b, self.c = L.connect_ready_clients(host_ip, port, ["A", "B", "C"])
        self.core = [self.a, self.b, self.c]
        self.extras = []
        self.evidence = []  # (desc, [must regex], [must_not regex]) checked against the host log at the end
        self.bad_sent = 0   # invalid MOVE samples this run has sent (lower bound of the host's dropped counter)
        self.peer_ids = {c.assigned_peer_id for c in self.core}
        for x in self.core:
            x.drain_field_updates(0.3)
        # tile pools: `live` tiles hold ITM_FOOD_APPLE; `empty` tiles are known-empty legal town tiles
        self.live = [t for t in L.TOWN_FIXTURE_TILES if self.a.world.tile_ut(*t) == ITM_FOOD_APPLE]
        self.empty = []
        L.info(f"snapshot shows {len(self.live)} apple fixture tiles")

    # --- plumbing ----------------------------------------------------------------------------------------
    def rid(self):
        return self.rid_gen()

    def settle(self, seconds=0.4):
        L.pump_sleep(seconds)

    def new_client(self, label):
        c = L.FakeClient(label, self.host_ip, self.port)
        c.connect_and_ready(quiet=True)
        self.extras.append(c)
        self.peer_ids.add(c.assigned_peer_id)
        return c

    def close_extras(self):
        for c in self.extras:
            try:
                c.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
        self.extras = []
        self.settle(0.2)

    def marks(self):
        return {c: c.inbox.mark() for c in self.core}

    def fus(self, marks, ut):
        """[(client label, value)] for every FIELD_UPDATE about `ut` that reached a core client since `marks`."""
        out = []
        for c in self.core:
            for g in c.field_updates_since(marks[c]):
                if L.field_update_tuple(g)[:2] == ut:
                    out.append((c.label, g.value))
        return out

    def fu_values(self, marks, ut, label):
        return [v for lb, v in self.fus(marks, ut) if lb == label]

    def wait_all_saw(self, marks, ut, value, timeout=2.0):
        return L.DEFAULT_HUB.wait_until(
            lambda: all(value in self.fu_values(marks, ut, c.label) for c in self.core), timeout)

    def ev(self, desc, must=(), must_not=()):
        self.evidence.append((desc, list(must), list(must_not)))

    def exactly_once(self, marks, ut, value, timeout=2.0):
        """Every core client received `value` for `ut`, and after a settle window EXACTLY once (no duplicates)."""
        if not self.wait_all_saw(marks, ut, value, timeout):
            return False
        self.settle(0.3)
        return all(self.fu_values(marks, ut, c.label) == [value] for c in self.core)

    def tile(self, ut):
        """Authoritative value of a town tile via a fresh client's complete snapshot (non-mutating)."""
        return L.read_tiles_via_snapshot(self.host_ip, self.port, [ut])[ut]

    # --- tile pools ----------------------------------------------------------------------------------------
    def take_live(self):
        if not self.live:
            raise OutOfTiles("no live (apple) fixture tile left")
        return self.live.pop(0)

    def take_empty(self):
        for i, cand in enumerate(self.empty):
            if cand != NO_DROP_TILE:
                return self.empty.pop(i)
        if not any(x != NO_DROP_TILE for x in self.live):
            raise OutOfTiles("no live (apple) fixture tile left")
        t = next(x for x in self.live if x != NO_DROP_TILE)
        self.live.remove(t)
        m = self.marks()
        res = self.a.pickup(t[0], t[1], self.rid(), timeout=1.0)
        if res is None or not res.accepted:
            raise OutOfTiles(f"could not empty fixture tile {t} (pickup result {res})")
        self.wait_all_saw(m, t, L.EMPTY_NO, 2.0)
        return t


# =====================================================================================================
# A: drop, pocket slot changed before the result
# =====================================================================================================

def group_a(env):
    a, b = env.a, env.b
    E = env.take_empty()
    m = env.marks()
    rid = env.rid()
    env.ev("INV-A-log: drop reserved, released by ABORT(SLOT_CHANGED), never committed",
           [rx_reserved(K_DROP, rid), rx_released(K_DROP, rid, f"abort:{R_SLOT_CHANGED}")], [rx_committed(K_DROP, rid)])
    res = a.drop(0, ITM_FOOD_CHERRY, E[0], E[1], rid, claim_at=E, timeout=1.0, auto_confirm=False)
    ck(f"INV-A1 drop onto empty tile {E}: provisional RESULT accepted=1 echoing the tile and the item",
       res is not None and res.accepted == 1 and (res.ut_x, res.ut_z) == E and res.placed_item == ITM_FOOD_CHERRY)
    env.settle(0.5)
    ck("INV-A2 while the reservation is pending NO FIELD_UPDATE reached any client (the field is not mutated at "
       "REQUEST time)", env.fus(m, E) == [])
    a.confirm(K_DROP, rid, ABORT, R_SLOT_CHANGED)
    env.settle(0.5)
    ck("INV-A3 after ABORT(SLOT_CHANGED) still no FIELD_UPDATE for the tile", env.fus(m, E) == [])
    ck("INV-A4 authoritative snapshot: the tile is still EMPTY (nothing was placed)", env.tile(E) == L.EMPTY_NO)
    a.send_drop_request(0, ITM_FOOD_CHERRY, E[0], E[1], rid)  # the client retries the same request_id
    r2 = a.wait_result(DROP_RES, rid, 1.0)
    ck("INV-A5 a retry of the aborted request_id is deterministically REJECTED (never re-granted)",
       r2 is not None and r2.accepted == 0)
    a.confirm(K_DROP, rid, COMMIT)  # a late/duplicate COMMIT after the abort
    env.settle(0.5)
    ck("INV-A6 a COMMIT after the ABORT is ignored: no FIELD_UPDATE and the tile is still EMPTY",
       env.fus(m, E) == [] and env.tile(E) == L.EMPTY_NO)
    rb = env.rid()
    res_b = b.drop(1, ITM_FOOD_APPLE, E[0], E[1], rb, claim_at=E, timeout=1.0)
    ck("INV-A7 the tile is available again: a second client's drop onto it is accepted",
       res_b is not None and res_b.accepted == 1 and res_b.placed_item == ITM_FOOD_APPLE)
    ck("INV-A8 every client then sees exactly (APPLE) on that tile -- never the aborted cherry",
       env.exactly_once(m, E, ITM_FOOD_APPLE))
    ck("INV-A9 authoritative snapshot: the tile holds the second client's apple", env.tile(E) == ITM_FOOD_APPLE)
    env.live.append(E)  # holds an apple again


# =====================================================================================================
# B: pickup with full inventory
# =====================================================================================================

def group_b(env):
    a, b = env.a, env.b
    P = env.take_live()
    m = env.marks()
    rid = env.rid()
    env.ev("INV-B-log: pickup reserved, released by ABORT(POCKETS_FULL), never committed",
           [rx_reserved(K_PICKUP, rid), rx_released(K_PICKUP, rid, f"abort:{R_POCKETS_FULL}")],
           [rx_committed(K_PICKUP, rid)])
    res = a.pickup(P[0], P[1], rid, timeout=1.0, auto_confirm=False)
    ck(f"INV-B1 pickup of live tile {P}: provisional RESULT accepted=1 granting the apple",
       res is not None and res.accepted == 1 and res.granted_item == ITM_FOOD_APPLE)
    env.settle(0.5)
    ck("INV-B2 while the reservation is pending NO FIELD_UPDATE reached any client (the item is still in the world)",
       env.fus(m, P) == [])
    a.confirm(K_PICKUP, rid, ABORT, R_POCKETS_FULL)
    env.settle(0.5)
    ck("INV-B3 after ABORT(POCKETS_FULL) no FIELD_UPDATE clears the tile", env.fus(m, P) == [])
    ck("INV-B4 authoritative snapshot: the tile still holds the apple (no loss)", env.tile(P) == ITM_FOOD_APPLE)
    a.send_pickup_request(P[0], P[1], rid)
    r2 = a.wait_result(PICKUP_RES, rid, 1.0)
    ck("INV-B5 a retry of the aborted request_id is deterministically REJECTED", r2 is not None and r2.accepted == 0)
    a.confirm(K_PICKUP, rid, COMMIT)
    env.settle(0.5)
    ck("INV-B6 a COMMIT after the ABORT is ignored: no FIELD_UPDATE, the tile still holds the apple",
       env.fus(m, P) == [] and env.tile(P) == ITM_FOOD_APPLE)
    rb = env.rid()
    res_b = b.pickup(P[0], P[1], rb, timeout=1.0)
    ck("INV-B7 a second client can still pick the item up (granted the apple)",
       res_b is not None and res_b.accepted == 1 and res_b.granted_item == ITM_FOOD_APPLE)
    ck("INV-B8 every client then sees exactly one FIELD_UPDATE clearing the tile",
       env.exactly_once(m, P, L.EMPTY_NO))
    env.empty.append(P)


# =====================================================================================================
# C(i): stale / unknown / mismatched confirms
# =====================================================================================================

def group_ci(env):
    a, b, c = env.a, env.b, env.c
    P = env.take_live()
    m = env.marks()
    rid = env.rid()
    res = a.pickup(P[0], P[1], rid, timeout=1.0, auto_confirm=False)
    ck("INV-Ci0 precondition: A holds a pending (provisional) pickup reservation", res is not None and res.accepted == 1)
    a.confirm(K_PICKUP, rid + 5000, COMMIT)   # unknown request id
    a.confirm(K_DROP, rid, COMMIT)            # the pending request's id, but the wrong kind
    c.confirm(K_PICKUP, rid, COMMIT)          # another peer citing A's request id
    c.confirm(K_DROP, 424242, COMMIT)         # a peer with nothing pending at all
    a.confirm(K_PICKUP, 1, COMMIT)            # an old, never-issued low id
    env.settle(0.6)
    ck("INV-Ci1 CONFIRM(COMMIT) with an unknown / wrong-kind / other-peer / never-issued request_id: no FIELD_UPDATE",
       env.fus(m, P) == [])
    ck("INV-Ci2 ... and the authoritative snapshot still shows the apple (no mutation)", env.tile(P) == ITM_FOOD_APPLE)
    rb = env.rid()
    res_b = b.pickup(P[0], P[1], rb, timeout=1.0)
    ck("INV-Ci3 the stale confirms did not disturb A's reservation: B's request for the tile is still rejected",
       res_b is not None and res_b.accepted == 0)
    a.confirm(K_PICKUP, rid, ABORT, R_CANCELLED)
    env.settle(0.4)
    env.live.append(P)
    # a duplicate COMMIT after the request is already DONE must be idempotent (no second mutation/broadcast)
    Q = env.take_live()
    m2 = env.marks()
    rq = env.rid()
    resq = a.pickup(Q[0], Q[1], rq, timeout=1.0)  # auto-CONFIRM(COMMIT)
    ck("INV-Ci4 control: a normal pickup (auto-confirmed) is accepted and every client sees exactly one FIELD_UPDATE",
       resq is not None and resq.accepted == 1 and env.exactly_once(m2, Q, L.EMPTY_NO))
    a.confirm(K_PICKUP, rq, COMMIT)
    a.confirm(K_PICKUP, rq, COMMIT)
    env.settle(0.6)
    ck("INV-Ci5 duplicate COMMITs after the request is DONE are idempotent (still exactly one FIELD_UPDATE per client)",
       all(env.fu_values(m2, Q, x.label) == [L.EMPTY_NO] for x in env.core))
    env.empty.append(Q)


# =====================================================================================================
# C(iii): peer disconnects while pending
# =====================================================================================================

def group_ciii(env):
    b = env.b
    P = env.take_live()
    m = env.marks()
    d = env.new_client("D-disc-pickup")
    rid = env.rid()
    env.ev("INV-Ciii-log: pending pickup released by the peer's disconnect, never committed",
           [rx_reserved(K_PICKUP, rid), rx_released(K_PICKUP, rid, "disconnect")], [rx_committed(K_PICKUP, rid)])
    res = d.pickup(P[0], P[1], rid, timeout=1.0, auto_confirm=False)
    ck("INV-Ciii0 precondition: D holds a pending pickup reservation", res is not None and res.accepted == 1)
    d.disconnect()
    env.settle(0.6)
    ck("INV-Ciii1 after D disconnected while pending: no FIELD_UPDATE (no ghost mutation)", env.fus(m, P) == [])
    res_b = b.pickup(P[0], P[1], env.rid(), timeout=1.0)
    ck("INV-Ciii2 the reservation was released: B's pickup of the same tile is accepted (granted the apple)",
       res_b is not None and res_b.accepted == 1 and res_b.granted_item == ITM_FOOD_APPLE)
    ck("INV-Ciii3 exactly one FIELD_UPDATE clearing the tile reached each client (B's commit only)",
       env.exactly_once(m, P, L.EMPTY_NO))
    env.empty.append(P)
    env.close_extras()

    E = env.take_empty()
    m = env.marks()
    d2 = env.new_client("D-disc-drop")
    rid2 = env.rid()
    env.ev("INV-Ciii-log: pending drop released by the peer's disconnect, never committed",
           [rx_reserved(K_DROP, rid2), rx_released(K_DROP, rid2, "disconnect")], [rx_committed(K_DROP, rid2)])
    res = d2.drop(0, ITM_FOOD_CHERRY, E[0], E[1], rid2, claim_at=E, timeout=1.0, auto_confirm=False)
    ck("INV-Ciii4 precondition: D2 holds a pending drop reservation", res is not None and res.accepted == 1)
    d2.disconnect()
    env.settle(0.6)
    ck("INV-Ciii5 after D2 disconnected while pending: no FIELD_UPDATE and the tile is still EMPTY",
       env.fus(m, E) == [] and env.tile(E) == L.EMPTY_NO)
    res_b = b.drop(0, ITM_FOOD_APPLE, E[0], E[1], env.rid(), claim_at=E, timeout=1.0)
    ck("INV-Ciii6 the drop reservation was released: B's drop onto the tile is accepted",
       res_b is not None and res_b.accepted == 1)
    ck("INV-Ciii7 every client sees only B's apple on that tile (never D2's cherry)",
       env.exactly_once(m, E, ITM_FOOD_APPLE))
    env.live.append(E)
    env.close_extras()


def group_timeout(env):
    b = env.b
    P = env.take_live()
    m = env.marks()
    d = env.new_client("D-silent")
    rid = env.rid()
    env.ev("INV-Ctimeout-log: pending pickup released by the peer's transport timeout (not by expiry), never committed",
           [rx_reserved(K_PICKUP, rid), rx_released(K_PICKUP, rid, "disconnect")],
           [rx_committed(K_PICKUP, rid), rx_released(K_PICKUP, rid, "expired")])
    res = d.pickup(P[0], P[1], rid, timeout=1.0, auto_confirm=False)
    ck("INV-Ctimeout0 precondition: D holds a pending pickup reservation", res is not None and res.accepted == 1)
    d.go_silent()  # vanishes without a DISCONNECT: the host's transport times the peer out (~5 s)
    env.settle(L.PCNET_TIMEOUT_S + 2.5)
    ck("INV-Ctimeout1 the timed-out peer's reservation caused no FIELD_UPDATE", env.fus(m, P) == [])
    res_b = b.pickup(P[0], P[1], env.rid(), timeout=1.0)
    ck("INV-Ctimeout2 a transport timeout releases the reservation well before the confirm timeout: B's pickup is "
       "accepted", res_b is not None and res_b.accepted == 1 and res_b.granted_item == ITM_FOOD_APPLE)
    d.abandon()
    env.empty.append(P)
    env.extras = [x for x in env.extras if x is not d]
    env.settle(0.3)


# =====================================================================================================
# C(iv): one reservation per tile across peers
# =====================================================================================================

def group_civ(env):
    a, b, c = env.a, env.b, env.c
    P = env.take_live()
    m = env.marks()
    ra = env.rid()
    env.ev("INV-Civ-log: A's pickup reserved, then committed exactly once",
           [rx_reserved(K_PICKUP, ra), rx_committed(K_PICKUP, ra)])
    res_a = a.pickup(P[0], P[1], ra, timeout=1.0, auto_confirm=False)
    ck("INV-Civ1 A's request for the live tile is accepted (provisional)", res_a is not None and res_a.accepted == 1)
    res_b = b.pickup(P[0], P[1], env.rid(), timeout=1.0)
    ck("INV-Civ2 B's request for the SAME tile is rejected while A's reservation is pending",
       res_b is not None and res_b.accepted == 0)
    res_c = c.pickup(P[0], P[1], env.rid(), timeout=1.0)
    ck("INV-Civ3 C's request for the same tile is rejected too", res_c is not None and res_c.accepted == 0)
    env.settle(0.4)
    ck("INV-Civ4 the rejected competitors caused no FIELD_UPDATE", env.fus(m, P) == [])
    a.confirm(K_PICKUP, ra, COMMIT)
    ck("INV-Civ5 A's later COMMIT still succeeds (the competitors did not disturb the reservation): every client "
       "(A, B, C) sees exactly one FIELD_UPDATE clearing the tile",
       env.exactly_once(m, P, L.EMPTY_NO))
    env.empty.append(P)

    E = env.take_empty()
    m = env.marks()
    rc = env.rid()
    env.ev("INV-Civ-log: C's drop reserved, released by ABORT(CANCELLED), never committed",
           [rx_reserved(K_DROP, rc), rx_released(K_DROP, rc, f"abort:{R_CANCELLED}")], [rx_committed(K_DROP, rc)])
    res_c = c.drop(0, ITM_FOOD_CHERRY, E[0], E[1], rc, claim_at=E, timeout=1.0, auto_confirm=False)
    ck("INV-Civ6 C's drop onto an empty tile is accepted (provisional)", res_c is not None and res_c.accepted == 1)
    res_b = b.drop(0, ITM_FOOD_APPLE, E[0], E[1], env.rid(), claim_at=E, timeout=1.0)
    ck("INV-Civ7 B's drop onto the SAME tile is rejected while C's drop reservation is pending",
       res_b is not None and res_b.accepted == 0)
    res_a = a.drop(0, ITM_FOOD_APPLE, E[0], E[1], env.rid(), claim_at=E, timeout=1.0)
    ck("INV-Civ8 A's drop onto the same tile is rejected too", res_a is not None and res_a.accepted == 0)
    env.settle(0.4)
    ck("INV-Civ9 no FIELD_UPDATE for the tile while it is only reserved", env.fus(m, E) == [])
    c.confirm(K_DROP, rc, ABORT, R_CANCELLED)
    env.settle(0.4)
    res_b = b.drop(0, ITM_FOOD_APPLE, E[0], E[1], env.rid(), claim_at=E, timeout=1.0)
    ck("INV-Civ10 after C aborted, the reservation is released: B's drop is accepted",
       res_b is not None and res_b.accepted == 1)
    ck("INV-Civ11 every client sees only B's apple on the tile (never C's aborted cherry)",
       env.exactly_once(m, E, ITM_FOOD_APPLE))
    env.live.append(E)


# =====================================================================================================
# C(v): reconnect into the same slot with a colliding request id
# =====================================================================================================

def group_cv(env, variant):
    b = env.b
    P, P2 = env.take_live(), env.take_live()
    d = env.new_client(f"D-{variant}")
    slot = d.assigned_peer_id
    R = env.rid()
    env.ev(f"INV-Cv-log [{variant}]: the old connection's pending pickup was released by the reset/disconnect",
           [rx_reserved(K_PICKUP, R), rx_released(K_PICKUP, R, "disconnect")])
    m = env.marks()  # before the request: a host that mutates at REQUEST time is caught deterministically
    res = d.pickup(P[0], P[1], R, timeout=1.0, auto_confirm=False)
    ck(f"INV-Cv0 [{variant}] precondition: D holds a pending pickup reservation on {P}",
       res is not None and res.accepted == 1)
    d.reconnect_and_ready(same_address_restart=(variant == "restart"))
    ck(f"INV-Cv1 [{variant}] precondition: the reconnect landed in the SAME slot {slot} (got {d.assigned_peer_id}) "
       "-- otherwise the collision is not exercised", d.assigned_peer_id == slot)
    d.confirm(K_PICKUP, R, COMMIT)  # the colliding request id: the OLD reservation must not be confirmable
    env.settle(0.6)
    ck(f"INV-Cv2 [{variant}] a COMMIT with the colliding request_id from the new connection does nothing: no "
       "FIELD_UPDATE", env.fus(m, P) == [])
    ck(f"INV-Cv3 [{variant}] ... and the authoritative snapshot still shows the apple", env.tile(P) == ITM_FOOD_APPLE)
    res2 = d.pickup(P2[0], P2[1], R, timeout=1.0, auto_confirm=False)
    ck(f"INV-Cv4 [{variant}] the SAME request_id from the new connection is validated fresh (accepted, echoing the "
       f"new tile {P2}, not the old connection's {P})", res2 is not None and (res2.ut_x, res2.ut_z) == P2
       and res2.accepted == 1)
    d.confirm(K_PICKUP, R, ABORT, R_CANCELLED)
    env.settle(0.3)
    res_b = b.pickup(P[0], P[1], env.rid(), timeout=1.0)
    ck(f"INV-Cv5 [{variant}] the old connection's reservation was released by the reconnect: B's pickup of {P} is "
       "accepted", res_b is not None and res_b.accepted == 1 and res_b.granted_item == ITM_FOOD_APPLE)
    env.wait_all_saw(m, P, L.EMPTY_NO, 2.0)
    env.empty.append(P)
    env.live.append(P2)
    env.close_extras()


# =====================================================================================================
# C(vi): a new request replaces the peer's own pending one
# =====================================================================================================

def group_cvi(env):
    a, b, c = env.a, env.b, env.c
    P1, P2 = env.take_live(), env.take_live()
    m = env.marks()
    r1, r2 = env.rid(), env.rid()
    env.ev("INV-Cvi-log: the replaced pickup was released as 'replaced', never committed; its replacement committed",
           [rx_reserved(K_PICKUP, r1), rx_released(K_PICKUP, r1, "replaced"), rx_reserved(K_PICKUP, r2),
            rx_committed(K_PICKUP, r2)], [rx_committed(K_PICKUP, r1)])
    res1 = a.pickup(P1[0], P1[1], r1, timeout=1.0, auto_confirm=False)
    ck("INV-Cvi1 A's first request is accepted (pending)", res1 is not None and res1.accepted == 1)
    res2 = a.pickup(P2[0], P2[1], r2, timeout=1.0, auto_confirm=False)
    ck("INV-Cvi2 A's SECOND request (new request_id, other tile) is accepted: it replaces the first",
       res2 is not None and res2.accepted == 1)
    a.confirm(K_PICKUP, r1, COMMIT)  # the replaced (implicitly aborted) request
    env.settle(0.6)
    ck("INV-Cvi3 a COMMIT for the replaced request is ignored: no FIELD_UPDATE, the first tile still holds the apple",
       env.fus(m, P1) == [] and env.tile(P1) == ITM_FOOD_APPLE)
    rb = env.rid()
    res_b = b.pickup(P1[0], P1[1], rb, timeout=1.0, auto_confirm=False)
    ck("INV-Cvi4 the replaced request's reservation was released: B's request for the first tile is accepted",
       res_b is not None and res_b.accepted == 1)
    if res_b is not None and res_b.accepted:
        b.confirm(K_PICKUP, rb, ABORT, R_CANCELLED)  # leave the tile untouched
    res_c = c.pickup(P2[0], P2[1], env.rid(), timeout=1.0)
    ck("INV-Cvi5 the new request keeps its own reservation: C's pickup of the second tile is rejected",
       res_c is not None and res_c.accepted == 0)
    a.confirm(K_PICKUP, r2, COMMIT)
    ck("INV-Cvi6 A's COMMIT for the surviving request clears the second tile for every client",
       env.wait_all_saw(m, P2, L.EMPTY_NO, 2.0))
    a.send_pickup_request(P1[0], P1[1], r1)  # a retry of exactly the replaced request id (its tile is free again)
    r_retry = a.wait_result(PICKUP_RES, r1, 1.0)
    ck("INV-Cvi7 a retry of the replaced request_id is rejected (accepted=0), not re-granted even though its tile "
       "is free", r_retry is not None and r_retry.accepted == 0)
    if r_retry is not None and r_retry.accepted:
        a.confirm(K_PICKUP, r1, ABORT, R_CANCELLED)
    env.settle(0.4)
    tiles = L.read_tiles_via_snapshot(env.host_ip, env.port, [P1, P2])
    ck("INV-Cvi8 authoritative snapshot: the first tile still holds its apple, the second is empty",
       tiles[P1] == ITM_FOOD_APPLE and tiles[P2] == L.EMPTY_NO)
    env.live.append(P1)
    env.empty.append(P2)


def group_cross(env):
    """One pending interaction PER KIND per peer: a pending pickup and a pending drop of the same peer COEXIST and are
    independent (neither aborts the other; each confirms / aborts / is replaced on its own), while a new request of the
    SAME kind still replaces that kind's pending one."""
    a, b = env.a, env.b
    P1, P2, P3 = env.take_live(), env.take_live(), env.take_live()
    E = env.take_empty()
    m = env.marks()
    rp1, rd, rp2 = env.rid(), env.rid(), env.rid()
    env.ev("INV-X-log: pickup rp1 reserved then replaced; drop rd reserved then committed; pickup rp2 committed",
           [rx_reserved(K_PICKUP, rp1), rx_released(K_PICKUP, rp1, "replaced"), rx_reserved(K_DROP, rd),
            rx_committed(K_DROP, rd), rx_reserved(K_PICKUP, rp2), rx_committed(K_PICKUP, rp2)],
           [rx_committed(K_PICKUP, rp1), rx_released(K_DROP, rd, "replaced"), rx_released(K_DROP, rd, "disconnect")])
    res_p = a.pickup(P1[0], P1[1], rp1, timeout=1.0, auto_confirm=False)
    res_d = a.drop(0, ITM_FOOD_CHERRY, E[0], E[1], rd, claim_at=E, timeout=1.0, auto_confirm=False)
    ck("INV-X1 A holds a pending PICKUP and, at the same time, a pending DROP (a drop request does not abort the "
       "pickup)", res_p is not None and res_p.accepted == 1 and res_d is not None and res_d.accepted == 1)
    rb1 = env.rid()
    rb2 = env.rid()
    res_b1 = b.pickup(P1[0], P1[1], rb1, timeout=1.0)
    res_b2 = b.drop(0, ITM_FOOD_APPLE, E[0], E[1], rb2, claim_at=E, timeout=1.0)
    ck("INV-X2 both reservations are live: B's pickup of the pickup tile AND B's drop onto the drop tile are rejected",
       res_b1 is not None and res_b1.accepted == 0 and res_b2 is not None and res_b2.accepted == 0)
    env.settle(0.3)
    ck("INV-X3 neither pending request caused a FIELD_UPDATE", env.fus(m, P1) == [] and env.fus(m, E) == [])
    a.confirm(K_DROP, rd, COMMIT)
    ck("INV-X4 COMMIT of the DROP commits it (every client sees the cherry on the drop tile exactly once)",
       env.exactly_once(m, E, ITM_FOOD_CHERRY))
    ck("INV-X5 ...and did NOT touch the pending pickup: its tile is still reserved (B rejected) and unchanged",
       env.fus(m, P1) == [] and b.pickup(P1[0], P1[1], env.rid(), timeout=1.0).accepted == 0
       and env.tile(P1) == ITM_FOOD_APPLE)
    res_p2 = a.pickup(P2[0], P2[1], rp2, timeout=1.0, auto_confirm=False)  # same kind: replaces rp1 only
    ck("INV-X6 a NEW pickup request replaces the pending PICKUP (its second tile is accepted)",
       res_p2 is not None and res_p2.accepted == 1)
    rb3 = env.rid()
    res_b3 = b.pickup(P1[0], P1[1], rb3, timeout=1.0, auto_confirm=False)
    ck("INV-X7 the replaced pickup's tile is released (B's request for it is accepted)",
       res_b3 is not None and res_b3.accepted == 1)
    if res_b3 is not None and res_b3.accepted:
        b.confirm(K_PICKUP, rb3, ABORT, R_CANCELLED)  # leave the tile untouched
    a.confirm(K_PICKUP, rp1, COMMIT)  # the replaced request: ignored
    a.confirm(K_PICKUP, rp2, COMMIT)
    ck("INV-X8 COMMIT of the replaced pickup is ignored, COMMIT of the surviving pickup clears only its own tile",
       env.exactly_once(m, P2, L.EMPTY_NO) and env.fus(m, P1) == [])
    env.settle(0.3)
    ck("INV-X9 authoritative snapshot: first pickup tile still holds the apple, second is empty, drop tile holds the "
       "cherry", (lambda t: t[P1] == ITM_FOOD_APPLE and t[P2] == L.EMPTY_NO and t[E] == ITM_FOOD_CHERRY)(
           L.read_tiles_via_snapshot(env.host_ip, env.port, [P1, P2, E])))
    # reverse direction: a pending DROP, then a PICKUP request; abort the drop -> the pickup survives, then commit it
    E2 = env.take_empty()
    rd2, rp3 = env.rid(), env.rid()
    res_d2 = a.drop(0, ITM_FOOD_CHERRY, E2[0], E2[1], rd2, claim_at=E2, timeout=1.0, auto_confirm=False)
    res_p3 = a.pickup(P3[0], P3[1], rp3, timeout=1.0, auto_confirm=False)
    ck("INV-X10 reverse order: a pending DROP followed by a PICKUP request: both accepted and coexist",
       res_d2 is not None and res_d2.accepted == 1 and res_p3 is not None and res_p3.accepted == 1)
    a.confirm(K_DROP, rd2, ABORT, R_CANCELLED)
    env.settle(0.4)
    res_b4 = b.pickup(P3[0], P3[1], env.rid(), timeout=1.0)
    res_b5 = b.drop(0, ITM_FOOD_APPLE, E2[0], E2[1], env.rid(), claim_at=E2, timeout=1.0, auto_confirm=False)
    ck("INV-X11 ABORT of the drop releases only the drop tile (B's drop is accepted) and leaves the pickup reserved "
       "(B's pickup is rejected)", res_b4 is not None and res_b4.accepted == 0 and res_b5 is not None
       and res_b5.accepted == 1)
    if res_b5 is not None and res_b5.accepted:
        b.confirm(K_DROP, res_b5.request_id, ABORT, R_CANCELLED)
    a.confirm(K_PICKUP, rp3, COMMIT)
    ck("INV-X12 the surviving pickup still commits independently", env.exactly_once(m, P3, L.EMPTY_NO))
    env.live.append(P1)
    env.empty.extend([P2, P3, E2])


# =====================================================================================================
# D: NaN / Inf / absurd positions
# =====================================================================================================

NAN = float("nan")
INF = float("inf")
SPEED_ABS_LIMIT = 1000.0  # host bound: a MOVE with |speed| > 1000 (or non-finite) is dropped at ingest

# (name, builder(cx, cy, cz) -> (x, y, z, speed)). The tile-centred component values are VALID; exactly one
# component is bad. With the old fail-open reach check most of these would let the request through.
D_PATTERNS = [
    ("NaN x", lambda x, y, z: (NAN, y, z, 0.0)),
    ("speed 1e30 (finite but absurd)", lambda x, y, z: (x, y, z, 1e30)),
    ("speed -1e30 (finite but absurd)", lambda x, y, z: (x, y, z, -1e30)),
    ("speed 5000 (> 1000 limit)", lambda x, y, z: (x, y, z, 5000.0)),
    ("NaN y", lambda x, y, z: (x, NAN, z, 0.0)),
    ("NaN z", lambda x, y, z: (x, y, NAN, 0.0)),
    ("NaN x,y,z", lambda x, y, z: (NAN, NAN, NAN, 0.0)),
    ("-NaN (0xFFC00000) y", lambda x, y, z: (x, L.F32_NEG_QNAN, z, 0.0)),
    ("signalling NaN (0x7FA00000) z", lambda x, y, z: (x, y, L.F32_SNAN, 0.0)),
    ("+Inf x", lambda x, y, z: (INF, y, z, 0.0)),
    ("-Inf z", lambda x, y, z: (x, y, -INF, 0.0)),
    ("+Inf y", lambda x, y, z: (x, INF, z, 0.0)),
    ("1e30 x", lambda x, y, z: (1e30, y, z, 0.0)),
    ("1e30 y", lambda x, y, z: (x, 1e30, z, 0.0)),
    ("-1e30 z", lambda x, y, z: (x, y, -1e30, 0.0)),
    ("y just over the position limit (100001)", lambda x, y, z: (x, 100001.0, z, 0.0)),
    ("NaN speed", lambda x, y, z: (x, y, z, NAN)),
    ("+Inf speed", lambda x, y, z: (x, y, z, INF)),
]


def _relayed_moves(observer, sender_id, since):
    out = []
    for m in observer.inbox.peek_all(L.p_msg_type(L.PC_NETGAME_MSG_MOVE), since=since):
        if L.MOVE_SPEC.matches(m.payload):
            g = L.MOVE_SPEC.decode(m.payload)
            if g.net_player_id == sender_id:
                out.append(g)
    return out


def _move_is_valid(g):
    return all(math.isfinite(v) and abs(v) <= L.PC_NETGAME_POS_ABS_LIMIT for v in (g.pos_x, g.pos_y, g.pos_z)) \
        and math.isfinite(g.speed) and abs(g.speed) <= SPEED_ABS_LIMIT


class DState:
    """The apple tile P and the empty tile E a D-group part works on (re-picked if a host mutates a tile anyway)."""

    def __init__(self, env):
        self.env = env
        self.P = env.take_live()
        self.E = env.take_empty()

    def centres(self):
        return L.tile_center(*self.P), L.tile_center(*self.E)

    def release(self):
        """Hand the tiles back to the pools (they hold their original apple / emptiness unless the host is buggy)."""
        self.env.live.append(self.P)
        self.env.empty.append(self.E)


def d_probe(env, st, client, kind, build, tag, far):
    """An invalid MOVE sample built around the target tile, then the request for it; the request must be REJECTED."""
    (cxp, cyp, czp), (cxe, cye, cze) = st.centres()
    if far:
        client.claim_position(0.0, 0.0, 0.0)  # a valid position far from every tile
    env.bad_sent += 1
    rid = env.rid()
    if kind == K_PICKUP:
        client.send_move_any(*build(cxp, cyp, czp))
        client.send_pickup_request(st.P[0], st.P[1], rid, auto_confirm=False)
        res = client.wait_result(PICKUP_RES, rid, 1.0)
    else:
        client.send_move_any(*build(cxe, cye, cze))
        client.send_drop_request(0, ITM_FOOD_CHERRY, st.E[0], st.E[1], rid, auto_confirm=False)
        res = client.wait_result(DROP_RES, rid, 1.0)
    ck(f"INV-D {tag} {'pickup' if kind == K_PICKUP else 'drop'} rejected after an invalid MOVE",
       res is not None and res.accepted == 0)
    if res is not None and res.accepted:
        client.confirm(kind, rid, ABORT, R_CANCELLED)
        env.settle(0.3)
        # a host that mutated anyway consumed the tile: continue on a fresh one so later patterns stay meaningful
        if kind == K_PICKUP and env.tile(st.P) != ITM_FOOD_APPLE:
            st.P = env.take_live()
        if kind == K_DROP and env.tile(st.E) != L.EMPTY_NO:
            st.E = env.take_empty()
    return res


def group_d_nomove(env):
    """(1) A connection whose ONLY MOVE samples are invalid has no position at all -> pickup and drop are rejected.
    (The host DROPS an invalid sample at ingest; the old host stored it and its fail-open reach check passed.)"""
    st = DState(env)
    n = env.new_client("D-nomove")
    for name, build in D_PATTERNS:
        d_probe(env, st, n, K_PICKUP, build, f"[no valid position ever; {name}]", far=False)
        d_probe(env, st, n, K_DROP, build, f"[no valid position ever; {name}]", far=False)
    st.release()


def group_d_far(env):
    """(2) A valid FAR position, then an invalid sample 'at' the target, then a request for the target -> rejected
    (reach still uses the last VALID position)."""
    a = env.a
    st = DState(env)
    for name, build in D_PATTERNS:
        d_probe(env, st, a, K_PICKUP, build, f"[far valid position; {name}]", far=True)
        d_probe(env, st, a, K_DROP, build, f"[far valid position; {name}]", far=True)
    st.release()


def group_d_relay(env, host_log=None):
    """The invalid samples are never relayed to other clients (a relayed NaN would spawn a teleporting remote avatar),
    and the host logs each dropped sample. No requests are involved: A just sends every bad sample."""
    a, b = env.a, env.b
    bmark = b.inbox.mark()
    a_id = a.assigned_peer_id
    cxp, cyp, czp = L.tile_center(56, 56)
    for name, build in D_PATTERNS:
        a.claim_position(0.0, 0.0, 0.0)  # valid (this one IS relayed)
        a.send_move_any(*build(cxp, cyp, czp))
        env.bad_sent += 1
        L.pump_sleep(0.02)
    env.settle(0.3)
    for _ in range(8):  # valid unreliable samples (normal speed control) so the relay path is provably alive
        a.send_move_any(cxp, cyp, czp, speed=12.5, reliable=False)
        L.pump_sleep(0.04)
    # the host log is rate limited (first 8 lines, then <= 1/s): one more invalid sample after >1 s makes the host
    # print a line carrying the up-to-date 'dropped so far' counter
    env.settle(1.3)
    a.send_move_any(NAN, cyp, czp)
    env.bad_sent += 1
    env.settle(0.5)
    moves = _relayed_moves(b, a_id, bmark)
    bad = [g for g in moves if not _move_is_valid(g)]
    good = [g for g in moves if _move_is_valid(g)]
    ck(f"INV-D relay-alive: observer B received valid relayed MOVE samples from A ({len(good)}) in the window",
       len(good) >= 1)
    ck(f"INV-D no non-finite / absurd sample was relayed to the observer (relayed {len(moves)}, bad {len(bad)})",
       len(bad) == 0)
    if host_log is not None:
        lines = list(re.finditer(RX_DROPPED_MOVE, host_log.log_text()))
        counters = [int(mo.group(4)) for mo in lines]
        ck(f"INV-D-log the host logged the dropped invalid MOVE samples ({len(lines)} line(s); the log is rate limited "
           "to the first 8 then <= 1/s, so >= 8 lines are expected)", len(lines) >= 8)
        ck(f"INV-D-log the 'dropped so far' counter is strictly increasing and its last value ({counters[-1:]}) covers "
           f"every invalid sample this run sent ({env.bad_sent})",
           bool(counters) and all(y > x for x, y in zip(counters, counters[1:])) and counters[-1] >= env.bad_sent)
        ck("INV-D-log every dropped-sample line names a peer of this run and shows a non-finite / absurd value",
           bool(lines) and all(int(mo.group(1)) in env.peer_ids for mo in lines)
           and all(re.search(r"nan|inf|e\+?[123]\d|100001|5000", mo.group(3), re.I) for mo in lines))
    else:
        L.info("INV-D-log skipped: no --host-log given (the model host prints no log)")



def group_d_rest(env):
    """(3) An invalid sample does not clobber the last VALID position; valid-position controls are accepted; nothing
    was changed by any rejected/aborted request."""
    a = env.a
    st = DState(env)
    (cxp, cyp, czp), _ = st.centres()
    a.claim_position(cxp, cyp, czp)
    a.send_move_any(NAN, cyp, czp)
    rid = env.rid()
    a.send_pickup_request(st.P[0], st.P[1], rid, auto_confirm=False)
    res = a.wait_result(PICKUP_RES, rid, 1.0)
    ck("INV-D last valid position retained: valid MOVE at the tile, then a NaN MOVE, then the pickup is still "
       "accepted (provisional)", res is not None and res.accepted == 1)
    if res is not None and res.accepted:
        a.confirm(K_PICKUP, rid, ABORT, R_CANCELLED)
    a.claim_position(0.0, 0.0, 0.0)
    a.send_move_any(cxp, cyp, czp, speed=12.5)  # a NORMAL speed control: must be ingested, not dropped
    rid = env.rid()
    a.send_pickup_request(st.P[0], st.P[1], rid, auto_confirm=False)
    res = a.wait_result(PICKUP_RES, rid, 1.0)
    ck("INV-D control: a MOVE with a normal speed (12.5) is ingested (the request from its position is accepted)",
       res is not None and res.accepted == 1)
    if res is not None and res.accepted:
        a.confirm(K_PICKUP, rid, ABORT, R_CANCELLED)
    rid = env.rid()
    res = a.pickup(st.P[0], st.P[1], rid, timeout=1.0, auto_confirm=False)
    ck("INV-D control: pickup from a valid position is accepted (provisional)", res is not None and res.accepted == 1)
    if res is not None and res.accepted:
        a.confirm(K_PICKUP, rid, ABORT, R_CANCELLED)
    rid = env.rid()
    res = a.drop(0, ITM_FOOD_CHERRY, st.E[0], st.E[1], rid, claim_at=st.E, timeout=1.0, auto_confirm=False)
    ck("INV-D control: drop from a valid position is accepted (provisional)", res is not None and res.accepted == 1)
    if res is not None and res.accepted:
        a.confirm(K_DROP, rid, ABORT, R_CANCELLED)
    env.settle(0.4)
    ck("INV-D no state changed: the apple tile still holds the apple and the drop tile is still empty",
       env.tile(st.P) == ITM_FOOD_APPLE and env.tile(st.E) == L.EMPTY_NO)
    st.release()


# =====================================================================================================
# C(ii): expiry
# =====================================================================================================

def group_expiry(env):
    a, b, c = env.a, env.b, env.c
    T = env.expiry_s
    P = env.take_live()
    E = env.take_empty()
    m = env.marks()
    ra, rc = env.rid(), env.rid()
    env.ev("INV-C-expiry-log: both reservations were released as expired and never committed",
           [rx_reserved(K_PICKUP, ra), rx_released(K_PICKUP, ra, "expired"), rx_reserved(K_DROP, rc),
            rx_released(K_DROP, rc, "expired")], [rx_committed(K_PICKUP, ra), rx_committed(K_DROP, rc)])
    res_a = a.pickup(P[0], P[1], ra, timeout=1.0, auto_confirm=False)
    res_c = c.drop(0, ITM_FOOD_CHERRY, E[0], E[1], rc, claim_at=E, timeout=1.0, auto_confirm=False)
    t0 = time.monotonic()
    ck("INV-C-expiry-0 precondition: A holds a pending pickup and C a pending drop reservation",
       res_a is not None and res_a.accepted == 1 and res_c is not None and res_c.accepted == 1)
    res_b = b.pickup(P[0], P[1], env.rid(), timeout=1.0)
    ck("INV-C-expiry-1 early on, the reservation holds: B's pickup of the reserved tile is rejected",
       res_b is not None and res_b.accepted == 0)
    env.settle(max(0.0, t0 + T * 0.5 - time.monotonic()))
    a.send_pickup_request(P[0], P[1], ra)  # the client's retry of the same request_id (must NOT extend the timer)
    r_replay = a.wait_result(PICKUP_RES, ra, 1.0)
    ck("INV-C-expiry-2 mid-window a retry of the same request_id replays the provisional accept (accepted=1)",
       r_replay is not None and r_replay.accepted == 1)
    L.info(f"INV-C-expiry: waiting for the confirm timeout ({T:.1f} s) + 2 s margin ...")
    env.settle(max(0.0, t0 + T + 2.0 - time.monotonic()))
    ck("INV-C-expiry-3 no FIELD_UPDATE for either tile through the expiry (expiry never mutates the field)",
       env.fus(m, P) == [] and env.fus(m, E) == [])
    a.confirm(K_PICKUP, ra, COMMIT)
    c.confirm(K_DROP, rc, COMMIT)
    env.settle(0.7)
    ck("INV-C-expiry-4 COMMIT after expiry (pickup and drop) causes no mutation: no FIELD_UPDATE",
       env.fus(m, P) == [] and env.fus(m, E) == [])
    tiles = L.read_tiles_via_snapshot(env.host_ip, env.port, [P, E])
    ck("INV-C-expiry-5 authoritative snapshot: the pickup tile still holds the apple, the drop tile is still empty "
       "(the timer was not extended by the retry, and the late COMMIT did nothing)",
       tiles[P] == ITM_FOOD_APPLE and tiles[E] == L.EMPTY_NO)
    a.send_pickup_request(P[0], P[1], ra)
    ra2 = a.wait_result(PICKUP_RES, ra, 1.0)
    c.send_drop_request(0, ITM_FOOD_CHERRY, E[0], E[1], rc)
    rc2 = c.wait_result(DROP_RES, rc, 1.0)
    ck("INV-C-expiry-6 a retry of an EXPIRED request_id is deterministically rejected (pickup and drop; never "
       "re-granted)", ra2 is not None and ra2.accepted == 0 and rc2 is not None and rc2.accepted == 0)
    res_b = b.pickup(P[0], P[1], env.rid(), timeout=1.0)
    ck("INV-C-expiry-7 the expired pickup tile is available again: B's pickup is accepted (granted the apple)",
       res_b is not None and res_b.accepted == 1 and res_b.granted_item == ITM_FOOD_APPLE)
    res_b2 = b.drop(0, ITM_FOOD_APPLE, E[0], E[1], env.rid(), claim_at=E, timeout=1.0)
    ck("INV-C-expiry-8 the expired drop tile is available again: B's drop is accepted",
       res_b2 is not None and res_b2.accepted == 1)
    ck("INV-C-expiry-9 every client sees only B's changes (P cleared, E = apple), never A's/C's expired ones",
       env.exactly_once(m, P, L.EMPTY_NO) and env.exactly_once(m, E, ITM_FOOD_APPLE))
    env.empty.append(P)
    env.live.append(E)


# =====================================================================================================

def log_evidence(env, host_log):
    if host_log is None:
        L.info("host-log evidence skipped: no --host-log given")
        return
    text = host_log.log_text()
    for desc, ok in evaluate_evidence(text, env.evidence):
        ck(desc, ok)
    stale = len(re.findall(r"ignored stale/invalid INTERACT_CONFIRM", text))
    L.info(f"host log: {stale} 'ignored stale/invalid INTERACT_CONFIRM' line(s) (verbose-only, informational)")


def final_integrity(env):
    env.settle(0.5)
    truth_client = env.new_client("truth")
    ck("INV-Z1 every core client's reliable stream stayed contiguous and it holds no world violations",
       all(x.delivered_in_order() and not x.world.violations for x in env.core))
    diffs = [x.world.diff(truth_client.world) for x in env.core]
    ck(f"INV-Z2 every core client's world view equals the host's authoritative snapshot (diffs {[d[:2] for d in diffs]})",
       all(d == [] for d in diffs))
    env.close_extras()


GROUPS = [
    ("a", group_a), ("b", group_b), ("ci", group_ci), ("civ", group_civ), ("cvi", group_cvi),
    ("cv-clean", lambda env: group_cv(env, "clean")), ("cv-restart", lambda env: group_cv(env, "restart")),
    ("ciii", group_ciii), ("timeout", group_timeout), ("d-nomove", group_d_nomove), ("d-far", group_d_far), ("d-relay", None), ("d-rest", group_d_rest),
    ("cross", group_cross), ("expiry", group_expiry),
]
ALIASES = {"cv": ["cv-clean", "cv-restart"], "d": ["d-nomove", "d-far", "d-relay", "d-rest"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host_ip")
    ap.add_argument("port", type=int)
    ap.add_argument("--expiry-s", type=float, default=L.PC_NETGAME_CONFIRM_TIMEOUT_MS / 1000.0)
    ap.add_argument("--only", default="", help="comma separated group names")
    ap.add_argument("--skip-expiry", action="store_true")
    ap.add_argument("--host-log", default=None, help="path of the host's --verbose log (optional evidence)")
    a = ap.parse_args()

    names = [n for n, _ in GROUPS]
    wanted = []
    for tok in [t.strip().lower() for t in a.only.split(",") if t.strip()]:
        wanted += ALIASES.get(tok, [tok])
    unknown = [w for w in wanted if w not in names]
    if unknown:
        print(f"unknown group(s) {unknown}; known: {names}")
        return 1
    selected = [n for n in names if (not wanted or n in wanted) and not (a.skip_expiry and n == "expiry")]

    host_log = None
    if a.host_log:
        class _Log:
            def __init__(self, path):
                self.path = path

            def log_text(self):
                try:
                    with open(self.path, "rb") as f:
                        return f.read().decode("utf-8", "replace")
                except OSError:
                    return ""
        host_log = _Log(a.host_log)

    env = Env(a.host_ip, a.port, a.expiry_s)
    if not env.live:
        L.pending("no apple fixture tile in the snapshot (launch the host with --pickup-test-seed)", pend)
        for x in env.core:
            x.close()
        return L.summary_and_exit_code(results, pend)
    print(f"groups: {selected}  (confirm timeout assumed {a.expiry_s:.1f} s)")
    fns = dict(GROUPS)
    for name in selected:
        print(f"--- group {name} ---")
        try:
            if name == "d-relay":
                group_d_relay(env, host_log)
            else:
                fns[name](env)
        except OutOfTiles as e:
            L.pending(f"group {name}: {e}", pend)
        except Exception as e:  # noqa: BLE001 - a crashing group is a failure, not a silent skip
            import traceback
            traceback.print_exc()
            ck(f"INV-{name} group ran to completion without an exception ({type(e).__name__}: {e})", False)
        finally:
            env.close_extras()
    final_integrity(env)
    log_evidence(env, host_log)
    for x in env.core:
        x.close()
    return L.summary_and_exit_code(results, pend)


if __name__ == "__main__":
    sys.exit(main())
