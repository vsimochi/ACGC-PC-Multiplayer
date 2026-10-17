#!/usr/bin/env python3
"""test_sell_basket_protocol.py - Nook bulk sell basket, HOST half: the basket's ONE sale is the existing SHOP_SELL (TXN_COMMIT kind 11) with a slot mask of MIXED items,
validated by the host slot by slot with THE shared predicate (pc_sell_logic.c pc_sell_refusal) and the host's own prices; all or nothing.

PROTOCOL test against a REAL listen host (`AnimalCrossing.exe --host --bootstrap-resident 0`, the w3 exe in the disposable fixture pc\\build64\\bin_fixture4_sell_proto,
ports 31000+, Samsung display / volume 1), scripted FakeClients (residents 1..2 + one guest). The fixture save is snapshotted and restored.
  V   single sales of a fish, an insect, an apple and a furniture piece (the host's own value of each = the wallet delta)
  M1  ONE mixed sale of all four + a stock item in five slots: APPLIED, value == the sum of the single values (+ the stock price / 4), every sold slot emptied,
      every other pocket / condition unchanged, rev + 1, ONE committed host line with that mask / value, sales_sum + value / 2 (mirror)
  M2  conservation / refusal: the same mixed set + ONE item Nook does not buy (money bag, wrapped present, exercise card, Wisp spirit, quest item) -> REJECTED
      NOT_SELLABLE, nothing changes (no applied line, rev unchanged, no post-image)
  M3  stale / wrong pre-image: base rev older than the last pocket transaction -> STALE_IMAGE; the primary slot no longer holds the item -> PRECOND; a masked slot empty -> PRECOND
  M4  wallet cap: the mixed sale over 99,999 -> vanilla overflow (a 30,000 bag in the LOWEST sold slot, the rest in the wallet)
  M5  replay of the mixed sale: APPLIED / REPLAYED, executed once
  M6  two clients sell mixed baskets at the same time: both APPLIED, independent
  M7  a GUEST sells a mixed basket (guests are full shop participants): APPLIED with the same values
Usage: python test_sell_basket_protocol.py [--port 31100] [--no-fixture]
"""
import argparse
import os
import re
import shutil
import sys

