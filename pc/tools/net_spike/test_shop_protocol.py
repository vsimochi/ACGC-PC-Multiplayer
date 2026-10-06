#!/usr/bin/env python3
"""test_shop_protocol.py - Town Services milestone 2 (NOOK'S SHOP), HOST half: the shop MIRROR (TOWN_SVC_STATE service 3 = the 320 raw bytes of
Save_t.shop) and the host-authoritative SHOP_BUY (TXN_COMMIT kind 10) / SHOP_SELL (kind 11) transactions.

PROTOCOL test against REAL host processes (`AnimalCrossing.exe --host --bootstrap-resident 0 [--txn-fault=...]`, started and stopped by this script from
the TEST COPY named by NET_SPIKE_GAME_BIN = pc\\build64\\bin_fixture4; host = resident 0, clients = residents 1..3, at most 3 simultaneous clients, ports
11100+). `require_test_bin_dir()` refuses the live dirs; the fixture save dir is SNAPSHOTTED at start and RESTORED after every host phase. Clients are
scripted FakeClients (net_spike_lib: txn_shop_buy / txn_shop_sell, TXN_RESULT / TOWN_SVC_STATE parsing); the host's C code runs for real. The stock and
the prices come from the host itself: the shop mirror blob (items[39], rare item, sales_sum, level) and the "[NET][SHOP] host: stock[i]=... price=P" log
lines the host prints whenever its shop state changes (so the test never re-implements the price tables). The real client half is covered by
test_shop_real_client.py (one purchase + one sale through the real client request path) and the source audit test_shop_src.py.

Phases (one host process each):
  M       mirror delivery at READY (blob == the host's logged digest, identical for two clients, exactly one push, stock == the logged stock),
          SHOP_BUY: success (stock slot marked SOLD with the vanilla marker, sales_sum + price, wallet debited by the HOST price, item in the mirror pocket,
          mirror push to a second client, svc_seq16 echo, RESULT before the mirror), replay x3 / CONFLICT, sold-out loser keeps wallet and slot, concurrent
          buy of the same unique item (one winner), NO_FUNDS, PRICE_MISMATCH (a cheap price never wins), money-bag payment (vanilla mSP_get_sell_price),
          pockets full, invalid stock code / item / paint, stationery (unlimited), BAD_IMAGE / STALE_IMAGE, BAD_SHAPE probes, SHOP_SELL: success (credit, sales_sum +
          value / 2, slot emptied, the shop stock does NOT change), several slots, unsellable (stationery / quest), free disposal of a worthless item,
          the 30000-bag overflow, empty slot, HANDSHAKE / parked / host-own peers ignored, a NEW session gets the final shop state
  F-drop  drop_result:1:1   the APPLIED RESULT of a purchase is lost but the mirror still arrives; the resend replays, executed once
  F-kill  kill_peer_after_commit:1:1   a purchase commits, the peer is dropped without a RESULT; the other client sees the new stock; a same-nonce
          reconnect replays; a NEW-PROCESS session sees the post-state

Tier: PROTOCOL TESTED (real host binary, scripted clients). Usage: python test_shop_protocol.py [--port 11100] [--only M,F-drop,F-kill]
"""
import argparse
import os
import re
import shutil
import struct
import sys

import net_spike_lib as L
import test_txn_protocol as TP
import test_ts_protocol as TS
from test_txn_protocol import APPLIED, REJECTED, R, D_NONE, D_POCKET, first_free

K_BUY, K_SELL = L.PC_NETGAME_TXN_KIND_SHOP_BUY, L.PC_NETGAME_TXN_KIND_SHOP_SELL
SVC_SHOP = L.PC_NETGAME_TS_SHOP
SOLD_LO, SOLD_HI = 0xFE0E, 0xFE18                 # RSV_SHOP_SOLD_PAPER .. RSV_SHOP_SOLD_SIGNBOARD
ITM_MONEY_30000, ITM_MONEY_100, ITM_MONEY_1000, ITM_MONEY_10000 = 0x2102, 0x2103, 0x2100, 0x2101
ITM_KABU_SPOILED, ITM_PAPER0 = 0x2F03, 0x2000
PAINT_LO, PAINT_HI = 0x222D, 0x2238
APPLE = 0x2800
WALLET_MAX = 99999
COND_QUEST = 2


# ----------------------------------------------------------------------------------------------------------------------
# Shop_c (PC little-endian image, 320 B): items at 0xCE (39 x u16), rare at 0x11C, lottery 0x11E (3), bag count s8 0x124, shop_info u16 0x126
# (bits 0..1 = level), sales_sum u32 0x128
# ----------------------------------------------------------------------------------------------------------------------
def shop_items(blob):
    return list(struct.unpack_from("<39H", blob, 0xCE))


def shop_rare(blob):
    return struct.unpack_from("<H", blob, 0x11C)[0]


def shop_level(blob):
    return struct.unpack_from("<H", blob, 0x126)[0] & 3


def shop_sales(blob):
    return struct.unpack_from("<I", blob, 0x128)[0]


def plus_sales(blob, amount):
    """The vanilla mSP_PlusSales result: sum + amount, clamped to the NEXT level's threshold unless the shop is already level 3."""
    s = shop_sales(blob) + amount
    cap = {0: 25000, 1: 90000, 2: 240000}.get(shop_level(blob))
    return s if cap is None else min(s, cap)


def stock_log(run):
    """{index: (item, class, price)} of the host's LATEST '[NET][SHOP] host: stock[i]=... price=P' lines (255 = the rare item)."""
    out = {}
    for m in re.finditer(r"\[NET\]\[SHOP\] host: stock\[(\d+)\]=0x([0-9A-F]{4}) class=(\d) price=(\d+)", run.log()):
        out[int(m.group(1))] = (int(m.group(2), 16), int(m.group(3)), int(m.group(4)))
    return out


def stationery_price(run):
    ms = re.findall(r"probe stationery 0x([0-9A-F]{4}) price=(\d+)", run.log())
    return int(ms[-1][1]) if ms else None


def listed(run, blob, n=None):
    """The buyable LISTED stock entries (index, item, price) of the CURRENT blob, cheapest first: stock slots only (not the rare item / counted items /
    paint), price <= 40000."""
    st = stock_log(run)
    items = shop_items(blob)
    out = [(i, items[i], st[i][2]) for i in range(39) if i in st and st[i][0] == items[i] and st[i][1] == 1 and 0 < st[i][2] <= 40000
           and not (PAINT_LO <= items[i] <= PAINT_HI)]
    out.sort(key=lambda t: (t[2], t[0]))
    return out if n is None else out[:n]


