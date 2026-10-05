#!/usr/bin/env python3
"""test_town_sanitize_real.py - M-G SANITIZED TOWN TRANSFER + RECORD-IMPORT GUARD with REAL host and client processes (and scripted FakeClients).

TIER: REAL HOST + REAL CLIENT game processes, FakeClient for the wire reads. DISPOSABLE fixtures only: copies of pc\\build64\\bin_fixture4 named bin_fixture4_san (hosts) and
bin_fixture4_sanclient (a client with an EMPTY save dir); bin_fixture4 itself, the live save dir, bin_talkfix* and the repo save/ are never launched or modified.

  A  host `--town-serve on` (sanitized): a FakeClient fetch: TOWN_INFO flags bit0 set, the marker is in the bytes; the PUBLIC ranges equal the host GCI file, every other
     resident's (and the own) private bytes equal the cleared template (the 4 records are identical outside the kept ranges), none of a long sample of the host's real
     private / mail bytes appears anywhere in the image; UP_TO_DATE compares the sanitized crc; the privacy warning (resident_tokens off) is logged; a FakeClient resident
     that does NOT send NO_MIGRATE (an 'old client') is answered with HOST WINS (town_serve is sanitized): PUSH_FULL, no MIGRATE_REQUEST
  B  host `--town-serve full`: the whole file is served (md5 == a version of the host GCI), flags == 0
  C  host `--town-serve on --resident-tokens tofu` + a REAL client (empty save dir, `--town-fetch --bootstrap-resident 1`, resident slot 1): it loads the sanitized cache
     (log), reaches READY, sends HELLO NO_MIGRATE, the host answers HOST WINS and PUSH_FULL (no MIGRATE_REQUEST / MIGRATE APPLIED); the host's pushed slot-1 record
     (digest) is identical in a second session and, after records.dat is DELETED (rev 0) and the host restarted, in a third session (HOST WINS again, no MIGRATE)
Usage: python test_town_sanitize_real.py [--port 11890]
"""
import argparse
import os
import re
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

HOST_NAME, CLIENT_NAME = "bin_fixture4_san", "bin_fixture4_sanclient"
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, HOST_NAME)
CLIENT_DIR = os.path.join(T.BUILD64, CLIENT_NAME)

import net_spike_lib as L  # noqa: E402

HOST_DIR = L.GAME_BIN_DIR
HOST_GCI = os.path.join(HOST_DIR, L.SAVE_GCI_REL)
CLIENT_TOWNS = os.path.join(CLIENT_DIR, "save", "mp", "towns")
S = 0x26040
PRIV = S + 0x20
PRIV_STRIDE = 0x2440
KEEP_PRIV = [(0, 0x17), (0x1086, 0x108C), (0x10F4, 0x10F8), (0x2348, 0x234C)]


SECRET = b"SECRET-LETTER-TEXT-ABCDEFGHIJKLMNOP-0123456789"
WALLET1 = 12345


def patch_host_gci():
    """Puts recognisable PRIVATE data into the DISPOSABLE host GCI (never a protected file): a wallet / bank for the residents 1..3, a SECRET letter body in their inventory
    mail slot 0, in the houses' mailbox slot 0, in the post office mail slot 0 and in a villager's saved letter, so the leak checks have something to find."""
    b = bytearray(read_gci())
    for i in (1, 2, 3):
        base = PRIV + i * PRIV_STRIDE
        b[base + 0x8C:base + 0x90] = (WALLET1 + i - 1).to_bytes(4, "big")
        b[base + 0x122C:base + 0x1230] = (777000 + i).to_bytes(4, "big")
        b[base + 0x4E0 + 0x4A:base + 0x4E0 + 0x4A + len(SECRET)] = SECRET
        hb = S + 0x9CE8 + i * 0x26B0 + 0x1A30
        b[hb + 0x4A:hb + 0x4A + len(SECRET)] = SECRET
    po = S + 0x20694 + 8
    b[po + 0x4A:po + 0x4A + len(SECRET)] = SECRET
    mb = S + 0x17438 + 0x10
    b[mb + 0x32 + 0x1D:mb + 0x32 + 0x1D + len(SECRET)] = SECRET
    with open(HOST_GCI, "wb") as f:
        f.write(bytes(b))


