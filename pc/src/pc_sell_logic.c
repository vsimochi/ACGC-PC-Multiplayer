/* pc_sell_logic.c - see pc_sell_logic.h. Item-id rules and the basket staging only; macros of m_name_table.h (no game function is called, so the native selftest
 * links this file alone). */
#include "pc_sell_logic.h"

#include <stdio.h>
#include <string.h>

#include "m_name_table.h"

/* the KNOWN item1 indexes per category (= m_name_table.c mNT_check_unknown; the same table as pc_dresser_items.c) */
static const short k_sell_item1_kinds[16] = {
    PAPER_NUM, MONEY_NUM, TOOL_NUM, FISH_NUM, CLOTH_NUM, ETC_NUM, CARPET_NUM, WALL_NUM, FRUIT_NUM, PLANT_NUM, MINIDISK_NUM, DIARY_NUM, TICKET_NUM, INSECT_NUM,
    HUKUBUKURO_NUM, KABU_NUM,
};

static int pc_sell_known(unsigned item) {
    const mActor_name_t it = (mActor_name_t)item;
    switch (ITEM_NAME_GET_TYPE(it)) {
        case NAME_TYPE_ITEM1: {
            const int cat = ITEM_NAME_GET_CAT(it);
            return cat >= 0 && cat < 16 && (int)ITEM_NAME_GET_INDEX(it) < k_sell_item1_kinds[cat];
        }
        case NAME_TYPE_FTR0:
        case NAME_TYPE_FTR1: {
            /* = m_room_type.c mRmTp_FtrItemNo2FtrIdx; a pocket piece has rotation 0 */
            const int idx = (ITEM_NAME_GET_TYPE(it) == NAME_TYPE_FTR0) ? (int)((it - FTR0_START) >> 2) : 0x400 + (int)((it - FTR1_START) >> 2);
            return (it & 3u) == 0u && idx >= 0 && idx < FTR_NUM;
        }
        default:
            return 0;
    }
}

int pc_sell_refusal(unsigned item, int cond, int is_sunday) {
    const mActor_name_t it = (mActor_name_t)item;
    if (item == 0u || item > 0xFFFFu || it == EMPTY_NO) {
        return PC_SELL_REF_EMPTY;
    }
    /* vanilla mSM_check_item_for_sell: only a NORMAL-condition pocket is offered; the dialogue names a QUEST item (aNSC_CHECK_BUY_REFUSE_QUEST_COND) */
    if (cond == PC_SELL_COND_QUEST) {
        return PC_SELL_REF_QUEST;
    }
    if (cond != PC_SELL_COND_NORMAL) {
        return PC_SELL_REF_PRESENT;
    }
    if (ITEM_NAME_GET_TYPE(it) == NAME_TYPE_ITEM1 && ITEM_NAME_GET_CAT(it) == ITEM1_CAT_MONEY) {
        return PC_SELL_REF_MONEY;
    }
    if ((it >= ITM_EXCERCISE_CARD00 && it <= ITM_EXCERCISE_CARD12) || it == ITM_KNIFE_AND_FORK || ITEM_IS_WISP(it)) {
        return PC_SELL_REF_SPECIAL;
    }
    if (!pc_sell_known(item)) {
        return PC_SELL_REF_UNKNOWN;
    }
    /* aNSC_check_buy_item_single: a turnip bundle on Sunday is refused; a spoiled turnip is taken off the player's hands on any day */
    if (ITEM_NAME_GET_TYPE(it) == NAME_TYPE_ITEM1 && ITEM_NAME_GET_CAT(it) == ITEM1_CAT_KABU && it != ITM_KABU_SPOILED && is_sunday) {
        return PC_SELL_REF_TURNIP_SUNDAY;
    }
    /* the host's SHOP_SELL: stationery stacks (price x stack, per-stack paper accounting) are not sold through the network seam */
    if (ITEM_IS_PAPER(it)) {
        return PC_SELL_REF_PAPER;
    }
    return PC_SELL_OK;
}