def marker_ok(item, marker):
    """The vanilla RSV_SHOP_SOLD_* marker mSP_ShopSaleReport leaves for `item`'s category (aSD_ReportGoodsSales' rsv_item)."""
    t = item >> 12
    if t in (1, 3):
        return marker in (0xFE10, 0xFE15)       # furniture: SOLD_FTR (SOLD_RARE only for the rare slot)
    if 0x2400 <= item < 0x2500:
        return marker == 0xFE0F                 # clothes
    if 0x2600 <= item < 0x2700:
        return marker == 0xFE11                 # carpet
    if 0x2700 <= item < 0x2800:
        return marker == 0xFE12                 # wallpaper
    if 0x2B00 <= item < 0x2C00:
        return marker == 0xFE0E                 # diaries
    if 0x2900 <= item < 0x2A00:
        return marker == 0xFE13                 # saplings / flower bags
    if 0x2200 <= item < 0x2300:
        return marker in (0xFE14, 0xFE16, 0xFE17, 0xFE18)   # tools / umbrellas
    return SOLD_LO <= marker <= SOLD_HI


def pre_free(c, slot, wallet, pockets=None, conds=None):
    """A pre-image with `slot` EMPTY (the client's local pockets otherwise) and the given wallet."""
    p, cc, _w = c.txn_pre_image()
    p = list(pockets if pockets is not None else p)
    p[slot] = 0
    return (tuple(p), cc if conds is None else conds, wallet)


def buy(run, c, slot, item, code, price, wallet, wait=3.0, pre=None, **kw):
    rid = run.fresh_rid()
    sent, r = c.txn_shop_buy(slot, item, code, price, rid=rid, timeout=wait, pre=pre if pre is not None else pre_free(c, slot, wallet), **kw)
    return rid, sent, r


def sell(run, c, mask, slot, item, pre, wait=3.0, **kw):
    rid = run.fresh_rid()
    sent, r = c.txn_shop_sell(mask, slot, item, rid=rid, timeout=wait, pre=pre, **kw)
    return rid, sent, r


def free_slots(c, n):
    p = c.txn_pre_image()[0]
    out = [i for i, x in enumerate(p) if x == 0]
    return out[:n]


def pay_ref(pockets, conds, wallet, amount):
    """Python reference of vanilla mSP_money_check + mSP_get_sell_price (wallet first, then bags 100 / 1000 / 10000 / 30000, lowest slot first)."""
    bags = [(ITM_MONEY_100, 100), (ITM_MONEY_1000, 1000), (ITM_MONEY_10000, 10000), (ITM_MONEY_30000, 30000)]
    p = list(pockets)
    if wallet >= amount:
        return p, wallet - amount
    m = wallet
    for it, v in bags:
        m += sum(v for i, x in enumerate(p) if x == it and ((conds >> (2 * i)) & 3) == 0)
        if m >= amount:
            break
    else:
        return None
    m = wallet
    for it, v in bags:
        for i in range(15):
            if p[i] == it and ((conds >> (2 * i)) & 3) == 0:
                p[i] = 0
                m += v
                if m >= amount:
                    return p, m - amount
    return None


def wait_shop(c, min_seq, timeout=3.0):
    return c.wait_ts_state(SVC_SHOP, timeout, min_seq=min_seq)


# ----------------------------------------------------------------------------------------------------------------------
# Phase M
# ----------------------------------------------------------------------------------------------------------------------
def m_mirror_at_ready(run, a, b):
    ck = run.check
    sa, sb = wait_shop(a, 1, 6.0), wait_shop(b, 1, 6.0)
    ck("SH1 both clients received the SHOP service (3) right after READY", sa is not None and sb is not None)
    if sa is None or sb is None:
        return None
    g, blob = sa
    ck("SH1 len 320, seq >= 1, digest == FNV-1a32 of the blob, and == the digest the HOST logged for that seq (the blob IS the host's Save_t.shop)",
       g.len == L.PC_NETGAME_TS_SHOP_LEN and len(blob) == 320 and g.seq >= 1 and g.digest == L.fnv1a32(blob) and TS.host_digests(run, SVC_SHOP, "SHOP").get(g.seq) == g.digest)
    ck("SH1 both clients hold the IDENTICAL blob and seq", sb[1] == blob and sb[0].seq == g.seq)
    m = re.findall(r"\[NET\]\[TS\] host: shop level (\d+) sales_sum (\d+) bag_count (-?\d+) rare 0x([0-9A-F]{4})", run.log())
    ck("SH1 the host's own log of level / sales_sum / rare item equals what the blob parses to (layout of Shop_c: items 0xCE, rare 0x11C, level 0x126, sales_sum 0x128)",
       bool(m) and (int(m[-1][0]), int(m[-1][1]), int(m[-1][3], 16)) == (shop_level(blob), shop_sales(blob), shop_rare(blob)))
    st = stock_log(run)
    items = shop_items(blob)
    ck("SH1 every stock entry the host logged (with its host-computed price) is exactly the blob's items[i]; the stock is not empty",
       len(st) >= 5 and all(it == items[i] for i, (it, _c, _p) in st.items() if i < 39) and len([1 for x in items if x]) >= 5)
    ck("SH1 the host's own shop state passes the client validator (no 'would be REFUSED' warning)", "would be REFUSED by clients" not in run.log())
    L.pump_sleep(2.5)
    ck("SH2 no spam: exactly ONE TOWN_SVC_STATE of the shop per client after 2.5 s idle (the digest poll sends only on a change)",
       TS.ts_count(a, SVC_SHOP) == 1 and TS.ts_count(b, SVC_SHOP) == 1)
    return sa


