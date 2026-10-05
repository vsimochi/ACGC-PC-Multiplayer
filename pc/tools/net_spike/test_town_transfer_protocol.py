#!/usr/bin/env python3
"""test_town_transfer_protocol.py - M-B TOWN TRANSFER (wire ids 62..65), HOST half.

TIER: PROTOCOL TESTED -- REAL host processes (`AnimalCrossing.exe --host <port> --bootstrap-resident 0 [--town-serve on]`, started / stopped by this script) driven by
scripted FakeClients (net_spike_lib: TOWN_FETCH_REQ / TOWN_INFO / TOWN_CHUNK / TOWN_DONE codec + town_fetch_start / town_fetch_collect). The host's C code runs for
real; the REAL client half (pre-boot prefetch, atomic install, fallbacks) is covered by test_town_fetch_real.py and the unit test test_town_cache_unit.py.

DISPOSABLE FIXTURE: a fresh copy of pc\\build64\\bin_fixture4 (never touched, only read) in pc\\build64\\bin_fixture4_town (basename starts with bin_fixture4, so the
harness' whole-save snapshot / restore applies); NET_SPIKE_GAME_BIN points at it. The live save dir, bin_talkfix* and bin_fixture4 are never used.

Phases (one host process each):
  P1  host WITH --town-serve full (M-G: on = sanitized): the streamed bytes' md5 == the host's GCI md5 and crc32 == TOWN_INFO.crc32, the TOWN_INFO town == the host's IDENTITY town (the terrain hash
      replica of pc_save_validate_gci_buffer is exact); 468 in-order chunks of <= 1000 B with a u32 offset; UP_TO_DATE for equal (crc, size) with no chunks, STREAM for a
      different crc; BAD_REQUEST (flags, protocol, size); an ABORT mid-stream leaves the host healthy (the next fetch and a normal READY client work); IDENTITY on a fetch
      connection is ignored (no ACK / REJECT, logged) and TOWN_DONE closes the connection; BUSY when 2 streams are active; a request after IDENTITY never streams; the
      per-address limiter eventually answers BUSY
  P2  host with NO town flag (default OFF): TOWN_INFO status REFUSED (4), nothing streamed, the host stays healthy for a normal READY client
Usage: python test_town_transfer_protocol.py [--port 11850]
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

os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, "bin_fixture4_town")
if __name__ == "__main__":
    T.make_fixture("bin_fixture4_town")

import net_spike_lib as L  # noqa: E402

DST_BIN = L.GAME_BIN_DIR
GCI = os.path.join(DST_BIN, L.SAVE_GCI_REL)


class Rig:
    def __init__(self, port0, results):
        self.port0, self.results, self.n = port0, results, 0
        self.check = lambda d, c: L.check(d, c, results)
        self.host = None
        self.port = None
        self.clients = []

    def start_host(self, tag, extra=()):
        self.n += 1
        port = self.port0 + self.n
        for i in range(3):
            h = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0"] + list(extra), log_path=T.log_path("town_proto_%s_try%d.log" % (tag, i)),
                              bin_dir=L.GAME_BIN_DIR).start()
            if h.wait_listening(60.0) and h.boot_to_field(timeout=90.0, slot=0):
                self.host, self.port = h, port
                return h
            h.stop()
            time.sleep(2.0)
        return None

    def new_client(self, label, slot=None):
        c = L.FakeClient(label, "127.0.0.1", self.port, player=L.resident_player(slot) if slot is not None else None)
        self.clients.append(c)
        return c

    def fetch(self, label, **req):
        L.pump_sleep(0.3)  # a fresh client never races the previous connection's DISCONNECT / the ephemeral port reuse
        c = self.new_client(label)
        c.connect(timeout=30.0)
        info = L.town_fetch_start(c, **req)
        return c, info

    def release(self):
        for c in self.clients:
            try:
                if c.state in (c.STATE_CONNECTED, c.STATE_PENDING):
                    c.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients = []
        L.pump_sleep(0.5)


def p1(rig):
    ck = rig.check
    h = rig.start_host("p1", ["--town-serve", "full"])  # M-G: the md5 == host GCI checks need the FULL image; sanitized mode: test_town_sanitize_real.py
    ck("P1 host started (--town-serve full) and boots to the field", h is not None)
    if h is None:
        return
    watch = T.GciWatcher(GCI).start()  # every version of the host's GCI (it re-saves every 60 s with random bytes): a snapshot must equal one of them

    def is_host_version(b):
        L.pump_sleep(0.3)
        watch._sample()
        return T.hashlib.md5(b).hexdigest() in watch.seen
    host_town = L.resolve_host_town("127.0.0.1", rig.port)
    L.pump_sleep(0.3)

    # a: full stream
    c, info = rig.fetch("town-a")
    ck("P1a TOWN_INFO is STREAM, total 467008, chunk_size 1000, 468 chunks, non-zero xfer, len 40", info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_STREAM
       and info.total_size == 467008 and info.chunk_size == 1000 and info.chunk_count == 468 and info.xfer_id != 0)
    data, problems = L.town_fetch_collect(c, info) if info is not None else (b"", ["no info"])
    ck("P1a 468 in-order chunks, offsets checked, no problems (%s)" % problems, not problems and len(data) == 467008)
    L.pump_sleep(0.3)
    watch._sample()
    first_md5 = T.hashlib.md5(data).hexdigest()
    ck("P1a streamed bytes md5 (%s) == the md5 of the host's GCI file (a version it had on disk: %s)" % (first_md5, sorted(watch.seen)), first_md5 in watch.seen)
    ck("P1a TOWN_INFO.crc32 == zlib.crc32 of the stream", info.crc32 == zlib.crc32(data) & 0xFFFFFFFF)
    ck("P1a TOWN_INFO town (land_name, land_id, terrain_hash) == the host's own identity town (the hash replica is exact)",
       (info.land_name, info.land_id, info.terrain_hash) == (host_town.land_name, host_town.land_id, host_town.terrain_hash))
    L.pump_sleep(0.6)
    t = h.log_text()
    ck("P1a host log: STREAM ... then TOWN_DONE status 0 closing the fetch connection", re.search(r"\[NET\]\[TOWN\] host: peer \d+ STREAM 467008 bytes", t) is not None
       and re.search(r"TOWN_DONE status 0 crc32 %08x" % (zlib.crc32(data) & 0xFFFFFFFF), t) is not None)
    a_info = info
    c.close()

    # BUSY: 2 active streams (never closed) -> the 3rd request is BUSY
    c1, i1 = rig.fetch("town-busy1")
    c2, i2 = rig.fetch("town-busy2")
    c3, i3 = rig.fetch("town-busy3")
    ck("P1h two concurrent streams are served and the 3rd request is BUSY (3)", i1.status == 0 and i2.status == 0 and i3 is not None and i3.status == L.PC_NETGAME_TOWN_STATUS_BUSY)
    ck("P1h host log: BUSY line", re.search(r"peer \d+ BUSY \(2 streams active", h.log_text()) is not None)
    for cc in (c1, c2, c3):
        cc.disconnect()
        cc.close()
    L.pump_sleep(1.0)

    # a fresh crc for the UP_TO_DATE checks (the host may have re-saved in between: retry with a fresh one)
    first_info = None
    i0 = a_info
    for _attempt in range(3):
        if _attempt > 0:  # the host re-saved in between: a fresh crc
            c, i0 = rig.fetch("town-fresh%d" % _attempt)
            L.town_fetch_collect(c, i0)
            c.close()
        c, info = rig.fetch("town-c", have_crc=i0.crc32, have_size=467008, town=host_town)
        if info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_UP_TO_DATE:
            first_info = i0
            break
        c.close()
    ok_utd = first_info is not None
    if first_info is None:
        first_info = i0

    # c: UP_TO_DATE, d: different crc streams
    ck("P1c equal (crc, size) -> UP_TO_DATE (status 1) with size/crc/town, chunk_count 0", ok_utd and info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_UP_TO_DATE
       and info.total_size == 467008 and info.crc32 == first_info.crc32 and info.chunk_count == 0)
    got = c.inbox.collect(lambda m: m.is_game and m.payload[0] == L.PC_NETGAME_MSG_TOWN_CHUNK, 1.0)
    ck("P1c UP_TO_DATE: no chunk is streamed", len(got) == 0)
    c.send_reliable(L.build_town_done(L.PC_NETGAME_TOWN_DONE_OK, info.xfer_id, info.crc32))
    L.pump_sleep(0.4)
    ck("P1c host log: UP_TO_DATE then the connection closed by TOWN_DONE", "UP_TO_DATE (crc32" in h.log_text())
    c.close()
    c, info = rig.fetch("town-d", have_crc=first_info.crc32 ^ 1, have_size=467008, town=host_town)
    ck("P1d a different crc (same size) is NOT up to date: STREAM", info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_STREAM)
    data, problems = L.town_fetch_collect(c, info)
    ck("P1d ... and streams the whole identical file", not problems and is_host_version(data))
    c.close()
    # e: abort mid-stream
    c, info = rig.fetch("town-e")
    data, problems = L.town_fetch_collect(c, info, stop_after=60000, ack_done=False)
    ck("P1e received %d bytes of the stream, then the client vanishes (disconnect)" % len(data), info.status == 0 and 60000 <= len(data) < 467008 and not problems)
    c.disconnect()
    c.close()
    L.pump_sleep(1.0)
    t = h.log_text()
    ck("P1e host stayed alive and logged the peer's disconnect", h.alive() and re.search(r"host: peer \d+ disconnected", t) is not None)
    c, info = rig.fetch("town-e2")
    data, problems = L.town_fetch_collect(c, info)
    ck("P1e the next fetch after the abort streams the identical file", info.status == 0 and not problems and is_host_version(data))
    c.close()

    # g: bad requests
    for desc, kw in (("flags != 0", dict(flags=1)), ("reserved != 0", dict(rsv0=7)), ("wrong protocol version", dict(protocol_version=7))):
        c, info = rig.fetch("town-bad", **kw)
        ck("P1g BAD_REQUEST (5) for %s" % desc, info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_BAD_REQUEST and info.total_size == 0)
        c.close()
    c = rig.new_client("town-bad-size")
    c.connect(timeout=30.0)
    c.send_reliable(L.build_town_fetch_req()[:39])
    m = c.inbox.wait_for(lambda mm: mm.is_game and mm.payload[0] == L.PC_NETGAME_MSG_TOWN_INFO, 5.0)
    ck("P1g BAD_REQUEST (5) for a 39-byte request", m is not None and L.TOWN_INFO_SPEC.decode(m.payload).status == L.PC_NETGAME_TOWN_STATUS_BAD_REQUEST)
    c.close()

    # f: IDENTITY on a fetch connection is ignored
    c, info = rig.fetch("town-f", town=host_town)
    ck("P1f fetch connection got a STREAM", info is not None and info.status == 0)
    mark = c.inbox.mark()
    c.send_identity(town=host_town, player=L.resident_player(1))
    got = c.inbox.collect(lambda m: m.is_game and m.payload[0] in (L.PC_NETGAME_MSG_IDENTITY_ACK, L.PC_NETGAME_MSG_REJECT), 2.0, since=mark)
    ck("P1f IDENTITY on a fetch connection gets NO IDENTITY_ACK and NO REJECT", len(got) == 0)
    t = h.log_text()
    ck("P1f host log: 'town FETCH connection: IDENTITY IGNORED' and the peer never became READY for it", "is a town FETCH connection: IDENTITY IGNORED" in t)
    data, problems = L.town_fetch_collect(c, info)
    ck("P1f the stream still completes after the ignored IDENTITY and TOWN_DONE closes the connection", not problems and is_host_version(data))
    c.close()

    # a normal READY client still works (host healthy)
    r = rig.new_client("ready1", slot=1)
    try:
        r.connect_and_ready(quiet=True)
        ready = True
    except Exception as exc:  # noqa: BLE001
        print("ready client failed: %r" % (exc,))
        ready = False
    ck("P1i a normal resident FakeClient still reaches READY after all those fetches (host healthy)", ready)
    # a request AFTER IDENTITY never streams
    if ready:
        mark = r.inbox.mark()
        r.send_reliable(L.build_town_fetch_req())
        got = r.inbox.collect(lambda m: m.is_game and m.payload[0] in (L.PC_NETGAME_MSG_TOWN_INFO, L.PC_NETGAME_MSG_TOWN_CHUNK), 1.5, since=mark)
        ck("P1i a TOWN_FETCH_REQ from a READY (identity-bound) peer streams nothing", not any(m.payload[0] == L.PC_NETGAME_MSG_TOWN_CHUNK for m in got)
           and not any(L.TOWN_INFO_SPEC.decode(m.payload).status == 0 for m in got if m.payload[0] == L.PC_NETGAME_MSG_TOWN_INFO and L.TOWN_INFO_SPEC.matches(m.payload)))
        ck("P1i host log: ignored (link state READY)", "TOWN_FETCH_REQ ignored" in h.log_text())
        r.disconnect()
        r.close()
        L.pump_sleep(0.5)

    # per-address limiter
    seen_busy = False
    for i in range(14):
        c, info = rig.fetch("town-rate%d" % i, have_crc=first_info.crc32, have_size=467008, town=host_town)
        if info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_BUSY:
            seen_busy = True
            c.close()
            break
        if info is not None:
            c.send_reliable(L.build_town_done(L.PC_NETGAME_TOWN_DONE_OK, info.xfer_id, info.crc32))
        c.close()
    ck("P1j the per-address limiter eventually answers BUSY (>= 10 counted requests in 60 s)", seen_busy and "per-address limit 10 per 60000 ms" in h.log_text())
    ck("P1k the host process is still alive at the end", h.alive())
    watch.stop()
    rig.release()
    h.stop()
    rig.host = None


def p2(rig):
    ck = rig.check
    h = rig.start_host("p2")
    ck("P2 host started WITHOUT a town flag (default OFF) and boots to the field", h is not None)
    if h is None:
        return
    c, info = rig.fetch("town-off")
    ck("P2 TOWN_INFO status is REFUSED (4), no size, no stream", info is not None and info.status == L.PC_NETGAME_TOWN_STATUS_REFUSED and info.total_size == 0 and info.chunk_count == 0)
    got = c.inbox.collect(lambda m: m.is_game and m.payload[0] == L.PC_NETGAME_MSG_TOWN_CHUNK, 1.0)
    ck("P2 not a single chunk was sent", len(got) == 0)
    ck("P2 host log: REFUSED (town_serve is off ...)", "REFUSED (town_serve is off" in h.log_text())
    c.close()
    r = rig.new_client("ready-off", slot=1)
    try:
        r.connect_and_ready(quiet=True)
        ready = True
    except Exception as exc:  # noqa: BLE001
        print("ready client failed: %r" % (exc,))
        ready = False
    ck("P2 a normal resident FakeClient still reaches READY (the host is healthy)", ready)
    rig.release()
    h.stop()
    rig.host = None


def run(port, results, only="P1,P2"):
    L.require_test_bin_dir()
    rig = Rig(port, results)
    try:
        for name, fn in (("P1", p1), ("P2", p2)):
            if name in only.split(","):
                fn(rig)
    finally:
        rig.release()
        if rig.host is not None:
            rig.host.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11850)
    ap.add_argument("--only", default="P1,P2")
    args = ap.parse_args()
    results = []
    run(args.port, results, args.only)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
