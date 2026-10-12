/* dresser_items_selftest.c - native item-class matrix of the dresser storage predicate (pc/src/pc_dresser_items.c) + the sidecar round trip
 * (PUT / save / reload / TAKE returns the same u16) for every accepted class. Needs the game headers (macros only), no game function.
 * Build + run (git-bash, PATH has /c/msys64/ucrt64/bin appended; <scratch> OUTSIDE pc/build64):
 *   gcc -std=gnu11 -w -DTARGET_PC -DPC_LOW_ADDRESS_64 -D_LANGUAGE_C -DF3DEX_GBI_2 -DBUGFIXES -DPC_ENHANCEMENTS -DVERSION=0 -Dnullptr=NULL \
 *       @pc/build64/CMakeFiles/ac_pc.dir/includes_C.rsp pc/tools/net_spike/dresser_items_selftest.c pc/src/pc_dresser_items.c pc/src/pc_dresser_store.c \
 *       pc/src/pc_tabfile.c pc/src/pc_keytab.c -o <scratch>/dresser_items_selftest.exe
 *   <scratch>/dresser_items_selftest.exe <scratch dir> */
#include <stdio.h>
#include <string.h>

#include "m_name_table.h"
#include "pc_dresser_items.h"
#include "pc_dresser_store.h"