def m_buy(run, a, b, sa):
    ck = run.check
    g0, blob0 = sa
    cand = listed(run, blob0)
    ck("B0 setup: the stock offers at least 6 buyable LISTED items (indices %s)" % [t[0] for t in cand[:6]], len(cand) >= 6)
    if len(cand) < 6:
        return None
    (i1, it1, p1), (i2, it2, p2), (i3, it3, p3), (i4, it4, p4), (i5, it5, p5), (i6, it6, p6) = cand[:6]
    s_a = free_slots(a, 6)
    base = a.rec_last
    pre = pre_free(a, s_a[0], p1 + 777)
    mark_a, off = a.inbox.mark(), len(run.log())
    rid, sent, r = buy(run, a, s_a[0], it1, i1, p1, p1 + 777, pre=pre)
    run.tx_ok("B1 SHOP_BUY of stock[%d]=0x%04X at the host price %d" % (i1, it1, p1), r, APPLIED, R["NONE"])
    if r is None:
        return None
    exp_post = tuple(it1 if i == s_a[0] else p for i, p in enumerate(pre[0]))
    ck("B1 RESULT echoes kind 10 / request / nonce / seq, dest POCKET, the slot and the item; rev == base + 1",
       (r.kind, r.request_id, r.txn_nonce, r.txn_seq, r.dest, r.slot, r.item) == (K_BUY, rid, sent.nonce, sent.seq, D_POCKET, s_a[0], it1)
       and (r.host_session, r.epoch, r.rev) == (base[0], base[1], base[2] + 1))
    ck("B1 post-image: the item is in the chosen pocket slot and the wallet is the pre-image wallet minus the HOST price (%d - %d = 777); conds / other pockets unchanged" % (p1 + 777, p1),
       tuple(r.post_pockets) == exp_post and r.post_wallet == 777 and r.post_conds == pre[1] and r.cdig == TP.cown_digest(a.rec_local))
    stA, stB = wait_shop(a, g0.seq + 1), wait_shop(b, g0.seq + 1)
    ck("B1 mirror: BOTH clients receive shop seq + 1 after the commit", stA is not None and stB is not None and stA[0].seq == g0.seq + 1 and stA[1] == stB[1])
    if stA is None:
        return None
    blob1 = stA[1]
    items1 = shop_items(blob1)
    ck("B1 the stock slot is marked SOLD with the vanilla marker of its category (0x%04X) and every OTHER stock entry / the rare item are unchanged" % items1[i1],
       items1[i1] != it1 and marker_ok(it1, items1[i1]) and all(items1[i] == shop_items(blob0)[i] for i in range(39) if i != i1) and shop_rare(blob1) == shop_rare(blob0))
    ck("B1 sales_sum rose by the HOST price through the vanilla mSP_PlusSales (%d -> %d, capped at the next level's threshold)" % (shop_sales(blob0), shop_sales(blob1)),
       shop_sales(blob1) == plus_sales(blob0, p1))
    ck("B1 svc_seq16 of the RESULT == the low 16 bits of the shop seq the commit produced; the digest matches the host log",
       r.svc_seq16 == (stA[0].seq & 0xFFFF) and TS.host_digests(run, SVC_SHOP, "SHOP").get(stA[0].seq) == stA[0].digest)
    ti, ts = TS.txn_idx(a, sent.seq, mark_a), TS.ts_indices(a, SVC_SHOP, mark_a)
    ck("B1 ordering: the TXN_RESULT reaches the requester BEFORE its TOWN_SVC_STATE", len(ti) == 1 and len(ts) == 1 and ti[0] < ts[0])
    ck("B1 host log: exactly one 'TXN APPLIED kind=SHOP_BUY' and one committed line with the host price and the wallet change",
       run.n_log(r"TXN APPLIED kind=SHOP_BUY request=%d " % rid, off) == 1
       and run.n_log(r"SHOP_BUY item=0x%04X stock=0x%02X pocket slot=%d price=%d wallet %d -> 777 .* committed \[TXN\]" % (it1, i1, s_a[0], p1, p1 + 777), off) == 1)

    # ---- replay / CONFLICT ----
    n_ts, off2 = TS.ts_count(a, SVC_SHOP), len(run.log())
    a.resend_txn(sent, copies=3)
    a.resend_txn(sent)
    got = a.wait_txn_result(sent.seq, 3.0, nth=3)
    allr = a.txn_results_for(sent.seq)
    ck("B2 three identical requests get three answers: APPLIED/NONE then two APPLIED/REPLAYED, all with the same svc_seq16 and post-image",
       got is not None and len(allr) == 3 and allr[0].reason == R["NONE"] and [x.reason for x in allr[1:]] == [R["REPLAYED"]] * 2
       and all(x.outcome == APPLIED and x.svc_seq16 == r.svc_seq16 and tuple(x.post_pockets) == tuple(r.post_pockets) and x.post_wallet == 777 for x in allr))
    L.pump_sleep(0.8)
    ck("B2 NO second execution: no new applied line, no new shop state, the wallet / sales_sum are unchanged",
       run.n_log(r"TXN APPLIED kind=SHOP_BUY request=%d " % rid, off2) == 0 and TS.ts_count(a, SVC_SHOP) == n_ts and TS.ts_count(b, SVC_SHOP) == n_ts
       and run.n_log(r"service 3 \(SHOP\) state ->", off2) == 0)
    bad = bytearray(sent.raw)
    bad[21] ^= 1  # tag.aux_cond (stock code, byte 21 of the 72-byte COMMIT): same (nonce, seq), different bytes
    a.send_txn_commit(K_BUY, rid, D_POCKET, s_a[0], it1, raw=bytes(bad))
    rc = a.wait_txn_result(sent.seq, 2.5, nth=4)
    ck("B2 same (nonce, seq) with DIFFERENT bytes -> REJECTED(CONFLICT), never executed", rc is not None and rc.outcome == REJECTED and rc.reason == R["CONFLICT"])

    # ---- sold out: B wants the item A just bought ----
    n_ts, off3, b_rev = TS.ts_count(b, SVC_SHOP), len(run.log()), b.rec_last[2]
    rid_b, sent_b, rb = buy(run, b, free_slots(b, 1)[0], it1, i1, p1, 5000)
    run.tx_ok("B3 B buys the item that is already SOLD (stale index / sold out)", rb, REJECTED, R["NOT_AVAILABLE"])
    L.pump_sleep(0.6)
    ck("B3 nothing changed: no applied line, no shop state, B's mirror rev unchanged, the RESULT carries NO post-image (B keeps wallet and slot)",
       run.n_log(r"TXN APPLIED kind=SHOP_BUY request=%d " % rid_b, off3) == 0 and TS.ts_count(b, SVC_SHOP) == n_ts and b.rec_last[2] == b_rev
       and rb is not None and all(p == 0 for p in rb.post_pockets) and rb.post_wallet == 0 and rb.rev == b_rev)

    # ---- concurrent purchase of the same unique item ----
    rid_a2, rid_b2 = run.fresh_rid(), run.fresh_rid()
    sa2 = a.send_txn_commit(K_BUY, rid_a2, D_POCKET, s_a[1], it2, pre=pre_free(a, s_a[1], p2 + 10), aux_cond=i2, aux_item=p2)
    sb2 = b.send_txn_commit(K_BUY, rid_b2, D_POCKET, free_slots(b, 1)[0], it2, pre=pre_free(b, free_slots(b, 1)[0], p2 + 20), aux_cond=i2, aux_item=p2)
    ra2, rb2 = a.wait_txn_result(sa2.seq, 3.0), b.wait_txn_result(sb2.seq, 3.0)
    ck("B4 concurrent purchase of the same unique item: exactly ONE APPLIED and the other REJECTED(NOT_AVAILABLE)",
       ra2 is not None and rb2 is not None and sorted([ra2.outcome, rb2.outcome]) == [APPLIED, REJECTED]
       and [x.reason for x in (ra2, rb2) if x.outcome == REJECTED] == [R["NOT_AVAILABLE"]])
    loser = rb2 if ra2 is not None and ra2.outcome == APPLIED else ra2
    ck("B4 the loser's RESULT has no post-image (wallet and slot unchanged); the item was sold exactly once",
       loser is not None and all(p == 0 for p in loser.post_pockets) and loser.post_wallet == 0
       and run.n_log(r"SHOP_BUY item=0x%04X stock=0x%02X .* committed \[TXN\]" % (it2, i2)) == 1)
    L.pump_sleep(0.8)
    cur = a.ts_latest(SVC_SHOP)[1]
    ck("B4 the winner's price was charged once (sales_sum %d)" % shop_sales(cur), marker_ok(it2, shop_items(cur)[i2]))

    # ---- funds, price, bags, full pockets ----
    n_ts, off4 = TS.ts_count(a, SVC_SHOP), len(run.log())
    _rid, _s, r_nf = buy(run, a, s_a[2], it3, i3, p3, p3 - 1)
    run.tx_ok("B5 the pre-image wallet is ONE bell short (%d < %d) -> NO_FUNDS" % (p3 - 1, p3), r_nf, REJECTED, R["NO_FUNDS"])
    _rid, _s, r_cheap = buy(run, a, s_a[2], it3, i3, max(1, p3 - 1), p3 + 5000)
    run.tx_ok("B5 PRICE TAMPER: the client expects a price one bell lower than the host's -> PRICE_MISMATCH (the cheap price never wins)", r_cheap, REJECTED, R["PRICE_MISMATCH"])
    _rid, _s, r_dear = buy(run, a, s_a[2], it3, i3, p3 + 1, p3 + 5000)
    run.tx_ok("B5 a HIGHER expected price is refused as well (the host price is the only price)", r_dear, REJECTED, R["PRICE_MISMATCH"])
    L.pump_sleep(0.6)
    ck("B5 none of the three changed the shop (no state, no applied line)", TS.ts_count(a, SVC_SHOP) == n_ts and run.n_log(r"TXN APPLIED kind=SHOP_BUY", off4) == 0
       and shop_items(a.ts_latest(SVC_SHOP)[1])[i3] == it3)
    rid_ok, _s, r_ok = buy(run, a, s_a[2], it3, i3, p3, p3)
    run.tx_ok("B5 ... and the exact wallet (== the price) succeeds", r_ok, APPLIED, R["NONE"])
    ck("B5 the wallet is exactly 0 after paying the HOST price %d" % p3, r_ok is not None and r_ok.post_wallet == 0)

    # money bags: wallet 0, a 30000 bag in one slot, pay p4 with the bag: change goes to the wallet
    k_bag, s_item = free_slots(a, 2)
    pk = list(a.txn_pre_image()[0])
    pk[k_bag] = ITM_MONEY_30000
    pre_b = (tuple(pk[:s_item] + [0] + pk[s_item + 1:]), a.txn_pre_image()[1], 0)
    exp = pay_ref(pre_b[0], pre_b[1], 0, p4)
    _rid, _s, r_bag = buy(run, a, s_item, it4, i4, p4, 0, pre=pre_b)
    run.tx_ok("B6 an empty wallet pays with a 30000 money bag (vanilla mSP_get_sell_price)", r_bag, APPLIED, R["NONE"])
    ck("B6 post-image: the bag is consumed (slot %d empty), the change %d is in the wallet, the item is in slot %d (python reference of the vanilla payment)" % (k_bag, 30000 - p4, s_item),
       r_bag is not None and exp is not None and exp[1] == 30000 - p4 and r_bag.post_wallet == exp[1]
       and tuple(r_bag.post_pockets) == tuple(it4 if i == s_item else exp[0][i] for i in range(15)))
    pk2 = list(a.txn_pre_image()[0])
    pk2[k_bag] = ITM_MONEY_100
    _rid, _s, r_bag2 = buy(run, a, s_item, it5, i5, p5, 0, pre=(tuple(pk2[:s_item] + [0] + pk2[s_item + 1:]), a.txn_pre_image()[1], 0))
    run.tx_ok("B6 a single 100 bag cannot pay stock[%d] (%d bells) -> NO_FUNDS" % (i5, p5), r_bag2, REJECTED, R["NO_FUNDS"]) if p5 > 100 else ck("B6 (skipped: the item costs <= 100)", True)

    full = tuple(APPLE for _ in range(15))
    _rid, _s, r_full = buy(run, a, 0, it5, i5, p5, p5 + 100, pre=(full, 0, p5 + 100))
    run.tx_ok("B7 FULL pockets (the chosen slot is occupied in the pre-image) -> PRECOND", r_full, REJECTED, R["PRECOND"])

    # ---- invalid slot / item ----
    _rid, _s, r_wrongcode = buy(run, a, s_a[3], it5, (i5 + 1) % 39, p5, p5 + 100)
    run.tx_ok("B8 a stock code that does not hold the item -> NOT_AVAILABLE", r_wrongcode, REJECTED, R["NOT_AVAILABLE"])
    _rid, _s, r_notstock = buy(run, a, s_a[3], 0x2305, 0, 100, 5000)
    run.tx_ok("B8 an item that is not in the shop at all (a fish) -> NOT_AVAILABLE", r_notstock, REJECTED, R["NOT_AVAILABLE"])
    _rid, _s, r_paint = buy(run, a, s_a[3], PAINT_LO, 0, 100, 5000)
    run.tx_ok("B8 paint is a house-service purchase: refused (deferred) -> NOT_AVAILABLE", r_paint, REJECTED, R["NOT_AVAILABLE"])
    _rid, _s, r_papercode = buy(run, a, s_a[3], ITM_PAPER0, i5, 100, 5000)
    run.tx_ok("B8 stationery with a LISTED stock code (instead of 0xFF) -> NOT_AVAILABLE", r_papercode, REJECTED, R["NOT_AVAILABLE"])

    # ---- stationery (unlimited) ----
    st_all = stock_log(run)
    paper = next(((i, v[0], v[2]) for i, v in sorted(st_all.items()) if i < 39 and v[1] == 3 and shop_items(a.ts_latest(SVC_SHOP)[1])[i] == v[0]), None)
    ck("B9 the shop stocks a stationery item (class 3) in items[]", paper is not None)
    if shop_items(a.ts_latest(SVC_SHOP)[1]).count(ITM_PAPER0) == 0:
        _rid, _s, r_np = buy(run, a, s_a[3], ITM_PAPER0, L.PC_NETGAME_SHOP_STOCK_UNLIMITED, stationery_price(run) or 1, 5000)
        run.tx_ok("B9 L1: stationery the shop does NOT stock (0x2000 is not in items[]) is refused even with the unlimited code 0xFF", r_np, REJECTED, R["NOT_AVAILABLE"])
    else:
        ck("B9 (0x2000 happens to be stocked: the not-stocked case is not reachable)", True)
    pp = paper[2] if paper else None
    cur_st = a.ts_latest(SVC_SHOP)
    ck("B9 the stocked stationery has a host price > 0 (%s)" % pp, bool(pp))
    if pp:
        _rid, _s, r_pp = buy(run, a, s_a[3], paper[1], L.PC_NETGAME_SHOP_STOCK_UNLIMITED, pp, pp + 50)
        run.tx_ok("B9 stationery (unlimited, stock code 0xFF) at the host price %d" % pp, r_pp, APPLIED, R["NONE"])
        exp_sales = plus_sales(cur_st[1], pp)
        if exp_sales != shop_sales(cur_st[1]):
            stp = wait_shop(a, cur_st[0].seq + 1, 3.0)
            ck("B9 the stock is unchanged (stationery never runs out) and sales_sum rose by the price (%d -> %d)" % (shop_sales(cur_st[1]), exp_sales),
               stp is not None and shop_items(stp[1]) == shop_items(cur_st[1]) and shop_sales(stp[1]) == exp_sales)
        else:
            ck("B9 sales_sum is at the level cap: the shop state does not change", a.ts_latest(SVC_SHOP)[0].seq == cur_st[0].seq)
        ck("B9 the post-image: wallet - price (50 left), the stationery in the slot", r_pp is not None and r_pp.post_wallet == 50 and r_pp.post_pockets[s_a[3]] == paper[1])

    # ---- bad / stale images ----
    pa = a.txn_pre_image()
    _rid, _s, r_bi = buy(run, a, s_a[3], it5, i5, p5, 0, pre=(pre_free(a, s_a[3], 0)[0], pa[1], 0xFFFFFFFF))
    run.tx_ok("B10 an impossible wallet in the pre-image -> BAD_IMAGE", r_bi, REJECTED, R["BAD_IMAGE"])
    _rid, _s, r_st = buy(run, a, s_a[3], it5, i5, p5, p5 + 1, base=(a.rec_last[1], max(0, a.rec_last[2] - 3)))
    run.tx_ok("B10 a base rev older than this resident's last pocket transaction -> STALE_IMAGE", r_st, REJECTED, R["STALE_IMAGE"])
    _rid, _s, r_st2 = buy(run, a, s_a[3], it5, i5, p5, p5 + 1, base=(a.rec_last[1] ^ 0x55, a.rec_last[2]))
    run.tx_ok("B10 a base from another lineage (epoch) -> STALE_IMAGE", r_st2, REJECTED, R["STALE_IMAGE"])
    L.pump_sleep(0.5)
    ck("B10 item stock[%d] (0x%04X) is still in stock after every rejection above" % (i5, it5), shop_items(a.ts_latest(SVC_SHOP)[1])[i5] == it5)
    return dict(i=(i1, i2, i3, i4, i5, i6), it=(it1, it2, it3, it4, it5, it6), p=(p1, p2, p3, p4, p5, p6))


