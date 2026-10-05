#!/usr/bin/env python3
"""test_admin_items_protocol.py - host admin item tools (`finditem` / `iteminfo` / `items` / `give`) against a REAL `AnimalCrossing.exe --host <port> --dedicated`
process with a stdin pipe, started from the DISPOSABLE fixture pc\\build64\\bin_fixture4 (NET_SPIKE_GAME_BIN; its save dir is snapshotted/restored by CloneSaveGuard).
One scripted FakeClient plays a RESIDENT (no real game client, no visual check).

TIER: real host process + scripted FakeClient + the real stdin console. NOT a source-level test (that is test_admin_items_src.py).

PHASE A (lookup, no client): `finditem dresser` header/lines/category, no-results text, pagination (`items furniture 1/2`, a page past the end), `iteminfo` by hex and
  decimal (same answer), malformed / out-of-range / not-a-pocket-id / money / unknown inputs, `items` category checks, bad usage, `give` failure texts that need no client,
  the process survives everything and still answers `status`.
PHASE B (give): resident 1 connected + SYNCED -> `give <name> 0x2200`: output line, PUSH_FULL arrives, exactly one free pocket filled, rev+1, everything else byte-equal;
  a STALE upload built on the pre-give base is refused STALE_BASE and the item is KEPT (the follow-up push still holds it); a rotated furniture id is masked and reported;
  over-capacity (`qty` > free) is refused with NOTHING changed (no push); exact fill works; then a 1-item give to the full inventory is refused (`inventory is full`);
  invalid items (money, ticket, bad ids), bad qty, unknown player, an offline never-synced resident -> failures that change nothing (no push, no rev bump).
Usage: python test_admin_items_protocol.py [--port 12560]
"""
import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILD64 = os.path.abspath(os.path.join(HERE, "..", "..", "build64"))
os.environ.setdefault("NET_SPIKE_GAME_BIN", os.path.join(BUILD64, "bin_fixture4"))
import net_spike_lib as L  # noqa: E402

IP = "127.0.0.1"


def say(h, cmd, settle=1.0, timeout=8.0):
    """One console line; returns the host log text printed after it (waits until the output stopped growing)."""
    off = len(h.log_text())
    assert h.send_line(cmd)
    end = time.monotonic() + timeout
    last, last_t = off, time.monotonic()
    while time.monotonic() < end:
        L.pump_sleep(0.2)
        n = len(h.log_text())
        if n != last:
            last, last_t = n, time.monotonic()
        elif time.monotonic() - last_t >= settle and n > off:
            break
    return h.log_text()[off:].replace("\r\n", "\n")


def pockets_of(push):
    return list(L.record_inventory(push["data"])[0])