os.environ.setdefault("AC_DISPLAY_NAME", "samsung")
os.environ.setdefault("AC_MASTER_VOLUME", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import town_test_support as T  # noqa: E402

FIXTURE = "bin_fixture4_sell_proto"
W3_EXE = os.path.join(T.PC, "build64_w3", "bin", "AnimalCrossing.exe")
os.environ["NET_SPIKE_GAME_BIN"] = os.path.join(T.BUILD64, FIXTURE)

FISH, INSECT, APPLE, FTR = 0x2301, 0x2D05, 0x2800, 0x1068
MONEY_1000, CARD, WISP = 0x2100, 0x2500 + 35, 0x2D00 + 40
COND_PRESENT, COND_QUEST = 1, 2
WALLET_MAX = 99999
ITM_MONEY_30000 = 0x2102


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=31100)
    ap.add_argument("--no-fixture", action="store_true")
    args = ap.parse_args()
    if not (31000 <= args.port <= 31990):
        print("ports 31000-31999 only")
        return 2
    if not args.no_fixture:
        T.make_fixture(FIXTURE)
    shutil.copy2(W3_EXE, os.path.join(T.BUILD64, FIXTURE, "AnimalCrossing.exe"))
    import net_spike_lib as L
    import test_txn_protocol as TP
    import test_guest_protocol as TG
    import test_shop_protocol as SH
    import test_ts_protocol as TS
    from test_txn_protocol import APPLIED, REJECTED, R

    L.require_test_bin_dir()
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "sell_basket_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    def body(run):
        ck = run.check
        a = run.ready("A", run.r1)
        b = run.ready("B", run.r2)
        ck("setup: A and B READY and SYNCED", a.rec_synced and b.rec_synced)
        sa = SH.wait_shop(a, 1, 6.0)
        stock = SH.listed(run, sa[1]) if sa else []
        ck("setup: the host logged at least one buyable stock item with its price (%s)" % (stock[:1],), bool(stock))
        st_item, st_price = (stock[0][1], stock[0][2]) if stock else (APPLE, 0)

        def place(c, items, conds_over=None, wallet=0):
            """a pre-image: `items` {slot: item} written over the client's pockets (conds 0 there unless conds_over {slot: cond})"""
            p, cc, _w = c.txn_pre_image()
            p = list(p)
            for s, it in items.items():
                p[s] = it
                cc &= ~(3 << (2 * s))
                if conds_over and s in conds_over:
                    cc |= conds_over[s] << (2 * s)
            return (tuple(p), cc, wallet)

        def sell(c, slots_items, pre, wait=3.0, **kw):
            mask = 0
            for s in slots_items:
                mask |= 1 << s
            prim = min(slots_items)
            rid = run.fresh_rid()
            sent, r = c.txn_shop_sell(mask, prim, slots_items[prim], rid=rid, timeout=wait, pre=pre, **kw)
            return rid, sent, r, mask

        # ---------------- V: the host's own single values
        fr = SH.free_slots(a, 6)
        ck("setup: A has >= 6 free pockets (%s)" % fr, len(fr) >= 6)
        if len(fr) < 6:
            return
        vals = {}
        for it in (FISH, INSECT, APPLE, FTR, st_item):
            s = fr[0]
            _rid, _s, r, _m = sell(a, {s: it}, place(a, {s: it}, wallet=0))
            run.tx_ok("V single sale of 0x%04X" % it, r, APPLIED, R["NONE"])
            vals[it] = r.post_wallet if r is not None else None
        ck("V the host values: %s (the stock item 0x%04X == its price / 4 = %d)" % ({hex(k): v for k, v in vals.items()}, st_item, st_price // 4),
           all(v is not None for v in vals.values()) and vals[st_item] == st_price // 4 and vals[FISH] > 0 and vals[INSECT] > 0)
        if any(v is None for v in vals.values()):
            return

        # ---------------- M1: one mixed sale
        mix = {fr[0]: FISH, fr[1]: INSECT, fr[2]: APPLE, fr[3]: FTR, fr[4]: st_item}
        total = sum(vals[it] for it in mix.values())
        pre = place(a, mix, wallet=1000)
        base = a.rec_last
        cur = a.ts_latest(SH.SVC_SHOP)
        off = len(run.log())
        rid, sent, r, mask = sell(a, mix, pre)
        run.tx_ok("M1 ONE SHOP_SELL of five MIXED items (fish, insect, apple, furniture, stock item)", r, APPLIED, R["NONE"])
        ck("M1 value == the sum of the single host values (%d): wallet 1000 -> %d; the five slots EMPTY, every other pocket / condition unchanged, rev == base + 1" % (total, 1000 + total),
           r is not None and r.post_wallet == 1000 + total and tuple(r.post_pockets) == tuple(0 if i in mix else p for i, p in enumerate(pre[0]))
           and r.post_conds == pre[1] and r.rev == base[2] + 1)
        ck("M1 host: exactly one committed SHOP_SELL line with mask 0x%04X value %d" % (mask, total),
           run.n_log(r"SHOP_SELL mask=0x%04X value=%d wallet 1000 -> %d .* committed \[TXN\]" % (mask, total, 1000 + total), off) == 1
           and run.n_log(r"TXN APPLIED kind=SHOP_SELL request=%d " % rid, off) == 1)
        exp_sales = SH.plus_sales(cur[1], total // 2) if cur else None
        if cur is not None and exp_sales != SH.shop_sales(cur[1]):
            stn = SH.wait_shop(a, cur[0].seq + 1, 3.0)
            ck("M1 sales_sum rose by value / 2 = %d through vanilla mSP_PlusSales (the shop stock unchanged)" % (total // 2),
               stn is not None and SH.shop_sales(stn[1]) == exp_sales and SH.shop_items(stn[1]) == SH.shop_items(cur[1]))
        else:
            ck("M1 sales_sum at its level cap (no state change)", True)

        # ---------------- M5: replay
        n_ts = TS.ts_count(a, SH.SVC_SHOP)
        a.resend_txn(sent)
        rep = a.wait_txn_result(sent.seq, 3.0, nth=2)
        L.pump_sleep(0.6)
        ck("M5 the identical resend replays APPLIED/REPLAYED with the same post-image; executed ONCE",
           rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and r is not None and tuple(rep.post_pockets) == tuple(r.post_pockets)
           and run.n_log(r"TXN APPLIED kind=SHOP_SELL request=%d " % rid, off) == 1 and TS.ts_count(a, SH.SVC_SHOP) == n_ts)

        # ---------------- M2: one unsellable item refuses the whole basket
        for label, extra_item, cond in (("a money bag", MONEY_1000, 0), ("a wrapped present", APPLE, COND_PRESENT), ("an exercise card", CARD, 0),
                                        ("a Wisp spirit", WISP, 0), ("a delivery (quest item)", INSECT, COND_QUEST)):
            m2 = {fr[0]: FISH, fr[1]: INSECT, fr[2]: APPLE, fr[5]: extra_item}
            pre2 = place(a, m2, conds_over={fr[5]: cond}, wallet=500)
            rev0, off2 = a.rec_last[2], len(run.log())
            rid2, _s2, r2, _m2 = sell(a, m2, pre2)
            run.tx_ok("M2 a basket with %s (0x%04X cond %d) -> the WHOLE sale NOT_SELLABLE" % (label, extra_item, cond), r2, REJECTED, R["NOT_SELLABLE"])
            L.pump_sleep(0.3)
            ck("M2 ... nothing changed (%s): no applied / committed line, rev unchanged, no post-image" % label,
               run.n_log(r"TXN APPLIED kind=SHOP_SELL request=%d " % rid2, off2) == 0 and run.n_log(r"SHOP_SELL mask=.* committed", off2) == 0 and a.rec_last[2] == rev0
               and r2 is not None and r2.post_wallet == 0 and all(x == 0 for x in r2.post_pockets))

        # ---------------- M3: stale / wrong pre-images
        m3 = {fr[0]: FISH, fr[1]: APPLE}
        _r, _s, r3a, _m = sell(a, m3, place(a, m3, wallet=10), base=(a.rec_last[1], max(0, a.rec_last[2] - 2)))
        run.tx_ok("M3 base rev older than the last pocket transaction -> STALE_IMAGE", r3a, REJECTED, R["STALE_IMAGE"])
        pre3 = place(a, m3, wallet=10)
        rid3 = run.fresh_rid()
        _sent3, r3b = a.txn_shop_sell((1 << fr[0]) | (1 << fr[1]), fr[0], INSECT, rid=rid3, timeout=3.0, pre=pre3)
        run.tx_ok("M3 the primary slot does not hold the offered item (the pockets changed) -> PRECOND", r3b, REJECTED, R["PRECOND"])
        pre3c = (tuple(0 if i == fr[1] else p for i, p in enumerate(pre3[0])), pre3[1], 10)
        _sent3c, r3c = a.txn_shop_sell((1 << fr[0]) | (1 << fr[1]), fr[0], FISH, rid=run.fresh_rid(), timeout=3.0, pre=pre3c)
        run.tx_ok("M3 a masked slot is EMPTY in the pre-image -> PRECOND", r3c, REJECTED, R["PRECOND"])

        # ---------------- M4: wallet cap
        m4 = {fr[0]: FISH, fr[1]: INSECT, fr[2]: st_item}
        v4 = vals[FISH] + vals[INSECT] + vals[st_item]
        w4 = WALLET_MAX - 1
        bells = w4 + v4
        _r, _s, r4, _m = sell(a, m4, place(a, m4, wallet=w4))
        run.tx_ok("M4 a mixed sale over the wallet cap (%d + %d = %d)" % (w4, v4, bells), r4, APPLIED, R["NONE"])
        ck("M4 vanilla overflow: a 30,000 bag in the LOWEST sold slot (%d), the other sold slots empty, wallet %d" % (fr[0], bells - 30000),
           r4 is not None and bells < WALLET_MAX + 30000 and r4.post_pockets[fr[0]] == ITM_MONEY_30000 and r4.post_pockets[fr[1]] == 0 and r4.post_pockets[fr[2]] == 0
           and r4.post_wallet == bells - 30000)

        # ---------------- M6: two clients at once
        fb = SH.free_slots(b, 3)
        ma = {fr[0]: FISH, fr[1]: APPLE}
        mb = {fb[0]: INSECT, fb[1]: FTR, fb[2]: st_item}
        rida, ridb = run.fresh_rid(), run.fresh_rid()
        sa_ = a.send_txn_commit(L.PC_NETGAME_TXN_KIND_SHOP_SELL, rida, L.PC_NETGAME_TXN_DEST_NONE, fr[0], FISH, pre=place(a, ma, wallet=7), aux_item=(1 << fr[0]) | (1 << fr[1]))
        sb_ = b.send_txn_commit(L.PC_NETGAME_TXN_KIND_SHOP_SELL, ridb, L.PC_NETGAME_TXN_DEST_NONE, fb[0], INSECT, pre=place(b, mb, wallet=9),
                                aux_item=(1 << fb[0]) | (1 << fb[1]) | (1 << fb[2]))
        ra, rb = a.wait_txn_result(sa_.seq, 3.0), b.wait_txn_result(sb_.seq, 3.0)
        ck("M6 two clients' mixed baskets at the same time: both APPLIED with their own values (A %s, B %s)" % (ra and ra.post_wallet, rb and rb.post_wallet),
           ra is not None and rb is not None and ra.outcome == APPLIED and rb.outcome == APPLIED and ra.post_wallet == 7 + vals[FISH] + vals[APPLE]
           and rb.post_wallet == 9 + vals[INSECT] + vals[FTR] + vals[st_item])

        # ---------------- M7: a guest
        g = run.guest("G", TG.gid(60))
        try:
            g.connect_and_ready(quiet=True)
        except Exception as e:  # noqa: BLE001
            ck("M7 guest joined (%r)" % e, False)
            g = None
        if g is not None:
            fg = SH.free_slots(g, 3)
            mg = {fg[0]: FISH, fg[1]: INSECT, fg[2]: APPLE}
            _r, _s, rg, _m = sell(g, mg, place(g, mg, wallet=0))
            run.tx_ok("M7 a GUEST sells a mixed basket (guests are full shop participants)", rg, APPLIED, R["NONE"])
            ck("M7 ... with the same host values (%d)" % (vals[FISH] + vals[INSECT] + vals[APPLE]),
               rg is not None and rg.post_wallet == vals[FISH] + vals[INSECT] + vals[APPLE] and all(rg.post_pockets[s] == 0 for s in mg))
        ck("host alive, no INTERNAL error / violation", run.host.alive() and "*** INTERNAL" not in run.log() and not re.search(r"\[NET\]\[REC\] host: peer \d+ violation|protocol violation", run.log()))

    def phase(results_, ip_, port, tag, extra, body_, snap, rest):
        host_slot = L.TEST_HOST_RESIDENT
        residents = [i for i, _p, ex in L.read_test_save_residents() if ex and i != host_slot]
        if len(residents) < 3:
            L.check("[%s] fixture has >= 3 non-host residents (%s)" % (tag, residents), False, results_)
            return
        host, ok = TP.start_host(ip_, port, "sellb" + tag, list(extra), results_)
        run = TG.GRun(ip_, port, host, results_, snap, host_slot, residents[:3])
        try:
            if ok:
                body_(run)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            L.check("[%s] phase raised %r" % (tag, exc), False, results_)
        finally:
            for cl in run.clients:
                try:
                    cl.close()
                except Exception:  # noqa: BLE001
                    pass
            rc = host.stop()
            L.check("[%s] host did not crash (exit code before stop: %s)" % (tag, rc), rc is None, results_)
            rest()

    try:
        phase(results, ip, args.port, "M", [], body, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
