/* pc_sell_logic.h - Nook bulk sell basket (docs/multiplayer-guest-roadmap.md "Nook bulk sell basket"): the PURE rules shared by the basket screen (client and
 * listen host), the Nook dialogue seam and the HOST's SHOP_SELL validation (pc_net_game.c pcnetgame_shop_sell_unit). No game state, no game function: item-id macros of
 * m_name_table.h only, so the native selftest (pc/tools/net_spike/test_sell_basket_native.py) compiles it alone.
 *
 * WHAT NOOK BUYS (one predicate, pc_sell_refusal): the vanilla sell-mode pocket filter m_submenu.c mSM_check_item_for_sell (a NORMAL-condition item that is not a
 * money bag, an exercise card, the knife and fork or a Wisp spirit), plus the vanilla dialogue refusals of ac_npc_shop_common.c (quest / delivery items:
 * aNSC_CHECK_BUY_REFUSE_QUEST_COND; turnips on Sunday: aNSC_CHECK_BUY_NO_SUNDAY_TURNIPS), plus the network rule of the host's SHOP_SELL (stationery stacks are
 * NOT_SELLABLE through the network seam), plus unknown ids. Worthless items (sale value 0, e.g. spoiled turnips) ARE accepted, exactly like vanilla ("I'll take
 * it off your hands").
 * SALE VALUE (pc_sell_unit_value): vanilla aNSC_buy_check_init: turnips = the turnip price x the bundle (10 / 50 / 100, spoiled 0), everything else
 * mSP_ItemNo2ItemPrice / SELL_BUY_RATIO (4). The total of a basket is the plain sum (no sale-day multiplier exists for selling). The caller supplies the two
 * game numbers (the item price and the turnip price). */
#ifndef PC_SELL_LOGIC_H
#define PC_SELL_LOGIC_H

#ifdef __cplusplus
extern "C" {
#endif

#define PC_SELL_BASKET_CELLS 15 /* one cell per pocket slot (mPr_POCKETS_SLOT_COUNT): every pocket can be sold at once */
#define PC_SELL_BASKET_COLS 5   /* the basket grid on screen: 5 x 3, like the pockets */
#define PC_SELL_BASKET_ROWS 3
#define PC_SELL_RATIO 4         /* == SELL_BUY_RATIO (include/ac_npc_shop_common.h) == PC_NETGAME_SHOP_SELL_RATIO */

/* the pocket condition values (== mPr_ITEM_COND_*, include/m_private.h; pinned by a static assert in pc_sell_ui.c) */
#define PC_SELL_COND_NORMAL 0
#define PC_SELL_COND_PRESENT 1
#define PC_SELL_COND_QUEST 2

enum {
    PC_SELL_OK = 0,
    PC_SELL_REF_EMPTY = 1,          /* nothing in the slot */
    PC_SELL_REF_QUEST = 2,          /* a delivery / errand item (QUEST condition) */
    PC_SELL_REF_PRESENT = 3,        /* a wrapped present (PRESENT condition) */
    PC_SELL_REF_MONEY = 4,          /* a money bag */
    PC_SELL_REF_SPECIAL = 5,        /* an exercise card, the knife and fork, a Wisp spirit */
    PC_SELL_REF_TURNIP_SUNDAY = 6,  /* Nook does not buy turnips on Sunday */
    PC_SELL_REF_PAPER = 7,          /* stationery stacks are not sold through the network seam */
    PC_SELL_REF_UNKNOWN = 8,        /* an unknown / non-pocket item id */
    PC_SELL_REF_NUM
};

/* PC_SELL_OK or the refusal of `item` in a pocket of condition `cond` (PC_SELL_COND_*); `is_sunday` = today is Sunday (the turnip rule). */
int pc_sell_refusal(unsigned item, int cond, int is_sunday);
/* the sale value of one sellable item: `item_price` = mSP_ItemNo2ItemPrice(item), `kabu_price` = Kabu_get_price() (turnips only). */
unsigned pc_sell_unit_value(unsigned item, unsigned item_price, unsigned kabu_price);
/* the player-facing text of a refusal (the basket notice), never NULL */
const char* pc_sell_refusal_text(int refusal);
/* "12,345" (thousands separated by commas); returns out */
char* pc_sell_format_bells(char* out, unsigned cap, unsigned value);

/* ---- the basket: a LOCAL staging view (cell -> pocket slot + the item it showed). Nothing in the pockets changes until the host's APPLIED of the sale. ---- */
typedef struct PCSellBasket {
    signed char slot[PC_SELL_BASKET_CELLS];     /* the pocket slot in that cell, -1 = empty cell */
    unsigned short item[PC_SELL_BASKET_CELLS];  /* the item that pocket held when it was put in (a changed pocket drops out: pc_sell_basket_prune) */
} PCSellBasket;

void pc_sell_basket_clear(PCSellBasket* b);
int  pc_sell_basket_cell_of(const PCSellBasket* b, int slot);  /* the cell holding pocket `slot`, -1 = none */
int  pc_sell_basket_add(PCSellBasket* b, int slot, unsigned item); /* the next free cell (lowest index), -1 = bad slot / already in / full */
int  pc_sell_basket_remove(PCSellBasket* b, int cell);           /* the pocket slot taken back out of `cell`, -1 = the cell was empty */
int  pc_sell_basket_count(const PCSellBasket* b);
unsigned pc_sell_basket_mask(const PCSellBasket* b);            /* bit i = pocket slot i is in the basket (the SHOP_SELL slot mask) */
/* drops every cell whose pocket no longer holds the remembered item or whose item is no longer sellable (pockets / conds = the current 15 pockets); returns
 * the number of cells dropped */
int  pc_sell_basket_prune(PCSellBasket* b, const unsigned short* pockets, const int* conds, int is_sunday);
/* the selection the Nook dialogue reads (the vanilla Submenu item list): the basket's (item, slot) pairs in CELL order, compacted; returns the count */
int  pc_sell_basket_selection(const PCSellBasket* b, unsigned short* items, unsigned char* slots, int cap);

/* the native selftest: 0 = PASS (prints "[SELL] selftest: PASS") */
int pc_sell_logic_selftest(void);

#ifdef __cplusplus
}
#endif
#endif