unsigned pc_sell_unit_value(unsigned item, unsigned item_price, unsigned kabu_price) {
    static const unsigned kabu_sum[4] = { 10u, 50u, 100u, 0u };
    const mActor_name_t it = (mActor_name_t)item;
    if (ITEM_NAME_GET_TYPE(it) == NAME_TYPE_ITEM1 && ITEM_NAME_GET_CAT(it) == ITEM1_CAT_KABU) {
        return kabu_price * kabu_sum[(unsigned)(it - ITM_KABU_START) & 3u]; /* spoiled = index 3 = 0 Bells */
    }
    return item_price / PC_SELL_RATIO;
}

const char* pc_sell_refusal_text(int refusal) {
    switch (refusal) {
        case PC_SELL_OK:
            return "";
        case PC_SELL_REF_QUEST:
            return "That's a delivery - Nook won't buy it";
        case PC_SELL_REF_PRESENT:
            return "Nook won't buy a wrapped present";
        case PC_SELL_REF_MONEY:
            return "Bells can't be sold";
        case PC_SELL_REF_TURNIP_SUNDAY:
            return "Nook doesn't buy turnips on Sunday";
        case PC_SELL_REF_PAPER:
            return "Stationery can't be sold here";
        case PC_SELL_REF_EMPTY:
        case PC_SELL_REF_SPECIAL:
        case PC_SELL_REF_UNKNOWN:
        default:
            return "Nook won't buy that";
    }
}

char* pc_sell_format_bells(char* out, unsigned cap, unsigned value) {
    if (out == NULL || cap == 0u) {
        return out;
    }
    if (value >= 1000000u) {
        snprintf(out, cap, "%u,%03u,%03u", value / 1000000u, (value / 1000u) % 1000u, value % 1000u);
    } else if (value >= 1000u) {
        snprintf(out, cap, "%u,%03u", value / 1000u, value % 1000u);
    } else {
        snprintf(out, cap, "%u", value);
    }
    return out;
}

void pc_sell_basket_clear(PCSellBasket* b) {
    int i;
    for (i = 0; i < PC_SELL_BASKET_CELLS; i++) {
        b->slot[i] = -1;
        b->item[i] = 0;
    }
}

int pc_sell_basket_cell_of(const PCSellBasket* b, int slot) {
    int i;
    for (i = 0; i < PC_SELL_BASKET_CELLS; i++) {
        if (b->slot[i] >= 0 && b->slot[i] == slot) {
            return i;
        }
    }
    return -1;
}

int pc_sell_basket_add(PCSellBasket* b, int slot, unsigned item) {
    int i;
    if (slot < 0 || slot >= PC_SELL_BASKET_CELLS || item == 0u || pc_sell_basket_cell_of(b, slot) >= 0) {
        return -1;
    }
    for (i = 0; i < PC_SELL_BASKET_CELLS; i++) {
        if (b->slot[i] < 0) {
            b->slot[i] = (signed char)slot;
            b->item[i] = (unsigned short)item;
            return i;
        }
    }
    return -1; /* full (cannot happen: 15 cells for 15 pockets) */
}

int pc_sell_basket_remove(PCSellBasket* b, int cell) {
    int slot;
    if (cell < 0 || cell >= PC_SELL_BASKET_CELLS || b->slot[cell] < 0) {
        return -1;
    }
    slot = b->slot[cell];
    b->slot[cell] = -1;
    b->item[cell] = 0;
    return slot;
}

int pc_sell_basket_count(const PCSellBasket* b) {
    int i, n = 0;
    for (i = 0; i < PC_SELL_BASKET_CELLS; i++) {
        n += b->slot[i] >= 0;
    }
    return n;
}

unsigned pc_sell_basket_mask(const PCSellBasket* b) {
    unsigned m = 0u;
    int i;
    for (i = 0; i < PC_SELL_BASKET_CELLS; i++) {
        if (b->slot[i] >= 0) {
            m |= 1u << (unsigned)b->slot[i];
        }
    }
    return m;
}

int pc_sell_basket_prune(PCSellBasket* b, const unsigned short* pockets, const int* conds, int is_sunday) {
    int i, n = 0;
    for (i = 0; i < PC_SELL_BASKET_CELLS; i++) {
        const int s = b->slot[i];
        if (s < 0) {
            continue;
        }
        if (pockets[s] != b->item[i] || pc_sell_refusal(pockets[s], conds[s], is_sunday) != PC_SELL_OK) {
            b->slot[i] = -1;
            b->item[i] = 0;
            n++;
        }
    }
    return n;
}

