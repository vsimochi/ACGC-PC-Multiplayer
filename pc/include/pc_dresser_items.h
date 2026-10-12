/* pc_dresser_items.h - which pocket items a dresser may store (docs/dresser-storage-redesign-plan.md 6f). ONE predicate shared by the host commit check, the
 * client STATE check, the UI put-in predicate and the put-in tag (all through pc_net_game.c pcnetgame_dresser_item_ok / pc_net_game_dresser_item_storable).
 * Item-id rules only (the pocket CONDITION is checked by the callers: only NORMAL pockets are put-in candidates, so wrapped presents (PRESENT) and work
 * parcels (QUEST) never qualify; the 608-byte record stores a bare u16 per slot, no condition).
 *
 * EXTRA slots (24, host sidecar):
 *   accepted: every ITEM1 item of a KNOWN index (stationery incl. its quantity encoded in the id, money bags, tools, clothing / umbrellas, misc, carpets,
 *             wallpaper, fruit, plants / saplings / seeds, music, diaries, tickets, fish, insects, lucky bags) EXCEPT turnips (they age by item id),
 *             the knife and fork, the exercise cards and the Wisp spirits;
 *             furniture (FTR0 / FTR1) in POCKET form: rotation bits 0 (a picked-up piece is mRmTp_FtrIdx2FtrItemNo(name, SOUTH)) and a known index;
 *   rejected: EMPTY_NO, every other id type (scenery ITEM0, ITEM2, warps, structures, actors, props, reserved ids), unknown indexes.
 * VANILLA slots (3, the house image's upper layers at the piece): exactly the vanilla filter m_submenu.c mSM_check_item_for_furniture (item part). */
#ifndef PC_DRESSER_ITEMS_H
#define PC_DRESSER_ITEMS_H

#ifdef __cplusplus
extern "C" {
#endif

enum {
    PC_DRESSER_ITEM_REJECT = 0,
    PC_DRESSER_ITEM_ORDINARY = 1, /* ITEM1, not fish / insect */
    PC_DRESSER_ITEM_CREATURE = 2, /* ITEM1 fish / insect (not a Wisp spirit) */
    PC_DRESSER_ITEM_FURNITURE = 3 /* FTR0 / FTR1, pocket form */
};

int pc_dresser_item_class(unsigned item); /* PC_DRESSER_ITEM_* */
int pc_dresser_item_extra_ok(unsigned item);   /* the 24 extra slots */
int pc_dresser_item_vanilla_ok(unsigned item); /* the 3 vanilla keep slots (the vanilla filter) */

#ifdef __cplusplus
}
#endif
#endif