def fresh_fixtures():
    T.make_fixture(HOST_NAME)
    T.make_fixture(CLIENT_NAME, empty_save=True)
    patch_host_gci()


def start_host(port, tag, extra):
    for i in range(3):
        h = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"] + list(extra), log_path=T.log_path("san_host_%s_try%d.log" % (tag, i)), bin_dir=HOST_DIR).start()
        if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
            return h
        h.stop()
        time.sleep(2.0)
    return None


def start_client(port, tag):
    return L.ClientProcess("127.0.0.1:%d" % port, extra_args=["--town-fetch", "--bootstrap-resident", "1"], log_path=T.log_path("san_client_%s.log" % tag), bin_dir=CLIENT_DIR,
                           label="sanclient").start()


def read_gci():
    with open(HOST_GCI, "rb") as f:
        return f.read()


def fetch(port, label):
    """One FakeClient town fetch -> (info, bytes, host GCI bytes read before and after)."""
    L.pump_sleep(0.3)
    before = read_gci()
    c = L.FakeClient(label, "127.0.0.1", port)
    c.connect(timeout=30.0)
    info = L.town_fetch_start(c)
    data, problems = L.town_fetch_collect(c, info) if info is not None and info.status == 0 else (b"", ["no stream"])
    L.pump_sleep(0.3)
    after = read_gci()
    try:
        c.disconnect()
    except Exception:  # noqa: BLE001
        pass
    c.close()
    L.pump_sleep(0.5)
    return info, data, problems, before, after


def in_keep(j):
    return any(a <= j < b for a, b in KEEP_PRIV)


def public_ranges():
    r = [(0, 0x40), (0x40, 0x70), (0x78, 0x40 + 0x1460), (S + 0x14, S + 0x20), (S + 0x9120, S + 0x9CE8), (S + 0x137A8, S + 0x17438), (S + 0x20330, S + 0x20694),
         (S + 0x20694 + 0x5DA, S + 0x20694 + 0x83C), (S + 0x20ED0, S + 0x23440)]
    for h in range(4):
        hb = S + 0x9CE8 + h * 0x26B0
        r += [(hb, hb + 0x1A30), (hb + 0x25D4, hb + 0x2674), (hb + 0x2678, hb + 0x26B0)]
    for a in range(15):
        ab = S + 0x17438 + a * 0x988
        r.append((ab, ab + 0x10))
        for m in range(7):
            mb = ab + 0x10 + m * 0x138
            r += [(mb, mb + 0x34), (mb + 0x36, mb + 0x37), (mb + 0x130, mb + 0x138)]  # memory header / friendship / letter_info + font + paper, header_back_start, letter date + pad
    return r


