/* pc_evnpc_shared.c - SHARED EVENT NPCs (worker E): the pure logic of the event NPC claims (TXN kind 21) and the service 9 blob. No game state; the native
 * selftest (pc_evc_selftest) and tools/net_spike/test_evnpc_shared_native.py compile this file alone. See pc_evnpc_shared.h. */
#include "pc_evnpc_shared.h"

#include <stdio.h>
#include <string.h>

#define EVC_COND(c, i) (((c) >> ((uint32_t)(i) << 1)) & 3u)
#define EVC_SET_COND_NORMAL(c, i) ((c) & ~(3u << ((uint32_t)(i) << 1)))

int pc_evc_shape_ok(int op, unsigned dest, unsigned slot, unsigned item, unsigned flags, unsigned aux_item, unsigned pocket_count) {
    if (slot >= pocket_count) {
        return 0;
    }
    switch (op) {
        case PC_EVC_OP_REDD_BUY:
            return dest == 1u && item != 0u && flags < (unsigned)PC_EVC_REDD_ITEMS && aux_item != 0u;
        case PC_EVC_OP_SAHARAH_TRADE:
            return dest == 1u && item != 0u && flags == 0u && aux_item != 0u;
        case PC_EVC_OP_ARTIST_TRADE:
            return dest == 1u && item != 0u && flags == 0u && aux_item == 0u;
        case PC_EVC_OP_DESIGNER_GIFT:
            return dest == 0u && item == 0u && flags == 0u && aux_item <= 1u;
        case PC_EVC_OP_WISP_RETURN:
            return dest == 0u && item == 0u && flags == 0u &&
                   (aux_item == PC_EVC_WISP_WEEDS || aux_item == PC_EVC_WISP_ITEM || (aux_item >= PC_EVC_WISP_ROOF && aux_item < PC_EVC_WISP_ROOF + PC_EVC_ROOF_PAL_MAX));
        default:
            return 0;
    }
}

int pc_evc_spirit_points(const uint16_t* pockets, uint32_t conds, int pocket_count, uint16_t spirit0) {
    int i, n = 0;
    for (i = 0; i < pocket_count; i++) {
        if (pockets[i] >= spirit0 && pockets[i] < (uint16_t)(spirit0 + PC_EVC_SPIRIT_POINTS) && EVC_COND(conds, i) == 0u) {
            n += (int)(pockets[i] - spirit0) + 1;
        }
    }
    return n;
}

int pc_evc_strip_spirits(uint16_t* pockets, uint32_t* conds, int pocket_count, uint16_t spirit0, uint16_t empty) {
    int i, n = 0;
    for (i = 0; i < pocket_count; i++) {
        if (pockets[i] >= spirit0 && pockets[i] < (uint16_t)(spirit0 + PC_EVC_SPIRIT_POINTS) && EVC_COND(*conds, i) == 0u) {
            pockets[i] = empty;
            *conds = EVC_SET_COND_NORMAL(*conds, i);
            n++;
        }
    }
    return n;
}

uint32_t pc_evc_saharah_price(int used) {
    uint32_t p = PC_EVC_SAHARAH_PRICE_START;
    if (used < 0) {
        used = 0;
    }
    if (used > PC_EVC_SAHARAH_MULT_CAP) {
        used = PC_EVC_SAHARAH_MULT_CAP;
    }
    while (used-- > 0) {
        p *= 2u;
    }
    return p;
}

int pc_evc_md_in_range(unsigned md, unsigned start, unsigned end) {
    if (start == 0u || end == 0u) {
        return 0;
    }
    if (start <= end) {
        return md >= start && md <= end;
    }
    return md >= start || md <= end; /* across new year */
}

void pc_evc_caps_build(uint8_t* blob, int on, int wisp_returned, uint32_t ops) {
    memset(blob, 0, PC_EVC_CAPS_LEN);
    blob[0] = (uint8_t)PC_EVC_CAPS_VERSION;
    blob[1] = on ? 1u : 0u;
    blob[2] = wisp_returned ? 1u : 0u;
    blob[4] = (uint8_t)(ops & 0xFFu);
    blob[5] = (uint8_t)((ops >> 8) & 0xFFu);
    blob[6] = (uint8_t)((ops >> 16) & 0xFFu);
    blob[7] = (uint8_t)((ops >> 24) & 0xFFu);
}

int pc_evc_caps_valid(const uint8_t* blob) {
    const uint32_t ops = (uint32_t)blob[4] | ((uint32_t)blob[5] << 8) | ((uint32_t)blob[6] << 16) | ((uint32_t)blob[7] << 24);
    return blob[0] == (uint8_t)PC_EVC_CAPS_VERSION && blob[1] <= 1u && blob[2] <= 1u && blob[3] == 0u && (ops & ~(uint32_t)PC_EVC_OPS_ALL) == 0u;
}

/* ---- native selftest ---- */
static int s_fail;
static void evc_ck(int ok, const char* what) {
    if (!ok) {
        s_fail++;
        printf("[EVC] selftest FAIL: %s\n", what);
    }
}