static int s_fail = 0;
#define CHECK(c)                                                        \
    do {                                                                \
        if (!(c)) {                                                     \
            printf("FAIL line %d: %s\n", __LINE__, #c);                 \
            s_fail++;                                                   \
        }                                                               \
    } while (0)

#define I1(cat, idx) ((unsigned)(0x2000u | ((unsigned)(cat) << 8) | (unsigned)(idx)))

typedef struct {
    const char* name;
    unsigned item;
    int cls;     /* expected PC_DRESSER_ITEM_* */
    int vanilla; /* expected vanilla-slot filter */
} Case;

int main(int argc, char** argv) {
    static const Case k[] = {
        /* accepted: ordinary (also the vanilla filter) */
        { "stationery (quantity in the id)", I1(ITEM1_CAT_PAPER, 0), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "stationery x4 (last)", I1(ITEM1_CAT_PAPER, PAPER_NUM - 1), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "money bag", I1(ITEM1_CAT_MONEY, MONEY_NUM - 1), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "tool", I1(ITEM1_CAT_TOOL, 0), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "clothing", I1(ITEM1_CAT_CLOTH, 9), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "misc (etc)", I1(ITEM1_CAT_ETC, 0), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "carpet", I1(ITEM1_CAT_CARPET, 3), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "wallpaper", I1(ITEM1_CAT_WALL, 3), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "fruit", I1(ITEM1_CAT_FRUIT, 0), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "plant / sapling / seed", I1(ITEM1_CAT_PLANT, 0), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "music", I1(ITEM1_CAT_MINIDISK, 0), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "diary", I1(ITEM1_CAT_DUMMY, 0), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "ticket", I1(ITEM1_CAT_TICKET, 0), PC_DRESSER_ITEM_ORDINARY, 1 },
        { "lucky bag", I1(ITEM1_CAT_HUKUBUKURO, 0), PC_DRESSER_ITEM_ORDINARY, 1 },
        /* accepted: extras only */
        { "fish", I1(ITEM1_CAT_FISH, 0), PC_DRESSER_ITEM_CREATURE, 0 },
        { "insect", I1(ITEM1_CAT_INSECT, 0), PC_DRESSER_ITEM_CREATURE, 0 },
        { "furniture FTR0 (pocket form)", 0x1068u, PC_DRESSER_ITEM_FURNITURE, 0 },
        { "furniture FTR1 (pocket form)", 0x3000u, PC_DRESSER_ITEM_FURNITURE, 0 },
        { "furniture FTR1, last index", (unsigned)(FTR1_START + (FTR_NUM - 1 - 0x400) * 4), PC_DRESSER_ITEM_FURNITURE, 0 },
        /* rejected */
        { "EMPTY_NO", EMPTY_NO, PC_DRESSER_ITEM_REJECT, 0 },
        { "turnips", I1(ITEM1_CAT_KABU, 0), PC_DRESSER_ITEM_REJECT, 0 },
        { "knife and fork", ITM_KNIFE_AND_FORK, PC_DRESSER_ITEM_REJECT, 0 },
        { "exercise card", ITM_EXCERCISE_CARD00, PC_DRESSER_ITEM_REJECT, 0 },
        { "Wisp spirit", ITM_SPIRIT0, PC_DRESSER_ITEM_REJECT, 0 },
        { "unknown tool index", I1(ITEM1_CAT_TOOL, TOOL_NUM), PC_DRESSER_ITEM_REJECT, 0 },
        { "unknown fruit index", I1(ITEM1_CAT_FRUIT, FRUIT_NUM), PC_DRESSER_ITEM_REJECT, 0 },
        { "placed furniture (rotation 1)", 0x1069u, PC_DRESSER_ITEM_REJECT, 0 },
        { "placed furniture (rotation 3)", 0x306Bu, PC_DRESSER_ITEM_REJECT, 0 },
        { "unknown furniture index", (unsigned)(FTR1_START + (FTR_NUM - 0x400) * 4), PC_DRESSER_ITEM_REJECT, 0 },
        { "scenery ITEM0 (signboard range)", 0x0800u, PC_DRESSER_ITEM_REJECT, 0 },
        { "ITEM2", 0x8000u, PC_DRESSER_ITEM_REJECT, 0 },
        { "structure", 0x5000u, PC_DRESSER_ITEM_REJECT, 0 },
        { "reserved id", 0xFE1Bu, PC_DRESSER_ITEM_REJECT, 0 },
        { "out of range", 0x12345u, PC_DRESSER_ITEM_REJECT, 0 },
    };
    const int n = (int)(sizeof(k) / sizeof(k[0]));
    PCDresserStore st;
    PCDresserTxn t;
    PCDresserResult r;
    uint8_t key[PC_DRESSER_KEY_LEN], own[8] = { 7, 7, 7, 7, 7, 7, 7, 7 };
    char path[600];
    int i, accepted = 0;
    unsigned seq = 1;
    snprintf(path, sizeof(path), "%s/dresser_items_test.dat", argc > 1 ? argv[1] : ".");
    remove(path);
    for (i = 0; i < n; i++) {
        const int c = pc_dresser_item_class(k[i].item);
        const int ok = c == k[i].cls && pc_dresser_item_extra_ok(k[i].item) == (k[i].cls != PC_DRESSER_ITEM_REJECT) &&
                       pc_dresser_item_vanilla_ok(k[i].item) == k[i].vanilla;
        printf("%s %-36s 0x%04X class %d vanilla %d\n", ok ? "ok  " : "FAIL", k[i].name, k[i].item, c, pc_dresser_item_vanilla_ok(k[i].item));
        CHECK(ok);
    }
    /* sidecar round trip for every accepted class: PUT (auto slot) -> save -> reload -> the same u16 in the slot -> TAKE returns it */
    CHECK(pc_dresser_store_init(&st, 8));
    pc_dresser_key_make(key, own, 0, 0, 3, 4, 0x1068);
    for (i = 0; i < n; i++) {
        if (k[i].cls == PC_DRESSER_ITEM_REJECT) {
            continue;
        }
        memset(&t, 0, sizeof(t));
        t.nonce = 0xABCDu;
        t.seq = seq++;
        t.msg_hash = 0x1000u + (unsigned)i;
        t.op = PC_DRESSER_OP_PUT;
        t.slot = 0xFF;
        t.item = (uint16_t)k[i].item;
        CHECK(pc_dresser_store_apply(&st, key, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED);
        accepted++;
    }
    CHECK(accepted <= PC_DRESSER_SLOTS && pc_dresser_store_used(pc_dresser_store_find(&st, key)) == accepted);
    CHECK(pc_dresser_store_save(&st, path));
    pc_dresser_store_free(&st);
    CHECK(pc_dresser_store_init(&st, 8) && pc_dresser_store_load(&st, path) == 1);
    {
        const PCDresserRec* rec = pc_dresser_store_find(&st, key);
        int slot = 0;
        CHECK(rec != NULL);
        for (i = 0; rec != NULL && i < n; i++) {
            if (k[i].cls == PC_DRESSER_ITEM_REJECT) {
                continue;
            }
            CHECK(rec->items[slot] == (uint16_t)k[i].item); /* persisted as the same u16 (furniture keeps its pocket form) */
            memset(&t, 0, sizeof(t));
            t.nonce = 0xABCDu;
            t.seq = seq++;
            t.msg_hash = 0x2000u + (unsigned)i;
            t.op = PC_DRESSER_OP_TAKE;
            t.slot = (uint8_t)slot;
            t.item = (uint16_t)k[i].item;
            CHECK(pc_dresser_store_apply(&st, key, &t, &r) && r.outcome == PC_DRESSER_OUT_APPLIED && r.item == (uint16_t)k[i].item);
            rec = pc_dresser_store_find(&st, key);
            slot++;
        }
        CHECK(rec != NULL && pc_dresser_store_used(rec) == 0);
    }
    pc_dresser_store_free(&st);
    remove(path);
    printf("dresser_items_selftest: %s (%d failures, %d classes accepted)\n", s_fail == 0 ? "PASS" : "FAIL", s_fail, accepted);
    return s_fail == 0 ? 0 : 1;
}