def m_sell(run, a, b, info):
    ck = run.check
    it1, it5, it6 = info["it"][0], info["it"][4], info["it"][5]
    p5, p6 = info["p"][4], info["p"][5]
    cur = a.ts_latest(SVC_SHOP)
    blob_before, seq_before = cur[1], cur[0].seq
    items_before = shop_items(blob_before)
    # sale of one stock-priced item: value = price / 4
    k, k2, k3, k4 = free_slots(a, 4)
    pk = list(a.txn_pre_image()[0])
    pk[k] = it5
    pre = (tuple(pk), a.txn_pre_image()[1], 1000)
    v5 = p5 // 4
    base = a.rec_last
    mark_a, off = a.inbox.mark(), len(run.log())
    rid, sent, r = sell(run, a, 1 << k, k, it5, pre)
    run.tx_ok("SE1 SHOP_SELL of one item priced %d (the shop price / 4 = %d)" % (p5, v5), r, APPLIED, R["NONE"])
    if r is None:
        return
    ck("SE1 RESULT echoes kind 11 / request / nonce / seq, dest NONE and the primary slot / item; rev == base + 1",
       (r.kind, r.request_id, r.txn_nonce, r.txn_seq, r.dest, r.slot, r.item) == (K_SELL, rid, sent.nonce, sent.seq, D_NONE, k, it5) and (r.host_session, r.epoch, r.rev) == (base[0], base[1], base[2] + 1))
    ck("SE1 post-image: the slot is EMPTY and the wallet is 1000 + %d (the HOST value); every other pocket / conds unchanged" % v5,
       tuple(r.post_pockets) == tuple(0 if i == k else p for i, p in enumerate(pk)) and r.post_wallet == 1000 + v5 and r.post_conds == pre[1] and r.cdig == TP.cown_digest(a.rec_local))
    new_sales = plus_sales(blob_before, v5 // 2)
    if new_sales != shop_sales(blob_before):
        st = wait_shop(a, seq_before + 1)
        ck("SE1 the shop mirror follows: sales_sum rose by value / 2 = %d (vanilla aNSC_buy_check -> mSP_PlusSales) and the STOCK is unchanged (sold items never enter the shop)" % (v5 // 2),
           st is not None and shop_sales(st[1]) == new_sales and shop_items(st[1]) == items_before and shop_rare(st[1]) == shop_rare(blob_before))
        ck("SE1 svc_seq16 == the shop seq of that state; RESULT before the mirror",
           st is not None and r.svc_seq16 == (st[0].seq & 0xFFFF) and TS.txn_idx(a, sent.seq, mark_a)[0] < TS.ts_indices(a, SVC_SHOP, mark_a)[0])
    else:
        ck("SE1 sales_sum is already at the level cap: no shop state change, svc_seq16 == the current seq", r.svc_seq16 == (a.ts_latest(SVC_SHOP)[0].seq & 0xFFFF))
    ck("SE1 host log: one committed SHOP_SELL line with value %d" % v5,
       run.n_log(r"SHOP_SELL mask=0x%04X value=%d wallet 1000 -> %d .* committed \[TXN\]" % (1 << k, v5, 1000 + v5), off) == 1)
    n_ts = TS.ts_count(a, SVC_SHOP)
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0, nth=2)
    L.pump_sleep(0.5)
    ck("SE1 the identical resend replays APPLIED/REPLAYED, not executed twice", rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"]
       and run.n_log(r"TXN APPLIED kind=SHOP_SELL request=%d " % rid, off) == 1 and TS.ts_count(a, SVC_SHOP) == n_ts)

    # several slots in one sale
    cur2 = a.ts_latest(SVC_SHOP)
    pk = list(a.txn_pre_image()[0])
    for kk, itx in ((k, it5), (k2, it6), (k3, it5)):
        pk[kk] = itx
    mask = (1 << k) | (1 << k2) | (1 << k3)
    vtot = p5 // 4 + p6 // 4 + p5 // 4
    _rid, _s, r3 = sell(run, a, mask, k, it5, (tuple(pk), a.txn_pre_image()[1], 200))
    run.tx_ok("SE2 SHOP_SELL of THREE slots at once", r3, APPLIED, R["NONE"])
    ck("SE2 value = the sum of the three host values (%d), all three slots emptied, wallet 200 + %d" % (vtot, vtot),
       r3 is not None and r3.post_wallet == 200 + vtot and all(r3.post_pockets[x] == 0 for x in (k, k2, k3)))

    # unsellable
    n_ts, off2 = TS.ts_count(a, SVC_SHOP), len(run.log())
    pk = list(a.txn_pre_image()[0])
    pk[k] = ITM_PAPER0
    _rid, _s, rp = sell(run, a, 1 << k, k, ITM_PAPER0, (tuple(pk), a.txn_pre_image()[1], 0))
    run.tx_ok("SE3 stationery is not sold through the network seam (deferred) -> NOT_SELLABLE", rp, REJECTED, R["NOT_SELLABLE"])
    pk = list(a.txn_pre_image()[0])
    pk[k] = it5
    conds = a.txn_pre_image()[1] | (COND_QUEST << (2 * k))
    _rid, _s, rq = sell(run, a, 1 << k, k, it5, (tuple(pk), conds, 0))
    run.tx_ok("SE3 a quest-condition item is never bought -> NOT_SELLABLE", rq, REJECTED, R["NOT_SELLABLE"])
    pk[k2] = it6
    _rid, _s, rm = sell(run, a, (1 << k) | (1 << k2), k2, it6, (tuple(pk), conds, 0))
    run.tx_ok("SE3 ONE quest item inside a multi-slot sale refuses the WHOLE sale (nothing is half-sold)", rm, REJECTED, R["NOT_SELLABLE"])
    _rid, _s, re_ = sell(run, a, (1 << k) | (1 << k2), k, it5, (tuple(0 if i == k2 else (it5 if i == k else p) for i, p in enumerate(a.txn_pre_image()[0])), a.txn_pre_image()[1], 0))
    run.tx_ok("SE3 a slot of the mask is EMPTY in the pre-image -> PRECOND", re_, REJECTED, R["PRECOND"])
    L.pump_sleep(0.5)
    ck("SE3 none of the refusals changed the shop (no state, no applied line)", TS.ts_count(a, SVC_SHOP) == n_ts and run.n_log(r"TXN APPLIED kind=SHOP_SELL", off2) == 0)

    # worthless item: free disposal
    pk = list(a.txn_pre_image()[0])
    pk[k] = ITM_KABU_SPOILED
    n_ts = TS.ts_count(a, SVC_SHOP)
    _rid, _s, rd = sell(run, a, 1 << k, k, ITM_KABU_SPOILED, (tuple(pk), a.txn_pre_image()[1], 4321))
    run.tx_ok("SE4 a spoiled turnip is taken off the player's hands (value 0)", rd, APPLIED, R["NONE"])
    L.pump_sleep(0.6)
    ck("SE4 the wallet is unchanged, the slot is emptied, sales_sum did not move (no shop state change)",
       rd is not None and rd.post_wallet == 4321 and rd.post_pockets[k] == 0 and TS.ts_count(a, SVC_SHOP) == n_ts)

    # 30000-bag overflow
    cur = a.ts_latest(SVC_SHOP)[1]
    big = max(listed(run, cur) or [(0, it5, p5)], key=lambda t: t[2])
    vbig = big[2] // 4
    pk = list(a.txn_pre_image()[0])
    pk[k] = big[1]
    wallet = WALLET_MAX - max(1, vbig // 2)
    bells = wallet + vbig
    _rid, _s, ro = sell(run, a, 1 << k, k, big[1], (tuple(pk), a.txn_pre_image()[1], wallet))
    run.tx_ok("SE5 the sale takes the wallet over the cap (%d + %d = %d >= %d)" % (wallet, vbig, bells, WALLET_MAX), ro, APPLIED, R["NONE"])
    ck("SE5 vanilla overflow: 30000 bells become a 30000 money bag IN THE SOLD SLOT and the wallet is %d" % (bells - 30000),
       ro is not None and bells >= WALLET_MAX and ro.post_wallet == bells - 30000 and ro.post_pockets[k] == ITM_MONEY_30000 and ro.post_wallet < WALLET_MAX)

    # a full-pocket sale: the sold slot itself becomes the bag, nothing else needs room
    full = tuple(APPLE if i != k else big[1] for i in range(15))
    _rid, _s, rfull = sell(run, a, 1 << k, k, big[1], (full, 0, wallet))
    run.tx_ok("SE5 the same sale with every pocket full still fits (the sold slot becomes the bag)", rfull, APPLIED, R["NONE"])
    ck("SE5 ... all other pockets untouched", rfull is not None and all(rfull.post_pockets[i] == APPLE for i in range(15) if i != k))


def m_malformed(run):
    ck = run.check
    p_it = 0x2305
    cases = [
        ("buy dest NONE", K_BUY, D_NONE, 2, p_it, dict(aux_cond=1, aux_item=100)),
        ("buy aux_item (price) 0", K_BUY, D_POCKET, 2, p_it, dict(aux_cond=1, aux_item=0)),
        ("buy stock code 39", K_BUY, D_POCKET, 2, p_it, dict(aux_cond=39, aux_item=100)),
        ("sell flags EXCHANGE (a SHOP_BUY's flags byte is the catalog generation since Patch 1: any value is shape-legal there)", K_SELL, D_NONE, 2, p_it, dict(aux_item=4, flags=1)),
        ("sell dest POCKET", K_SELL, D_POCKET, 2, p_it, dict(aux_item=4)),
        ("sell mask 0", K_SELL, D_NONE, 2, p_it, dict(aux_item=0)),
        ("sell primary slot not in the mask", K_SELL, D_NONE, 2, p_it, dict(aux_item=1)),
        ("sell mask bit 15", K_SELL, D_NONE, 2, p_it, dict(aux_item=0x8004)),
        ("sell aux_cond != 0", K_SELL, D_NONE, 2, p_it, dict(aux_item=4, aux_cond=1)),
        ("buy slot 15", K_BUY, D_POCKET, 15, p_it, dict(aux_cond=1, aux_item=100)),
    ]
    t = None
    for n, (label, kind, dest, slot, item, kw) in enumerate(cases):
        if n % 2 == 0:
            t = run.ready("S%d" % n, run.r3)
        rev = t.rec_last[2]
        sent = t.send_txn_commit(kind, run.fresh_rid(), dest, slot, item, **kw)
        r = t.wait_txn_result(sent.seq, 2.5)
        run.tx_ok(f"M{n} malformed shop COMMIT ({label})", r, REJECTED, R["BAD_SHAPE"])
        ck(f"M{n} ... the peer is still connected after the violation and the mirror did not move", t.is_connected() and t.rec_last[2] == rev)
        if n % 2 == 1:
            run.release(t)


def m_peers(run, a, it, i, price):
    ck = run.check
    host = run.host
    L.pump_sleep(0.3)
    rev_chk, n_ts = a.rec_last[2], TS.ts_count(a, SVC_SHOP)
    pre = (tuple(0 for _ in range(15)), 0, price + 100)
    probe = a.build_txn_commit_bytes(K_BUY, 1, D_POCKET, 0, it, pre=pre, base=(a.rec_last[1], a.rec_last[2]), aux_cond=i, aux_item=price)
    hs = L.FakeClient("HS", run.ip, run.port, context_flags=None, wait_snapshot=False, record_hello=False)
    hs.connect(timeout=3.0)
    off = len(host.log_text())
    hs.send_reliable(probe)
    L.pump_sleep(1.0)
    ck("X1 a HANDSHAKE peer (no IDENTITY) gets no answer to a SHOP_BUY and no shop state; the host logs no shop / TXN activity",
       not hs.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_TOWN_SVC_STATE))
       and "[NET][TXN]" not in run.log(off) and "[NET][SHOP]" not in run.log(off) and hs.is_connected())
    hs.close()
    holder = run.ready("H", run.r3)
    park = L.FakeClient("PK", run.ip, run.port, player=L.resident_player(run.r3), context_flags=None, wait_snapshot=False, record_hello=False)
    park.connect(timeout=3.0)
    park.send_identity(town=L.resolve_host_town(run.ip, run.port), player=L.resident_player(run.r3))
    L.pump_sleep(0.5)
    off = len(host.log_text())
    hrev = holder.rec_last[2]
    park.send_reliable(probe)
    L.pump_sleep(1.0)
    ck("X2 a PARKED peer (claim on a live resident) gets no TXN_RESULT / TOWN_SVC_STATE and causes no TXN activity",
       not park.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_IDENTITY_ACK, L.PC_NETGAME_MSG_TOWN_SVC_STATE))
       and "[NET][TXN]" not in run.log(off) and holder.rec_last[2] == hrev)
    park.close()
    own = L.FakeClient("OWN", run.ip, run.port, player=L.resident_player(run.host_slot), context_flags=None, wait_snapshot=False, record_hello=False)
    own.connect(timeout=3.0)
    off = len(host.log_text())
    try:
        own.send_identity(town=L.resolve_host_town(run.ip, run.port), player=L.resident_player(run.host_slot))
        L.pump_sleep(0.5)
        own.send_reliable(probe)
        L.pump_sleep(0.8)
    except Exception:  # noqa: BLE001
        pass
    ck("X3 a peer claiming the HOST's own resident (refused at identity) gets no TXN_RESULT / TOWN_SVC_STATE and nothing is mutated",
       not own.inbox.peek_all(lambda m: m.channel == L.CH_RELIABLE and m.msg_type in (L.PC_NETGAME_MSG_TXN_RESULT, L.PC_NETGAME_MSG_TOWN_SVC_STATE))
       and "TXN APPLIED" not in run.log(off) and "[NET][SHOP]" not in run.log(off))
    own.close()
    run.release(holder)
    ck("X4 nothing above changed A's mirror or sent A another shop state", a.rec_last[2] == rev_chk and TS.ts_count(a, SVC_SHOP) == n_ts)


