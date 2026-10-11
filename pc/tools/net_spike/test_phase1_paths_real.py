#!/usr/bin/env python3
"""test_phase1_paths_real.py - runtime proof of three code paths that were previously only source-reviewed (REAL host + REAL client process, scripted FakeClients; volume from the environment).

Test-only hooks used (PC_NET_TEST_HOOKS build, AC_TEST_HOOKS=1): client `wldstate SEQ ENT X Z` (one synthetic WILDLIFE_STATE through the REAL client handler incl. the seq check), `wldlast`,
`evclaim OP` (the event-NPC claim entry the Gulliver / K.K. dialogue seams call), host `refusesend TYPE COUNT` / `refusestats` (the transport refuses the next COUNT reliable sends of one message type, like a full window).
  A  W-14  out-of-order / duplicate WILDLIFE_STATE: a newer state is applied, a stale one and a duplicate are dropped and do not overwrite it, the next newer one applies again; ordinary in-order host traffic is
           not dropped (last seq advances, drop counter stays 0)
  B  F3    a client whose link is NOT READY (the host process is gone) still routes the event-NPC claim to the host path and is REFUSED locally (claim_active=1, ret=0): no local vanilla grant. The READY client's claim is sent
           (ret=1) before that
  C  F-01  the host's reliable sends of WORK_STATE (68), EVNPC_STATE (69), NPC_LEASE (70) and PAGE (71) are refused 3 times each while a client connects: the refusals happen, the sender does NOT mark the message sent and
           the next ticks deliver it (accepted >= 1)
NOT covered: NPC_STATE (source change only), the dialogue seams themselves (they call claim_active, source-grep test_evnpc_protocol S4), a refusal caused by a REAL full window.
Usage: python test_phase1_paths_real.py [--port 12960]"""
import argparse
import os
import re
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "0")
os.environ["NET_SPIKE_DEFAULT_RESIDENT"] = "2"  # the real client holds resident 1: the scripted FakeClients must take another slot
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_lifecycle_real as LC  # noqa: E402
import net_spike_lib as L  # noqa: E402

ENT = 0


def num(rx, txt, default=None):
    m = re.search(rx, txt or "")
    return m.group(1) if m else default


def feed(A, seq, x):
    r = A.cmd("wldstate %d %d %.1f 0.0" % (seq, ENT, x), 20)
    return (int(num(r"this call dropped=(\d)", r, "-1")), float(num(r"target=\(?(?:NONE)?\(?(-?[\d.]+),", r, "nan") or "nan"), r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12960)
    args = ap.parse_args()
    results = []
    R = LC.Rig(args, results)
    check = R.check
    try:
        if not (R.start_host() and R.start_client("A", 1)):
            check("host and client in the field", False)
            return L.summary_and_exit_code(results)
        H, A = R.players["host"], R.players["A"]

        # ------------------------------------------------------------------ A: W-14
        # a real creature so that the client TRACKS the entity (a state for an unknown entity is ignored): a fish the host injects next to its own player, in the client's presentation
        global ENT
        H.cmd("tp 2700 1450", 20)
        time.sleep(2)
        ans = H.cmd("hspawn 8 2794.0 1404.0", 30)
        ENT = int(num(r"entity (\d+)", ans, "0"))
        check("A-setup the host injected a fish (entity %d)" % ENT, ENT > 0)
        LC.T.enter_acre(A, 4, 2)
        time.sleep(6)
        r0 = A.cmd("wldlast", 20)
        last0, drop0 = int(num(r"last=(\d+)", r0, "-1")), int(num(r"dropped=(\d+)", r0, "-1"))
        time.sleep(3)
        r1 = A.cmd("wldlast", 20)
        last1, drop1 = int(num(r"last=(\d+)", r1, "-1")), int(num(r"dropped=(\d+)", r1, "-1"))
        check("A0 ordinary in-order host traffic is applied and never dropped (last seq %d -> %d, dropped %d -> %d)" % (last0, last1, drop0, drop1), last1 > last0 > 0 and drop1 == 0)
        base = last1 + 100000  # far ahead of the real host's own counter, so nothing real interleaves with the sequence below
        d, tx, raw = feed(A, base, 100.0)
        check("A1 a newer state is accepted and becomes the target (%s)" % raw, d == 0 and tx == 100.0)
        d, tx, raw = feed(A, base + 2, 200.0)
        check("A2 a still newer state (seq +2) is accepted: target 200 (%s)" % raw, d == 0 and tx == 200.0)
        d, tx, raw = feed(A, base + 1, 300.0)
        check("A3 the STALE state (seq +1, arrives after +2) is dropped and does not overwrite the target (%s)" % raw, d == 1 and tx == 200.0)
        d, tx, raw = feed(A, base + 2, 400.0)
        check("A4 a DUPLICATE of the last applied seq is dropped and does not overwrite the target (%s)" % raw, d == 1 and tx == 200.0)
        d, tx, raw = feed(A, base + 3, 500.0)
        check("A5 the next valid state (seq +3) is applied again: target 500 (%s)" % raw, d == 0 and tx == 500.0)

        # ------------------------------------------------------------------ C: F-01 (before B, which removes the host)
        for typ, name in ((68, "WORK_STATE"), (69, "EVNPC_STATE"), (70, "NPC_LEASE"), (71, "PAGE")):
            H.cmd("refusesend %d 3" % typ, 20)
            f = L.FakeClient("C%d" % typ, "127.0.0.1", args.port)
            try:
                f.connect_and_ready()
                acc = ref = 0
                for _ in range(25):
                    L.pump_sleep(1.0)
                    st = H.cmd("refusestats", 20)
                    ref, acc = int(num(r"refused=(\d+)", st, "0")), int(num(r"accepted=(\d+)", st, "0"))
                    if ref >= 3 and acc >= 1:
                        break
                check("C %s: the transport refused the first 3 sends (refused=%d) and the sender delivered it afterwards (accepted=%d): a refused send is not recorded as sent" % (name, ref, acc), ref == 3 and acc >= 1)
            finally:
                try:
                    f.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            L.pump_sleep(2.0)

        # ------------------------------------------------------------------ B: F3
        r = A.cmd("evclaim 1 0", 30)
        check("B1 READY client: the claim is sent to the host (ret=1, claim_active=1, link_ready=1) (%s)" % r, num(r"ret=(-?\d+)", r) == "1" and num(r"claim_active=(\d)", r) == "1" and num(r"link_ready=(\d)", r) == "1")
        time.sleep(3)  # let the host's verdict (Gulliver is not visiting: refused) finish the pending claim
        R.stop_client("B") if "B" in R.clients else None
        R.host.stop()  # the host process disappears: the client keeps role CLIENT, its link leaves READY
        r = None
        for _ in range(40):
            time.sleep(1.0)
            r = A.cmd("evclaim 1 0", 30)
            if r and num(r"link_ready=(\d)", r) == "0":
                break
        check("B2 link NOT ready: the claim is routed to the host path and REFUSED locally (ret=0), claim_active stays 1 so the dialogue seam does NOT fall back to a vanilla grant (%s)" % r,
              r is not None and num(r"link_ready=(\d)", r) == "0" and num(r"claim_active=(\d)", r) == "1" and num(r"ret=(-?\d+)", r) == "0")
    finally:
        R.stop_all()
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
