#!/usr/bin/env python3
"""test_wildlife_special_protocol.py - worker A wildlife special cases against a REAL `AnimalCrossing.exe` host with scripted FakeClient peers.

TIER: PROTOCOL + HOST LOGIC (real host process, real vanilla insect programs on the host; the peers are FakeClients). Records are made with the TEST-ONLY host
autopilot `hspawnbug SPECIES X Z` (a chosen species at a chosen place -- the vanilla roll cannot be steered); everything after that is production code.
Run through wlA_run.py (fresh disposable fixture pc\\build64\\bin_fixture4_wlA_<name> with the pc\\build64_w3 exe; ports 14000-14999; Samsung, volume 1).

  SPIRITS  SP1 a 1st spirit catch is granted into a free pocket (SPIRIT0); SP2 (G1) the 2nd catch STACKS: CATCH_REQUEST names the pocket holding SPIRIT0
           with item SPIRIT1 and the host APPLIES it (before the fix: REJECTED, "pocket slot is not free"); SP3 a wrong stack (skips a level / onto a
           non-spirit claim) is REJECTED; SP4 (G2) the host's ghost table: the spawned spirit's acre leaves the table when its actor appears and does NOT
           come back elsewhere after a peer catches it; SP5 an uncaught spirit nobody attends is released after 12 s and moves to another acre (vanilla);
           SP6 GHOST_HUNT (87) from a peer starts the host's hunt (ACTIVE flag) only while the host's event has its common area.
  ANTS     AN1 an ant record on candy is shown on the host (the real ant actor) and broadcast; AN2 its CATCH (species 38, item 0x2D26) is now
           ACCEPTED with exactly one despawn (W-06; before: rejected); AN3 an ant whose candy is taken away is released by the host within ~2 s
           (WILDLIFE_DESPAWN), so the acre is free again.
  LATENT   LB1 a hidden bagworm cannot be caught (host refuses); LB2 WILDLIFE_TOOL_ACTION (86) from out of reach / malformed is ignored; LB3 from
           reach it reveals the host's bagworm (WILDLIFE_STATE action leaves HIDE) and the catch is then ACCEPTED; LB4 a mole cricket (dig) and a pill
           bug (rock hit) leave HIDE on the host after the peer's tool action.
Exit code: 0 all checks passed, 1 otherwise."""
import argparse
import os
import re
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
from net_spike_lib import FakeClient, check, summary_and_exit_code  # noqa: E402
import wplay_lib as W  # noqa: E402
from test_wildlife_bug_catch import collect_spawns, collect_despawns, collect_catch_results, get_current_generation  # noqa: E402

KIND_BUG = 1
SP_SPIRIT, SP_ANT, SP_BAGWORM, SP_MOLE, SP_PILL, SP_BUTTERFLY = 40, 38, 35, 33, 36, 0
SPIRIT0 = 0x2D28
WILDLIFE_STATE = 73
MSG_TOOL_ACTION, MSG_GHOST_HUNT = 86, 87


def acre_center(bx, bz):
    return bx * 640.0 + 320.0, bz * 640.0 + 320.0


def ut_of(x, z):
    return int(x // 40), int(z // 40)


def hspawnbug(hp, species, x, z):
    a = hp.cmd("hspawnbug %d %.0f %.0f" % (species, x, z), 20) or ""
    m = re.search(r"entity (\d+)", a)
    return int(m.group(1)) if m and " ok " in a else 0


def ghost_table(hp, args="show"):
    """(area, active, run, [(bx,bz) registered]) of the host's ghost common area"""
    a = hp.cmd("ghost %s" % args, 20) or ""
    m = re.search(r"area=(\d) active=(\d) run=(\d) blocks=([\d,;]*)", a)
    if not m:
        return None
    blocks = [tuple(int(v) for v in b.split(",")) for b in m.group(4).split(";") if b]
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), [b for b in blocks if b != (255, 255)]


def state_actions(client, entity, duration):
    """every WILDLIFE_STATE action value seen for `entity` within `duration` s"""
    out = []

    def pred(m):
        return m.payload and m.payload[0] == WILDLIFE_STATE and len(m.payload) >= 8
    for m in client.inbox.collect(pred, duration):
        n = m.payload[1]
        for i in range(n):
            off = 8 + 20 * i
            if off + 20 > len(m.payload):
                break
            ent, = struct.unpack_from("<I", m.payload, off)
            if ent == entity:
                out.append(m.payload[off + 18])
    return out


