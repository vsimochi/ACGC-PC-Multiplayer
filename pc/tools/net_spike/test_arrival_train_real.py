#!/usr/bin/env python3
"""test_arrival_train_real.py - SYNCED ARRIVAL TRAIN on REAL processes, with screenshots of every window at the same moments.

TIER: REAL PROCESSES + SCREENSHOTS (PrintWindow of each process's own window; Samsung display, volume 1). DISPOSABLE fixture pc\\build64\\bin_fixture4_arrtrain_real (recreated
from bin_fixture4 + the pc\\build64_w3 exe; its save is never written back anywhere else). Processes, all `-debug`:
  host      `--host P --bootstrap-resident 2`   (listen host, a resident NOT in the first-job events; parked on the platform as an observer)
  resident  `--connect --bootstrap-resident 3`  (observer, parked on the platform)
  guest A   `--connect --guest-profile trainA`  (arrives by train; waits in Porter's welcome until B holds its seat, so the overlap is deterministic)
  guest B   `--connect --guest-profile trainB`  (joins A's train: seat 1, the next one off)
  guest C   `--connect --guest-profile trainC`  (started when B steps off: it asks for a seat while the train is leaving -> a brand-new train, or the stopped one)
Porter's welcome is confirmed with the TEST-ONLY autopilot's A presses (`mash 1`), like a human pressing A.
Asserted from the logs (wall-clock first-seen times of each line, polled every 0.25 s):
  R1 every process started the SAME synced train 1 (`[TRAIN][SYNC] synced train 1 started here`)
  R2 A has seat 0, B seat 1 of train 1
  R3 the host saw: A stepped off, A welcomed, THEN B stepped off, B welcomed (B is the next one off, never on Porter's spot with A)
  R4 B's get-off began after A's welcome ended (B's 'train action 3' after A's 'train action 4')
  R5 the train departs on every process within 1.5 s of each other
  R6 Porter went to the train door and back on the observers (host, resident)
  R7 C got a seat and every process showed C's train
  R8 every guest got control back (`local arrival finished`); nobody started a separate T4 train for a synced passenger; all alive
Screenshots: pc/tools/net_spike/train_sync_shots/c_joins_at_<depart|b_off>/frame_NN_<stage>.png = one image per moment with ALL windows side by side (labels: process, seconds since A asked).
Usage: python test_arrival_train_real.py [--port 9890]"""
import argparse
import datetime
import os
import re
import shutil
import sys
import time