def phase_main(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    ck("SHOP setup: A and B READY and SYNCED", a.rec_synced and b.rec_synced and a.rec_last is not None and b.rec_last is not None)
    sa = m_mirror_at_ready(run, a, b)
    if sa is None:
        return
    info = m_buy(run, a, b, sa)
    if info is None:
        return
    m_sell(run, a, b, info)
    m_malformed(run)
    # an item that is STILL in stock for the ignored-peer probes (a rejected purchase would also show nothing)
    cur = a.ts_latest(SVC_SHOP)[1]
    rest = listed(run, cur)
    probe_item = rest[0] if rest else (info["i"][5], info["it"][5], info["p"][5])
    m_peers(run, a, probe_item[1], probe_item[0], probe_item[2])
    final = a.ts_latest(SVC_SHOP)[1]
    run.release(a)
    run.release(b)
    c = run.ready("C", run.r1)
    sc = wait_shop(c, 1, 4.0)
    ck("N1 a NEW session of A gets the FINAL shop state at READY (late join covers every earlier change)", sc is not None and sc[1] == final)
    run.release(c)


def phase_drop(run):
    ck = run.check
    a = run.ready("A", run.r1)
    sa = wait_shop(a, 1, 6.0)
    cand = listed(run, sa[1], 1)
    if not cand:
        ck("F1 setup: a buyable item exists", False)
        return
    i1, it1, p1 = cand[0]
    s = free_slots(a, 1)[0]
    base = a.rec_last
    rid, sent, first = buy(run, a, s, it1, i1, p1, p1 + 5, wait=1.6)
    L.pump_sleep(0.3)
    ck("F1 the APPLIED RESULT is lost (drop_result) but the host DID commit: no result at A, the shop mirror still reaches A (item sold)",
       first is None and run.n_log(r"TXN APPLIED kind=SHOP_BUY request=%d " % rid) == 1 and "FAULT INJECTION FIRED mode=drop_result" in run.log())
    st = wait_shop(a, sa[0].seq + 1, 3.0)
    ck("F1 ... the mirror carries the sale (the stock slot holds a sold marker)", st is not None and marker_ok(it1, shop_items(st[1])[i1]))
    a.resend_txn(sent)
    res = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.4)
    ck("F1 the resend gets APPLIED/REPLAYED with the SAME svc_seq16 as the mirror seq, rev == base + 1, post-wallet 5",
       res is not None and res.outcome == APPLIED and res.reason == R["REPLAYED"] and st is not None and res.svc_seq16 == (st[0].seq & 0xFFFF)
       and res.rev == base[2] + 1 and res.post_wallet == 5)
    ck("F1 executed exactly once: one APPLIED line, one replay line, one shop state change",
       run.n_log(r"TXN APPLIED kind=SHOP_BUY request=%d " % rid) == 1 and run.replay_lines(rid) == 1 and TS.ts_count(a, SVC_SHOP) == 2)


