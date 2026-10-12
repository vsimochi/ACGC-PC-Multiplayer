/* pc_dresser_items.c - see pc_dresser_items.h. Item-id rules only; macros of m_name_table.h (no game function is called, so the native selftest links it alone). */
#include "pc_dresser_items.h"

#include "m_name_table.h"

/* the KNOWN item1 indexes per category (= m_name_table.c mNT_check_unknown) */
static const short k_item1_kinds[16] = {
    PAPER_NUM, MONEY_NUM, TOOL_NUM, FISH_NUM, CLOTH_NUM, ETC_NUM, CARPET_NUM, WALL_NUM, FRUIT_NUM, PLANT_NUM, MINIDISK_NUM, DIARY_NUM, TICKET_NUM, INSECT_NUM,
    HUKUBUKURO_NUM, KABU_NUM,
};

int pc_dresser_item_class(unsigned item) {
    const mActor_name_t it = (mActor_name_t)item;
    if (item == 0u || item > 0xFFFFu || it == EMPTY_NO) {
        return PC_DRESSER_ITEM_REJECT;
    }
    switch (ITEM_NAME_GET_TYPE(it)) {
        case NAME_TYPE_ITEM1: {
            const int cat = ITEM_NAME_GET_CAT(it);
            if (cat < 0 || cat >= 16 || (int)ITEM_NAME_GET_INDEX(it) >= k_item1_kinds[cat]) {
                return PC_DRESSER_ITEM_REJECT; /* unknown item */
            }
            if (cat == ITEM1_CAT_KABU || it == ITM_KNIFE_AND_FORK || (it >= ITM_EXCERCISE_CARD00 && it <= ITM_EXCERCISE_CARD12) || ITEM_IS_WISP(it)) {
                return PC_DRESSER_ITEM_REJECT;
            }
            return (cat == ITEM1_CAT_FISH || cat == ITEM1_CAT_INSECT) ? PC_DRESSER_ITEM_CREATURE : PC_DRESSER_ITEM_ORDINARY;
        }
        case NAME_TYPE_FTR0:
        case NAME_TYPE_FTR1: {
            /* = m_room_type.c mRmTp_FtrItemNo2FtrIdx; the pocket form has rotation 0 */
            const int idx = (ITEM_NAME_GET_TYPE(it) == NAME_TYPE_FTR0) ? (int)((it - FTR0_START) >> 2) : 0x400 + (int)((it - FTR1_START) >> 2);
            if ((it & 3u) != 0u || idx < 0 || idx >= FTR_NUM) {
                return PC_DRESSER_ITEM_REJECT;
            }
            return PC_DRESSER_ITEM_FURNITURE;
        }
        default:
            return PC_DRESSER_ITEM_REJECT;
    }
}

int pc_dresser_item_extra_ok(unsigned item) {
    return pc_dresser_item_class(item) != PC_DRESSER_ITEM_REJECT;
}

int pc_dresser_item_vanilla_ok(unsigned item) {
    return pc_dresser_item_class(item) == PC_DRESSER_ITEM_ORDINARY; /* = mSM_check_item_for_furniture: ITEM1, no fish / insect / turnip / knife+fork / cards */
}
