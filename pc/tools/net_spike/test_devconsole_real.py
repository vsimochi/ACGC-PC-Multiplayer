#!/usr/bin/env python3
"""test_devconsole_real.py - the developer commands of the dedicated-server console (where / net / time / event / wildlife / test / tp / bell), REAL dedicated host process (stdin console)
from the DISPOSABLE fixture bin_fixture4 (its exe is refreshed from build64\\bin first) + one FakeClient. TIER: real host process, console text. No gameplay, no keystrokes.
  A  hooks DISABLED (AC_TEST_HOOKS=0): read-only commands work (time get, event list / status / status <name> / unknown name, net status / peers / resync, wildlife list / info / sync, where, test status / help),
     tp / bell / event start / wildlife spawn / despawn are REFUSED with a reason and change nothing; a FakeClient's position shows in `where` / `where <name>` / `where peer N`
  B  hooks ENABLED: wildlife spawn argument validation (bad species, NaN / out-of-town coordinates, bugs unsupported), a real spawn through the host's injection path appears in `wildlife list` / `info`, despawn removes it
Usage: python test_devconsole_real.py [--port 12990]"""
import argparse
import os
import re
import shutil
import sys
import time

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "0")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402
import test_dedicated_runtime as DR  # noqa: E402


def ask(h, cmd, end_rx, timeout=10.0):
    """sends one console line, returns the log text after it up to a line matching end_rx (None on timeout)"""
    off = len(h.log_text())
    h.send_line(cmd)
    mo = h.wait_for_log(end_rx, timeout, since_offset=off)
    time.sleep(0.2)
    return h.log_text()[off:] if mo is not None else None


def adopt_legacy_town(h):
    """a first launch of a dedicated server on a fixture without servers/default asks 'use this town (1) or generate (2)': answer 1 (the fixture's own town)"""
    if h.wait_for_log(r"Use this town", 12.0) is not None:
        h.send_line("1")