os.environ["AC_DISPLAY_NAME"] = "samsung"
os.environ["AC_MASTER_VOLUME"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

NAME = "bin_fixture4_arrtrain_real"
BIN = os.path.join(T.BUILD64, NAME)
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = BIN
SHOT_DIR = os.path.join(HERE, "train_sync_shots")
PROFILES = {"trainA": ("Ayla", 0x4E41, 24201), "trainB": ("Brin", 0x4E42, 24202), "trainC": ("Cato", 0x4E43, 24203)}
HOST_SLOT, RES_SLOT = 2, 3
OBS_POS = {"host": (2320.0, 900.0), "resident": (2110.0, 900.0)}
L = None
MWL = None

RX = {
    "asked": r"\[TRAIN\]\[SYNC\] client: arrival: asked the host for a seat",
    "seat": r"\[TRAIN\]\[SYNC\] arrival: SYNCED: seat (\d) on train (\d+)",
    "fallback": r"\[TRAIN\]\[SYNC\] arrival: no usable seat",
    "act3": r"\[JOIN\] \+\d+ms arrival: train action 3 ",
    "act4": r"\[JOIN\] \+\d+ms arrival: train action 4 ",
    "done": r"\[TRAIN\] local arrival finished: the player stands in the town",
    "started1": r"\[TRAIN\]\[SYNC\] synced train 1 started here",
    "started2": r"\[TRAIN\]\[SYNC\] synced train 2 started here",
    "departs1": r"\[TRAIN\]\[SYNC\] synced train 1 departs",
    "porter_door": r"\[TRAIN\]\[SYNC\] Porter: to the train door",
    "porter_back": r"\[TRAIN\]\[SYNC\] Porter: back at his place",
    "t4": r"\[TRAIN\] remote arrival of player \d+: starting the local arrival train",
}


def wait_for_quiet_hour():
    n = datetime.datetime.now()
    s = n.minute * 60 + n.second
    if 8 * 60 <= s <= 21 * 60 + 30:
        w = 21 * 60 + 30 - s
        L.info("local clock %s: the hourly train (hh:14:50) is near; waiting %d s" % (n.strftime("%H:%M:%S"), w))
        time.sleep(w)


class Proc:
    def __init__(self, tag, proc, ap_file):
        self.tag, self.proc, self.ap_file, self.t, self.next_id = tag, proc, ap_file, {}, 1
        self.last_mash = 0.0

    def poll(self, t0):
        txt = self.proc.log_text()
        for k, rx in RX.items():
            if k not in self.t:
                m = re.search(rx, txt)
                if m:
                    self.t[k] = (time.monotonic() - t0, m)
        return txt

    def ap(self, line):
        cid = self.next_id
        self.next_id += 1
        with open(self.ap_file, "w") as f:
            f.write("%d %s\n" % (cid, line))
        return cid


def stage(pr):
    """the latest arrival step of a guest, for the screenshot names"""
    if pr is None:
        return "-"
    ks = [k for k in ("asked", "seat", "act3", "act4", "done") if k in pr.t]
    return {"asked": "asked", "seat": "seated", "act3": "gettingoff", "act4": "walkoff", "done": "control"}[ks[-1]] if ks else "boot"


def compose(frames, path, title):
    from PIL import Image, ImageDraw
    ims = []
    for lab, p in frames:
        try:
            im = Image.open(p).convert("RGB")
        except OSError:
            continue
        im = im.resize((640, int(im.height * 640 / max(1, im.width))))
        ims.append((lab, im))
    if not ims:
        return False
    cols = 3 if len(ims) > 4 else 2
    rows = (len(ims) + cols - 1) // cols
    h = max(im.height for _l, im in ims)
    out = Image.new("RGB", (cols * 640, rows * (h + 28) + 30), (20, 20, 20))
    d = ImageDraw.Draw(out)
    d.text((8, 8), title, fill=(255, 255, 0))
    for i, (lab, im) in enumerate(ims):
        x, y = (i % cols) * 640, 30 + (i // cols) * (h + 28)
        d.text((x + 6, y + 6), lab, fill=(255, 255, 255))
        out.paste(im, (x, y + 24))
    out.save(path)
    for _lab, p in frames:
        try:
            os.remove(p)
        except OSError:
            pass
    return True


def main():
    global L, MWL
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9890)
    ap.add_argument("--shot-period", type=float, default=2.0)
    ap.add_argument("--max-wait", type=float, default=420.0)
    ap.add_argument("--c-when", choices=("b_off", "depart"), default="depart", help="start guest C when B steps off (it usually joins the stopped train) or when train 1 departs (a brand-new train)")
    args = ap.parse_args()
    for attempt in range(10):
        try:
            T.make_fixture(NAME)
            break
        except PermissionError:
            time.sleep(1.0)
    shutil.copy2(W3_EXE, os.path.join(BIN, "AnimalCrossing.exe"))
    import net_spike_lib as L  # noqa: E402
    import test_menu_world_live_real as MWL  # noqa: E402  (PrintWindow screenshot helper)
    os.environ["NET_SPIKE_GAME_BIN"] = BIN
    global SHOT_DIR
    SHOT_DIR = os.path.join(SHOT_DIR, "c_joins_at_" + args.c_when)
    os.makedirs(SHOT_DIR, exist_ok=True)
    for f in os.listdir(SHOT_DIR):
        if f.endswith(".png"):
            os.remove(os.path.join(SHOT_DIR, f))
    results = []
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    save_dir = os.path.join(BIN, "save")
    os.makedirs(os.path.join(save_dir, "mp"), exist_ok=True)
    for key, (nm, pid, lid) in PROFILES.items():
        with open(os.path.join(save_dir, "mp", "guest_%s.ini" % key), "wb") as f:
            f.write(("# test profile\nname = %s\ngender = 1\nface = 5\nhome_town = Hometwn\nplayer_id = 0x%04X\nland_id = %d\n" % (nm, pid, lid)).encode("ascii"))
    procs = {}
    addr = "127.0.0.1:%d" % args.port

    def start(tag, extra, host=False):
        apf = os.path.join(BIN, "ac_auto_%s.txt" % tag)
        if os.path.exists(apf):
            os.remove(apf)
        env = {"AC_TEST_AUTOPILOT": apf}
        lp = os.path.join(HERE, "arrtrain_real_%s.log" % tag)
        if host:
            p = L.HostProcess(port=args.port, extra_args=extra, log_path=lp, bin_dir=BIN, env=env).start()
        else:
            p = L.ClientProcess(addr, extra_args=extra, log_path=lp, bin_dir=BIN, env=env, label=tag).start()
        procs[tag] = Proc(tag, p, apf)
        return procs[tag]

    nshot = [0]
    t0 = [time.monotonic()]

    def shoot(tag_note):
        frames = []
        for tag, pr in procs.items():
            if pr.proc.alive():
                p = os.path.join(SHOT_DIR, "_tmp_%s.png" % tag)
                if MWL.shot(pr.proc, p):
                    frames.append(("%s" % tag, p))
        nshot[0] += 1
        name = "frame_%02d_%s.png" % (nshot[0], tag_note)
        ok = compose(frames, os.path.join(SHOT_DIR, name), "t=%.1fs  %s" % (time.monotonic() - t0[0], tag_note))
        print("[SHOT] %s %s (%d windows)" % (name, "saved" if ok else "FAILED", len(frames)))

    try:
        wait_for_quiet_hour()
        h = start("host", ["--bootstrap-resident", str(HOST_SLOT), "-debug"], host=True)
        check("host listening and in the field (resident %d)" % HOST_SLOT, h.proc.wait_listening(60.0) and h.proc.boot_to_field(timeout=120.0, slot=HOST_SLOT))
        r = start("resident", ["--bootstrap-resident", str(RES_SLOT), "-debug"])
        check("resident client in the field (resident %d)" % RES_SLOT, r.proc.boot_to_field(timeout=120.0, slot=RES_SLOT))
        time.sleep(3.0)
        for tag in ("host", "resident"):
            # a REAL walk (the autopilot steers the stick) so the acre change runs and the player's block table follows (a bare tp leaves it behind:
            # then the train actors are never spawned there, aTRC_area_check)
            x, z = OBS_POS[tag]
            pr = procs[tag]

            def pos(pr=pr):
                cid = pr.ap("pos")
                mp = pr.proc.wait_for_log(r"\[AUTO\] pos %d [^\r\n]*" % cid, 20.0)
                return mp.group(0) if mp else None
            L.info("%s at boot: %s" % (tag, pos()))
            cid = pr.ap("walk %.1f %.1f 20" % (x, z))
            mw = pr.proc.wait_for_log(r"\[AUTO\] done %d walk (\S+)" % cid, 60.0)
            if mw is None or mw.group(1) != "ok":
                cid = pr.ap("tp %.1f %.1f" % (x, z))
                pr.proc.wait_for_log(r"\[AUTO\] tp %d parked" % cid, 20.0)
            after = pos() or ""
            L.info("%s after the walk (%s): %s" % (tag, mw and mw.group(1), after))
            check("%s walked onto the platform at (%.0f, %.0f) as an observer, block table row 1 (%s)" % (tag, x, z, after), "block=(3,1)" in after)
        time.sleep(3.0)
        t0[0] = time.monotonic()
        shoot("observers_parked")
        a = start("A", ["--guest-profile", "trainA", "-debug"])
        b = None
        c = None
        last_shot = 0.0
        a_mash_ok = False
        end = time.monotonic() + args.max_wait
        b_started = False
        while time.monotonic() < end:
            for pr in list(procs.values()):
                pr.poll(t0[0])
            now = time.monotonic()
            if not b_started and "asked" in a.t:
                time.sleep(2.0)
                b = start("B", ["--guest-profile", "trainB", "-debug"])
                b_started = True
            if b is not None and "seat" in b.t and not a_mash_ok:
                a_mash_ok = True
                L.info("B holds seat %s of train %s: A may now confirm Porter's welcome" % (b.t["seat"][1].group(1), b.t["seat"][1].group(2)))
            for g in (a, b, c):
                if g is None or "act3" not in g.t or "act4" in g.t or (g is a and not a_mash_ok):
                    continue
                if now - g.last_mash > 1.4:
                    g.last_mash = now
                    g.ap("mash 1")
            if c is None and b is not None and ((args.c_when == "b_off" and "act3" in b.t) or (args.c_when == "depart" and "departs1" in h.t)):
                c = start("C", ["--guest-profile", "trainC", "-debug"])
            if now - last_shot >= args.shot_period:
                last_shot = now
                note = "A%s_B%s_C%s" % (stage(a), stage(b), stage(c))
                shoot(note)
            if c is not None and "done" in c.t and "done" in a.t and "done" in b.t and now - t0[0] - c.t["done"][0] > 8.0:
                break
            if any(not pr.proc.alive() for pr in procs.values()):
                L.info("a process exited: %s" % [t for t, pr in procs.items() if not pr.proc.alive()])
                break
            time.sleep(0.25)
        for pr in procs.values():
            pr.poll(t0[0])
        shoot("end")

        def tt(pr, k):
            return pr.t[k][0] if pr is not None and k in pr.t else None
        everyone = [procs[k] for k in ("host", "resident", "A", "B") if k in procs]
        check("R1 host, resident, A and B all started synced train 1 (%s)" % {p.tag: tt(p, "started1") for p in everyone}, all(tt(p, "started1") is not None for p in everyone))
        sa = a.t.get("seat")
        sb = b.t.get("seat") if b else None
        check("R2 A has seat 0 and B seat 1 of train 1 (A %s, B %s)" % (sa and sa[1].groups(), sb and sb[1].groups()),
              sa and sb and sa[1].groups() == ("0", "1") and sb[1].groups() == ("1", "1"))
        htxt = h.proc.log_text()
        pa = r"\[TRAIN\]\[SYNC\] host: peer %s %s"
        order = []
        for who, pr in (("A", a), ("B", b)):
            m = re.search(r"\[TRAIN\]\[SYNC\] host: peer (\d+) -> train 1 seat %d" % (0 if who == "A" else 1), htxt)
            if m:
                for what in ("stepped off the train", "was welcomed by Porter"):
                    mm = re.search(pa % (m.group(1), what), htxt)
                    order.append((mm.start() if mm else None, who, what))
        seq = [(w, x) for pos, w, x in sorted([o for o in order if o[0] is not None])]
        check("R3 host order: A off, A welcomed, B off, B welcomed (%s)" % seq,
              seq == [("A", "stepped off the train"), ("A", "was welcomed by Porter"), ("B", "stepped off the train"), ("B", "was welcomed by Porter")])
        check("R4 B's get-off began after A's welcome ended (A act4 %.1f s, B act3 %s s)" % (tt(a, "act4") or -1, tt(b, "act3")),
              tt(a, "act4") is not None and tt(b, "act3") is not None and tt(b, "act3") > tt(a, "act4"))
        dep = {p.tag: tt(p, "departs1") for p in everyone}
        dv = [v for v in dep.values() if v is not None]
        check("R5 train 1 departed on every process within 1.5 s (%s)" % dep, len(dv) == len(everyone) and max(dv) - min(dv) <= 1.5)
        for tag in ("host", "resident"):
            txt = procs[tag].proc.log_text()
            ms = re.search(r"\[TRAIN\]\[SYNC\] synced train 1 started here", txt)
            sp = re.search(r"\[TRAIN\] actor (spawned|not in area)[^\r\n]*", txt[ms.end():]) if ms else None
            check("R6 %s: the train actors of train 1 were spawned there (%s)" % (tag, sp and sp.group(0)), sp is not None and sp.group(1) == "spawned")
        for tag in ("host", "resident"):
            pr = procs[tag]
            check("R6 %s: Porter went to the train door and later back (%s / %s)" % (tag, tt(pr, "porter_door"), tt(pr, "porter_back")),
                  tt(pr, "porter_door") is not None and tt(pr, "porter_back") is not None and tt(pr, "porter_back") > tt(pr, "porter_door"))
        sc = c.t.get("seat") if c else None
        if sc:
            trn = sc[1].group(2)
            key = "started2" if trn == "2" else "started1"
            all_c = [p for p in everyone + [c]]
            check("R7 C got seat %s on train %s (%s) and every process showed that train (%s)" % (sc[1].group(1), trn, "a brand-new train" if trn == "2" else "the stopped train",
                                                                                            {p.tag: tt(p, key) for p in all_c}),
                  all(tt(p, key) is not None for p in all_c))
            if trn == "2":
                st2 = [tt(p, "started2") for p in everyone + [c]]
                check("R7 train 2 started on every process within 1.5 s (%s)" % st2, all(v is not None for v in st2) and max(st2) - min(st2) <= 1.5)
        else:
            check("R7 C got a seat (fallback: %s)" % (c is not None and "fallback" in c.t), False)
        for g in (a, b, c):
            check("R8 guest %s got control back (local arrival finished at %s s)" % (g.tag if g else "?", tt(g, "done")), g is not None and tt(g, "done") is not None)
        check("R8 no separate T4 train for a synced passenger on any process (%s)" % {p.tag: tt(p, "t4") for p in procs.values()}, all(tt(p, "t4") is None for p in procs.values()))
        check("R8 every process alive", all(pr.proc.alive() for pr in procs.values()))
    finally:
        for pr in reversed(list(procs.values())):
            try:
                pr.proc.stop()
            except Exception:  # noqa: BLE001
                pass
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
