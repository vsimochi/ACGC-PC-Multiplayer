#!/usr/bin/env python3
"""test_dresser_real.py - extra dresser storage (TXN kind 18) on REAL processes: one REAL host + one REAL client game process (no GUI automation), disposable fixture copy
(NET_SPIKE_GAME_BIN=<abs path of pc\\build64\\bin_fixture4_<name>>).

Host: feature ON by default + TEST-ONLY AC_TEST_DRESSER_ANY_SCENE=1 (the autopilot client stands in town, not in its room).
Client: TEST-ONLY AC_TEST_DRESSER_ROUNDTRIP=1 -> the REAL client sender (pc_net_game_dresser_begin / pc_net_game_ts_poll / pcnetgame_txn_apply_dresser): DRESSER_QUERY capability probe,
PUT of the first storable pocket item into the pool, TAKE back. The client logs pockets+pool count of that item before and after (CONSERVED / MISMATCH).
  R1 the client learned the capability (pool adopted, enabled=1)
  R2 PUT APPLIED and TAKE APPLIED through the real sender; the host committed exactly one PUT and one TAKE
  R3 pockets + pool count of the item is conserved through the round trip
Usage: python test_dresser_real.py [--port 16330]"""
import argparse
import os
import re
import shutil
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["AC_TEST_HOOKS"] = "1"
if os.environ.get("AC_MP_DRESSER_STORAGE") is None:
    os.environ["AC_MP_DRESSER_STORAGE"] = "1"  # the host default is OFF during the per-dresser redesign
import net_spike_lib as L  # noqa: E402
import test_txn_real_client as RC  # noqa: E402


CHEST = 0x1068  # a vanilla ITEM-STORAGE furniture (DRAWERS profile, ac_sum_bla_chest01): v2 only accepts storage pieces as dressers


def seed_chest(resident):
    """TEST-ONLY: the fixture houses hold no storage furniture; the host seeds one chest into an empty floor-0 cell of the resident's house at the first probe
    (AC_TEST_DRESSER_SEED). Returns (cell, the rest of the empty cells)."""
    img = L.house_from_gci(L.GAME_BIN_DIR, resident)
    emp = [c for c in range(L.HOUSE_CELLS) if L.house_cell(img, 0, 0, c) == 0 and c > 40]
    os.environ["AC_TEST_DRESSER_SEED"] = "%d:%d=%04X" % (resident, emp[8], CHEST)
    return emp[8], [c for c in emp if c != emp[8]]