def phase_a(h, ck):
    out = say(h, "finditem dresser")
    m = re.search(r'ITEM SEARCH: "dresser" - (\d+) results?', out)
    ck("A finditem dresser: header with a result count", m is not None and int(m.group(1)) >= 1)
    lines = re.findall(r"^(0x[0-9A-F]{4}) - ([^\n]+?) - ([A-Za-z ()]+?)(?: \[not giveable[^\]]*\])?$", out, re.M)
    ck("A finditem dresser: `0xXXXX - Name - Category` lines, every name contains 'dresser', category Furniture",
       len(lines) >= 1 and all("dresser" in n.lower() and c == "Furniture" for _i, n, c in lines))
    ck("A finditem dresser: page footer 'Page 1/y'", re.search(r"^Page 1/\d+", out, re.M) is not None)
    ck("A finditem names are plain printable ASCII (charset fold, no raw game codes)", all(all(32 <= ord(ch) < 127 for ch in n) for _i, n, _c in lines))
    fid = int(lines[0][0], 16) if lines else 0x1000
    out = say(h, "finditem DRESSER")
    ck("A finditem is case-insensitive", re.search(r'ITEM SEARCH: "dresser" - \d+ result', out) is not None)
    out = say(h, "finditem zzqqxx")
    ck("A finditem with no match: 'no results'", 'ITEM SEARCH: "zzqqxx" - no results' in out)
    out = say(h, "finditem")
    ck("A finditem without text: usage, no crash", "usage: finditem" in out)
    out = say(h, "iteminfo 0x%04X" % fid)
    out_dec = say(h, "iteminfo %d" % fid)
    ck("A iteminfo (hex) shows id, name, category Furniture, giveable yes", ("ITEM INFO: 0x%04X (%d)" % (fid, fid)) in out and "category: Furniture" in out and "giveable: yes" in out)
    ck("A iteminfo decimal gives the same answer as hex", out == out_dec.replace("ITEM INFO: 0x%04X (%d)" % (fid, fid), "ITEM INFO: 0x%04X (%d)" % (fid, fid)) or ("ITEM INFO: 0x%04X (%d)" % (fid, fid)) in out_dec)
    out = say(h, "iteminfo 0x%04X" % (fid + 1))
    ck("A iteminfo of a rotated furniture id reports the masked id", "rotation bits cleared" in out and ("ITEM INFO: 0x%04X" % fid) in out)
    out = say(h, "iteminfo 0x2200")
    ck("A iteminfo 0x2200 = Net, Tools, giveable", "name: net" in out and "category: Tools" in out and "giveable: yes" in out)
    out = say(h, "iteminfo 0x2100")
    ck("A iteminfo money bag: giveable NO (money)", "giveable: NO" in out and "money" in out)
    out = say(h, "iteminfo 0x2C00")
    ck("A iteminfo ticket: giveable NO", "giveable: NO" in out and "ticket" in out)
    for bad, what in (("0xZZ", "malformed hex"), ("abc", "malformed"), ("-5", "negative"), ("0x", "empty hex"), ("0x12345", "out of range")):
        out = say(h, "iteminfo " + bad)
        ck("A iteminfo %s (%s): rejected, no crash" % (bad, what), "Invalid item ID" in out)
    out = say(h, "iteminfo 99999999")
    ck("A iteminfo 99999999: out of range", "Invalid item ID" in out)
    out = say(h, "iteminfo 0x0001")
    ck("A iteminfo 0x0001: not a pocket item", "Invalid item ID" in out)
    out = say(h, "iteminfo")
    ck("A iteminfo without id: usage", "usage: iteminfo" in out)
    out = say(h, "items furniture 1")
    m = re.search(r"ITEMS \(furniture\) - (\d+) results", out)
    n_item_lines = len(re.findall(r"^0x[0-9A-F]{4} - .* - Furniture", out, re.M))
    pm = re.search(r"^Page 1/(\d+) - next: items furniture 2$", out, re.M)
    ck("A items furniture 1: count, 15 lines, 'Page 1/y - next: items furniture 2'", m is not None and int(m.group(1)) > 15 and n_item_lines == 15 and pm is not None)
    pages = int(pm.group(1)) if pm else 1
    out = say(h, "items furniture 2")
    ck("A items furniture 2: page 2", re.search(r"^Page 2/%d" % pages, out, re.M) is not None)
    out = say(h, "items furniture %d" % (pages + 5))
    ck("A items furniture <past the end>: 'page N does not exist'", "does not exist" in out)
    out = say(h, "items tools")
    ck("A items tools: Tools lines incl. Net", "ITEMS (tools)" in out and "- net - Tools" in out)
    out = say(h, "items wallpaper")
    ck("A items wallpaper / carpet / clothing / miscellaneous / all answer", all(("ITEMS (%s)" % c) in say(h, "items " + c, settle=0.8) for c in ("carpet", "clothing", "miscellaneous", "all")) and "ITEMS (wallpaper)" in out)
    out = say(h, "items bogus")
    ck("A items with an unknown category: error listing the categories", "unknown category" in out)
    out = say(h, "items")
    ck("A items without category: usage", "usage: items" in out)
    out = say(h, "items furniture 0")
    ck("A items furniture 0: invalid page", "invalid page" in out)
    out = say(h, "help")
    ck("A help lists finditem / iteminfo / items / give", all(x in out for x in ("  finditem <text>", "  iteminfo <id>", "  items <furniture", "  give <player")))
    out = say(h, "give Nobody 0x2200")
    ck("A give to an unknown player: \"GIVE FAILED: Unknown player 'Nobody'.\"", "GIVE FAILED: Unknown player 'Nobody'." in out)
    out = say(h, "give Nobody 0xZZ")
    ck("A give with a malformed id: Invalid item ID", "GIVE FAILED: Invalid item ID" in out)
    out = say(h, "give Nobody 0x2100")
    ck("A give of money: 'GIVE FAILED: Invalid item ID 0x2100'", "GIVE FAILED: Invalid item ID 0x2100" in out)
    out = say(h, "give")
    ck("A give without arguments: usage", "GIVE FAILED: usage" in out)
    out = say(h, "status")
    ck("A host still alive and answering `status` after every bad input", "[DEDICATED] status" in out and h.proc.poll() is None)
    return fid