def run(args, results):
    check = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    fresh = os.path.join(BUILD64, "bin", "AnimalCrossing.exe")
    shutil.copy2(fresh, os.path.join(L.GAME_BIN_DIR, "AnimalCrossing.exe"))  # the disposable fixture gets the freshly built exe
    # ------------------------------------------------------------------ A: hooks disabled
    h = DR.start(args.port, "devA", args.log_dir, verbose=False, new_group=True, env={"AC_TEST_HOOKS": "0"})
    cl = None
    try:
        adopt_legacy_town(h)
        check("A host started", h.wait_listening(60.0))
        check("A boot to a ready world", h.boot_to_dedicated(timeout=120.0))
        t = ask(h, "help", r"  stop     graceful shutdown")
        check("A help documents where / net / time / event / wildlife / test / tp", t is not None and all(re.search(r"^  %s " % c, t, re.M) for c in ("where", "net", "time", "event", "wildlife", "test", "tp / bell")))
        t = ask(h, "time get", r"\[DEDICATED\] time: .*|TIME FAILED.*")
        check("A time get prints a real date and weekday (%s)" % (t or "").strip(), t is not None and re.search(r"time: \d{4}-\d\d-\d\d \d\d:\d\d:\d\d (Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)", t))
        check("A `time set` is refused with a usage line", "usage: time get" in (ask(h, "time set 5", r"usage: time get") or ""))
        t = ask(h, "event list", r"\n  \s*\d+ sale_day|\n  \s*\d+ koinobori|\n  \s*\d+ 76 ")
        names = re.findall(r"^  \s*(\d+) (\S+)\s+scheduled-today=(yes|no) active-now=(yes|no) status:", t or "", re.M)
        check("A event list prints every event type with the game's own schedule/status (%d rows)" % len(names), len(names) >= 100 and any(n[1] == "new_years_day" for n in names))
        t = ask(h, "event status", r"\[DEDICATED\] events .*[\s\S]*?(?:\n  \s*\d+ \S+ |no event is scheduled)")
        check("A event status lists only scheduled/running events (or says none)", t is not None)
        t = ask(h, "event status new_years_day", r"scheduled-today=\w+ active-now=\w+ status:|EVENT FAILED[^\n]*")
        check("A event status <name> reports that event", t is not None and "new_years_day" in t)
        check("A event status with an unknown name fails clearly", "unknown event 'nonsense'" in (ask(h, "event status nonsense", r"EVENT FAILED[^\n]*") or ""))
        t = ask(h, "event start halloween", r"EVENT start REFUSED[^\n]*\n[^\n]*\n[^\n]*")
        check("A event start is REFUSED with the reason, nothing changed", t is not None and "mEv_init_force" in t and "Nothing was changed" in t)
        check("A net status prints role/port/peers", re.search(r"net status: role=host port=%d peers=0 ready=0" % args.port, ask(h, "net status", r"net status: [^\n]*") or ""))
        check("A net resync is UNSUPPORTED, nothing sent", "NET RESYNC UNSUPPORTED" in (ask(h, "net resync", r"NET RESYNC UNSUPPORTED[^\n]*") or ""))
        check("A net with a bad subcommand prints usage", "usage: net status" in (ask(h, "net foo", r"usage: net status[^\n]*") or ""))
        check("A wildlife list works read-only", re.search(r"wildlife: \d+ authoritative record", ask(h, "wildlife list", r"wildlife: \d+ authoritative record[^\n]*") or ""))
        check("A wildlife info with an unknown id fails clearly", "no authoritative record with id 99999" in (ask(h, "wildlife info 99999", r"WILDLIFE FAILED[^\n]*") or ""))
        check("A wildlife info without an id prints usage", "usage: wildlife info" in (ask(h, "wildlife info", r"usage: wildlife info") or ""))
        check("A wildlife sync is UNSUPPORTED", "WILDLIFE SYNC UNSUPPORTED" in (ask(h, "wildlife sync", r"WILDLIFE SYNC UNSUPPORTED[^\n]*") or ""))
        check("A wildlife spawn is REFUSED with hooks disabled", "WILDLIFE SPAWN REFUSED" in (ask(h, "wildlife spawn fish seabass 2794 1404", r"WILDLIFE SPAWN REFUSED[^\n]*") or ""))
        check("A wildlife despawn is REFUSED with hooks disabled", "WILDLIFE DESPAWN REFUSED" in (ask(h, "wildlife despawn 1", r"WILDLIFE DESPAWN REFUSED[^\n]*") or ""))
        check("A tp is UNSUPPORTED with the reason (movement owned by the remote client)", "tp UNSUPPORTED" in (ask(h, "tp bob 1 2 3", r"tp UNSUPPORTED[^\n]*") or ""))
        check("A bell is UNSUPPORTED with the reason", "bell UNSUPPORTED" in (ask(h, "bell add 100", r"bell UNSUPPORTED[^\n]*") or ""))
        t = ask(h, "test status", r"test status: [^\n]*")
        check("A test status reports hooks DISABLED", t is not None and "hooks DISABLED" in t)
        check("A test help explains the gate", "AC_TEST_HOOKS=1" in (ask(h, "test help", r"Read-only commands[^\n]*") or ""))
        check("A where with nobody connected says so", "no players connected" in (ask(h, "where", r"no players connected[^\n]*") or ""))
        check("A where <unknown> fails clearly", "no connected player 'zed'" in (ask(h, "where zed", r"WHERE FAILED[^\n]*") or ""))
        cl = DR.Rig(results).join(args.port, "devclient", 0)
        for _ in range(8):
            cl.claim_position(1234.0, 160.0, 2345.0)
            L.pump_sleep(0.3)
        t = ask(h, "where", r"peer \d+ \"[^\"]*\": (?:x=|no position)[^\n]*")
        mo = re.search(r"peer (\d+) \"([^\"]*)\": x=([-\d.]+) y=([-\d.]+) z=([-\d.]+) acre=\((\d+),(\d+)\)", t or "")
        check("A where lists the connected player's last reported position (%s)" % (mo.group(0) if mo else t), mo is not None and abs(float(mo.group(3)) - 1234.0) < 2 and abs(float(mo.group(5)) - 2345.0) < 2
              and (mo.group(6), mo.group(7)) == ("1", "3"))
        if mo:
            t = ask(h, "where %s" % mo.group(2), r"peer \d+ \"[^\"]*\": [^\n]*")
            check("A where <name> finds the same player", t is not None and ("x=1234" in t or "x=1233" in t))
            t = ask(h, "where peer %s" % mo.group(1), r"peer \d+ \"[^\"]*\": [^\n]*")
            check("A where peer N finds the same player", t is not None and "z=234" in t)
        check("A net peers lists the peer", re.search(r"peer \d+: link=READY", ask(h, "net peers", r"peer \d+: link=\w+[^\n]*") or ""))
    finally:
        if cl is not None:
            DR.Rig(results).release(cl)
        h.stop()
    # ------------------------------------------------------------------ B: hooks enabled
    h = DR.start(args.port + 1, "devB", args.log_dir, verbose=False, new_group=True, env={"AC_TEST_HOOKS": "1"})
    try:
        adopt_legacy_town(h)
        check("B host started", h.wait_listening(60.0))
        check("B boot to a ready world", h.boot_to_dedicated(timeout=120.0))
        check("B test status reports hooks ENABLED", "hooks ENABLED" in (ask(h, "test status", r"test status: [^\n]*") or ""))
        check("B spawn: unknown species fails", "unknown fish species 'dragon'" in (ask(h, "wildlife spawn fish dragon 2794 1404", r"WILDLIFE SPAWN FAILED[^\n]*") or ""))
        t = ask(h, "wildlife spawn fish seabass nan 1404", r"WILDLIFE SPAWN FAILED[^\n]*|usage: wildlife spawn[^\n]*|WILDLIFE SPAWN: [^\n]*")
        check("B spawn: NaN coordinates are rejected, no record created (%s)" % (t or "").strip()[-70:], t is not None and "WILDLIFE SPAWN: " not in t)
        check("B spawn: out-of-town coordinates fail", "outside the town" in (ask(h, "wildlife spawn fish seabass 99999 1404", r"WILDLIFE SPAWN FAILED[^\n]*") or ""))
        check("B spawn: bugs are UNSUPPORTED (usage says so)", "bugs: UNSUPPORTED" in (ask(h, "wildlife spawn bug ant 2794 1404", r"usage: wildlife spawn[^\n]*") or ""))
        t = ask(h, "wildlife spawn fish seabass 2794 1404", r"WILDLIFE SPAWN: [^\n]*|WILDLIFE SPAWN FAILED[^\n]*")
        mo = re.search(r"authoritative record (\d+)", t or "")
        check("B spawn through the host injection path creates an authoritative record (%s)" % (t or "").strip(), mo is not None)
        if mo:
            eid = mo.group(1)
            t = ask(h, "wildlife list", r"id %s fish[^\n]*" % eid)
            check("B the new record is in `wildlife list`", t is not None)
            t = ask(h, "wildlife info %s" % eid, r"id %s fish[^\n]*" % eid)
            check("B `wildlife info <id>` shows it", t is not None and "species" in t)
            t = ask(h, "wildlife despawn %s" % eid, r"WILDLIFE DESPAWN[^\n]*")
            check("B despawn releases it", t is not None and "released" in t)
            check("B it is gone from the list afterwards", "id %s fish" % eid not in (ask(h, "wildlife list", r"wildlife: \d+ authoritative record[^\n]*") or "id %s fish" % eid))
            check("B despawn of the same id again fails", "no record with id %s" % eid in (ask(h, "wildlife despawn %s" % eid, r"WILDLIFE DESPAWN[^\n]*") or ""))
    finally:
        h.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12990)
    ap.add_argument("--log-dir", default=HERE)
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    with L.CloneSaveGuard():
        try:
            run(args, results)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("test aborted by an exception: %s" % e, False, results)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