int pc_sell_basket_selection(const PCSellBasket* b, unsigned short* items, unsigned char* slots, int cap) {
    int i, n = 0;
    for (i = 0; i < PC_SELL_BASKET_CELLS && n < cap; i++) {
        if (b->slot[i] >= 0) {
            items[n] = b->item[i];
            slots[n] = (unsigned char)b->slot[i];
            n++;
        }
    }
    return n;
}

/* ---------------- native selftest ---------------- */
#define PC_SELL_I1(cat, idx) ((unsigned)(0x2000u | ((unsigned)(cat) << 8) | (unsigned)(idx)))

int pc_sell_logic_selftest(void) {
    static const struct {
        const char* name;
        unsigned item;
        int cond, sunday, expect;
    } k[] = {
        /* accepted: what vanilla's sell picker offers (NORMAL condition) */
        { "fish", PC_SELL_I1(ITEM1_CAT_FISH, 0), 0, 0, PC_SELL_OK },
        { "insect", PC_SELL_I1(ITEM1_CAT_INSECT, 3), 0, 0, PC_SELL_OK },
        { "fruit", ITM_FOOD_PEACH, 0, 0, PC_SELL_OK },
        { "tool", PC_SELL_I1(ITEM1_CAT_TOOL, 0), 0, 0, PC_SELL_OK },
        { "clothing", PC_SELL_I1(ITEM1_CAT_CLOTH, 9), 0, 0, PC_SELL_OK },
        { "carpet", PC_SELL_I1(ITEM1_CAT_CARPET, 3), 0, 0, PC_SELL_OK },
        { "wallpaper", PC_SELL_I1(ITEM1_CAT_WALL, 3), 0, 0, PC_SELL_OK },
        { "music", PC_SELL_I1(ITEM1_CAT_MINIDISK, 0), 0, 0, PC_SELL_OK },
        { "signboard (misc)", ITM_SIGNBOARD, 0, 0, PC_SELL_OK },
        { "furniture FTR0 (pocket form)", 0x1068u, 0, 0, PC_SELL_OK },
        { "furniture FTR1 (pocket form)", 0x3000u, 0, 0, PC_SELL_OK },
        { "turnips x10 (weekday)", ITM_KABU_10, 0, 0, PC_SELL_OK },
        { "spoiled turnips (Sunday too: 0 Bells, taken off the hands)", ITM_KABU_SPOILED, 0, 1, PC_SELL_OK },
        /* refused */
        { "EMPTY_NO", EMPTY_NO, 0, 0, PC_SELL_REF_EMPTY },
        { "0", 0u, 0, 0, PC_SELL_REF_EMPTY },
        { "a delivery (QUEST condition)", PC_SELL_I1(ITEM1_CAT_FRUIT, 0), PC_SELL_COND_QUEST, 0, PC_SELL_REF_QUEST },
        { "a wrapped present (PRESENT condition)", PC_SELL_I1(ITEM1_CAT_CLOTH, 2), PC_SELL_COND_PRESENT, 0, PC_SELL_REF_PRESENT },
        { "money bag 1000", PC_SELL_I1(ITEM1_CAT_MONEY, 1), 0, 0, PC_SELL_REF_MONEY },
        { "money bag 30000", PC_SELL_I1(ITEM1_CAT_MONEY, MONEY_NUM - 1), 0, 0, PC_SELL_REF_MONEY },
        { "exercise card", ITM_EXCERCISE_CARD00, 0, 0, PC_SELL_REF_SPECIAL },
        { "exercise card 12", ITM_EXCERCISE_CARD12, 0, 0, PC_SELL_REF_SPECIAL },
        { "knife and fork", ITM_KNIFE_AND_FORK, 0, 0, PC_SELL_REF_SPECIAL },
        { "Wisp spirit", ITM_SPIRIT0, 0, 0, PC_SELL_REF_SPECIAL },
        { "Wisp spirit 4", ITM_SPIRIT4, 0, 0, PC_SELL_REF_SPECIAL },
        { "turnips x100 on Sunday", ITM_KABU_START + 2, 0, 1, PC_SELL_REF_TURNIP_SUNDAY },
        { "stationery", PC_SELL_I1(ITEM1_CAT_PAPER, 0), 0, 0, PC_SELL_REF_PAPER },
        { "stationery x4", PC_SELL_I1(ITEM1_CAT_PAPER, PAPER_NUM - 1), 0, 0, PC_SELL_REF_PAPER },
        { "unknown tool index", PC_SELL_I1(ITEM1_CAT_TOOL, TOOL_NUM), 0, 0, PC_SELL_REF_UNKNOWN },
        { "placed furniture (rotation 1)", 0x1069u, 0, 0, PC_SELL_REF_UNKNOWN },
        { "scenery ITEM0", 0x0800u, 0, 0, PC_SELL_REF_UNKNOWN },
        { "structure", 0x5000u, 0, 0, PC_SELL_REF_UNKNOWN },
        { "out of range", 0x12345u, 0, 0, PC_SELL_REF_EMPTY },
    };
    const int n = (int)(sizeof(k) / sizeof(k[0]));
    int i, fail = 0;
    char buf[32];
    PCSellBasket b;
    unsigned short pockets[PC_SELL_BASKET_CELLS], items[PC_SELL_BASKET_CELLS];
    unsigned char slots[PC_SELL_BASKET_CELLS];
    int conds[PC_SELL_BASKET_CELLS];
#define SELL_CHECK(c)                                                    \
    do {                                                                 \
        if (!(c)) {                                                      \
            printf("[SELL] selftest FAIL line %d: %s\n", __LINE__, #c);  \
            fail++;                                                      \
        }                                                                \
    } while (0)
    for (i = 0; i < n; i++) {
        const int r = pc_sell_refusal(k[i].item, k[i].cond, k[i].sunday);
        if (r != k[i].expect) {
            printf("[SELL] selftest FAIL: %s 0x%04X cond %d sunday %d -> %d (expected %d)\n", k[i].name, k[i].item, k[i].cond, k[i].sunday, r, k[i].expect);
            fail++;
        }
        SELL_CHECK(pc_sell_refusal_text(r) != NULL);
    }
    /* sale values: price / 4 (integer), turnip bundles, spoiled 0 */
    SELL_CHECK(pc_sell_unit_value(PC_SELL_I1(ITEM1_CAT_FISH, 0), 1000u, 99u) == 250u);
    SELL_CHECK(pc_sell_unit_value(PC_SELL_I1(ITEM1_CAT_FISH, 0), 3u, 99u) == 0u);
    SELL_CHECK(pc_sell_unit_value(ITM_KABU_10, 0u, 100u) == 1000u);
    SELL_CHECK(pc_sell_unit_value(ITM_KABU_START + 1, 0u, 100u) == 5000u);
    SELL_CHECK(pc_sell_unit_value(ITM_KABU_START + 2, 0u, 660u) == 66000u);
    SELL_CHECK(pc_sell_unit_value(ITM_KABU_SPOILED, 0u, 660u) == 0u);
    SELL_CHECK(strcmp(pc_sell_format_bells(buf, sizeof(buf), 0u), "0") == 0);
    SELL_CHECK(strcmp(pc_sell_format_bells(buf, sizeof(buf), 999u), "999") == 0);
    SELL_CHECK(strcmp(pc_sell_format_bells(buf, sizeof(buf), 1000u), "1,000") == 0);
    SELL_CHECK(strcmp(pc_sell_format_bells(buf, sizeof(buf), 99999u), "99,999") == 0);
    SELL_CHECK(strcmp(pc_sell_format_bells(buf, sizeof(buf), 990005u), "990,005") == 0);
    SELL_CHECK(strcmp(pc_sell_format_bells(buf, sizeof(buf), 1234567u), "1,234,567") == 0);
    /* every refusal text uses the notice charset (letters, digits, space, - ' . ,) */
    for (i = 0; i < PC_SELL_REF_NUM; i++) {
        const char* t = pc_sell_refusal_text(i);
        for (; *t; t++) {
            const char c = *t;
            SELL_CHECK((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == ' ' || c == '-' || c == '\'' || c == '.' || c == ',');
        }
    }
    /* the basket: next free cell, no duplicates, take back frees the cell, the mask, the selection in cell order, prune */
    pc_sell_basket_clear(&b);
    SELL_CHECK(pc_sell_basket_count(&b) == 0 && pc_sell_basket_mask(&b) == 0u);
    SELL_CHECK(pc_sell_basket_add(&b, 4, 0x2300u) == 0);
    SELL_CHECK(pc_sell_basket_add(&b, 0, 0x2D03u) == 1);
    SELL_CHECK(pc_sell_basket_add(&b, 4, 0x2300u) == -1); /* already in */
    SELL_CHECK(pc_sell_basket_add(&b, 15, 0x2300u) == -1 && pc_sell_basket_add(&b, -1, 0x2300u) == -1 && pc_sell_basket_add(&b, 3, 0u) == -1);
    SELL_CHECK(pc_sell_basket_add(&b, 14, 0x1068u) == 2);
    SELL_CHECK(pc_sell_basket_count(&b) == 3 && pc_sell_basket_mask(&b) == ((1u << 4) | 1u | (1u << 14)));
    SELL_CHECK(pc_sell_basket_remove(&b, 1) == 0 && pc_sell_basket_remove(&b, 1) == -1 && pc_sell_basket_count(&b) == 2);
    SELL_CHECK(pc_sell_basket_add(&b, 7, ITM_FOOD_PEACH) == 1); /* the freed cell is reused */
    SELL_CHECK(pc_sell_basket_cell_of(&b, 7) == 1 && pc_sell_basket_cell_of(&b, 0) == -1);
    SELL_CHECK(pc_sell_basket_selection(&b, items, slots, PC_SELL_BASKET_CELLS) == 3 && slots[0] == 4 && slots[1] == 7 && slots[2] == 14 && items[0] == 0x2300u &&
               items[1] == ITM_FOOD_PEACH && items[2] == 0x1068u);
    for (i = 0; i < PC_SELL_BASKET_CELLS; i++) {
        SELL_CHECK(pc_sell_basket_add(&b, i, 0x2300u + (unsigned)i) >= 0 || pc_sell_basket_cell_of(&b, i) >= 0);
    }
    SELL_CHECK(pc_sell_basket_count(&b) == PC_SELL_BASKET_CELLS && pc_sell_basket_mask(&b) == 0x7FFFu); /* all 15 pockets at once */
    SELL_CHECK(pc_sell_basket_add(&b, 3, 0x2300u) == -1);
    pc_sell_basket_clear(&b);
    memset(pockets, 0xFF, sizeof(pockets));
    for (i = 0; i < PC_SELL_BASKET_CELLS; i++) {
        pockets[i] = EMPTY_NO;
        conds[i] = 0;
    }
    pockets[2] = 0x2300u;
    pockets[5] = 0x2D03u;
    pockets[9] = ITM_KABU_10;
    (void)pc_sell_basket_add(&b, 2, 0x2300u);
    (void)pc_sell_basket_add(&b, 5, 0x2D03u);
    (void)pc_sell_basket_add(&b, 9, ITM_KABU_10);
    SELL_CHECK(pc_sell_basket_prune(&b, pockets, conds, 0) == 0 && pc_sell_basket_count(&b) == 3);
    pockets[5] = EMPTY_NO; /* sold / moved meanwhile */
    SELL_CHECK(pc_sell_basket_prune(&b, pockets, conds, 1) == 2 && pc_sell_basket_count(&b) == 1 && pc_sell_basket_cell_of(&b, 2) == 0); /* + Sunday drops the turnips */
    conds[2] = PC_SELL_COND_QUEST;
    SELL_CHECK(pc_sell_basket_prune(&b, pockets, conds, 0) == 1 && pc_sell_basket_count(&b) == 0);
    printf("[SELL] selftest: %s (%d refusal cases, %d failures)\n", fail == 0 ? "PASS" : "FAIL", n, fail);
#undef SELL_CHECK
    return fail == 0 ? 0 : 1;
}