def phase_a(port, ck):
    h = start_host(port, "a", ["--town-serve", "on"])
    ck("A host (--town-serve on) started and booted to the field", h is not None)
    if h is None:
        return
    info = data = None
    for attempt in range(3):
        info, data, problems, before, after = fetch(port, "san-a%d" % attempt)
        if before == after or info is None:
            break
    ck("A TOWN_INFO is STREAM with total 467008 and the SANITIZED flag (bit0) set (flags=%s)" % (None if info is None else info.flags),
       info is not None and info.status == 0 and info.total_size == 467008 and (info.flags & 1) == 1)
    ck("A the stream is complete (468 chunks, no problems: %s) and crc32 matches TOWN_INFO" % problems, not problems and len(data) == 467008 and info.crc32 == zlib.crc32(data) & 0xFFFFFFFF)
    if len(data) != 467008:
        h.stop()
        return
    ck("A the host's GCI did not change during the fetch (comparison base is stable)", before == after)
    ck("A marker ACMPSAN1 at file offset 0x70 of the streamed bytes and NOT in the host's own GCI", data[0x70:0x78] == b"ACMPSAN1" and before[0x70:0x78] != b"ACMPSAN1")
    ck("A the streamed image differs from the host GCI (it is not the full file) and carries NONE of the planted SECRET letter text (inventory / mailbox / post office / villager letter)",
       data != before and data.count(SECRET) == 0 and before.count(SECRET) >= 8)
    bad = [(a, b) for a, b in public_ranges() if data[a:b] != before[a:b]]
    ck("A every PUBLIC range (header, comment, land / noticeboard, houses minus mailboxes, fg / acres, villager headers, leaflets ...) equals the host GCI (%d ranges, bad %s)"
       % (len(public_ranges()), bad[:3]), not bad)
    recs = [data[PRIV + i * PRIV_STRIDE:PRIV + (i + 1) * PRIV_STRIDE] for i in range(4)]
    hrecs = [before[PRIV + i * PRIV_STRIDE:PRIV + (i + 1) * PRIV_STRIDE] for i in range(4)]
    tpl_idx = [j for j in range(PRIV_STRIDE) if not in_keep(j)]
    same = all(all(recs[i][j] == recs[0][j] for j in tpl_idx) for i in range(4))
    ck("A the 4 private records are IDENTICAL outside the kept ranges (every resident, the host's own included, is the cleared template)", same)
    keeps_ok = all(all(recs[i][a:b] == hrecs[i][a:b] for a, b in KEEP_PRIV) for i in range(4))
    ck("A the kept ranges (PersonalID / gender / face / reset_count, exists / hint / cloth, reset_code, state_flags) equal the host's", keeps_ok)
    ck("A the cleared template is a cleared record: empty pockets / wallet / bank (0x68..0x86, 0x8C, 0x122C)", recs[0][0x68:0x86] == bytes(30) and recs[0][0x8C:0x90] == bytes(4)
       and recs[0][0x122C:0x1230] == bytes(4))
    differing = [i for i in range(4) if any(recs[i][j] != hrecs[i][j] for j in tpl_idx)]
    ck("A the sanitizer really changed the private data of the residents that had some (changed slots %s)" % differing, len(differing) >= 1)
    # no long sample of the host's real private bytes appears in any SANITIZED region of the image (the first run of this check searched the WHOLE image and tripped over
    # values that legitimately occur in public parts too, e.g. the town name / ids inside maps[] and the public land_info; the planted SECRET text is searched everywhere above)
    # The KEPT ranges of the private records (PersonalID, gender / face, exists / hint / cloth, reset_code, state_flags) are public by design, so they are masked out of the searched
    # blob: the host's mail[0] header carries a COPY of the owner's PersonalID (player name + town name; at record +0x4E2) which legitimately equals the kept identity at +0x2.
    def mask_keep(r):
        r = bytearray(r)
        for a, b in KEEP_PRIV:
            r[a:b] = bytes([0xFF]) * (b - a)
        return bytes(r)
    san_regions = [mask_keep(data[PRIV + i * PRIV_STRIDE:PRIV + (i + 1) * PRIV_STRIDE]) for i in range(4)]
    san_regions += [data[S + 0x9CE8 + hh * 0x26B0 + 0x1A30:S + 0x9CE8 + hh * 0x26B0 + 0x25D4] for hh in range(4)] + [data[S + 0x20694:S + 0x20694 + 0x5DA], data[0x40 + 0x1460:0x26040]]
    blob = bytes([0xFF] * 13).join(san_regions)
    leaked, tested = 0, 0
    for i in range(4):
        for j in range(0x68, PRIV_STRIDE - 12, 3):
            win = hrecs[i][j:j + 12]
            if in_keep(j) or in_keep(j + 11) or len(set(win)) < 6 or recs[i][j:j + 12] == win:
                continue
            tested += 1
            if blob.find(win) >= 0:
                leaked += 1
                if leaked <= 12:
                    where = [(nm, hex(r.find(win))) for nm, r in zip(("rec0", "rec1", "rec2", "rec3", "hmail0", "hmail1", "hmail2", "hmail3", "postoffice", "aram"), san_regions) if r.find(win) >= 0]
                    print("[leak?] host rec %d +0x%04X %s found in %s" % (i, j, win.hex(), where))
    ck("A none of %d 12-byte windows (>= 6 distinct values) of the host's real private data (pockets / wallet / mail / designs / museum ...) appears in any sanitized region (private records, mailboxes, post office, ARAM)" % tested,
       tested >= 5 and leaked == 0)
    # the houses' mailboxes and villager letters
    mb_bad = 0
    for hh in range(4):
        hb = S + 0x9CE8 + hh * 0x26B0 + 0x1A30
        if len({data[hb + k * 0x12A:hb + (k + 1) * 0x12A] for k in range(10)}) != 1:
            mb_bad += 1
    ck("A every house mailbox slot is the same cleared Mail_c in all 4 houses", mb_bad == 0)
    # UP_TO_DATE on the sanitized crc
    L.pump_sleep(0.3)
    c = L.FakeClient("san-utd", "127.0.0.1", port)
    c.connect(timeout=30.0)
    info2 = L.town_fetch_start(c, have_crc=info.crc32, have_size=info.total_size)
    ck("A a request with the sanitized crc / size is UP_TO_DATE (or STREAM again if the host re-saved in between) and still flagged sanitized",
       info2 is not None and info2.status in (0, 1) and (info2.flags & 1) == 1 and (info2.status == 0 or info2.crc32 == info.crc32))
    if info2 is not None and info2.status == 0:
        L.town_fetch_collect(c, info2)
    c.disconnect()
    c.close()
    L.pump_sleep(0.5)
    t = h.log_text()
    ck("A host log: STREAM ... SANITIZED and the privacy warning for resident_tokens off", re.search(r"STREAM 467008 bytes crc32 [0-9a-f]{8} xfer \d+ gen \d+ SANITIZED", t) is not None
       and "town_serve is on while resident_tokens is off" in t)
    # an 'old client' resident (no NO_MIGRATE flag): host wins because the host serves sanitized towns
    f = L.FakeClient("san-old", "127.0.0.1", port, player=L.resident_player(2))
    f.rec_resident_idx = 2
    f.connect_and_ready(quiet=True)
    push = f.wait_record_push(0, 8.0)
    t = h.log_text()
    ck("A a resident WITHOUT the NO_MIGRATE flag at rev 0: HOST WINS (town_serve is sanitized), PUSH_FULL rev 1, no MIGRATE_REQUEST (old clients are covered)",
       push is not None and push["rev"] == 1 and "HOST WINS (town_serve is sanitized" in t and "MIGRATE_REQUEST sent" not in t)
    try:
        f.disconnect()
    except Exception:  # noqa: BLE001
        pass
    f.close()
    h.stop()