def phase_kill(run):
    ck = run.check
    a = run.ready("A", run.r1)
    b = run.ready("B", run.r2)
    sb = wait_shop(b, 1, 6.0)
    cand = listed(run, sb[1], 1)
    if not cand:
        ck("K1 setup: a buyable item exists", False)
        return
    i1, it1, p1 = cand[0]
    s = free_slots(a, 1)[0]
    pre = pre_free(a, s, p1 + 9)
    base = a.rec_last
    rid, sent, r = buy(run, a, s, it1, i1, p1, 0, wait=1.0, pre=pre)
    closed = TP.wait_closed(a, 4.0)
    L.pump_sleep(0.4)
    ck("K1 kill_peer_after_commit: the host dropped A after committing and sent NO result",
       closed and r is None and not a.txn_results_for(sent.seq) and "kill_peer_after_commit: peer" in run.log() and run.n_log(r"TXN APPLIED kind=SHOP_BUY") == 0
       and run.n_log(r"SHOP_BUY item=0x%04X .* committed \[TXN\]" % it1) == 1)
    stB = wait_shop(b, sb[0].seq + 1, 3.0)
    ck("K1 the OTHER client still gets the new shop state (the item is sold)", stB is not None and marker_ok(it1, shop_items(stB[1])[i1]))
    exp = tuple(it1 if i == s else p for i, p in enumerate(pre[0]))
    a.reconnect_and_ready(timeout=8.0)
    pushed = a.rec_pushes[-1] if a.rec_pushes else None
    ck("K2 the same-nonce reconnect is pushed the mirror: it already HAS the item and the debited wallet (9), rev == base + 1",
       pushed is not None and pushed["rev"] == base[2] + 1 and L.record_inventory(pushed["data"])[0] == exp and L.record_inventory(pushed["data"])[2] == 9)
    sa = a.wait_ts_state(SVC_SHOP, 4.0, min_seq=1)
    ck("K2 ... and its new session receives the post-sale shop state at READY", sa is not None and sa[1] == stB[1])
    a.resend_txn(sent)
    rep = a.wait_txn_result(sent.seq, 3.0)
    L.pump_sleep(0.3)
    ck("K3 the OLD COMMIT resent after a same-nonce reconnect replays APPLIED/REPLAYED (journal survives the peer reset), NO second execution",
       rep is not None and rep.outcome == APPLIED and rep.reason == R["REPLAYED"] and tuple(rep.post_pockets) == exp
       and run.n_log(r"SHOP_BUY item=0x%04X .* committed \[TXN\]" % it1) == 1 and b.ts_latest(SVC_SHOP)[1] == stB[1])
    run.release(a)
    a2 = run.client("A2", run.r1)
    a2.connect_and_ready(quiet=True)
    p2 = a2.rec_pushes[-1] if a2.rec_pushes else None
    s2 = TS.latest(a2, SVC_SHOP, 1, 4.0)
    ck("K4 a NEW-PROCESS rejoin: the pushed record has the item AND the mirror shows the stock slot sold",
       p2 is not None and L.record_inventory(p2["data"])[0] == exp and s2 is not None and s2[1] == stB[1])
    ck("K4 the host survived the dropped peer", run.host.alive())


