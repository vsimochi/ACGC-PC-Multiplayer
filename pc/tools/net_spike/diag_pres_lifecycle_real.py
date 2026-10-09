"""diag_pres_lifecycle_real.py - INVESTIGATION/verification (not a regression test): after real catches the presentation entries of the caught (despawned) fish must be freed on the
host and on the client, while entries of still-existing creatures stay. Real host + resident A on the Samsung display at volume 1."""
import os, re, sys, time
os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_wildlife_proxy_lifecycle_real as LC  # noqa: E402  (fresh fixture, Rig, env)
import test_wildlife_proxy_capacity_real as C  # noqa: E402
import test_wildlife_sim_real as T  # noqa: E402


def pres(R, who):
    P = R.players[who]
    off = len(R.log(who))
    P.cmd("pres", 20)
    time.sleep(1)
    return [(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))) for m in re.finditer(r"\[AUTO\] pres entity (\d+) kind (\d) species (\d+) has_actor=(\d) alive=(\d)", R.log(who)[off:])]


def main():
    class A:
        port = 12980
    R = C.Rig(A, [])
    try:
        assert R.start_host() and R.start_client("A", 1)
        caught = []
        for k in range(3):
            ok, peer = C.fish_once(R, "A", "PRES catch %d" % (k + 1))
            m = re.findall(r"peer \d+ CATCH entity (\d+) .*accepted", R.log("host"))
            caught = [int(x) for x in m]
        time.sleep(3)
        print("caught entities (host log):", caught)
        for who in ("host", "A"):
            ents = pres(R, who)
            ids = [e[0] for e in ents]
            print("%s presentation entries now: %s" % (who, ents))
            left = [c for c in caught if c in ids]
            print("%s: entries of CAUGHT entities still present: %s  -> %s" % (who, left, "OK (freed)" if not left else "LEAK"))
    finally:
        R.stop_all()


if __name__ == "__main__":
    main()