def tool_action(client, action, ut_x, ut_z, rsv=0):
    client.send_reliable(struct.pack("<BBBBHH", MSG_TOOL_ACTION, action, ut_x, ut_z, 1, rsv))


def catch(client, gen, entity, rid, species, **kw):
    sent = client.send_catch_txn(entity, gen, rid, species, kind="bug", **kw)
    return client.wait_txn_result(sent.seq, 3.0)


def run(port, log_dir, results):
    W.make_fixture()
    host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "--authoritative-wildlife"], env={"AC_TEST_AUTOPILOT": "ac_auto_host.txt"},
                         bin_dir=W.BIN, log_path=os.path.join(log_dir, "wlA_special_host.log")).start()
    try:
        check("host reached the field", host.wait_listening(60.0) and host.boot_to_field(timeout=90.0, slot=0), results)
        hp = W.Player(L, "host", host)
        a = FakeClient("A", "127.0.0.1", port)
        a.connect_and_ready()
        a.drain_field_updates(timeout=0.3)
        gen = get_current_generation(a)
        check("the current wildlife generation was read", gen is not None, results)

        # ------------------------------------------------------------------ SPIRITS
        print("=" * 72 + "\nSPIRITS")
        gt = ghost_table(hp, "on 2,2 3,4 5,5 1,5 4,6")
        check("SP0 the host's ghost hunt is on with 5 spirit acres (%s)" % (gt,), gt is not None and gt[1] == 1 and len(gt[3]) == 5, results)
        sx, sz = acre_center(2, 2)
        a.send_move_any(sx, 0.0, sz, reliable=True)
        time.sleep(0.5)
        e1 = hspawnbug(hp, SP_SPIRIT, sx, sz)
        spawns = collect_spawns(a, 1.0)
        check("SP1 a spirit record (entity %d) was broadcast" % e1, e1 != 0 and any(s["entity_id"] == e1 and s["species"] == SP_SPIRIT for s in spawns), results)
        time.sleep(1.0)
        gt = ghost_table(hp)
        check("SP4 the spirit's acre (2,2) left the host's table when its actor appeared (%s)" % (gt,), gt is not None and (2, 2) not in gt[3] and len(gt[3]) == 4, results)
        r1 = catch(a, gen, e1, 7001, SP_SPIRIT)
        pk = list(a.txn_pre_image()[0])
        check("SP1 the 1st spirit catch is APPLIED into a free pocket (SPIRIT0)", r1 is not None and r1.outcome == 0 and r1.item == SPIRIT0 and SPIRIT0 in pk, results)
        slot = pk.index(SPIRIT0) if SPIRIT0 in pk else -1
        check("SP1 exactly one despawn for it", collect_despawns(a, 0.6).count(e1) == 1, results)
        time.sleep(2.0)
        gt = ghost_table(hp)
        check("SP4 (G2) after the peer's catch the spirit did NOT come back in another acre (%s)" % (gt,), gt is not None and len(gt[3]) == 4, results)
        check("SP4 host log: the caught spirit keeps its acre", "was caught -- it does not move to another acre" in host.log_text(), results)

        if slot < 0:
            print("SP1 failed: the stacking checks cannot run (a stack claim on no pocket is a BAD_SHAPE that counts toward the peer's violation close)")
            slot = 0
        e2 = hspawnbug(hp, SP_SPIRIT, sx + 40.0, sz)
        time.sleep(0.8)
        r2 = catch(a, gen, e2, 7002, SP_SPIRIT, slot=slot, item=SPIRIT0 + 1)
        pk2 = list(a.txn_pre_image()[0])
        check("SP2 (G1) the 2nd spirit STACKS: the request names pocket %d (SPIRIT0) with SPIRIT1 and is APPLIED (%s)" % (slot, r2 and (r2.outcome, r2.reason)),
              r2 is not None and r2.outcome == 0 and r2.slot == slot and r2.item == SPIRIT0 + 1, results)
        check("SP2 the post-image holds SPIRIT1 in that pocket and no other spirit", slot >= 0 and pk2[slot] == SPIRIT0 + 1 and sum(1 for p in pk2 if SPIRIT0 <= p <= SPIRIT0 + 4) == 1,
              results)
        e3 = hspawnbug(hp, SP_SPIRIT, sx, sz + 40.0)
        bf = hspawnbug(hp, SP_BUTTERFLY, sx + 40.0, sz + 40.0)
        time.sleep(0.8)
        r3 = catch(a, gen, e3, 7003, SP_SPIRIT, slot=slot, item=SPIRIT0 + 3)
        check("SP3 a stack that skips a level (SPIRIT1 -> SPIRIT3) is REJECTED", r3 is not None and r3.outcome == 1, results)
        r4 = catch(a, gen, bf, 7004, SP_BUTTERFLY, slot=slot, item=SPIRIT0 + 2) if bf else None
        check("SP3 a stack claim for a non-spirit record is REJECTED", r4 is not None and r4.outcome == 1, results)
        r5 = catch(a, gen, e3, 7005, SP_SPIRIT, slot=slot, item=SPIRIT0 + 2)
        check("SP2 the 3rd spirit stacks to SPIRIT2 (%s)" % ((r5 and (r5.outcome, r5.reason)),), r5 is not None and r5.outcome == 0 and list(a.txn_pre_image()[0])[slot] == SPIRIT0 + 2,
              results)
        # SP5 an uncaught spirit nobody attends: released after 12 s, moves to another acre (vanilla)
        ux_, uz_ = acre_center(3, 4)
        gt0 = ghost_table(hp)
        e4 = hspawnbug(hp, SP_SPIRIT, ux_, uz_)
        time.sleep(1.0)
        gt1 = ghost_table(hp)
        a.send_move_any(sx, 0.0, sz, reliable=True)
        got = []
        t0 = time.time()
        while time.time() - t0 < 25.0 and e4 not in got:
            got += collect_despawns(a, 1.0)
        time.sleep(1.0)
        gt2 = ghost_table(hp)
        check("SP5 the unattended spirit %d was released (despawn) within 25 s" % e4, e4 in got, results)
        check("SP5 its release moved it to another acre: table %s -> %s -> %s" % (gt0 and gt0[3], gt1 and gt1[3], gt2 and gt2[3]),
              gt1 is not None and gt2 is not None and len(gt2[3]) == len(gt1[3]) + 1 and (3, 4) not in gt2[3], results)
        # SP6 the Wisp acceptance of a peer
        ghost_table(hp, "off")
        a.send_reliable(struct.pack("<B3x", MSG_GHOST_HUNT))
        time.sleep(0.8)
        gt = ghost_table(hp)
        check("SP6 GHOST_HUNT from a peer starts the host's hunt (%s)" % (gt,), gt is not None and gt[1] == 1, results)
        check("SP6 host log names the peer's acceptance", "accepted the Wisp's request -- the spirit hunt is on" in host.log_text(), results)

        # ------------------------------------------------------------------ ANTS
        print("=" * 72 + "\nANTS")
        ax, az = acre_center(5, 3)
        aux, auz = ut_of(ax, az)
        a.send_move_any(ax, 0.0, az, reliable=True)
        r = hp.cmd("place 2806 %d %d" % (aux, auz), 20)
        check("AN0 candy placed at ut(%d,%d) (%s)" % (aux, auz, r), r is not None and "NOT placed" not in r, results)
        time.sleep(0.5)
        ant = hspawnbug(hp, SP_ANT, aux * 40 + 20, auz * 40 + 20)
        spawns = collect_spawns(a, 1.0)
        check("AN1 the ant record %d is broadcast" % ant, ant != 0 and any(s["entity_id"] == ant and s["species"] == SP_ANT for s in spawns), results)
        check("AN1 the host shows it with the real ant actor", re.search(r"entity %d kind 1 species 38 \(ANT\) materialized locally" % ant, host.log_text()) is not None, results)
        ra = catch(a, gen, ant, 7101, SP_ANT)
        check("AN2 (W-06) the ant catch (species 38, item 0x2D26) is now ACCEPTED (%s)" % ((ra and (ra.outcome, ra.reason, hex(ra.item))),),
              ra is not None and ra.outcome == 0 and ra.item == 0x2D26, results)
        check("AN2 exactly one despawn for the ant", collect_despawns(a, 0.6).count(ant) == 1, results)
        ant2 = hspawnbug(hp, SP_ANT, aux * 40 + 20, auz * 40 + 20)
        collect_spawns(a, 0.5)
        r = hp.cmd("place 0 %d %d" % (aux, auz), 20)
        got = []
        t0 = time.time()
        while time.time() - t0 < 4.0 and ant2 not in got:
            got += collect_despawns(a, 0.5)
        check("AN3 the host releases ant %d within 4 s once its candy is gone (%.1f s)" % (ant2, time.time() - t0), ant2 != 0 and ant2 in got, results)
        check("AN3 host log names the tile rule", "no longer holds candy / a spoiled turnip: record released" in host.log_text(), results)

        # ------------------------------------------------------------------ LATENT
        print("=" * 72 + "\nLATENT")
        bx, bz = acre_center(4, 2)
        but = ut_of(bx, bz)
        a.send_move_any(bx, 0.0, bz + 60.0, reliable=True)
        time.sleep(0.5)
        bw = hspawnbug(hp, SP_BAGWORM, but[0] * 40 + 20, but[1] * 40 + 20)
        time.sleep(1.5)
        acts = state_actions(a, bw, 1.5)
        check("LB0 the host simulates the bagworm hidden (WILDLIFE_STATE action 2) %s" % sorted(set(acts)), bw != 0 and acts and all(v == 2 for v in acts), results)
        rb = catch(a, gen, bw, 7201, SP_BAGWORM)
        check("LB1 the catch of the still-hidden bagworm is REFUSED by the host", rb is not None and rb.outcome == 1, results)
        check("LB1 host log names the latent refusal", "copy of this latent bug is still hidden" in host.log_text(), results)
        a.send_move_any(bx + 2000.0, 0.0, bz, reliable=True)
        time.sleep(0.4)
        tool_action(a, 4, but[0], but[1])
        tool_action(a, 9, but[0], but[1])
        time.sleep(0.8)
        log = host.log_text()
        check("LB2 an out-of-reach tool action is ignored", "out of reach of its player" in log, results)
        check("LB2 a malformed tool action is ignored", "WILDLIFE_TOOL_ACTION malformed (action 9)" in log, results)
        a.send_move_any(bx, 0.0, bz + 60.0, reliable=True)
        acts = state_actions(a, bw, 3.0)  # back in its acre: the host's actor (culled while nobody was near) is re-created and simulated again
        check("LB2 the bagworm is still hidden after them %s" % sorted(set(acts)), acts and all(v == 2 for v in acts), results)
        tool_action(a, 4, but[0], but[1])
        acts = state_actions(a, bw, 2.0)
        check("LB3 the peer's shake reveals the host's bagworm (actions %s leave 2)" % sorted(set(acts)), any(v != 2 for v in acts), results)
        check("LB3 host log: queued and handed to the insects", "queued for the insects" in host.log_text() and "handed to the insects" in host.log_text(), results)
        rb2 = catch(a, gen, bw, 7202, SP_BAGWORM)
        check("LB3 the revealed bagworm's catch is ACCEPTED (%s)" % ((rb2 and (rb2.outcome, rb2.reason)),), rb2 is not None and rb2.outcome == 0, results)
        for sp, act, hide, name, acre in ((SP_MOLE, 3, 2, "mole cricket (dig)", (1, 4)), (SP_PILL, 1, 3, "pill bug (axe on its rock)", (4, 5))):
            cx, cz = acre_center(*acre)
            cu = ut_of(cx, cz)
            a.send_move_any(cx, 0.0, cz + 60.0, reliable=True)
            time.sleep(0.5)
            ent = hspawnbug(hp, sp, cu[0] * 40 + 20, cu[1] * 40 + 20)
            time.sleep(1.2)
            before = state_actions(a, ent, 1.0)
            tool_action(a, act, cu[0], cu[1])
            after = state_actions(a, ent, 2.0)
            check("LB4 %s: hidden (%s) until the peer's tool action, then out (%s)" % (name, sorted(set(before)), sorted(set(after))),
                  ent != 0 and before and all(v == hide for v in before) and any(v != hide for v in after), results)
        a.close()
    finally:
        host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=14100)
    args = ap.parse_args()
    results = []
    log_dir = os.path.join(HERE, "wlA_logs")
    os.makedirs(log_dir, exist_ok=True)
    run(args.port, log_dir, results)
    return summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