int pc_evc_selftest(void) {
    uint16_t p[15];
    uint32_t c = 0;
    uint8_t b[PC_EVC_CAPS_LEN];
    const uint16_t S0 = 0x2328; /* any base: the logic is relative */
    s_fail = 0;
    /* shapes */
    evc_ck(pc_evc_shape_ok(1, 1, 3, 0x1234, 2, 0x5000, 15), "redd buy legal");
    evc_ck(!pc_evc_shape_ok(1, 1, 3, 0x1234, 3, 0x5000, 15), "redd lineup index 3 refused");
    evc_ck(!pc_evc_shape_ok(1, 0, 3, 0x1234, 0, 0x5000, 15), "redd dest NONE refused");
    evc_ck(!pc_evc_shape_ok(1, 1, 15, 0x1234, 0, 0x5000, 15), "slot 15 refused");
    evc_ck(pc_evc_shape_ok(2, 1, 0, 0x2600, 0, 3000, 15) && !pc_evc_shape_ok(2, 1, 0, 0x2600, 1, 3000, 15), "saharah shape");
    evc_ck(pc_evc_shape_ok(3, 1, 0, 0x2300, 0, 0, 15) && !pc_evc_shape_ok(3, 1, 0, 0, 0, 0, 15), "artist shape");
    evc_ck(pc_evc_shape_ok(4, 0, 0, 0, 0, 1, 15) && !pc_evc_shape_ok(4, 0, 0, 0, 0, 2, 15) && !pc_evc_shape_ok(4, 0, 0, 5, 0, 0, 15), "designer shape");
    evc_ck(pc_evc_shape_ok(5, 0, 0, 0, 0, 0, 15) && pc_evc_shape_ok(5, 0, 0, 0, 0, 2, 15) && pc_evc_shape_ok(5, 0, 0, 0, 0, 0x10B, 15) &&
               !pc_evc_shape_ok(5, 0, 0, 0, 0, 0x10C, 15) && !pc_evc_shape_ok(5, 0, 0, 0, 0, 1, 15) && !pc_evc_shape_ok(5, 1, 0, 0, 0, 0, 15),
           "wisp shape");
    evc_ck(!pc_evc_shape_ok(0, 1, 0, 1, 0, 1, 15) && !pc_evc_shape_ok(6, 1, 0, 1, 0, 1, 15), "unknown op refused");
    /* spirits */
    memset(p, 0, sizeof(p));
    p[2] = (uint16_t)(S0 + 1); /* 2 points */
    p[5] = (uint16_t)(S0 + 2); /* 3 points */
    p[7] = 0x1111;
    evc_ck(pc_evc_spirit_points(p, c, 15, S0) == 5, "2 + 3 spirit points = 5");
    c = (1u << (5 * 2)); /* slot 5 PRESENT: does not count */
    evc_ck(pc_evc_spirit_points(p, c, 15, S0) == 2, "a wrapped spirit does not count");
    c = 0;
    p[9] = (uint16_t)(S0 + 4);
    evc_ck(pc_evc_spirit_points(p, c, 15, S0) == 10, "SPIRIT4 counts 5");
    evc_ck(pc_evc_strip_spirits(p, &c, 15, S0, 0) == 3 && p[2] == 0 && p[5] == 0 && p[9] == 0 && p[7] == 0x1111, "strip removes every spirit, nothing else");
    evc_ck(pc_evc_spirit_points(p, c, 15, S0) == 0, "no spirit left");
    /* Saharah */
    evc_ck(pc_evc_saharah_price(0) == 3000u && pc_evc_saharah_price(1) == 6000u && pc_evc_saharah_price(4) == 48000u && pc_evc_saharah_price(9) == 48000u &&
               pc_evc_saharah_price(-3) == 3000u,
           "saharah price doubling, capped");
    /* dates */
    evc_ck(pc_evc_md_in_range(0x0A0F, 0x0A0F, 0x0A10) && !pc_evc_md_in_range(0x0A11, 0x0A0F, 0x0A10) && pc_evc_md_in_range(0x0101, 0x0C1F, 0x0102) &&
               !pc_evc_md_in_range(0x0A0F, 0, 0),
           "visit date range");
    /* caps */
    pc_evc_caps_build(b, 1, 1, PC_EVC_OPS_ALL);
    evc_ck(pc_evc_caps_valid(b) && b[1] == 1 && b[2] == 1 && b[4] == 0x3E, "caps round trip");
    b[3] = 1;
    evc_ck(!pc_evc_caps_valid(b), "caps reserved byte must be 0");
    pc_evc_caps_build(b, 0, 0, 0x40);
    evc_ck(!pc_evc_caps_valid(b), "caps unknown op bit refused");
    printf("[EVC] selftest: %s (%d failure(s))\n", s_fail ? "FAIL" : "PASS", s_fail);
    return s_fail;
}
