"""test_kk_work_guests_work.py - the Nook Work Mode half of test_kk_work_guests_real.py (REAL host + several REAL guest / resident clients). See that file's header.

Scenarios (all on the host's real work_jobs.dat, which is parsed here as a v3 / v2 file):
  W1  four characters (three guests with DIFFERENT PersonalIDs + a resident client) each ENTER Work Mode and STAY: four records, four distinct job ids, each keyed by its own character
  W2  a guest reconnects (new process, other wire id): its job is still there (Nook's first page offers 'Check my job'); a brand-new guest has none
  W3  guest A quits its job: B's job is untouched (still active); A's record is the only one that changed
  W4  three clients ENTER at the same time: nothing is overwritten, every record keeps its identity
  W5  host restart: the file is reloaded, every character finds its own record
  W6  an OLD version-2 file (64 records) is read by the host and rewritten as version 3
  W7  more than 64 characters: a version-3 file with 200 records loads, and a NEW character gets record 201
"""
import os
import re
import struct
import time

import net_spike_lib as L

REC = 56
KEY = 20
MAGIC = 0x4B574341
JOB_NONE, JOB_ACTIVE = 0, 1  # PC_WORK_STATE_* are checked through the first-page choice 618 / 614, the raw state byte is only compared for change


def fnv(b):
    h = 2166136261
    for x in b:
        h = ((h ^ x) * 16777619) & 0xFFFFFFFF
    return h


def parse(path):
    """work_jobs.dat -> (version, next_job_id, [record bytes]) or None"""
    try:
        d = open(path, "rb").read()
    except OSError:
        return None
    if len(d) < 20:
        return None
    magic, ver, aux, cnt = struct.unpack("<IIII", d[:16])
    if magic != MAGIC:
        return None
    n = cnt if ver == 3 else cnt // REC
    recs = [d[16 + i * REC:16 + (i + 1) * REC] for i in range(n)]
    return ver, aux, recs


def write_v3(path, aux, recs):
    hdr = struct.pack("<IIII", MAGIC, 3, aux, len(recs))
    crc = fnv(hdr)
    for i, r in enumerate(recs):
        crc ^= (fnv(r) + i * 0x9E3779B1) & 0xFFFFFFFF
    open(path, "wb").write(hdr + b"".join(recs) + struct.pack("<I", crc & 0xFFFFFFFF))


def write_v2(path, aux, recs):
    body = b"".join(recs) + b"\0" * REC * (64 - len(recs))
    hdr = struct.pack("<IIII", MAGIC, 2, aux, 64 * REC)
    crc = fnv(hdr) ^ fnv(body)
    open(path, "wb").write(hdr + body + struct.pack("<I", crc & 0xFFFFFFFF))


def fake_rec(i, job_id):
    key = b"FAKE%04d" % i + b"\0" * 12
    r = bytearray(REC)
    r[:KEY] = key
    struct.pack_into("<I", r, 20, job_id)       # job_id
    struct.pack_into("<I", r, 24, 300)          # reward
    r[48], r[49], r[50], r[51] = 1, 1, 1, 1     # used, mode_on, state ACTIVE, FRUIT
    struct.pack_into("<H", r, 36, 0x1100)       # obj_item
    struct.pack_into("<H", r, 38, 1)            # obj_count
    return bytes(r)


def by_key(recs):
    return {r[:KEY]: r for r in recs}


def job_id(r):
    return struct.unpack_from("<I", r, 20)[0]


def state(r):
    return r[50]


def mode_on(r):
    return r[49]


def first_page(proc, tag, timeout=150.0):
    """True/False: does the loaded Nook first page of `tag` carry choice 618 ('Check my job'); None when it never loaded"""
    m = proc.wait_for_log(r"\[MSG\]\[TEST-ONLY\] %s message 0x1092 loaded, len (\d+), bytes:((?: [0-9A-F]{2})+)" % tag, timeout)
    if m is None:
        return None
    b = bytes(int(x, 16) for x in m.group(2).split())
    return (b"\x02\x6A" in b) and (b"\x02\x66" not in b)  # 618 = 0x026A ('Check my job'), 614 = 0x0266 ('I'd like to work')