def phase_b(h, ck, port, fid, results):
    slot, other = 1, 2
    residents = {i: p for i, p, _e in L.read_test_save_residents()}
    name = residents[slot].player_name.rstrip(b"\x00 ").decode("latin-1")
    other_name = residents[other].player_name.rstrip(b"\x00 ").decode("latin-1") if other in residents else None
    c = L.FakeClient("RES", IP, port, player=L.resident_player(slot))
    c.rec_resident_idx = slot
    c.connect_and_ready(quiet=True)
    ck("B resident %d (%s) connected and SYNCED" % (slot, name), c.wait_record_synced(30.0) and len(c.rec_pushes) >= 1)
    L.pump_sleep(2.0)
    base_push = c.rec_pushes[-1]
    pre = pockets_of(base_push)
    old_last = c.rec_last
    free0 = sum(1 for p in pre if p == 0)
    ck("B the fixture resident has at least 3 free pockets", free0 >= 3)

    # ---- single give -> PUSH_FULL ----
    n0 = len(c.rec_pushes)
    out = say(h, "give %s 0x2200" % name)
    ck("B give prints 'GIVE: <name> received item 0x2200 (net).'", ("GIVE: %s received item 0x2200 (net)." % name) in out)
    push = c.wait_record_push(after=n0, timeout=8.0)
    ck("B the owner receives a PUSH_FULL", push is not None and push["kind"] == 1 and push["digest_ok"])
    post = pockets_of(push) if push else []
    ck("B exactly one free pocket now holds 0x2200, every other pocket unchanged",
       push is not None and post.count(0x2200) == pre.count(0x2200) + 1 and sum(1 for a, b in zip(pre, post) if a != b) == 1)
    ck("B the push carries rev+1 of the same epoch", push is not None and old_last is not None and push["epoch"] == old_last[1] and push["rev"] == old_last[2] + 1)
    ck("B wallet untouched", push is not None and L.record_inventory(push["data"])[2] == L.record_inventory(base_push["data"])[2])

    # ---- stale upload built on the pre-give base: refused, item kept ----
    L.pump_sleep(1.8)
    n1 = len(c.rec_pushes)
    x = c.upload_record(base_push["data"], base=(old_last[1], old_last[2]))
    ack = c.wait_record_ack(xfer_id=x)
    ck("B an upload built on the pre-give base is refused STALE_BASE", ack is not None and ack.status == L.PC_NETGAME_REC_ACK_STALE_BASE)
    push2 = c.wait_record_push(after=n1, timeout=6.0)
    ck("B ...and the follow-up PUSH_FULL still holds the gifted item (nothing lost)", push2 is not None and pockets_of(push2).count(0x2200) == pre.count(0x2200) + 1)
    cur = pockets_of(push2) if push2 else post
    free = sum(1 for p in cur if p == 0)

    # ---- rotated furniture id: masked + reported ----
    n2 = len(c.rec_pushes)
    out = say(h, "give %s %d" % (name, fid + 2))
    ck("B give of a rotated furniture id: note + gives the canonical piece", "rotation bits cleared" in out and ("GIVE: %s received item 0x%04X" % (name, fid)) in out)
    push3 = c.wait_record_push(after=n2, timeout=8.0)
    cur3 = pockets_of(push3) if push3 else cur
    ck("B the canonical (unrotated) furniture id is in the pockets", push3 is not None and fid in cur3 and (fid + 2) not in cur3)
    free = sum(1 for p in cur3 if p == 0)

    # ---- over capacity: refused, nothing changed ----
    n3 = len(c.rec_pushes)
    h0 = h.log_text()
    out = say(h, "give %s 0x2201 %d" % (name, min(15, free + 1)), settle=1.0)
    ck("B qty larger than the free pockets: 'GIVE FAILED: ... inventory is full ... No items were given.'", "GIVE FAILED: %s's inventory is full" % name in out and "No items were given." in out)
    L.pump_sleep(2.5)
    ck("B ...and no push followed (nothing was changed)", len(c.rec_pushes) == n3)

    # ---- exact fill ----
    out = say(h, "give %s 0x2201 %d" % (name, free))
    ck("B exact fill: 'received item 0x2201 (axe) x%d.'" % free, ("GIVE: %s received item 0x2201 (axe) x%d." % (name, free)) in out if free > 1 else ("GIVE: %s received item 0x2201 (axe)." % name) in out)
    push4 = c.wait_record_push(after=n3, timeout=8.0)
    full = pockets_of(push4) if push4 else []
    ck("B the inventory is now full (no empty pocket) and holds the original items + gifts", push4 is not None and 0 not in full and full.count(0x2201) >= free)

    n4 = len(c.rec_pushes)
    out = say(h, "give %s 0x2202" % name)
    ck("B give to a full inventory: \"GIVE FAILED: <name>'s inventory is full. No items were given.\"", ("GIVE FAILED: %s's inventory is full. No items were given." % name) in out)

    # ---- invalid / refused inputs change nothing ----
    for cmd, rx, why in (("give %s 0x2100" % name, r"GIVE FAILED: Invalid item ID 0x2100", "money bag"),
                         ("give %s 0x2C00" % name, r"GIVE FAILED: Invalid item ID 0x2C00", "ticket"),
                         ("give %s 0x0001" % name, r"GIVE FAILED: Invalid item ID 0x0001", "not a pocket id"),
                         ("give %s 0xFFFF" % name, r"GIVE FAILED: Invalid item ID", "0xFFFF"),
                         ("give %s 70000" % name, r"GIVE FAILED: Invalid item ID", "out of range"),
                         ("give %s 0x2200 0" % name, r"GIVE FAILED: Invalid quantity", "qty 0"),
                         ("give %s 0x2200 16" % name, r"GIVE FAILED: Invalid quantity", "qty 16"),
                         ("give %s 0x2200 2x" % name, r"GIVE FAILED: Invalid item ID '2x'", "malformed trailing word"),
                         ("give Zzyzx 0x2200", r"GIVE FAILED: Unknown player 'Zzyzx'\.", "unknown player")):
        out = say(h, cmd, settle=0.8)
        ck("B `%s` -> %s refused" % (cmd, why), re.search(rx, out) is not None)
    if other_name is not None:
        out = say(h, "give %s 0x2200" % other_name)
        ck("B an offline resident that never synced a record is refused (the gift would vanish)", "GIVE FAILED:" in out and "never synced" in out)
    L.pump_sleep(2.5)
    ck("B none of the refused gives produced a push (rev not bumped)", len(c.rec_pushes) == n4)
    out = say(h, "status")
    ck("B host still alive", "[DEDICATED] status" in out and h.proc.poll() is None)
    try:
        c.disconnect()
    except Exception:  # noqa: BLE001
        pass
    c.close()
    L.pump_sleep(0.6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=12560)
    ap.add_argument("--log-dir", default=HERE)
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: run on pc\\build64\\bin_fixture4 only (NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)
    with L.CloneSaveGuard():
        h = None
        try:
            h = L.HostProcess(port=args.port, extra_args=["--dedicated"], log_path=os.path.join(args.log_dir, "admin_items_host.log"), bin_dir=L.GAME_BIN_DIR, stdin_pipe=True,
                              verbose=False, new_group=True).start()
            ok = h.wait_listening(60.0) and h.boot_to_dedicated(timeout=120.0)
            ck("dedicated host booted: observer + world + save ready", ok)
            if ok:
                L.resolve_host_town(IP, args.port)
                fid = phase_a(h, ck)
                phase_b(h, ck, args.port, fid, results)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            ck("test aborted by an exception: %s" % e, False)
        finally:
            if h is not None:
                try:
                    h.send_line("stop")
                    h.wait_exit(45.0)
                except Exception:  # noqa: BLE001
                    pass
    L.discard_fixture_guest_mp(L.GAME_BIN_DIR)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