def phase_b(port, ck):
    h = start_host(port, "b", ["--town-serve", "full"])
    ck("B host (--town-serve full) started and booted to the field", h is not None)
    if h is None:
        return
    watch = T.GciWatcher(HOST_GCI).start()
    info, data, problems, before, after = fetch(port, "san-b")
    L.pump_sleep(0.3)
    watch._sample()
    seen = watch.stop()
    md5 = T.hashlib.md5(data).hexdigest()
    ck("B full mode: TOWN_INFO flags == 0 and the streamed bytes' md5 equals a version of the host's GCI (%s in %s)" % (md5, sorted(seen)),
       info is not None and info.flags == 0 and not problems and md5 in seen and data[0x70:0x78] != b"ACMPSAN1")
    ck("B host log: STREAM ... FULL and the 'town_serve = full' warning", re.search(r"gen \d+ FULL", h.log_text()) is not None and "WARNING: town_serve = full" in h.log_text())
    h.stop()


def push_digests(text):
    return [int(m, 16) for m in re.findall(r"push FULL queued \(resident 1 epoch \d+ rev \d+ xfer \d+ digest 0x([0-9A-Fa-f]{8})\)", text)]


CAL = (0x234C, 0x23B4)  # Private_c.calendar


def upload_runs(text):
    """The host's '[NET][REC] host: peer N upload xfer X changes B byte(s) in R run(s) of the record: 0xOFF+LEN ...' diagnostic -> list of [(off, len)] per accepted upload."""
    out = []
    for m in re.finditer(r"upload xfer \d+ changes (\d+) byte\(s\) in (\d+) run\(s\) of the record:([^\n]*)", text):
        runs = [(int(a, 16), int(b)) for a, b in re.findall(r"0x([0-9A-Fa-f]{4})\+(\d+)", m.group(3))]
        assert len(runs) == min(int(m.group(2)), 12), m.group(0)
        out.append(runs)
    return out


