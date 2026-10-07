/* peer_table_selftest.c - native unit test of pc/src/pc_peer_table.c, the pure logic behind the dynamic peer table (capacity phase 2): the id space (which ids exist for a capacity,
 * the host's id skipped, 0xFF never a peer), free-slot choice with reuse, the 256-bit peer set that replaced the 16-bit talk-hold mask, and the runtime-sized per-peer tables
 * (inline -> heap -> inline, zeroing, all-or-nothing growth under allocation failure). No sockets, no game.
 * Usage: peer_table_selftest   (prints PASS:/FAIL: lines and "RESULT passed=N failed=M") */
#include "pc_peer_table.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int s_pass, s_fail;
static void check(const char* what, int ok) {
    printf("%s: %s\n", ok ? "PASS" : "FAIL", what);
    if (ok) s_pass++; else s_fail++;
}

#define HOST_ID 8 /* == PC_NETGAME_HOST_WIRE_ID == PC_NET_RESERVED_PEER_ID */

/* ---- a simulated slot table: what pc_net.c does with its PCNetPeerSlot.state ---- */
static unsigned char s_used[300];
static int is_free(void* ctx, int id) {
    (void)ctx;
    return !s_used[id];
}
static int alloc_peer(int span) {
    int id = pc_peer_find_free(span, HOST_ID, is_free, NULL);
    if (id >= 0) s_used[id] = 1;
    return id;
}

/* ---- failing allocator ---- */
static int s_allocs, s_frees, s_fail_at;
static void* test_alloc(size_t n, size_t sz) {
    if (s_fail_at > 0 && s_allocs + 1 == s_fail_at) {
        s_allocs++;
        return NULL;
    }
    s_allocs++;
    return calloc(n, sz);
}
static void test_free(void* p) {
    s_frees++;
    free(p);
}

typedef struct Big { uint32_t a[5]; uint8_t b; } Big;
#define INL 8
PC_PEER_TABLE(Big, t_big, INL)
PC_PEER_TABLE(uint16_t, t_small, INL)
PC_PEER_TABLE2(uint8_t, t_rows, INL, 100)
static PCPeerTable s_tabs[] = { PC_PEER_TABLE_ENTRY(t_big), PC_PEER_TABLE_ENTRY(t_small), PC_PEER_TABLE_ENTRY(t_rows) };
#define NT 3
PC_GROW_TABLE(Big, g_big, 4, 8)
PC_GROW_TABLE(uint16_t, g_small, 4, 8)
PC_GROW_TABLE2(uint8_t, g_rows, 4, 8, 100)
static PCGrowTable g_tabs[] = { PC_GROW_TABLE_ENTRY(g_big, 4), PC_GROW_TABLE_ENTRY(g_small, 4), PC_GROW_TABLE_ENTRY(g_rows, 4) };

static int all_zero(const void* p, size_t n) {
    const unsigned char* c = (const unsigned char*)p;
    size_t i;
    for (i = 0; i < n; i++) if (c[i]) return 0;
    return 1;
}