PHASES = [
    ("SH-M", [], phase_main),
    ("SH-drop", ["--txn-fault=drop_result:1:1"], phase_drop),
    ("SH-kill", ["--txn-fault=kill_peer_after_commit:1:1"], phase_kill),
]
PHASE_KEYS = {"M": "SH-M", "F-drop": "SH-drop", "F-kill": "SH-kill"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11100)
    ap.add_argument("--only", default="", help="comma list of phase names (M, F-drop, F-kill)")
    args = ap.parse_args()
    L.require_test_bin_dir()
    if not os.path.basename(os.path.normpath(L.GAME_BIN_DIR)).startswith("bin_fixture4"):
        print("REFUSING: this test snapshots/restores and mutates the game save; run it on pc\\build64\\bin_fixture4 only "
              "(NET_SPIKE_GAME_BIN=<absolute path>)", file=sys.stderr)
        return 2
    only = [PHASE_KEYS.get(x, x) for x in args.only.split(",") if x]
    results = []
    ip = "127.0.0.1"
    save_dir = os.path.join(L.GAME_BIN_DIR, TP.SAVE_DIR_REL)
    gci_path = os.path.join(L.GAME_BIN_DIR, L.SAVE_GCI_REL)
    snap_dir = os.path.join(os.environ.get("TEMP", "."), "shop_protocol_save_snapshot_%d" % os.getpid())
    shutil.copytree(save_dir, snap_dir)
    with open(gci_path, "rb") as f:
        snap_gci = f.read()

    def restore():
        shutil.rmtree(save_dir, ignore_errors=True)
        shutil.copytree(snap_dir, save_dir)

    try:
        for i, (tag, extra, body) in enumerate(PHASES):
            if only and tag not in only:
                continue
            TS.run_phase(results, ip, args.port + i, tag, extra, body, snap_gci, restore)
    finally:
        restore()
        shutil.rmtree(snap_dir, ignore_errors=True)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
