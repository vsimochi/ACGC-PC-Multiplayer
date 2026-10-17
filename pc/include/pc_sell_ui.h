/* pc_sell_ui.h - Nook bulk sell basket (TARGET_PC; docs/multiplayer-guest-roadmap.md "Nook bulk sell basket").
 *
 * In a multiplayer session (a network CLIENT, or the listen HOST's own player) Nook's "I want to sell" opens the SELL BASKET instead of the vanilla sell picker: the
 * dresser screen (the gyroid overlay in dresser mode, pc_dresser_ui.h) in BASKET mode, a 5 x 3 grid (one cell per pocket) above the pockets. A on a pocket item ->
 * "Add to Basket" (the item moves VISUALLY into the next free cell), A on a cell -> "Take Back". Items Nook does not buy are greyed and refused with a notice
 * (pc_sell_refusal, the SAME predicate the host's SHOP_SELL validation uses). The basket is a LOCAL STAGING VIEW: no pocket, no wallet changes while it is open.
 * Closing it (B) hands the staged items to Nook as the vanilla multi-item selection (Submenu item list, the shape vanilla's X-mark "Sell all" produces): Nook
 * quotes the count and the total (PC_NOOK_MSG_SELL_BASKET, Yes / No); Yes = ONE sale: a client sends ONE SHOP_SELL (TXN_COMMIT kind 11 with the slot mask, the
 * existing wire: the host values every slot itself, all or nothing), the listen host runs the vanilla local plural sale; No = "anything else?" and the basket keeps
 * its contents for the next open in this conversation.
 * Single player / offline keeps the vanilla picker. Off switches: settings.ini [Network] bulk_sell = 0, env AC_MP_BULK_SELL=0 (this process: the vanilla picker). */
#ifndef PC_SELL_UI_H
#define PC_SELL_UI_H

#ifdef __cplusplus
extern "C" {
#endif

int  pc_sell_ui_enabled(void);    /* 1 = Nook's sell menu opens the basket (session role HOST / CLIENT, bulk_sell on) */
void pc_sell_ui_open(void);       /* the Nook seam opens the basket screen (basket mode of pc_dresser_ui; a fresh basket unless keep_next() was called) */
void pc_sell_ui_keep_next(void);  /* Nook's "No" to the quote: the next open keeps the staged items (pockets still holding them) */
int  pc_sell_ui_active(void);     /* the basket screen is open / awaiting its close */
/* the basket screen closed: writes the staged items into the vanilla selection of `submenu` (a Submenu*: item_p[k] = { item, slot }, selected_item_num = n) and
 * leaves basket mode; returns n (0 = nothing staged: the vanilla "nothing selected" path) */
int  pc_sell_ui_close(void* submenu);
int  pc_sell_ui_take_from_basket(void); /* one-shot: 1 = the selection Nook reads now came from the basket (>= 1 item) */
int  pc_sell_ui_last_count(void);       /* the count / the advisory total (the client's prices; the host's are authoritative) of the last closed basket */
unsigned pc_sell_ui_last_total(void);

/* the screen (pc_dresser_ui.c basket mode, m_tag_ovl.c, m_haniwa_ovl.c, m_inventory_ovl.c) */
int  pc_sell_ui_cell_item(int cell);    /* the item shown in basket cell `cell` (0 = empty) */
int  pc_sell_ui_pocket_hidden(int slot); /* 1 = that pocket is in the basket (not drawn in the pocket grid) */
int  pc_sell_ui_pocket_offer(int slot);  /* 1 = A on that pocket offers "Add to Basket" */
int  pc_sell_ui_put(int slot);           /* stage pocket `slot` -> PC_DRESSER_UI_ACT_DONE / _REFUSED (+ notice) / _FULL */
int  pc_sell_ui_take(int cell);          /* unstage `cell` -> PC_DRESSER_UI_ACT_DONE / _REFUSED */
void pc_sell_ui_refuse_pocket(int slot); /* A on a greyed pocket: the refusal notice (nothing for an empty / staged pocket) */
unsigned pc_sell_ui_selectable_mask(void); /* bit i = pocket i is sellable (the inventory's selectable / greyed state) */
/* the summary drawn in the basket window: line1 "N items", line2 the advisory total ("12,345"), line3 "Bells"; returns the count */
int  pc_sell_ui_header(char* l1, int c1, char* l2, int c2, char* l3, int c3);
const char* pc_sell_ui_notice(void);     /* the current basket notice (~3 s, wall clock), NULL = none */
/* the gyroid overlay's construct / destruct: a basket that was never collected (its conversation ended abnormally) is dropped at the next construct */
void pc_sell_ui_overlay_constructed(void);
void pc_sell_ui_overlay_destroyed(void);

/* TEST-ONLY (PC_NET_TEST_HOOKS, AC_TEST_HOOKS=1, AC_TEST_SELL_BASKET=1; client role; never active in normal play): per-frame driver of the real-game basket test. */
void pc_sell_ui_test_tick(void);

#ifdef __cplusplus
}
#endif
#endif