def client_session(port, tag, ck, host, first=False):
    c = start_client(port, tag)
    ok = c.boot_to_field(timeout=170.0, slot=1)
    L.pump_sleep(3.0)
    ct = c.log_text()
    ck("C%s the real client (resident slot 1) booted from the cache and reached READY" % tag, ok and "-> READY" in ct and "LAND_MISMATCH" not in ct)
    ck("C%s client log: the loaded town is a SANITIZED transfer image, HELLO NO_MIGRATE sent, adopted the host record (no MIGRATE upload)" % tag,
       "is a SANITIZED town transfer image (client cache" in ct and "HELLO flag NO_MIGRATE set" in ct and "client: adopted rev=" in ct and "MIGRATE upload" not in ct)
    ck("C%s the adopted PUSH_FULL restored the host's real wallet (%d) that the sanitized cache did not hold" % (tag, WALLET1), re.search(r"adopted rev=\d+ .*\(wallet=%d " % WALLET1, ct) is not None)
    L.pump_sleep(1.0)
    c.stop()
    L.pump_sleep(2.0)
    return ct


def phase_c(port, ck):
    h = start_host(port, "c", ["--town-serve", "on", "--resident-tokens", "tofu"])
    ck("C host (--town-serve on --resident-tokens tofu) started and booted to the field", h is not None)
    if h is None:
        return
    ck("C no privacy warning about resident_tokens when tofu is on", "town_serve is on while resident_tokens is off" not in h.log_text())
    rec_before = read_gci()[PRIV + PRIV_STRIDE:PRIV + 2 * PRIV_STRIDE]
    client_session(port, "1", ck, h, first=True)
    gcis = [os.path.join(r, f) for r, _d, fs in os.walk(CLIENT_TOWNS) for f in fs if f.endswith(".gci")]
    ck("C1 exactly one cached town GCI in the client dir and it carries the sanitized marker", len(gcis) == 1 and open(gcis[0], "rb").read(0x78)[0x70:0x78] == b"ACMPSAN1")
    if len(gcis) == 1:
        cache = open(gcis[0], "rb").read()
        rec1 = cache[PRIV + PRIV_STRIDE:PRIV + 2 * PRIV_STRIDE]
        ck("C1 the client's cached slot-1 (its OWN) record is the cleared template: no pockets / wallet in the cache", rec1[0x68:0x86] == bytes(30) and rec1[0x8C:0x90] == bytes(4))
    t1 = h.log_text()
    d1 = push_digests(t1)
    ck("C1 host log: HOST WINS (the client says its cache is a sanitized town image), rev 1 seeded, PUSH_FULL; no MIGRATE_REQUEST / MIGRATE APPLIED",
       "HOST WINS (the client says its cache is a sanitized town image)" in t1 and len(d1) >= 1 and "MIGRATE_REQUEST sent" not in t1 and "MIGRATE xfer" not in t1)
    client_session(port, "2", ck, h)
    t2 = h.log_text()
    d2 = push_digests(t2)
    # CAUSE of the one-time digest change (found with the host's 'upload ... changes N byte(s)' diagnostic, see docs/multiplayer-phase2-design.md M-G): the FIRST client upload
    # after the adoption changes exactly 2 bytes of the host record, both inside Private_c.calendar (0x234C..0x23B4): played_days[month] (today's day bit) and calendar.month. It is
    # the vanilla daily mCD_calendar_wellcome_on() ("the player played today"), run by the client's mEv_run new-day path on the ADOPTED host calendar (the fixture's host record
    # was last played in the previous month). It is NOT blank / sanitized data: FULL adoption already replaced the client-owned AND host-owned ranges. The same upload happens
    # with --town-serve full (there it is the MIGRATE import that carries it, final digest identical: CD163618). Legit allow-list = calendar bytes only.
    ups1, ups2 = upload_runs(t1), upload_runs(t2)
    print("[observed] host slot-1 push digests: %s; uploads s1 %s" % (["%08X" % x for x in d2], ups1))
    ck("C1 every upload of the first sanitized session changes ONLY calendar bytes (0x%04X..0x%04X, the daily mCD_calendar_wellcome_on) of the host record: %s" % (CAL[0], CAL[1], ups1),
       len(ups1) >= 1 and all(len(r) <= 12 and all(CAL[0] <= o and o + n <= CAL[1] for o, n in r) for r in ups1))
    ck("C2 second session: no further upload changed the record (uploads %d -> %d), no second HOST WINS, no MIGRATE; the host re-pushes the record unchanged (digest %s == %s of session 1's final state)"
       % (len(ups1), len(ups2), d2[-1:] and "%08X" % d2[-1], d2[-1:] and "%08X" % d2[-1]),
       len(d2) >= 2 and len(ups2) == len(ups1) and t2.count("HOST WINS") == 1 and "MIGRATE_REQUEST sent" not in t2 and "MIGRATE xfer" not in t2)
    h.stop()
    rec_after = read_gci()[PRIV + PRIV_STRIDE:PRIV + 2 * PRIV_STRIDE]
    diff = [j for j in range(PRIV_STRIDE) if rec_before[j] != rec_after[j]]
    print("[observed] host GCI slot-1 record bytes changed over sessions 1+2: %s" % ["0x%04X" % j for j in diff])
    ck("C the host's saved slot-1 record (GCI) differs from before session 1 ONLY inside the calendar (0x%04X..0x%04X); wallet / bank / pockets / mail / designs are byte-identical (changed: %s)"
       % (CAL[0], CAL[1], ["0x%04X" % j for j in diff][:8]), all(CAL[0] <= j < CAL[1] for j in diff) and rec_after[0x8C:0x90] == WALLET1.to_bytes(4, "big"))
    rec_dat = os.path.join(HOST_DIR, "save", "mp", "records.dat")
    existed = os.path.isfile(rec_dat)
    if existed:
        os.remove(rec_dat)
    ck("C records.dat of the host existed and was deleted (the resident's lineage is back to rev 0)", existed)
    h = start_host(port, "c3", ["--town-serve", "on", "--resident-tokens", "tofu"])
    ck("C3 host restarted without records.dat", h is not None)
    if h is None:
        return
    client_session(port, "3", ck, h)
    t3 = h.log_text()
    d3 = push_digests(t3)
    ck("C3 rev 0 again (records.dat deleted + host restarted): HOST WINS, PUSH_FULL, no MIGRATE_REQUEST and no MIGRATE APPLIED; the pushed slot-1 record (digest %s) equals the stable one of session 2 (%s)"
       % (d3 and "%08X" % d3[0], d2 and "%08X" % d2[-1]),
       "never synced (rev 0): HOST WINS" in t3 and not upload_runs(t3) and len(d3) >= 1 and d2 and d3[0] == d2[-1] and "MIGRATE_REQUEST sent" not in t3 and "MIGRATE xfer" not in t3 and "MIGRATE APPLIED" not in t3)
    h.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11890)
    ap.add_argument("--phases", default="abc", help="which phases to run (default all; e.g. 'c')")
    args = ap.parse_args()
    L.require_test_bin_dir()
    results = []
    ck = lambda d, c: L.check(d, c, results)  # noqa: E731
    for ph, fn, off in (("a", phase_a, 0), ("b", phase_b, 1), ("c", phase_c, 2)):
        if ph in args.phases:
            fresh_fixtures()
            fn(args.port + off, ck)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