def phase_work(R, save_dir):
    check = R.check
    wj = os.path.join(save_dir, "mp", "work_jobs.dat")
    if os.path.exists(wj):
        os.remove(wj)
    residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != L.TEST_HOST_RESIDENT]
    host = R.host()
    if host is None:
        return
    base = {"AC_TEST_DUMP_MSG": "0x1092"}
    keep = dict(base, AC_TEST_WORK_ENTER="1", AC_TEST_WORK_KEEP="1")

    # ---------------- W1: four characters ENTER and stay
    known = {}
    seen = set()
    procs = {}
    for who, mk in (("alpha", lambda: R.guest("alpha", keep, "w1")), ("beta", lambda: R.guest("beta", keep, "w1")), ("gamma", lambda: R.guest("gamma", keep, "w1")),
                    ("resident", lambda: R.resident(residents[0], keep, "w1"))):
        p = mk()
        procs[who] = p
        a_ok = first_page(p, "after-ENTER")
        check("W1 %s: after its real ENTER Nook's first page offers 'Check my job'" % who, a_ok is True)
        f = parse(wj)
        new = [k for k in by_key(f[2]) if k not in seen] if f else []
        check("W1 %s: the host's work_jobs.dat gained exactly one NEW record (its own)" % who, len(new) == 1)
        if new:
            known[who] = new[0]
            seen.add(new[0])
    f = parse(wj)
    recs = by_key(f[2]) if f else {}
    ids = [job_id(recs[k]) for k in known.values() if k in recs]
    check("W1 four characters -> four records, four DISTINCT job ids %s, all in Work Mode with an active job" % ids,
          len(known) == 4 and len(set(ids)) == 4 and all(state(recs[k]) == 1 and mode_on(recs[k]) == 1 for k in known.values()))
    check("W1 the file is version 3", f is not None and f[0] == 3)
    snap_w1 = dict(recs)

    # ---------------- W2: reconnect keeps the job; a new guest has none
    off = len(host.log_text())
    R.stop(procs["alpha"])
    host.wait_for_log(r"\[NET\] host: peer \d+ disconnected", 30.0, since_offset=off)
    a2 = R.guest("alpha", base, "w2")
    check("W2 guest A reconnects (new process / wire id): Nook's first page still offers 'Check my job' (its job survived)", first_page(a2, "initial") is True)
    time.sleep(65.0)  # the per-address new-guest-token window (3 / 60 s, class D abuse limit) - delta is the 4th new guest
    d = R.guest("delta", base, "w2")
    check("W2 a brand-new guest D (other PersonalID) sees NO job: 'I'd like to work'", first_page(d, "initial") is False)
    f = parse(wj)
    check("W2 neither the reconnect nor the lookup changed or created any record", f is not None and by_key(f[2]) == snap_w1)

    # ---------------- W3: A quits; B is untouched
    R.stop(a2)
    a3 = R.guest("alpha", dict(base, AC_TEST_WORK_ENTER="1"), "w3")  # initial -> ENTER (idempotent) -> LEAVE -> final
    check("W3 A: initial page offers 'Check my job'", first_page(a3, "initial") is True)
    check("W3 A: after ENTER (idempotent: same job) still 'Check my job'", first_page(a3, "after-ENTER") is True)
    check("W3 A: after LEAVE the page is back to 'I'd like to work'", first_page(a3, "final") is False)
    f = parse(wj)
    recs = by_key(f[2]) if f else {}
    ka, kb, kg = known["alpha"], known["beta"], known["gamma"]
    check("W3 A's record: SAME job id as before ENTER (ENTER is idempotent), no longer in Work Mode / no active job",
          ka in recs and job_id(recs[ka]) == job_id(snap_w1[ka]) and mode_on(recs[ka]) == 0 and state(recs[ka]) == 0)
    check("W3 B's and G's records are byte-identical to before (A's quit cannot touch them)", kb in recs and kg in recs and recs[kb] == snap_w1[kb] and recs[kg] == snap_w1[kg])
    R.stop(a3)
    R.stop(d)
    for w in ("beta", "gamma", "resident"):
        R.stop(procs[w])

    # ---------------- W4: three clients ENTER at once
    snap_w4 = dict(recs)
    off = len(host.log_text())
    cs = {"alpha": R.guest("alpha", keep, "w4"), "beta": R.guest("beta", keep, "w4"), "delta": R.guest("delta", keep, "w4")}
    ok = {w: first_page(p, "after-ENTER") for w, p in cs.items()}
    check("W4 three guests ENTER at the same time: every one gets 'Check my job' %s" % ok, all(v is True for v in ok.values()))
    f = parse(wj)
    recs = by_key(f[2]) if f else {}
    check("W4 A got a NEW job (its old one was abandoned: a different job id), B kept the one it had (ENTER is idempotent)",
          ka in recs and kb in recs and job_id(recs[ka]) != job_id(snap_w4[ka]) and recs[kb] == snap_w4[kb])
    new = [k for k in recs if k not in snap_w4]
    check("W4 D (never worked) got its own new record; G's and the resident's records are untouched", len(new) == 1 and recs[kg] == snap_w4[kg] and recs[known["resident"]] == snap_w4[known["resident"]])
    check("W4 every record still has a distinct job id (no cross-overwrite): %s" % sorted(job_id(r) for r in recs.values()),
          len({job_id(r) for r in recs.values() if state(r) == 1}) == len([r for r in recs.values() if state(r) == 1]))
    snap_w5 = dict(recs)
    for p in cs.values():
        R.stop(p)

    # ---------------- W5: host restart reloads the file
    R.stop(host)
    time.sleep(2.0)
    host = R.host()
    if host is None:
        return
    b2 = R.guest("beta", base, "w5")
    check("W5 B (after the host restart, new wire id): still 'Check my job'", first_page(b2, "initial") is True)
    check("W5 the restarted host loaded (lazily, at the first work lookup) work_jobs.dat (version 3, %d characters)" % len(snap_w5),
          re.search(r"\[NET\]\[WORK\] host: loaded .*work_jobs.dat \(next job id \d+, %d character\(s\), file version 3\)" % len(snap_w5), host.log_text()) is not None)
    a4 = R.guest("alpha", base, "w5")
    check("W5 A: its record survived the restart as it was: 'Check my job'", first_page(a4, "initial") is True)
    f = parse(wj)
    check("W5 nothing was lost or rewritten by the restart", f is not None and by_key(f[2]) == snap_w5)
    R.stop(b2)
    R.stop(a4)
    R.stop(host)

    # ---------------- W6: an old VERSION 2 file (64 slots) loads and is rewritten as version 3
    time.sleep(2.0)
    f = parse(wj)
    real = list(by_key(f[2]).values())
    fakes = [fake_rec(i, 5000 + i) for i in range(64 - len(real))]
    write_v2(wj, f[1], real + fakes)
    pv = parse(wj)
    check("W6 the fixture file is a genuine version 2 file with 64 records (%d real + %d fake)" % (len(real), len(fakes)), pv is not None and pv[0] == 2 and len(pv[2]) == 64)
    host = R.host()
    if host is None:
        return
    g2 = R.guest("gamma", base, "w6")
    check("W6 G's job, loaded from the version-2 file, is found by its PersonalID: 'Check my job'", first_page(g2, "initial") is True)
    check("W6 the host reads the version-2 file: %d characters" % (len(real) + len(fakes)),
          re.search(r"\[NET\]\[WORK\] host: loaded .*work_jobs.dat \(next job id \d+, %d character\(s\), file version 2\)" % (len(real) + len(fakes)), host.log_text()) is not None)
    e = R.guest("eps", keep, "w6")  # a NEW character on a FULL old-style table: the 65th record
    check("W6 a NEW character E (the 65th: the old 64-row table was full) can ENTER Work Mode", first_page(e, "after-ENTER") is True)
    f = parse(wj)
    check("W6 the host rewrote the file as version 3 with %d records (the 65th included)" % (len(real) + len(fakes) + 1), f is not None and f[0] == 3 and len(f[2]) == len(real) + len(fakes) + 1)
    R.stop(g2)
    R.stop(e)
    R.stop(host)

    # ---------------- W7: hundreds of characters
    time.sleep(2.0)
    f = parse(wj)
    real = list(by_key(f[2]).values())
    fakes = [fake_rec(1000 + i, 9000 + i) for i in range(200)]
    write_v3(wj, f[1] + 1000, real + fakes)
    total = len(real) + len(fakes)
    host = R.host()
    if host is None:
        return
    time.sleep(65.0)
    dd = R.guest("delta", base, "w7")
    check("W7 D (one of %d characters) still finds its own record" % total, first_page(dd, "initial") is True)
    check("W7 the host loads a version-3 file of %d characters" % total,
          re.search(r"\[NET\]\[WORK\] host: loaded .*work_jobs.dat \(next job id \d+, %d character\(s\), file version 3\)" % total, host.log_text()) is not None)
    r2 = R.resident(residents[0], base, "w7")
    check("W7 the resident client's record is still found too", first_page(r2, "initial") is True)
    f = parse(wj)
    check("W7 no record was lost: %d records" % total, f is not None and len(f[2]) == total)
    R.stop(dd)
    R.stop(r2)
    check("host alive at the end", host.alive())
    R.stop_all()