int main(void) {
    int cap, id, i, ok;

    /* ---- id space ---- */
    check("capacity max is 254 (0..254 usable, minus the host's id)", pc_peer_capacity_max(HOST_ID) == 254 && PC_PEER_ID_LAST == 254 && PC_PEER_ID_NOBODY == 0xFF);
    check("span: capacities 1..8 are exactly the old table sizes (no hole)", pc_peer_span(1, HOST_ID) == 1 && pc_peer_span(4, HOST_ID) == 4 && pc_peer_span(8, HOST_ID) == 8);
    check("span: 9 peers need 10 slots (the host id is a hole), 254 need 255", pc_peer_span(9, HOST_ID) == 10 && pc_peer_span(16, HOST_ID) == 17 && pc_peer_span(254, HOST_ID) == 255);
    check("span: 0, negative and 255+ capacities are refused", pc_peer_span(0, HOST_ID) == 0 && pc_peer_span(-3, HOST_ID) == 0 && pc_peer_span(255, HOST_ID) == 0 && pc_peer_span(100000, HOST_ID) == 0);
    ok = 1;
    for (cap = 1; cap <= 254; cap++) {
        const int span = pc_peer_span(cap, HOST_ID);
        int usable = 0;
        for (id = 0; id < span; id++) usable += pc_peer_id_usable(id, span, HOST_ID);
        ok = ok && usable == cap && !pc_peer_id_usable(HOST_ID, span, HOST_ID) && !pc_peer_id_usable(span, span, HOST_ID) && !pc_peer_id_usable(-1, span, HOST_ID) && !pc_peer_id_usable(255, span, HOST_ID);
    }
    check("every capacity 1..254: exactly `capacity` usable ids, never the host id, never >= span, never 0xFF", ok);

    /* ---- allocation, growth beyond 8, release, reuse, exhaustion, for every capacity ---- */
    ok = 1;
    for (cap = 1; cap <= 254; cap++) {
        const int span = pc_peer_span(cap, HOST_ID);
        int n_ok = 0, seen_host = 0, seen_nobody = 0, last = -1, mono = 1;
        memset(s_used, 0, sizeof(s_used));
        for (i = 0; i < cap; i++) {
            id = alloc_peer(span);
            if (id < 0) break;
            n_ok++;
            seen_host |= id == HOST_ID;
            seen_nobody |= id == PC_PEER_ID_NOBODY || id > PC_PEER_ID_LAST;
            mono &= id > last;
            last = id;
        }
        ok = ok && n_ok == cap && !seen_host && !seen_nobody && mono && alloc_peer(span) == -1;
    }
    check("every capacity 1..254: `capacity` allocations succeed in increasing id order, skip the host id, never reach 0xFF, the next one is refused", ok);

    memset(s_used, 0, sizeof(s_used));
    {
        const int span = pc_peer_span(8, HOST_ID);
        int ids[8];
        for (i = 0; i < 8; i++) ids[i] = alloc_peer(span);
        ok = 1;
        for (i = 0; i < 8; i++) ok = ok && ids[i] == i;
        check("capacity 8 (the old table): ids are exactly 0..7 in order, the 9th is refused", ok && alloc_peer(span) == -1);
    }
    memset(s_used, 0, sizeof(s_used));
    {
        const int span = pc_peer_span(20, HOST_ID);
        int ids[20];
        ok = 1;
        for (i = 0; i < 20; i++) ids[i] = alloc_peer(span);
        for (i = 0; i < 8; i++) ok = ok && ids[i] == i;
        for (i = 8; i < 20; i++) ok = ok && ids[i] == i + 1;
        check("capacity 20: ids 0..7 as before, then 9..20 (8 skipped)", ok);
        s_used[3] = 0;
        s_used[12] = 0;
        s_used[9] = 0;
        check("release 3, 9, 12: the lowest free id is reused first (3, then 9, then 12), then full again", alloc_peer(span) == 3 && alloc_peer(span) == 9 && alloc_peer(span) == 12 && alloc_peer(span) == -1);
        s_used[HOST_ID] = 0;
        check("the host id slot (an unused hole) is never handed out even when its slot is free", alloc_peer(span) == -1);
    }
    check("pc_peer_find_free refuses a NULL predicate", pc_peer_find_free(8, HOST_ID, NULL, NULL) == -1);

    /* ---- peer set ---- */
    {
        PCPeerSet s;
        pc_peer_set_clear(&s);
        check("set: empty at start", pc_peer_set_empty(&s) && pc_peer_set_count(&s) == 0 && !pc_peer_set_has_other(&s, 0));
        ok = 1;
        for (id = 0; id < 256; id++) {
            pc_peer_set_add(&s, id);
            ok = ok && pc_peer_set_has(&s, id) && pc_peer_set_count(&s) == id + 1;
        }
        check("set: every wire id 0..255 can be a member (the old mask stopped at 15)", ok && !pc_peer_set_empty(&s));
        for (id = 0; id < 256; id++) {
            pc_peer_set_del(&s, id);
            ok = ok && !pc_peer_set_has(&s, id) && pc_peer_set_count(&s) == 255 - id;
        }
        check("set: delete removes exactly that id", ok && pc_peer_set_empty(&s));
        pc_peer_set_add(&s, -1);
        pc_peer_set_add(&s, 256);
        pc_peer_set_add(&s, 100000);
        check("set: ids outside 0..255 are ignored", pc_peer_set_empty(&s) && !pc_peer_set_has(&s, -1) && !pc_peer_set_has(&s, 256));
        pc_peer_set_add(&s, 40);
        check("set: has_other is false for the only member and true for any other", !pc_peer_set_has_other(&s, 40) && pc_peer_set_has_other(&s, 41));
        pc_peer_set_add(&s, 200);
        check("set: has_other sees a second member (exclusive-lease test)", pc_peer_set_has_other(&s, 40) && pc_peer_set_has_other(&s, 200) && pc_peer_set_count(&s) == 2);
        pc_peer_set_clear(&s);
        ok = 1;
        for (id = 0; id < 16; id++) {
            pc_peer_set_clear(&s);
            pc_peer_set_add(&s, id);
            ok = ok && pc_peer_set_low32(&s) == (1u << id);
        }
        check("set: low32 equals the old `1u << peer` mask for peers 0..15 (log format unchanged)", ok);
    }

    /* ---- runtime-sized per-peer tables ---- */
    pc_peer_tables_set_allocator(test_alloc, test_free);
    check("tables start inline and zeroed", t_big == t_big_inl && t_small == t_small_inl && (void*)t_rows == (void*)t_rows_inl && all_zero(t_big_inl, sizeof(t_big_inl)));
    t_big[3].a[2] = 77;
    t_small[5] = 9;
    t_rows[7][99] = 5;
    check("resize to span <= inline stays inline and ZEROES the tables", pc_peer_tables_resize(s_tabs, NT, INL, 8) && t_big == t_big_inl && t_big[3].a[2] == 0 && t_small[5] == 0 && t_rows[7][99] == 0);
    s_allocs = s_frees = 0;
    check("resize to 255 slots allocates one heap block per table", pc_peer_tables_resize(s_tabs, NT, INL, 255) && s_allocs == NT && s_frees == 0 && t_big != t_big_inl && t_small != t_small_inl && (void*)t_rows != (void*)t_rows_inl);
    ok = 1;
    for (i = 0; i < 255; i++) ok = ok && all_zero(&t_big[i], sizeof(Big)) && t_small[i] == 0 && all_zero(t_rows[i], 100);
    t_big[254].a[4] = 123;
    t_small[254] = 0xBEEF;
    t_rows[254][99] = 0xAB;
    t_rows[8][0] = 1;
    check("heap tables: all 255 elements zeroed, the last element (id 254) is writable, rows are independent", ok && t_big[254].a[4] == 123 && t_small[254] == 0xBEEF && t_rows[254][99] == 0xAB && t_rows[9][0] == 0);
    s_allocs = s_frees = 0;
    check("growing again frees the old heap blocks and returns zeroed tables", pc_peer_tables_resize(s_tabs, NT, INL, 300) && s_allocs == NT && s_frees == NT && t_big[254].a[4] == 0 && t_small[254] == 0 && t_rows[254][99] == 0);

    /* all-or-nothing under failure: fail at the 2nd of 3 allocations */
    t_big[10].a[0] = 11;
    t_small[10] = 22;
    {
        Big* big_before = t_big;
        uint16_t* small_before = t_small;
        s_allocs = s_frees = 0;
        s_fail_at = 2;
        ok = pc_peer_tables_resize(s_tabs, NT, INL, 400) == 0;
        s_fail_at = 0;
        check("a failed allocation fails the whole resize", ok);
        check("...and changes NOTHING: same pointers, same contents, the blocks already allocated were freed", t_big == big_before && t_small == small_before && t_big[10].a[0] == 11 && t_small[10] == 22 && s_frees == s_allocs - 1);
    }
    s_allocs = s_frees = 0;
    s_fail_at = 1;
    check("failure at the first allocation is handled too", pc_peer_tables_resize(s_tabs, NT, INL, 400) == 0);
    s_fail_at = 0;
    s_allocs = s_frees = 0;
    pc_peer_tables_release(s_tabs, NT, INL);
    check("release returns to the zeroed inline storage and frees the heap", t_big == t_big_inl && t_small == t_small_inl && (void*)t_rows == (void*)t_rows_inl && s_frees == NT && all_zero(t_big_inl, sizeof(t_big_inl)) && all_zero(t_small_inl, sizeof(t_small_inl)) && all_zero(t_rows_inl, sizeof(t_rows_inl)));
    check("a second release is harmless", (pc_peer_tables_release(s_tabs, NT, INL), t_big == t_big_inl));
    check("bad arguments are refused", !pc_peer_tables_resize(NULL, NT, INL, 8) && !pc_peer_tables_resize(s_tabs, 0, INL, 8) && !pc_peer_tables_resize(s_tabs, NT, INL, 0));
    pc_peer_tables_set_allocator(NULL, NULL);
    check("with the default allocator a resize works too", pc_peer_tables_resize(s_tabs, NT, INL, 64) && t_big != t_big_inl && (pc_peer_tables_release(s_tabs, NT, INL), t_big == t_big_inl));

    /* ---- growable tables (the guest store's arrays: 4 resident slots + a growing number of guests) ---- */
    pc_peer_tables_set_allocator(test_alloc, test_free);
    {
        int have = 8, k;
        s_allocs = s_frees = 0;
        for (k = 0; k < 4 + 8; k++) { g_big[k].a[1] = (uint32_t)(1000 + k); g_small[k] = (uint16_t)(2000 + k); g_rows[k][5] = (uint8_t)(k + 1); }
        check("grow: tables start inline (base 4 + 8)", g_big == g_big_inl && g_small == g_small_inl && (void*)g_rows == (void*)g_rows_inl);
        check("grow: want <= have is a no-op (no allocation)", pc_grow_tables_ensure(g_tabs, NT, have, 8) && s_allocs == 0 && g_big == g_big_inl);
        check("grow 8 -> 9: one block per table, every element KEPT (4 base + 8 used), the new one zeroed", pc_grow_tables_ensure(g_tabs, NT, have, 9) && s_allocs == NT && s_frees == 0 &&
              g_big != g_big_inl);
        ok = 1;
        for (k = 0; k < 4 + 8; k++) ok = ok && g_big[k].a[1] == (uint32_t)(1000 + k) && g_small[k] == (uint16_t)(2000 + k) && g_rows[k][5] == (uint8_t)(k + 1);
        ok = ok && all_zero(&g_big[12], sizeof(Big)) && g_small[12] == 0 && all_zero(g_rows[12], 100);
        check("grow: old contents identical, the added element is zero", ok);
        have = 9;
        s_allocs = s_frees = 0;
        check("grow 9 -> 40 frees the previous heap block and keeps everything", pc_grow_tables_ensure(g_tabs, NT, have, 40) && s_allocs == NT && s_frees == NT && g_big[3].a[1] == 1003 && g_big[11].a[1] == 1011 && g_rows[11][5] == 12);
        have = 40;
        g_big[43].a[0] = 4343;
        {
            Big* b_before = g_big;
            s_allocs = s_frees = 0;
            s_fail_at = 2;
            ok = pc_grow_tables_ensure(g_tabs, NT, have, 400) == 0;
            s_fail_at = 0;
            check("a failed growth fails as a whole and changes nothing (pointers, contents, no leak)", ok && g_big == b_before && g_big[43].a[0] == 4343 && s_frees == s_allocs - 1);
        }
        s_allocs = s_frees = 0;
        check("growth to 5000 guests works and the last element is addressable", pc_grow_tables_ensure(g_tabs, NT, have, 5000) && (g_big[4 + 4999].a[0] = 9, g_rows[4 + 4999][99] = 7, g_big[43].a[0] == 4343 && g_big[4 + 4999].a[0] == 9));
        check("grow: bad arguments are refused", !pc_grow_tables_ensure(NULL, NT, 8, 9) && !pc_grow_tables_ensure(g_tabs, 0, 8, 9) && !pc_grow_tables_ensure(g_tabs, NT, -1, 9) && !pc_grow_tables_ensure(g_tabs, NT, 8, 0));
        pc_peer_tables_set_allocator(NULL, NULL);
    }

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail == 0 ? 0 : 1;
}