def run_ui(args, results, ip, rig):
    """--ui: the client walks into its OWN room (AC_TEST_DRESSER_ROOM) and the dresser UI states are driven by AC_TEST_DRESSER_UI (no scene bypass on the host:
    the real own-house owner check runs)."""
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    pass  # the host default is ON; the caller may set AC_MP_DRESSER_STORAGE (0 = OFF run)
    os.environ.pop("AC_TEST_DRESSER_ANY_SCENE", None)
    seed_chest(residents[0])
    os.environ["AC_TEST_DRESSER_UI_FTR"] = "%04X" % CHEST
    host = rig.start_host("dresserui", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    os.environ["AC_TEST_DRESSER_ROOM"] = "1"
    os.environ["AC_TEST_DRESSER_UI"] = "1"
    cl = rig.start_client("dresserui", args.port, residents[0], [])
    os.environ.pop("AC_TEST_DRESSER_ROOM", None)
    os.environ.pop("AC_TEST_DRESSER_UI", None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"\[NET\]\[DRESSER\]\[TEST-ONLY\] ui: (TAKE|PUT REJECTED|SKIPPED|pool put-in refused|the stored item|'Put in' picked -> refused|pool item choice \d+ picked -> refused)", 180.0)
    text = cl.log_text()
    check("U-1 the client walked into its own house room", re.search(r"room warp: entering own house \d+", text) is not None)
    check("U-2 the UI page was built with a Put in choice", re.search(r"ui: page 0 built \(put-in choice \d\)", text) is not None)
    check("U-3 'Put in' picked through the real page handler -> the put-in submenu path", "'Put in' picked -> put-in submenu requested" in text)
    check("U-4 PUT APPLIED through the real pool put-in + wait state (host owner check passed)", "ui: PUT APPLIED" in text)
    check("U-5 the stored item picked on the rebuilt page -> TAKE running", re.search(r"ui: pool item choice \d+ picked -> TAKE running", text) is not None)
    m = re.search(r"ui: TAKE (APPLIED|REJECTED), item 0x([0-9A-F]{4}) pockets\+pool count (\d+) before (\d+) -> (\w+)", text)
    check("U-6 TAKE APPLIED and pockets + pool CONSERVED (%s)" % (m.group(0) if m else None), m is not None and m.group(1) == "APPLIED" and m.group(5) == "CONSERVED")
    cl.wait_for_log(r"\[NET\]\[SCENE\] local scene live: scene=7 .*seq=3", 60.0)
    L.pump_sleep(5.0)
    text = cl.log_text()
    ht = host.log_text()
    check("U-8 the client LEFT its room afterwards (scene change out, back in the field)", "ui: leaving the room" in text and re.search(r"local scene live: scene=7 .*seq=3", text) is not None)
    bad = [x for x in ("CONSERVATION failed", "in-room commit NOT sent", " STALE (", "INVALID_CELL", "BAD_DIGEST", "rollback", "taint set") if x in ht or x in text]
    applied_commit = re.search(r"OWNER_COMMIT xfer \d+ house \d+ APPLIED", ht) is not None
    check("U-9 no house-sync conservation failure / refused or rolled-back commit / taint on either side after the dresser round trip and the leave (bad=%s; leave commit %s)"
          % (bad, "APPLIED" if applied_commit else "not needed: the room was clean"), not bad)
    check("U-10 the client used the in-room dresser gate and rebased the in-room baseline (PUT and TAKE)", text.count("in-room conservation baseline follows the dresser") == 2)
    check("U-7 the HOST committed exactly one PUT and one TAKE", len(re.findall(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ PUT ", ht)) == 1 and
          len(re.findall(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ TAKE ", ht)) == 1)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def run_ui_direct(args, results, ip, rig):
    """--ui-direct VARIANT (hardening 6f, WRITTEN, NOT RUN: needs the 6f build): the player presses A in front of the chest through the REAL pad path (the --ui-lock
    placement); the open demo's WAIT_MSG runs aMR_PcDresserDirectOpen.
      empty    : no extra item; the 27-slot screen opens DIRECTLY (no vanilla menu)
      occupied : one extra item (put-only round trip); the 27-slot screen opens directly and shows it
      timeout  : the host ignores every SELECT (TEST-ONLY AC_TEST_DRESSER_IGNORE_SELECT): after the 3 s WALL-CLOCK cap the VANILLA menu opens (fallback,
                 notice 6); the client runs UNCAPPED (--no-framelimit) so a frame-counted wait would fall back far earlier
      off      : the host feature is OFF (AC_MP_DRESSER_STORAGE=0): the vanilla menu opens at once, no direct-open line (byte-identical vanilla)
      reenter  : the user's bug: one extra item (put-only round trip), open + close, LEAVE the room, walk in again, A at once: the FIRST open of the new visit
                 (selection + contents forgotten, a full SELECT -> STATE round trip) opens the 27-slot screen, never the vanilla menu (UNCAPPED client)
      slow     : a SLOW host (TEST-ONLY AC_TEST_DRESSER_SELECT_DELAY_MS=1500) and an UNCAPPED client (--no-framelimit, fixture vsync = 0; the logged fps
                 must be > 120): the direct open still succeeds after ~1.5 s, notice 12 ("Asking the host...") shown while waiting, exactly one 'adopted'"""
    check = rig.check
    variant = args.ui_direct
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    seed_chest(residents[0])
    host_env = {"AC_TEST_DRESSER_ANY_SCENE": "1"}
    if variant == "timeout":
        host_env["AC_TEST_DRESSER_IGNORE_SELECT"] = "1"
    if variant == "slow":
        host_env["AC_TEST_DRESSER_SELECT_DELAY_MS"] = "1500"
    if variant == "off":
        host_env["AC_MP_DRESSER_STORAGE"] = "0"
    saved = {k: os.environ.get(k) for k in host_env}
    os.environ.update(host_env)
    host = rig.start_host("dresserdirect", args.port, [])
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    env = {"AC_TEST_DRESSER_ROOM": "1", "AC_TEST_DRESSER_UI": "moved" if variant.startswith("moved") else "direct", "AC_TEST_DRESSER_UI_FTR": "%04X" % CHEST}
    if variant == "moved-stuck":
        env["AC_TEST_HCL_NO_INROOM_COMMIT"] = "1"
    if variant == "moved":
        env["AC_TEST_HCL_HOLD_MS"] = "9000"  # in-room commits held 12 s after the placement: A in between must meet the notice-4 refusal (cap 8 s from the first refusal)
    if variant in ("occupied", "reenter"):
        env["AC_TEST_DRESSER_ROUNDTRIP"] = "put"
    if variant == "reenter":
        env["AC_TEST_DRESSER_UI_REENTER"] = "1"
    os.environ.update(env)
    # frame-rate independence: the timing variants run the client UNCAPPED (the user's settings: vsync 0, max_fps 0); the fixture's settings.ini has vsync = 0
    uncapped = variant in ("timeout", "reenter", "slow")
    cl = rig.start_client("dresserdirect", args.port, residents[0], ["--no-framelimit"] if uncapped else [])
    for k in env:
        os.environ.pop(k, None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    if variant == "reenter":
        cl.wait_for_log(r"ui: reenter: second visit done|direct: nothing opened|ui: SKIPPED", 600.0)
    else:
        cl.wait_for_log(r"ui: leaving the room|direct: nothing opened|ui: SKIPPED", 480.0)
    L.pump_sleep(4.0)
    text, ht = cl.log_text(), host.log_text()
    fps = [int(x) for x in re.findall(r"direct open: dresser 0x[0-9A-F]{4} .* -> 27-slot screen after \d+ frame\(s\), \d+ ms \(~(\d+) fps\)", text)]
    fb = [(int(a), int(b)) for a, b in re.findall(r"direct open: no host view of dresser 0x[0-9A-F]{4} after (\d+) ms \((\d+) frames\)", text)]
    if variant in ("moved", "moved-stuck"):
        opens = re.findall(r"direct open: dresser 0x%04X \(floor 0 cell (\d+),(\d+)\) -> 27-slot screen" % CHEST, text)
        mm = re.search(r"moved: the chest now stands at cell (\d+),(\d+)", text)
        check("D-1 %s: the first A opened the 27-slot screen directly (%s)" % (variant, opens[:1]), len(opens) >= 1)
        check("D-2 %s: the EMPTY chest was picked up (B, real) and placed again (the inventory drop path) at %s" % (variant, mm.group(0) if mm else None),
              "B pickup of the chest 0x%04X: IN A POCKET" % CHEST in text and "moved: placed again from pocket" in text and mm is not None)
        if variant == "moved":
            check("D-3 moved: A shortly after the placement was REFUSED for now with notice 4 (the move not committed yet), no SELECT sent, no vanilla menu",
                  "REFUSED for now: the room is being saved" in text and "the VANILLA dresser menu opened" not in text)
            mb = re.search(r"moved: B \(pickup\) of the uncommitted chest: (\w+(?: \w+)?) \((.*)\)", text)
            check("D-3b moved (6l): B (pickup) of the moved chest whose cell is not committed: UNKNOWN -> refused for now with notice 4, never notice 3 (%s)" %
                  (mb.group(0) if mb else None), mb is not None and mb.group(1) == "REFUSED" and "notice 4" in mb.group(2) and
                  "whose move is not committed yet REFUSED for now -> notice 4" in text)
            check("D-4 moved: the in-room commit of the pickup + placement was APPLIED", re.search(r"OWNER_COMMIT xfer \d+ house \d+ APPLIED", ht) is not None)
            check("D-5 moved: a later A opened the 27-slot screen DIRECTLY at the new cell (%s)" % (opens,),
                  mm is not None and any((x, y) == (mm.group(1), mm.group(2)) for x, y in opens[1:]))
            check("D-6 moved: the host never refused a SELECT", "DRESSER_SELECT refused" not in ht)
        else:
            check("D-3 stuck: in-room commits stopped -> the uncommitted dresser opened the VANILLA menu at once (no endless refusal)",
                  "in-room commits cannot land now -> vanilla menu" in text and "the VANILLA dresser menu opened" in text)
            check("D-4 stuck: no SELECT for the uncommitted cell reached the host refusal path", "DRESSER_SELECT refused" not in ht)
    elif variant in ("empty", "occupied"):
        m = re.search(r"direct open: dresser 0x%04X .* -> 27-slot screen after (\d+) frame\(s\)" % CHEST, text)
        check("D-1 %s: A opened the 27-slot screen DIRECTLY (%s)" % (variant, m.group(0) if m else None), m is not None)
        check("D-2 %s: no vanilla dresser menu in between" % variant, "direct: the VANILLA dresser menu opened" not in text)
        n = 1 if variant == "occupied" else 0
        check("D-3 %s: the screen shows %d extra item(s)" % (variant, n), "direct: the 27-slot screen is OPEN (%d extra item(s))" % n in text)
    elif variant == "timeout":
        check("D-1 timeout: the host ignored the SELECT", "DRESSER_SELECT of peer" in ht and "ignored" in ht)
        check("D-2 timeout: after the 3 s WALL-CLOCK cap (not a frame count) the VANILLA menu opened (fallback; the furniture stays usable) (%s)" % (fb,),
              len(fb) == 1 and 3000 <= fb[0][0] < 4500 and "direct: the VANILLA dresser menu opened" in text)
        check("D-2b timeout: the client really ran uncapped (%s frames in %s ms > 2 x 180)" % (fb[0][1] if fb else None, fb[0][0] if fb else None),
              len(fb) == 1 and fb[0][1] > 360)
        check("D-2c timeout: notice 12 while waiting, then notice 6 at the fallback",
              "waiting for the host's view of the dresser (asking the host) -> notice 12" in text and "no host view of the dresser in time -> the vanilla menu -> notice 6" in text)
    elif variant == "reenter":
        opens = re.findall(r"direct: the 27-slot screen is OPEN \((\d+) extra item\(s\)\) after \d+ frames, (\d+) ms \(visit (\d+)\)", text)
        m2 = re.search(r"ui: reenter: room visit (\d+) \(was (\d+)\)", text)
        second = text[m2.start():] if m2 else ""
        check("D-1 reenter: the first visit opened the 27-slot screen directly, the client left and walked in again (a NEW room visit) (%s; %s)" %
              (opens, m2.group(0) if m2 else None), len(opens) >= 1 and m2 is not None and "room warp: re-armed" in text)
        check("D-2 reenter: the FIRST open after re-entering opened the 27-slot screen showing the stored item (%s)" % (opens[1:],),
              len(opens) == 2 and opens[1][0] == "1" and m2 is not None and opens[1][2] == m2.group(1))
        check("D-3 reenter: no fallback / vanilla menu in either visit", "vanilla menu (fallback)" not in text and "the VANILLA dresser menu opened" not in text)
        check("D-3b reenter: the second visit asked the host again (a new generation) and adopted its answer",
              re.search(r"client: select dresser floor 0 .*\(gen \d+\)", second) is not None and " adopted" in second)
        check("D-3c reenter: the client ran uncapped (fps %s)" % (fps,), len(fps) == 2 and min(fps) > 120)
    elif variant == "slow":
        m = re.search(r"direct open: dresser 0x%04X .* -> 27-slot screen after (\d+) frame\(s\), (\d+) ms \(~(\d+) fps\)" % CHEST, text)
        check("D-1 slow: the host held the SELECT 1.5 s", "DRESSER_SELECT of peer" in ht and "delayed 1500 ms" in ht and "is answered now" in ht)
        check("D-2 slow: the direct open still opened the 27-slot screen after the slow answer (%s)" % (m.group(0) if m else None),
              m is not None and int(m.group(2)) >= 1400 and "direct: the 27-slot screen is OPEN (0 extra item(s))" in text)
        check("D-3 slow: the client ran UNCAPPED (%s fps > 120, %s frames > 180: the old frame-counted wait would have fallen back)" %
              (m.group(3) if m else None, m.group(1) if m else None), m is not None and int(m.group(3)) > 120 and int(m.group(1)) > 180)
        check("D-4 slow: notice 12 ('Asking the host...') was shown while waiting; no fallback, no vanilla menu",
              "waiting for the host's view of the dresser (asking the host) -> notice 12" in text and "vanilla menu (fallback)" not in text and
              "the VANILLA dresser menu opened" not in text)
        check("D-5 slow: exactly one 'adopted' for the one open (the slow answer was not discarded / re-asked)",
              len(re.findall(r"\[NET\]\[DRESSER\] client: dresser floor \d+ layer \d+ cell \d+,\d+ \(gen \d+\) rev \d+ adopted", text)) == 1)
    else:
        check("D-1 off: the vanilla menu opened at once, no direct-open line (byte-identical vanilla)",
              "direct: the VANILLA dresser menu opened" in text and "direct open:" not in text)
    check("D-4 the opened screen / menu closed (B) and the client left its room", "direct: closed" in text and "ui: leaving the room" in text)
    bad = [x for x in ("CONSERVATION failed", "in-room commit NOT sent", " STALE (", "INVALID_CELL", "BAD_DIGEST", "taint set") if x in ht or x in text]
    check("D-5 no conservation failure / refused commit / taint (%s)" % bad, not bad)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()


def run_ui_lock(args, results, ip, rig):
    """--ui-lock (6l): the move lock through the REAL input path. One extra item is put into the seeded chest (put-only round trip), the client walks into its
    own room, the hook places the player on a free unit next to the chest facing it, and the REAL pad path presses B (pickup), pushes (A held + stick into the chest),
    pulls (A held + stick away) and rotates (A held + stick sideways; a short A tap OPENS a dresser): all refused (notice 3), the chest stays. The extra item is
    taken out (kind-18 TAKE); an ordinary item goes into a VANILLA keep slot (the room's own vanilla put) and is committed; B / push / pull / rotate are refused
    again (27-slot lock, notice 3); the keep item is taken out and B pressed AT ONCE: refused for now (notice 4: the take-out is not committed yet); once it is
    committed B is ALLOWED (chest in a pocket); the room exit commits the pickup."""
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    seed_chest(residents[0])
    os.environ["AC_TEST_DRESSER_ANY_SCENE"] = "1"
    host = rig.start_host("dresserlock", args.port, [])
    os.environ.pop("AC_TEST_DRESSER_ANY_SCENE", None)
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    env = {"AC_TEST_DRESSER_ROOM": "1", "AC_TEST_DRESSER_UI": "lock", "AC_TEST_DRESSER_UI_FTR": "%04X" % CHEST, "AC_TEST_DRESSER_ROUNDTRIP": "put"}
    os.environ.update(env)
    cl = rig.start_client("dresserlock", args.port, residents[0], [])
    for k in env:
        os.environ.pop(k, None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"lock: B \(pickup\) after the extras|lock: SKIPPED|lock: no extra item|lock: TAKE REJECTED|vanilla keep put of 0x[0-9A-F]{4} into the chest: NOTHING", 600.0)
    cl.wait_for_log(r"\[NET\]\[SCENE\] local scene live: scene=7 .*seq=3", 60.0)
    L.pump_sleep(6.0)
    text, ht = cl.log_text(), host.log_text()
    check("L-1 one extra item was seeded into the chest (put-only round trip APPLIED)", "roundtrip put-only" in text)
    check("L-2 the player was placed next to the chest holding 1 extra item", re.search(r"lock: chest 0x%04X holds 1 extra item\(s\); placing the player next to it \(side [0-3]\)" % CHEST, text) is not None)
    m = re.search(r"lock: B \(pickup\) with extra items: (\w+(?: \w+)?) \(.*\)", text)
    check("L-3 B (pickup, real pad path) with extra items REFUSED, notice 3, the chest stays, not in a pocket (%s)" % (m.group(0) if m else None),
          m is not None and m.group(1) == "REFUSED" and "notice 3" in m.group(0))
    check("L-3b the client logged the Option-A refusal line", "pickup of a dresser holding extra items REFUSED (notice 3" in text)
    for n, mv in enumerate(("push \\(A held \\+ stick into the chest\\)", "pull \\(A held \\+ stick away\\)", "rotate \\(A held \\+ stick sideways\\)")):
        m = re.search(r"lock: %s with extra items: (\w+(?: \w+)?) \((.*)\)" % mv, text)
        # 6q (S2): moving / rotating a dresser that holds items is ALLOWED now (the host carries its storage); only pickup stays locked
        check("L-4%s %s with extra items (real pad path) is ALLOWED (6q: not refused) (%s)" % ("abc"[n], mv.replace("\\", ""), m.group(0) if m else None),
              m is not None and m.group(1) == "NOT REFUSED")
    # 6q (S2): push / pull / rotate are no longer refused, so the held-input per-action counter regression (L-C) has no refusals to count here any more; the
    # counter itself stays source-pinned (C1 / C2) and still counts pickup refusals
    check("L-C (6q) no push / pull / rotate attempt was refused at all", "push of the dresser" not in text and "pull of the dresser" not in text and
          "rotate of the dresser" not in text)
    check("L-5 the extra item was taken out (kind-18 TAKE APPLIED, 0 left)", "lock: TAKE APPLIED, the chest holds 0 extra item(s) now" in text)
    check("L-K1 an ordinary item went into a VANILLA keep slot and was committed (host canonical image)",
          re.search(r"lock: vanilla keep put of 0x[0-9A-F]{4} into the chest: DONE", text) is not None and "lock: the keep item 0x" in text and " is COMMITTED " in text)
    m = re.search(r"lock: B \(pickup\) with a vanilla keep item: (\w+(?: \w+)?) \((.*)\)", text)
    check("L-K2 B (pickup) with a vanilla keep item REFUSED, notice 3 (27-slot lock) (%s)" % (m.group(0) if m else None),
          m is not None and m.group(1) == "REFUSED" and "notice 3" in m.group(2) and "holds vanilla keep item(s): move / rotate / pickup REFUSED (27-slot lock)" in text)
    for n, mv in enumerate(("push \\(A held \\+ stick into the chest\\)", "pull \\(A held \\+ stick away\\)", "rotate \\(A held \\+ stick sideways\\)")):
        m = re.search(r"lock: %s with a vanilla keep item: (\w+(?: \w+)?) \((.*)\)" % mv, text)
        check("L-K3%s %s with a vanilla keep item is ALLOWED (6q) (%s)" % ("abc"[n], mv.replace("\\", ""), m.group(0) if m else None), m is not None and m.group(1) == "NOT REFUSED")
    m = re.search(r"lock: B \(pickup\) right after the uncommitted keep take-out: (\w+(?: \w+)?) \((.*)\)", text)
    check("L-K4 B right after the (uncommitted) keep take-out REFUSED for now with notice 4, never notice 3 (%s)" % (m.group(0) if m else None),
          m is not None and m.group(1) == "REFUSED" and "notice 4" in m.group(2) and "keep items taken out but not committed yet" in text)
    check("L-K5 the keep take-out was committed", "lock: the keep take-out is COMMITTED" in text)
    m = re.search(r"lock: B \(pickup\) after the extras were taken out: (\w+(?: \w+)?) \(.*\)", text)
    check("L-6 B (pickup) of the EMPTY chest (no extra, no keep item, committed) is ALLOWED: the chest is in a pocket (%s)" % (m.group(0) if m else None), m is not None and m.group(1) == "ALLOWED")
    check("L-7 the pickup was committed by furniture sync after the room exit (OWNER_COMMIT APPLIED)", re.search(r"OWNER_COMMIT xfer \d+ house \d+ APPLIED", ht) is not None)
    check("L-7b the host never had to refuse a commit (the client gates held: no step-9b refusal)", "REFUSED (take the stored items out first)" not in ht and
          "REFUSED (27-slot lock" not in ht)
    check("L-7c (6q) a pushed / pulled chest's storage was never orphaned on the host (it follows the piece: re-key / fingerprint follow)",
          "storage ORPHANED" not in ht)
    bad = [x for x in ("CONSERVATION failed", "in-room commit NOT sent", " STALE (", "INVALID_CELL", "BAD_DIGEST", "taint set") if x in ht or x in text]
    check("L-8 no conservation failure / refused commit / taint (%s)" % bad, not bad)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()


def run_ui_store(args, results, ip, rig):
    """--ui-store (6l, slot order (d)): the 27-slot screen's own put / take logic (pc_dresser_ui_put / _take / _poll + the room's vanilla keep-slot operations; the
    pad / window are not driven). An ordinary item goes into a VANILLA slot (vanilla first, unchanged), a furniture item right after it is REFUSED because the
    room holds that uncommitted edit -> notice 5 (never "full", nothing moves); once the vanilla put is committed the "reopened" screen stores the furniture in an
    EXTRA slot (kind-18 PUT), then both are taken back; the room exit commits the vanilla take-out."""
    print("SUPERSEDED (6q S1): this scenario relied on the pre-6q vanilla-first rule (an ordinary item went into a vanilla keep slot = a room edit); ordinary items "
          "go to the extra slots now. Covered by --ui-mixopen (aligned list) and the source checks.")
    return

    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    seed_chest(residents[0])
    host = rig.start_host("dresserstore", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    env = {"AC_TEST_DRESSER_ROOM": "1", "AC_TEST_DRESSER_UI": "store", "AC_TEST_DRESSER_UI_FTR": "%04X" % CHEST}
    os.environ.update(env)
    cl = rig.start_client("dresserstore", args.port, residents[0], [])
    for k in env:
        os.environ.pop(k, None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"store: done:|store: SKIPPED", 400.0)
    cl.wait_for_log(r"\[NET\]\[SCENE\] local scene live: scene=7 .*seq=3", 60.0)
    L.pump_sleep(6.0)
    text, ht = cl.log_text(), host.log_text()
    m1 = re.search(r"store: put of the ordinary item 0x([0-9A-F]{4}) \(pocket \d+\) -> act (\d+)", text)
    check("A-1 the ordinary item went into a VANILLA keep slot first (%s)" % (m1.group(0) if m1 else None), m1 is not None and m1.group(2) == "1" and
          "into a vanilla slot -> 1" in text)
    m2 = re.search(r"store: put of the furniture 0x([0-9A-F]{4}) \(pocket \d+\) right after it -> act (\d+) \((\w+)\), notice (\d+), extras (\d+), pocket still 0x([0-9A-F]{4})", text)
    # 6p: the put WAITS (deferred, notice 4) for the in-room commit; this API-level drive closes the (logical) screen right away, so it is dropped: nothing moved
    check("A-2 the furniture put right after it was DEFERRED (not 'full'), notice 4, nothing moved (%s)" % (m2.group(0) if m2 else None),
          m2 is not None and m2.group(3) == "DEFERRED" and m2.group(4) == "4" and m2.group(5) == "0" and m2.group(6) == m2.group(1))
    check("A-2b the client logged the refusal cause (uncommitted edits), the deferral and the drop at the close",
          "extra-slot transaction refused: the room holds uncommitted edits (close the dresser, reopen it) -> notice 5" in text and
          "WAITS for the in-room commit of this screen's vanilla-slot edits (notice 4)" in text and "not sent: dropped (nothing moved)" in text)
    check("A-3 the vanilla put was committed (in-room commit APPLIED)", "store: the vanilla put is COMMITTED" in text and
          re.search(r"OWNER_COMMIT xfer \d+ house \d+ APPLIED", ht) is not None)
    check("A-4 'reopened': the furniture went into an EXTRA slot (kind-18 PUT APPLIED)", "store: reopened; put of the furniture" in text and "(host transaction)" in text and
          re.search(r"store: extra PUT of 0x[0-9A-F]{4} -> host answer APPLIED \(extras 1\)", text) is not None)
    check("A-5 both taken back: the extra TAKE APPLIED and the vanilla slot emptied",
          re.search(r"store: extra TAKE of 0x[0-9A-F]{4} -> host answer APPLIED \(extras 0\)", text) is not None and
          re.search(r"store: take of the vanilla slot \d -> act 1", text) is not None)
    md = re.search(r"store: done: the ordinary item 0x[0-9A-F]{4} in pockets (\d+), the furniture 0x[0-9A-F]{4} in pockets (\d+), extras (\d+)", text)
    check("A-6 both items are back in the pockets, extras empty (%s)" % (md.group(0) if md else None), md is not None and int(md.group(1)) >= 1 and
          int(md.group(2)) >= 1 and md.group(3) == "0")
    check("A-7 the host committed exactly one PUT and one TAKE", len(re.findall(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ PUT ", ht)) == 1 and
          len(re.findall(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ TAKE ", ht)) == 1)
    bad = [x for x in ("CONSERVATION failed", "in-room commit NOT sent", " STALE (", "INVALID_CELL", "BAD_DIGEST", "taint set") if x in ht or x in text]
    check("A-8 no conservation failure / refused commit / taint (%s)" % bad, not bad)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()


def run_ui_keepguard(args, results, ip, rig):
    """--ui-keepguard (6l, real client): the client puts an ordinary item into a VANILLA keep slot of the seeded chest and waits until the in-room commit made it
    canonical, leaves the room, then the swap hook (outside the room) moves the chest WITH its keep item (AC_TEST_HOUSE_CLIENT_SWAP_KEEP: layers 1..3 travel,
    as in a vanilla move) plus an UNRELATED plain furniture piece in the same edit. The client commit guard must count the canonical keep item as stored
    (27-slot lock) and REVERT only the chest move in the outgoing image + the local save; the host APPLIES the rest (no step-9b refusal)."""
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    ha = residents[0]
    img = L.house_from_gci(L.GAME_BIN_DIR, ha)
    fcell, ecells = seed_chest(ha)
    plain = [c for c in range(L.HOUSE_CELLS) if (L.house_cell(img, 0, 0, c) >> 12) in (1, 3) and (L.house_cell(img, 0, 0, c) & ~3) != CHEST]
    ecells = [c for c in ecells if all(L.house_cell(img, 0, l, c) == 0 for l in (1, 2, 3))]
    check("setup: a free cell and a plain furniture piece exist", len(ecells) >= 2 and len(plain) >= 1)
    if len(ecells) < 2 or not plain:
        return
    host = rig.start_host("dresserkeepg", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    env = {"AC_TEST_DRESSER_ROOM": "1", "AC_TEST_DRESSER_UI": "keep", "AC_TEST_DRESSER_UI_FTR": "%04X" % CHEST,
           "AC_TEST_HOUSE_CLIENT_SWAP": "0,%d,%d,%d,%d" % (fcell, ecells[0], plain[0], ecells[1]), "AC_TEST_HOUSE_CLIENT_SWAP_KEEP": "1"}
    os.environ.update(env)
    cl = rig.start_client("dresserkeepg", args.port, ha, [])
    for k in env:
        os.environ.pop(k, None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"\[NET\]\[HOUSE\]\[TEST-ONLY\] client: swapped the layer-0 cells|keep: vanilla keep put of 0x[0-9A-F]{4} into the chest: NOTHING", 400.0)
    host.wait_for_log(r"\[NET\]\[HOUSE\] host: peer \d+ OWNER_COMMIT xfer \d+ house \d+ (APPLIED|REJECTED)", 60.0)
    cl.wait_for_log(r"APPLIED image floor 0 layer 1:", 30.0)
    L.pump_sleep(3.0)
    text, ht = cl.log_text(), host.log_text()
    mk = re.search(r"keep: vanilla keep put of 0x([0-9A-F]{4}) into the chest: DONE", text)
    check("K-1 a vanilla keep item went into the chest and was COMMITTED in the room (%s)" % (mk.group(0) if mk else None),
          mk is not None and "keep: the keep item is COMMITTED" in text)
    kit = int(mk.group(1), 16) if mk else -1
    check("K-2 outside the room the swap moved the chest WITH its keep item (cell %d -> %d) + a plain piece (cell %d -> %d)" % (fcell, ecells[0], plain[0], ecells[1]),
          "swapped the layer-0 cells" in text and "the swap carried layers 1..3 too" in text and
          text.find("ui: leaving the room") < text.find("swapped the layer-0 cells"))
    # 6q (S2): moving a dresser with a keep item is LEGAL now: the guard leaves it (no revert), the keep item travels with the piece (house image)
    check("K-3/K-4 (6q) the commit guard did NOT revert the move of the chest holding a keep item (moves are legal; only a removal would be flagged)",
          "reverted in the outgoing image" not in text and "NOT revertible here" not in text)
    check("K-5 the host APPLIED the commit (no step-9b refusal, no rollback)", re.search(r"OWNER_COMMIT xfer \d+ house \d+ APPLIED", ht) is not None and
          "REFUSED (27-slot lock" not in ht and "REFUSED (take the stored items out first)" not in ht and "restored to the host's state" not in text)
    ma = re.search(r"(?s).*APPLIED image floor 0: cell %d = 0x([0-9A-F]{4}) cell %d = 0x([0-9A-F]{4}) cell %d = 0x([0-9A-F]{4}) cell %d = 0x([0-9A-F]{4})" %
                   (fcell, ecells[0], plain[0], ecells[1]), text)
    m1 = re.search(r"(?s).*APPLIED image floor 0 layer 1: cell %d = 0x([0-9A-F]{4}) cell %d = 0x([0-9A-F]{4})" % (fcell, ecells[0]), text)
    check("K-6 (6q) the APPLIED image has the chest AND its keep item 0x%04X MOVED to cell %d, cell %d empty (%s | %s)" % (kit, ecells[0], fcell,
                                                                                                                     ma.group(0)[-160:] if ma else None, m1.group(0)[-90:] if m1 else None),
          ma is not None and int(ma.group(2), 16) & ~3 == CHEST and int(ma.group(1), 16) == 0 and m1 is not None and int(m1.group(2), 16) == kit and
          int(m1.group(1), 16) == 0)
    check("K-7 the UNRELATED plain furniture move (cell %d -> %d) is INTACT in the same applied commit" % (plain[0], ecells[1]),
          ma is not None and int(ma.group(3), 16) == 0 and int(ma.group(4), 16) == L.house_cell(img, 0, 0, plain[0]))
    bad = [x for x in ("CONSERVATION failed", " STALE (", "INVALID_CELL", "BAD_DIGEST", "taint set") if x in ht or x in text]
    check("K-8 no conservation failure / refused commit / taint (%s)" % bad, not bad)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()


def run_ui_ftr(args, results, ip, rig):
    """--ui-ftr VARIANT (6o): a FURNITURE pocket item stored and taken back through the REAL 27-slot screen, driven by pad input (AC_TEST_DRESSER_GYROID_STEPS):
      empty   : the chest's extras are empty: the furniture must land in extra 0 = grid cell 3 and be taken back from grid cell 3
      prefill : one ordinary item is put into extra 0 first (put-only round trip in town): the furniture must land in extra 1 = grid cell 4 (offset check)"""
    check = rig.check
    variant = args.ui_ftr
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    rec = L.record_from_gci(L.GAME_BIN_DIR, residents[0])
    pk = list(struct.unpack(">15H", rec[0x68:0x68 + 30]))
    fslot = next((i for i, x in enumerate(pk) if (x >> 12) in (1, 3)), None)
    check("setup: the client's pockets hold a furniture item (%s)" % (None if fslot is None else "pocket %d = 0x%04X" % (fslot, pk[fslot])), fslot is not None)
    if fslot is None:
        return
    fitem = pk[fslot]
    seed_chest(residents[0])
    os.environ["AC_TEST_DRESSER_ANY_SCENE"] = "1"
    host = rig.start_host("dresserftr", args.port, [])
    os.environ.pop("AC_TEST_DRESSER_ANY_SCENE", None)
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    want = 0 if variant == "empty" else 1
    # grid 0 -> down x3 = the pockets' top row (pocket column 0) -> right to the furniture pocket -> A, A ("Put this in") -> up x3 = grid row 0 ->
    # left x8 = grid 0 -> right to grid 3 + want -> A, A ("Take it") -> B x3
    steps = [64, 64, 64] + [2] * fslot + [1, 1] + [128, 128, 128] + [4] * 8 + [2] * want + [1, 1] + [8, 8, 8]  # 6q S1: extra j = grid j
    env = {"AC_TEST_DRESSER_ROOM": "1", "AC_TEST_DRESSER_UI": "gyroid", "AC_TEST_DRESSER_UI_FTR": "%04X" % CHEST, "AC_TEST_DRESSER_GYROID": "1",
           "AC_TEST_DRESSER_GYROID_STEPS": ",".join(str(x) for x in steps)}
    if variant == "prefill":
        env["AC_TEST_DRESSER_ROUNDTRIP"] = "put"
    os.environ.update(env)
    cl = rig.start_client("dresserftr", args.port, residents[0], [])
    for k in env:
        os.environ.pop(k, None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"ui: gyroid overlay closed|ui: SKIPPED", 400.0)
    L.pump_sleep(6.0)
    text, ht = cl.log_text(), host.log_text()
    check("F-0 all %d pad steps ran" % len(steps), "gyroid pad step %d:" % len(steps) in text)
    mp = re.search(r"gyroid overlay: put pocket %d item 0x%04X into the extra storage \(host transaction\)" % (fslot, fitem), text)
    check("F-1 the furniture 0x%04X (pocket %d) went to the EXTRA storage through the real put path (never a vanilla slot)" % (fitem, fslot),
          mp is not None and "put pocket %d item 0x%04X into a vanilla slot -> 1" % (fslot, fitem) not in text)
    mh = re.search(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ PUT .*", ht)
    check("F-2 the host APPLIED the PUT (%s)" % (mh.group(0)[:160] if mh else None), mh is not None)
    cells = re.findall(r"gyroid overlay: drawn cells:(.*)", text)
    shown = [c for c in cells if "0x%04X" % fitem in c]
    check("F-3 the screen DRAWS the furniture at grid cell %d (= extra %d, the first free cell: 6q aligned list) (%s)" % (want, want, shown[:1]),
          bool(shown) and ("grid %d = 0x%04X" % (want, fitem)) in shown[0])
    mt = re.search(r"gyroid overlay: take extra slot (\d+) item 0x%04X -> pocket (\d+) \(host transaction\)" % fitem, text)
    check("F-4 'Take it' on grid cell %d sent a kind-18 TAKE of extra %d (%s)" % (want, want, mt.group(0) if mt else None),
          mt is not None and int(mt.group(1)) == want)
    check("F-5 the host APPLIED the TAKE and the furniture is back in a pocket",
          "gyroid overlay: host answer APPLIED" in text[text.find(mt.group(0)) if mt else 0:] and
          len(re.findall(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ TAKE ", ht)) >= 1 and
          any(("0x%04X" % fitem) not in c for c in cells[-1:]))
    refused = re.findall(r"gyroid overlay: take extra slot \d+ refused locally|host answer REJECTED \(reason \d+\)", text)
    check("F-6 no refused / rejected take (%s)" % refused, not refused)
    bad = [x for x in ("CONSERVATION failed", " STALE (", "INVALID_CELL", "BAD_DIGEST", "taint set") if x in ht or x in text]
    check("F-7 no conservation failure / refused commit / taint (%s)" % bad, not bad)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()


def run_ui_ftrmix(args, results, ip, rig):
    """--ui-ftrmix (6o, the user's 13:2x session): with the 27-slot screen's own put / take logic: the FURNITURE is stored first (extra slot, host transaction),
    then an ORDINARY item (vanilla slot = a room edit); the screen is closed, the room commits the vanilla put IN THE ROOM, the screen is reopened and the
    furniture is taken back (extra slot) and the ordinary item too. Before the 6o fix the in-room commit failed its conservation pre-check (the baseline did not
    follow the furniture PUT: "0x1304 (1 -> 0)"), in-room commits stopped and the furniture could not be taken out (notice 2) until the room exit."""
    print("SUPERSEDED (6q S1): this scenario relied on the pre-6q vanilla-first rule (an ordinary item went into a vanilla keep slot = a room edit); ordinary items "
          "go to the extra slots now. Covered by --ui-mixopen (aligned list) and the source checks.")
    return

    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    seed_chest(residents[0])
    host = rig.start_host("dresserfmix", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    env = {"AC_TEST_DRESSER_ROOM": "1", "AC_TEST_DRESSER_UI": "ftrmix", "AC_TEST_DRESSER_UI_FTR": "%04X" % CHEST}
    os.environ.update(env)
    cl = rig.start_client("dresserfmix", args.port, residents[0], [])
    for k in env:
        os.environ.pop(k, None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"ftrmix: done:|ftrmix: SKIPPED", 400.0)
    cl.wait_for_log(r"\[NET\]\[SCENE\] local scene live: scene=7 .*seq=3", 60.0)
    L.pump_sleep(6.0)
    text, ht = cl.log_text(), host.log_text()
    check("M-1 the furniture went into an EXTRA slot (kind-18 PUT APPLIED)", re.search(r"ftrmix: extra PUT of 0x[0-9A-F]{4} -> host answer APPLIED \(extras 1\)", text) is not None)
    mb = re.search(r"in-room conservation baseline follows the dresser PUT of 0x([0-9A-F]{4})(.*)", text)
    check("M-2 the in-room conservation baseline followed the FURNITURE put (%s)" % (mb.group(0) if mb else None), mb is not None)
    check("M-3 then the ordinary item went into a VANILLA slot (a room edit)", re.search(r"ftrmix: put of the ordinary item 0x[0-9A-F]{4} -> act 1 ", text) is not None)
    check("M-4 after closing, the in-room commit of the vanilla put was APPLIED (no conservation stop)",
          "ftrmix: the vanilla put is COMMITTED" in text and "in-room commit NOT sent: the snapshot + pockets do not conserve" not in text and
          re.search(r"OWNER_COMMIT xfer \d+ house \d+ APPLIED", ht) is not None)
    mt = re.search(r"ftrmix: reopened; take of the furniture 0x[0-9A-F]{4} from grid (\d+) -> act (\d+) \(notice (\d+)\)", text)
    check("M-5 reopened: the take of the furniture from grid 3 (extra 0) was SENT (%s)" % (mt.group(0) if mt else None),
          mt is not None and mt.group(1) == "3" and mt.group(2) == "2")
    check("M-6 the host APPLIED the TAKE (furniture back in a pocket) and the ordinary item came back too",
          re.search(r"ftrmix: extra TAKE of 0x[0-9A-F]{4} -> host answer APPLIED \(extras 0\)", text) is not None and
          re.search(r"ftrmix: take of the ordinary item from vanilla slot \d -> act 1", text) is not None and
          re.search(r"ftrmix: done: the ordinary item 0x[0-9A-F]{4} in pockets 1, the furniture 0x[0-9A-F]{4} in pockets 1, extras 0", text) is not None)
    bad = [x for x in ("CONSERVATION failed", " STALE (", "INVALID_CELL", "BAD_DIGEST", "taint set", "in-room commits stopped") if x in ht or x in text]
    check("M-7 no conservation failure / stopped in-room commits / refused commit / taint (%s)" % bad, not bad)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()


def run_ui_mixopen(args, results, ip, rig):
    """--ui-mixopen (6q S1 since the aligned list; was 6p; WRITTEN, NOT RUN when it was added: needs a game process, the user's session was active): the user's 14:0x sequence in ONE open 27-slot
    screen, driven by pad input through the real overlay: the FURNITURE goes in (extra 0 = grid 3, host transaction), then the CLOTHES (0x24xx) and the PAPER
    (0x20xx) go into vanilla slots (room edits), then 'Take it' on grid 3 while the screen stays OPEN. Expected (6p): the take WAITS (notice 4), the room is
    committed IN THE ROOM while the screen is open (its settled state counts as quiet), the deferred take is then SENT and APPLIED, the furniture is back in a
    pocket; nothing is refused for good, no conservation stop."""
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    rec = L.record_from_gci(L.GAME_BIN_DIR, residents[0])
    pk = list(struct.unpack(">15H", rec[0x68:0x68 + 30]))
    fslot = next((i for i, x in enumerate(pk) if (x >> 12) in (1, 3)), None)
    oslots = [i for i, x in enumerate(pk) if (x >> 12) == 2][:2]
    check("setup: pockets hold a furniture item and two ordinary items (%s)" % pk[:5], fslot is not None and len(oslots) == 2)
    if fslot is None or len(oslots) < 2:
        return
    seed_chest(residents[0])
    if getattr(args, "dedicated", False):
        # --dedicated: a REAL dedicated server (`--host PORT --dedicated`, the observer, no resident) whose town + sidecar live in <fixture>\servers\default; a fresh
        # tree (removed first; the first launch adopts the fixture's save/card_a town by copy). Only this fixture's own tree is touched.
        shutil.rmtree(L.server_dir(), ignore_errors=True)
        rig.restore_save()
        host = L.HostProcess(port=args.port, extra_args=["--dedicated"], log_path=os.path.join(HERE, "txn_real_dressermixo_dedicated_host.log"), bin_dir=L.GAME_BIN_DIR,
                             stdin_pipe=True).start()
        if host.wait_for_log(r"\[1\] Use this town", 60.0) is not None:  # first launch with the fixture's legacy town: answer the console question (use it)
            host.send_line("1")
        ok = host.wait_listening(60.0) and host.boot_to_dedicated(timeout=120.0)
        rig.host = host
        hl = host.log_text()
        check("DEDICATED host: '[DEDICATED] dedicated server starting' + the observer is ready (status), NOT a bootstrap resident",
              ok and "[DEDICATED] dedicated server starting" in hl and "[NET][OBSERVER] host: observer active" in hl and "bootstrap-resident 0: resident bound" not in hl)
        if not ok:
            return
    else:
        host = rig.start_host("dressermixo", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)

    def to_pocket(cur, dst):
        return [2] * (dst - cur) if dst > cur else [4] * (cur - dst)
    # grid 0 -> down x3 = pocket 0; furniture: A, A; the two ordinary items: A, A each; up x3 = grid row 0; left x8; right x3 = grid 3; A, A (take); B x3
    steps = [64, 64, 64] + to_pocket(0, fslot) + [1, 1] + to_pocket(fslot, oslots[0]) + [1, 1] + to_pocket(oslots[0], oslots[1]) + [1, 1]
    steps += [128, 128, 128] + [4] * 8 + [1, 1] + [8, 8, 8]  # 6q S1: the furniture is the FIRST item = grid 0
    env = {"AC_TEST_DRESSER_ROOM": "1", "AC_TEST_DRESSER_UI": "gyroid", "AC_TEST_DRESSER_UI_FTR": "%04X" % CHEST, "AC_TEST_DRESSER_GYROID": "1",
           "AC_TEST_DRESSER_GYROID_STEPS": ",".join(str(x) for x in steps)}
    os.environ.update(env)
    cl = rig.start_client("dressermixo", args.port, residents[0], [])
    for k in env:
        os.environ.pop(k, None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"ui: gyroid overlay closed|ui: SKIPPED", 500.0)
    L.pump_sleep(6.0)
    text, ht = cl.log_text(), host.log_text()
    closed = text.find("ui: gyroid overlay closed")
    check("X-0 all %d pad steps ran" % len(steps), "gyroid pad step %d:" % len(steps) in text)
    # 6q S1: ONE aligned list -- every item (furniture, clothes, paper) goes to the first free EXTRA slot in put order (grid 0, 1, 2); no vanilla-slot room edit
    puts = re.findall(r"gyroid overlay: put pocket (\d+) item 0x([0-9A-F]{4}) (into the extra storage \(host transaction\)|into a vanilla slot[^\n]*)", text)
    check("X-1 all three items went to the EXTRA storage (host transactions), none to a vanilla slot (%s)" % puts,
          len(puts) == 3 and all("extra storage" in p_[2] for p_ in puts))
    cells = re.findall(r"gyroid overlay: drawn cells:([^\n]*)", text)
    want = " grid 0 = 0x%04X grid 1 = 0x%04X grid 2 = 0x%04X" % (int(puts[0][1], 16), int(puts[1][1], 16), int(puts[2][1], 16)) if len(puts) == 3 else "?"
    check("X-2 the screen shows them ALIGNED in put order: %s" % want, any(c.rstrip("\r").startswith(want) for c in cells))
    mt = re.search(r"gyroid overlay: take extra slot 0 item 0x([0-9A-F]{4}) -> pocket \d+ \(host transaction\)", text)
    check("X-3 'Take it' on grid 0 took the furniture (extra slot 0) (%s)" % (mt.group(0) if mt else None), mt is not None and int(mt.group(1), 16) == int(puts[0][1], 16) if puts else False)
    check("X-4 the host APPLIED 3 PUTs and 1 TAKE; the client applied the TAKE before the screen closed",
          len(re.findall(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ PUT ", ht)) == 3 and len(re.findall(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ TAKE ", ht)) == 1 and
          0 <= text.find("client: TAKE APPLIED") < closed)
    check("X-5 no refusal at all (nothing waited for a room commit, nothing refused locally)", "refused locally" not in text and "WAITS for the in-room commit" not in text)
    bad = [x for x in ("CONSERVATION failed", "in-room commits stopped", " STALE (", "INVALID_CELL", "BAD_DIGEST", "taint set", "gave up after") if x in ht or x in text]
    check("X-6 no conservation stop / refused commit / taint / give-up (%s)" % bad, not bad)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()


def run_ui_grab(args, results, ip, rig):
    """--ui-grab (6r B; WRITTEN, NOT RUN when it was added: needs a game process, the user's session was active): the GRAB of the 27-cell screen through the
    REAL pad path (cell tag: Take it / Grab / Quit; pocket tag: Put Away / Grab / Quit; the grab row = A, down, A). Pockets 0x240D / 0x1304 / 0x2037:
      1 put pocket 0 -> grid 0, pocket 1 -> grid 1
      2 grab grid 0, A on the EMPTY grid 2 = MOVE (op 3)
      3 grab grid 1 (furniture), A on pocket 2 (paper) = SWAP (op 4): grid 1 = paper, pocket 2 = furniture
      4 grab pocket 2 (furniture), A on the EMPTY grid 0 = PUT with the chosen slot
      5 grab grid 2, B = cancel (nothing moves)
    Expected: grid 0 = 0x1304, grid 1 = 0x2037, grid 2 = 0x240D; the host logged MOVE / SWAP / PUT; conservation holds."""
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    rec = L.record_from_gci(L.GAME_BIN_DIR, residents[0])
    pk = list(struct.unpack(">15H", rec[0x68:0x68 + 30]))
    check("setup: pockets 0..2 = 0x240D 0x1304 0x2037 (%s)" % [hex(x) for x in pk[:4]], pk[:3] == [0x240D, 0x1304, 0x2037])
    if pk[:3] != [0x240D, 0x1304, 0x2037]:
        return
    seed_chest(residents[0])
    host = rig.start_host("dressergrab", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    GRAB = [1, 64, 1]
    steps = [64, 64, 64, 1, 1, 2, 1, 1]                     # 1: pocket 0 -> grid 0, pocket 1 -> grid 1
    steps += [128, 128, 128] + [4] * 8 + GRAB + [2, 2, 1]   # 2: grab grid 0, place on grid 2 (MOVE)
    steps += [4] + GRAB + [64, 64, 64] + [4] * 5 + [2, 2, 1]  # 3: grab grid 1, place on pocket 2 (SWAP)
    steps += GRAB + [128, 128, 128] + [4] * 8 + [1]          # 4: grab pocket 2, place on grid 0 (PUT)
    steps += [2, 2] + GRAB + [8]                              # 5: grab grid 2, B = cancel
    steps += [8, 8, 8]
    env = {"AC_TEST_DRESSER_ROOM": "1", "AC_TEST_DRESSER_UI": "gyroid", "AC_TEST_DRESSER_UI_FTR": "%04X" % CHEST, "AC_TEST_DRESSER_GYROID": "1",
           "AC_TEST_DRESSER_GYROID_STEPS": ",".join(str(x) for x in steps)}
    os.environ.update(env)
    cl = rig.start_client("dressergrab", args.port, residents[0], [])
    for k in env:
        os.environ.pop(k, None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"ui: gyroid overlay closed|ui: SKIPPED", 600.0)
    L.pump_sleep(6.0)
    text, ht = cl.log_text(), host.log_text()
    check("R-0 all %d pad steps ran" % len(steps), "gyroid pad step %d:" % len(steps) in text)
    check("R-1 both puts went to grid 0 / grid 1", re.search(r"drawn cells: grid 0 = 0x240D grid 1 = 0x1304", text) is not None)
    check("R-2 grab grid 0 + A on the empty grid 2 = MOVE (host op 3)", "GRAB 0x240D from grid cell 0" in text and "GRAB place: grid 0 -> grid 2 (MOVE)" in text and
          re.search(r"host: peer \d+ resident \d+ MOVE item=0x240D pool slot 0 -> 2", ht) is not None)
    check("R-3 grab grid 1 + A on pocket 2 (paper) = SWAP (host op 4): grid 1 = paper, pocket 2 = furniture",
          "GRAB place: grid 1 <-> pocket 2 (SWAP)" in text and re.search(r"host: peer \d+ resident \d+ SWAP item=0x1304 pocket=2 pool slot=1", ht) is not None and
          "client: SWAP APPLIED" in text)
    check("R-4 grab pocket 2 + A on the empty grid 0 = PUT into slot 0", "GRAB 0x1304 from pocket 2" in text and "GRAB place: pocket 2 -> empty grid 0 (PUT)" in text and
          re.search(r"host: peer \d+ resident \d+ PUT item=0x1304 pocket=2 pool slot=0", ht) is not None)
    check("R-5 grab grid 2 + B = cancelled, nothing moved", "grab of 0x240D cancelled (nothing moved)" in text)
    check("R-6 the screen ends with grid 0 = 0x1304, grid 1 = 0x2037, grid 2 = 0x240D",
          re.search(r"drawn cells: grid 0 = 0x1304 grid 1 = 0x2037 grid 2 = 0x240D\s*$", text, re.M) is not None)
    bad = [x for x in ("CONSERVATION failed", " STALE (", "INVALID_CELL", "BAD_DIGEST", "taint set", "refused locally", "inconsistent post-image") if x in ht or x in text]
    check("R-7 no refusal / conservation failure / taint (%s)" % bad, not bad)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()


def sweep_steps(close_cell=(2, 7), close_button=8):
    """Pad bits: 1 A, 2 right, 4 left, 8 B, 16 L, 32 R, 64 down, 128 up, 256 START, 512 Z. A snake over all 27 cells; on EVERY cell A (on the filled cell
    grid 3 this opens the tag window), L, R, Z (inside the tag window on grid 3, then B closes the tag window only); finally the cursor goes to `close_cell`
    (row, col) and `close_button` closes the overlay there (the 10:17 crash: B on row 2 col 7)."""
    steps, row, col = [], 0, 0
    order = [(r, c) for r in range(3) for c in (range(9) if r % 2 == 0 else range(8, -1, -1))]
    for i, (r, c) in enumerate(order):
        if i > 0:
            steps.append(64 if r != row else (2 if c > col else 4))
        row, col = r, c
        if r * 9 + c == 0:  # 6q S1: the seeded extra slot 0 = grid 0
            steps += [1, 16, 32, 512, 8]  # tag window open on the filled extra slot: L / R / Z inside it, then B closes the tag window only
        else:
            steps += [1, 16, 32, 512]
    tr, tc = close_cell
    while row > tr:
        steps.append(128)
        row -= 1
    while col < tc:
        steps.append(2)
        col += 1
    while col > tc:
        steps.append(4)
        col -= 1
    steps.append(close_button)
    return steps


def run_ui_sweep(args, results, ip, rig):
    """--ui-sweep (crash regression, 10:17 access violation in mHW_haniwa_ovl_move): every button (A, L, R, Z; B inside the tag window) on every cell of the
    3 x 9 grid with one extra item, then B (or START: --ui-sweep start) on row 2 col 7 closes the overlay; the client must stay alive and leave its room."""
    check = rig.check
    button = 256 if args.ui_sweep == "start" else 8
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    seed_chest(residents[0])
    os.environ["AC_TEST_DRESSER_ANY_SCENE"] = "1"
    host = rig.start_host("dressersweep", args.port, [])
    os.environ.pop("AC_TEST_DRESSER_ANY_SCENE", None)
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    steps = sweep_steps(close_button=button)
    env = {"AC_TEST_DRESSER_ROOM": "1", "AC_TEST_DRESSER_UI": "gyroid", "AC_TEST_DRESSER_UI_FTR": "%04X" % CHEST, "AC_TEST_DRESSER_GYROID": "1",
           "AC_TEST_DRESSER_ROUNDTRIP": "put", "AC_TEST_DRESSER_GYROID_STEPS": ",".join(str(x) for x in steps)}
    os.environ.update(env)
    cl = rig.start_client("dressersweep", args.port, residents[0], [])
    for k in env:
        os.environ.pop(k, None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"ui: gyroid overlay closed|ui: SKIPPED", 520.0)
    L.pump_sleep(8.0)
    text, ht = cl.log_text(), host.log_text()
    check("S-1 the overlay opened in dresser mode with 1 extra item", re.search(r"ui: gyroid overlay requested \(dresser furniture \d+, 1 extra item\(s\)\)", text) is not None)
    check("S-2 all %d scripted steps ran (every button on every one of the 27 cells)" % len(steps), "gyroid pad step %d:" % len(steps) in text)
    seen = set(int(g) for g in re.findall(r"gyroid pad step \d+ done: cursor table 6 row \d+ col \d+ \(grid (\d+)\)", text)) | {0}
    check("S-3 the cursor visited all 27 cells (%d)" % len(seen), seen >= set(range(27)))
    check("S-4 the tag window opened on the filled cell (A) and was closed by B inside it", "tag window" in text or "type 40" in text)
    check("S-5 %s on row 2 col 7 closed the overlay: dresser mode ended" % ("START" if button == 256 else "B"), "ui: gyroid overlay closed" in text)
    check("S-6 the client left its room afterwards", "ui: leaving the room" in text)
    check("S-7 the CLIENT and the host are ALIVE (no access violation)", host.alive() and cl.alive())
    bad = [x for x in ("CONSERVATION failed", "taint set", "REJECTED INVALID_CELL") if x in ht or x in text]
    check("S-8 no conservation failure / refused commit / taint (%s)" % bad, not bad)
    cl.stop()


def run_ui_gyroid(args, results, ip, rig):
    """--ui-gyroid: the REAL gyroid-style overlay in dresser mode, driven by scripted pad input through the real pad path (AC_TEST_DRESSER_GYROID).
    The client first puts one pocket item into the chest's EXTRA storage (round-trip hook, put-only), walks into its own room, the room opens the
    overlay on the seeded chest, and the pad script: D-right x3 (page 1 column 4 = extra slot 1), A, A ("Take it"), R, L (pages), D-left x2, C-down, A, A
    ("Put this in"), B x3; then the client leaves the room (the vanilla-slot change is committed by furniture sync)."""
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    seed_chest(residents[0])
    os.environ["AC_TEST_DRESSER_ANY_SCENE"] = "1"  # the put-only seeding happens in town
    host = rig.start_host("dressergyroid", args.port, [])
    os.environ.pop("AC_TEST_DRESSER_ANY_SCENE", None)
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    gsteps = [2] * 8 + [64] + [4] * 8 + [64] + [2] * 8 + [128, 128] + [4] * 8 + [1, 1] + [64, 64, 64, 4, 1, 1] + [8, 8, 8]  # 6q S1: the take on grid 0
    env = {"AC_TEST_DRESSER_ROOM": "1", "AC_TEST_DRESSER_UI": "gyroid", "AC_TEST_DRESSER_UI_FTR": "%04X" % CHEST, "AC_TEST_DRESSER_GYROID": "1",
           "AC_TEST_DRESSER_ROUNDTRIP": "put", "AC_TEST_DRESSER_GYROID_STEPS": ",".join(str(x) for x in gsteps)}
    os.environ.update(env)
    cl = rig.start_client("dressergyroid", args.port, residents[0], [])
    for k in env:
        os.environ.pop(k, None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"ui: gyroid overlay closed|ui: SKIPPED", 200.0)
    cl.wait_for_log(r"\[NET\]\[SCENE\] local scene live: scene=7 .*seq=3", 60.0)
    L.pump_sleep(6.0)
    text, ht = cl.log_text(), host.log_text()
    check("G-1 the extra storage was seeded with one item (put-only round trip APPLIED)", "roundtrip put-only" in text)
    m = re.search(r"ui: gyroid overlay requested \(dresser furniture \d+, (\d+) extra item\(s\)\)", text)
    check("G-2 the room opened the gyroid-style overlay in DRESSER MODE on the chest (%s)" % (m.group(0) if m else None), m is not None and int(m.group(1)) == 1)
    check("G-3 the scripted pad input went through all %d steps" % len(gsteps), "gyroid pad step %d" % len(gsteps) in text)
    seen = set(int(g) for g in re.findall(r"gyroid pad step \d+ done: cursor table 6 row \d+ col \d+ \(grid (\d+)\)", text))
    if re.search(r"gyroid pad step 1 done: cursor table 6 row 0 col 1 \(grid 1\)", text):
        seen.add(0)  # the cursor starts on grid 0 (one 'right' from it reached grid 1)
    nc = len(re.findall(r"gyroid pad step \d+: NOT consumed", text))
    check("G-3b U3 one page: the cursor reached EVERY one of the 27 grid cells (%d reached, %d presses not consumed)" % (len(seen), nc), seen == set(range(27)))
    check("G-3c 'down' from the grid's bottom row reaches the pockets' top row, 'left' pocket 0",
          re.search(r"gyroid pad step 41 done: cursor table 0 row 0 col \d \(grid -1\)", text) is not None and
          re.search(r"gyroid pad step 42 done: cursor table 0 row 0 col 0 \(grid -1\)", text) is not None)
    mt = re.search(r"gyroid overlay: take extra slot 0 item 0x([0-9A-F]{4}) -> pocket (\d+) \(host transaction\)", text)
    check("G-4 'Take it' on grid cell 0 (6q: extra slot 1) sent a kind-18 TAKE of extra slot 1 (%s)" % (mt.group(0) if mt else None), mt is not None)
    check("G-5 the host APPLIED it (the gyroid's 'Got it!' path) and committed exactly one TAKE",
          "gyroid overlay: host answer APPLIED" in text and len(re.findall(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ TAKE ", ht)) == 1)
    check("G-6 U3 one page: no paging at all (no page turn logged)", "gyroid overlay: page" not in text)
    mp = re.search(r"gyroid overlay: put pocket (\d+) item 0x([0-9A-F]{4}) (into a vanilla slot -> 1|into the extra storage \(host transaction\))", text)
    check("G-7 'Put this in' on a pocket item stored it in the EXTRA storage (6q: extras first) (%s)" % (mp.group(0) if mp else None),
          mp is not None and "into the extra storage" in mp.group(3))
    mc = re.search(r"ui: gyroid overlay closed \((\d+) extra item\(s\) now\)", text)
    check("G-8 the overlay closed (B) and dresser mode ended (%s)" % (mc.group(0) if mc else None), mc is not None)
    check("G-9 the client left its room afterwards", "ui: leaving the room" in text)
    bad = [x for x in ("CONSERVATION failed", "in-room commit NOT sent", " STALE (", "INVALID_CELL", "BAD_DIGEST", "taint set") if x in ht or x in text]
    check("G-10 no conservation failure / refused commit / taint on either side (%s)" % bad, not bad)
    if mp is not None and "vanilla slot" in mp.group(3):
        check("G-11 the vanilla-slot change was committed by furniture sync (an OWNER_COMMIT APPLIED after the room exit)", re.search(r"OWNER_COMMIT xfer \d+ house \d+ APPLIED", ht) is not None)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def run_ui_edit(args, results, ip, rig):
    """--ui-edit: three client visits to the OWN room, each followed by a furniture edit (AC_TEST_HOUSE_CLIENT_SWAP, a conserving move, applied after the room exit)
    so the house commit is NON-empty and must be APPLIED by the host's pockets+house conservation check with the dresser delta in the record:
      V1 put only (pocket -> pool), V2 take only (pool -> pocket), V3 put + take in the same visit."""
    import test_house_sync_protocol as HS
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    ha = residents[0]
    img = L.house_from_gci(L.GAME_BIN_DIR, ha)
    fcell, ecells = seed_chest(ha)
    fid = CHEST
    check("the fixture house %d has a movable furniture piece (cell %s) and a free cell" % (ha, fcell), fcell is not None and len(ecells) > 0)
    if fcell is None or not ecells:
        return
    pass  # the host default is ON; the caller may set AC_MP_DRESSER_STORAGE (0 = OFF run)
    os.environ.pop("AC_TEST_DRESSER_ANY_SCENE", None)
    host = rig.start_host("dresseredit", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    for n, mode in enumerate(("put", "take", "putake")):
        tag = "V%d %s" % (n + 1, mode)
        off = len(host.log_text())
        os.environ["AC_TEST_DRESSER_ROOM"] = "1"
        os.environ["AC_TEST_DRESSER_UI"] = mode
        os.environ["AC_TEST_DRESSER_UI_FTR"] = "%04X" % fid  # v2: the dresser is the furniture the swap moves (its storage must follow it: host re-key)
        plain = [c for c in range(L.HOUSE_CELLS) if (L.house_cell(img, 0, 0, c) >> 12) in (1, 3) and (L.house_cell(img, 0, 0, c) & ~3) != CHEST]
        if mode == "put" and plain:
            # 6h: ONE commit = the illegal move of the chest holding 1 extra item + an UNRELATED plain furniture move + a pocket edit
            os.environ["AC_TEST_HOUSE_CLIENT_SWAP"] = "0,%d,%d,%d,%d" % (fcell, ecells[0], plain[0], ecells[1])
            os.environ["AC_TEST_HOUSE_CLIENT_POCKET"] = "1"
        else:
            os.environ["AC_TEST_HOUSE_CLIENT_SWAP"] = "0,%d,%d" % (fcell, ecells[0])
        cl = rig.start_client("dredit%d" % n, args.port, ha, [])
        for k in ("AC_TEST_DRESSER_ROOM", "AC_TEST_DRESSER_UI", "AC_TEST_DRESSER_UI_FTR", "AC_TEST_HOUSE_CLIENT_SWAP", "AC_TEST_HOUSE_CLIENT_POCKET"):
            os.environ.pop(k, None)
        check("%s: real client booted and connected" % tag, cl is not None)
        if cl is None:
            return
        cl.wait_for_log(r"\[NET\]\[HOUSE\]\[TEST-ONLY\] client: swapped the layer-0 cells|ui: (PUT REJECTED|SKIPPED|take-only: the pool is empty|pool put-in refused)", 180.0)
        ap = host.wait_for_log(r"\[NET\]\[HOUSE\] host: peer \d+ OWNER_COMMIT xfer \d+ house \d+ (APPLIED|REJECTED)", 60.0) if "swapped the layer-0 cells" in cl.log_text() else None
        L.pump_sleep(3.0)
        text, ht = cl.log_text(), host.log_text()[off:]
        if mode == "put":
            m = re.search(r"ui: put-only: item 0x([0-9A-F]{4}) pockets\+pool count (\d+) before (\d+) -> (\w+)", text)
        else:
            m = re.search(r"ui: TAKE (APPLIED), item 0x([0-9A-F]{4}) pockets\+pool count (\d+) before (\d+) -> (\w+)", text)
        check("%s: the dresser transaction(s) APPLIED and pockets + pool CONSERVED (%s)" % (tag, m.group(0) if m else None), m is not None and m.group(m.lastindex) == "CONSERVED")
        check("%s: the client left the room, THEN edited its house (a furniture move)" % tag, "ui: leaving the room" in text and "swapped the layer-0 cells" in text and
              text.find("ui: leaving the room") < text.find("swapped the layer-0 cells"))
        if mode == "put":
            # 6h: the CLIENT guard reverts ONLY the illegal chest move in the outgoing image (and the local save); the unrelated furniture move and the pocket edit
            # stay in the SAME commit, which the host APPLIES (its step-9b refusal is not needed)
            ma = re.search(r"APPLIED image floor 0: cell %d = 0x([0-9A-F]{4}) cell %d = 0x([0-9A-F]{4})(?: cell %d = 0x([0-9A-F]{4}) cell %d = 0x([0-9A-F]{4}))?" %
                           (fcell, ecells[0], plain[0] if plain else -1, ecells[1]), text)
            # 6q (S2): moving a dresser that holds items is LEGAL: the client guard leaves it, the host applies it and re-keys the storage to the new cell
            check("%s: the client guard did NOT revert the move of the chest holding 1 extra item (6q: moves are legal)" % tag,
                  "reverted in the outgoing image" not in text and "NOT revertible here" not in text)
            check("%s: the SAME commit was APPLIED by the host (no step-9b refusal, no rollback)" % tag,
                  re.search(r"OWNER_COMMIT xfer \d+ house \d+ APPLIED", ht) is not None and "REFUSED (take the stored items out first)" not in ht and
                  "REJECTED INVALID_CELL" not in ht and "restored to the host's state" not in text)
            check("%s: the APPLIED image has the chest MOVED (cell %d empty, cell %d = the chest) and the host re-keyed its storage (%s)" % (tag, fcell, ecells[0],
                                                                                                                                         ma.group(0) if ma else None),
                  ma is not None and int(ma.group(1), 16) == 0 and int(ma.group(2), 16) & ~3 == CHEST and
                  re.search(r"0x%04X with extra storage MOVED cell %d -> %d: storage re-keyed|FOLLOWED its piece cell %d -> %d" % (CHEST, fcell, ecells[0], fcell, ecells[0]), ht)
                  is not None)
            if plain:
                check("%s: the UNRELATED plain furniture move (cell %d -> %d) is INTACT in the applied image" % (tag, plain[0], ecells[1]),
                      ma is not None and ma.group(3) is not None and int(ma.group(3), 16) == 0 and int(ma.group(4), 16) == L.house_cell(img, 0, 0, plain[0]))
                mp = re.search(r"ALSO moved pocket (\d+) -> pocket (\d+) \(item 0x([0-9A-F]{4})\)", text)
                ok_p = False
                if mp and ma:
                    pk = re.search(r"; pockets ([0-9A-F,]+)", ma.string[ma.start():].split("\n")[0])
                    if pk:
                        vals = pk.group(1).split(",")
                        ok_p = int(vals[int(mp.group(2))], 16) == int(mp.group(3), 16) and int(vals[int(mp.group(1))], 16) == 0
                check("%s: the pocket edit of the same commit is INTACT (%s)" % (tag, mp.group(0) if mp else None), ok_p)
        else:
            check("%s: the house commit (house edit + the record carrying the dresser delta) was APPLIED by the host's conservation check (the extras are empty)" % tag,
                  re.search(r"OWNER_COMMIT xfer \d+ house \d+ APPLIED", ht) is not None)
        bad = [x for x in ("CONSERVATION failed", "in-room commit NOT sent", " STALE (", "BAD_DIGEST", "taint set", "INVALID_CELL") if x in ht or x in text]
        check("%s: no conservation failure / unexpected refused commit / taint on either side (%s)" % (tag, bad), not bad)
        cl.stop()
        L.pump_sleep(2.0)
    check("host alive", host.alive())
    host.stop()
    rig.host = None


def run(args, results, ip, rig):
    check = rig.check
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    pass  # the host default is ON; the caller may set AC_MP_DRESSER_STORAGE (0 = OFF run)
    os.environ["AC_TEST_DRESSER_ANY_SCENE"] = "1"
    seed_chest(residents[0])
    host = rig.start_host("dresser", args.port, [])
    check("host reached field-ready state", host is not None)
    if host is None:
        return
    L.resolve_host_town(ip, args.port)
    os.environ["AC_TEST_DRESSER_ROUNDTRIP"] = "1"
    cl = rig.start_client("dresser", args.port, residents[0], [])
    os.environ.pop("AC_TEST_DRESSER_ROUNDTRIP", None)
    check("real client booted into the field and connected", cl is not None)
    if cl is None:
        return
    cl.wait_for_log(r"\[NET\]\[DRESSER\]\[TEST-ONLY\] client: roundtrip (TAKE|PUT REJECTED|SKIPPED|TAKE could not)", 150.0)
    text = cl.log_text()
    check("R1 the client adopted the host's pool (capability probe answered, enabled=1)", re.search(r"\[NET\]\[DRESSER\] client: the host offers dresser storage \(enabled=1\)", text) is not None)
    skipped = "roundtrip SKIPPED" in text
    check("the client had a storable pocket item to test with (not SKIPPED)", not skipped)
    check("R2 PUT APPLIED through the real sender", "roundtrip PUT APPLIED" in text)
    m = re.search(r"roundtrip TAKE (APPLIED|REJECTED) \(reason (\d+)\): item 0x([0-9A-F]{4}) pockets\+pool count (\d+) before (\d+) -> (\w+)", text)
    check("R2 TAKE APPLIED through the real sender (%s)" % (m.group(0) if m else None), m is not None and m.group(1) == "APPLIED")
    check("R3 pockets + pool count of the item CONSERVED through the round trip", m is not None and m.group(6) == "CONSERVED")
    ht = host.log_text()
    check("R2 the HOST committed exactly one PUT and one TAKE", len(re.findall(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ PUT ", ht)) == 1 and
          len(re.findall(r"\[NET\]\[DRESSER\] host: peer \d+ resident \d+ TAKE ", ht)) == 1)
    check("host and client alive", host.alive() and cl.alive())
    cl.stop()
    host.stop()
    rig.host = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=16330)
    ap.add_argument("--ui-sweep", choices=("b", "start"), default=None, help="every button on every grid cell, then close on row 2 col 7 (crash regression)")
    ap.add_argument("--ui-lock", action="store_true", help="the Option-A move / pickup refusal through the real pad input path")
    ap.add_argument("--dedicated", action="store_true", help="--ui-mixopen only: the host is a REAL dedicated server (--dedicated, servers/default tree)")
    ap.add_argument("--ui-grab", action="store_true", help="6r B: the Grab option (move / swap / put / cancel) through the real pad path")
    ap.add_argument("--ui-mixopen", action="store_true", help="6p: furniture + clothes + paper in ONE open screen, then take the furniture (deferred until the in-room commit)")
    ap.add_argument("--ui-ftrmix", action="store_true", help="6o: furniture -> extras, ordinary -> vanilla, close, reopen, take both (the user's session)")
    ap.add_argument("--ui-ftr", choices=("empty", "prefill"), default=None, help="6o: furniture put + take through the real 27-slot screen (pad)")
    ap.add_argument("--ui-keepguard", action="store_true", help="6l: the client commit guard reverts the move of a chest holding a CANONICAL keep item")
    ap.add_argument("--ui-store", action="store_true", help="6l: vanilla put, refused extra put (notice 5), commit, extra put, take both")
    ap.add_argument("--ui-direct", choices=("empty", "occupied", "timeout", "off", "moved", "moved-stuck", "reenter", "slow"), default=None,
                    help="direct open of the 27-slot screen by A (6f; reenter / slow / timeout: frame-rate independence on an uncapped client)")
    ap.add_argument("--ui-gyroid", action="store_true", help="the gyroid-style overlay in dresser mode, driven by scripted pad input")
    ap.add_argument("--ui-edit", action="store_true", help="three own-room visits (put / take / put+take) each followed by a furniture edit and an asserted APPLIED house commit")
    ap.add_argument("--ui", action="store_true", help="drive the dresser UI states in the client's own room instead of the sender-only round trip")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on a disposable bin_fixture4_* copy only", file=sys.stderr)
        return 2
    results = []
    save_dir = os.path.join(L.GAME_BIN_DIR, RC.SAVE_DIR_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "dresser_real_snap_%d" % os.getpid())
    side = os.path.join(save_dir, "mp", "dresser_store.dat")
    for p0 in (side, side + ".v1.bak"):  # never snapshot a sidecar (a stale one would come back with every restore)
        if os.path.isfile(p0):
            os.remove(p0)
    shutil.copytree(save_dir, snap_dir)
    rig = RC.Rig(args.port, results, save_dir, snap_dir)
    try:
        if os.path.exists(side):
            os.remove(side)
        (run_ui_grab if args.ui_grab else run_ui_mixopen if args.ui_mixopen else run_ui_ftrmix if args.ui_ftrmix else run_ui_ftr if args.ui_ftr else run_ui_keepguard if args.ui_keepguard else run_ui_store if args.ui_store else run_ui_sweep if args.ui_sweep else run_ui_direct if args.ui_direct else run_ui_lock if args.ui_lock else run_ui_gyroid if args.ui_gyroid else run_ui_edit if args.ui_edit else run_ui if args.ui else run)(args, results, "127.0.0.1", rig)
    finally:
        if rig.host is not None:
            rig.host.stop()
        rig.restore_save()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
