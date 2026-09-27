/* pc_field_authority.c - persistent town-field addressing + authority helpers (Workstream C).
 * See pc/include/pc_field_authority.h for the public contract.
 *
 * Source facts this file relies on (verified against the decomp at HEAD 79b28f1):
 *
 *  - Town scene block -> Save fg aliasing, m_field_make.c:161-184 (mFM_SetFgUtPtoSaveData, only
 *    called for SCENE_FG, m_field_make.c:787-790): for the 7x10 town scene, block (bx, bz) with
 *    0 < bx < 6 and 0 < bz < 7 gets items_p = Save fg[bz - 1][bx - 1]; every other block gets
 *    l_fg_outer_fill (an RSV_NO-filled static) except the two island blocks at bz == 8
 *    (mISL_BLOCK_Z), which point at Save island.fgblock. Hence ax = bx - 1, az = bz - 1.
 *  - Scene unit addressing, m_field_info.c:377-404 + 1212-1238 (mFI_UtNum2BlockNum /
 *    mFI_GetUtNumInBK / mFI_UtNum2UtFG): bx = ut_x / 16, in-block ux = ut_x & 15, likewise z;
 *    tile index = uz * 16 + ux. So ax = ut_x / 16 - 1, az = ut_z / 16 - 1 (contract mapping
 *    confirmed), valid town FG range ut_x 16..95, ut_z 16..111.
 *  - Deposit aliasing, m_field_make.c:686-699 (mFM_SetFgDepositP, SCENE_FG): the same 30 blocks
 *    get deposit_p = Save deposit[n] with n incremented row-major, i.e. n == acre. Bit layout
 *    m_field_info.c:2132-2156: row deposit[acre][uz], bit (1 << ux).
 *  - Loaded-view refresh: mFI_UtNumtoFGSet_common(update=TRUE) (m_field_info.c:1354-1358) is just
 *    "store into the aliased grid, then mFI_SetFGUpData()". update_fg makes bg_item rebuild its
 *    draw tables (bg_item_common.c_inc:2983-2988). Item collision is NOT cached: m_collision_bg.c:155
 *    reads the grid through mFI_UtNum2UtFG() on every query. So a direct Save write while the grid
 *    aliases Save + mFI_SetFGUpData() is exactly equivalent to the vanilla update path.
 */
#include "pc_field_authority.h"
#include "pc_net_game.h" /* pc_net_game_role() only (read-only use; owned by Workstream B) */

#include "m_common_data.h"
#include "m_field_make.h"
#include "m_field_info.h"
#include "m_scene_table.h"
#include "m_land.h"
#include "m_private.h"
#include "m_name_table.h"

#include <stddef.h>
#include <stdio.h>
#include <string.h>

/* ---- layout assertions: the wire/API addressing is defined to be the save layout ---------- */
_Static_assert(sizeof(mActor_name_t) == sizeof(uint16_t), "tile values are 16-bit");
_Static_assert(FG_BLOCK_X_NUM == PCFA_ACRE_X_NUM && FG_BLOCK_Z_NUM == PCFA_ACRE_Z_NUM, "acre grid");
_Static_assert(FG_BLOCK_TOTAL_NUM == PCFA_ACRE_NUM, "acre count");
_Static_assert(UT_TOTAL_NUM == PCFA_TILE_NUM && UT_X_NUM == 16 && UT_Z_NUM == 16, "tile grid");
_Static_assert(sizeof(mFM_fg_c) == PCFA_TILE_NUM * sizeof(uint16_t), "mFM_fg_c is 16x16 u16, no padding");
_Static_assert(sizeof(((Save_t*)0)->fg) == PCFA_ACRE_NUM * sizeof(mFM_fg_c), "Save fg is 30 contiguous acres");
_Static_assert(sizeof(((Save_t*)0)->deposit) == PCFA_ACRE_NUM * PCFA_DEPOSIT_ROWS * sizeof(uint16_t),
               "Save deposit is 30 x 16 u16 rows");
_Static_assert(BLOCK_X_NUM == PCFA_ACRE_X_NUM + 2 && BLOCK_Z_NUM == PCFA_ACRE_Z_NUM + 4, "town scene is 7x10");
_Static_assert(PCFA_ACRE_NUM <= 32, "dirty mask fits in u32");

/* ---- state ------------------------------------------------------------------------------- */
static uint32_t s_dirty_mask;
static int      s_net_apply_active;
static uint32_t s_guard_hits; /* client write-guard occurrences (for rate limiting) */

/* ---- small helpers ----------------------------------------------------------------------- */
static int pcfa_acre_ok(int acre) {
    return acre >= 0 && acre < PCFA_ACRE_NUM;
}

static int pcfa_tile_ok(int tile) {
    return tile >= 0 && tile < PCFA_TILE_NUM;
}

/* Only ever called with a validated acre. */
static mActor_name_t* pcfa_acre_items(int acre) {
    return Save_Get(fg[acre / PCFA_ACRE_X_NUM][acre % PCFA_ACRE_X_NUM]).items[0];
}

/* Only ever called with a validated acre. */
static u16* pcfa_acre_deposit(int acre) {
    return Save_Get(deposit[acre]);
}

/* Maps an address inside Save fg to (acre, tile). 0 for any other address. */
static int pcfa_fg_slot_to_acre_tile(const uint16_t* slot, int* acre, int* tile) {
    uintptr_t base = (uintptr_t)&Save_Get(fg[0][0]).items[0][0];
    uintptr_t addr = (uintptr_t)slot;
    uintptr_t off;
    size_t idx;

    if (slot == NULL || addr < base) {
        return 0;
    }
    off = addr - base;
    if (off >= sizeof(Save_Get(fg)) || (off % sizeof(uint16_t)) != 0) {
        return 0;
    }
    idx = (size_t)(off / sizeof(uint16_t));
    *acre = (int)(idx / PCFA_TILE_NUM);
    *tile = (int)(idx % PCFA_TILE_NUM);
    return 1;
}

/* Maps an address of a u16 row inside Save deposit to (acre, row). 0 for any other address. */
static int pcfa_deposit_row_to_acre(const uint16_t* row, int* acre, int* uz) {
    uintptr_t base = (uintptr_t)&Save_Get(deposit[0][0]);
    uintptr_t addr = (uintptr_t)row;
    uintptr_t off;
    size_t idx;

    if (row == NULL || addr < base) {
        return 0;
    }
    off = addr - base;
    if (off >= sizeof(Save_Get(deposit)) || (off % sizeof(uint16_t)) != 0) {
        return 0;
    }
    idx = (size_t)(off / sizeof(uint16_t));
    *acre = (int)(idx / PCFA_DEPOSIT_ROWS);
    *uz = (int)(idx % PCFA_DEPOSIT_ROWS);
    return 1;
}

/* Client write-guard diagnostic (contract section 2). Log only -- never suppresses anything.
 * Quiet unless this process is a network CLIENT and no network-originated apply is in progress.
 * Rate limited: the first 16 occurrences, then one line per 1024 (with the running total), so a
 * per-frame writer can never flood the log. */
static void pcfa_guard_log(const char* what, int acre, int index, unsigned old_value, unsigned new_value) {
    if (s_net_apply_active) {
        return;
    }
    if (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT) {
        return;
    }
    s_guard_hits++;
    if (s_guard_hits <= 16u || (s_guard_hits & 1023u) == 0u) {
        printf("[NET][FIELD][GUARD] client-local %s write outside net-apply: acre=%d %s=%d old=0x%04X new=0x%04X "
               "(total %u)%s\n",
               what, acre, (what[0] == 't') ? "tile" : "row", index, old_value & 0xFFFFu, new_value & 0xFFFFu,
               (unsigned)s_guard_hits, (s_guard_hits == 16u) ? " -- further lines rate-limited" : "");
    }
}

/* ---- readiness / scene ------------------------------------------------------------------- */

/* Save fg/deposit are (re)written wholesale only at these points (see the Workstream C report):
 *   - pc_save_check_and_load / pc_save_reload -> pc_save_read_gci (pc_m_card.c:507) at boot
 *     (second_game.c:101), on the title screen (ac_animal_logo.c:157) and in common_data_reinit
 *     (m_common_data.c:28, called from trademark_init, i.e. every return to the title);
 *     common_data_reinit bzero()s common_data first, so Now_Private is NULL until re-set.
 *   - mSDI_StartInitNew -> mFM_InitFgCombiSaveData / deposit bzero (m_start_data_init.c:200-205),
 *     reached from mCD_InitGameStart_bg during the player-select scenes.
 *   - mCD_toNextLand (pc_m_card.c:1192, travel: Save := other town, player_no := mPr_FOREIGNER) and
 *     mCD_ReCheckLoadLand (pc_m_card.c:1245, return from travel, still foreigner, then
 *     PLAYERSELECT_2).
 * Every one of those happens while Now_Private is NULL, the scene is the title demo / a
 * player-select scene, or the player is a foreigner -- all of which report "not ready" here. */
int pcfa_save_ready(void) {
    int scene;

    if (Common_Get(now_private) == NULL) {
        return 0; /* boot before any load, or right after common_data_reinit / mCD_toNextLand wipes */
    }

    scene = Save_Get(scene_no);
    switch (scene) {
        case SCENE_TITLE_DEMO:        /* title screen: Save may be demo-mutated or about to be reloaded */
        case SCENE_PLAYERSELECT:      /* player select: mSDI_StartInit* (re)initialises Save here */
        case SCENE_PLAYERSELECT_2:
        case SCENE_PLAYERSELECT_3:
        case SCENE_PLAYERSELECT_SAVE: /* save / restart scene (ac_my_house_move.c_inc:439) */
        case SCENE_FIELD_TOOL:        /* debug scenes; FIELD_TOOL uses the title-demo field */
        case SCENE_FIELD_TOOL_INSIDE:
            return 0;
        default:
            break;
    }

    if (!mLd_CHECK_LAND_ID(Save_Get(land_info.id))) {
        return 0; /* zeroed / never-initialised Save */
    }

    if (Common_Get(player_no) >= mPr_FOREIGNER) {
        return 0; /* travelling: Save currently holds another town (mCD_toNextLand) */
    }

    return 1;
}

int pcfa_scene_is_town(void) {
    const mFM_fdinfo_c* fd = g_fdinfo;
    const mFM_block_info_c* blocks;
    int ax;
    int az;

    if (fd == NULL || fd->block_info == NULL) {
        return 0;
    }
    if (fd->block_x_max != BLOCK_X_NUM || fd->block_z_max != BLOCK_Z_NUM) {
        return 0;
    }

    blocks = fd->block_info;
    for (az = 0; az < PCFA_ACRE_Z_NUM; az++) {
        for (ax = 0; ax < PCFA_ACRE_X_NUM; ax++) {
            const mFM_block_info_c* b = &blocks[(az + 1) * BLOCK_X_NUM + (ax + 1)];

            if (b->fg_info.items_p != Save_Get(fg[az][ax]).items[0]) {
                return 0;
            }
        }
    }

    return 1;
}

/* ---- addressing -------------------------------------------------------------------------- */
int pcfa_town_ut_to_acre_tile(int ut_x, int ut_z, int* acre, int* tile) {
    int bx;
    int bz;
    int ax;
    int az;

    if (ut_x < 0 || ut_z < 0) {
        return 0;
    }
    bx = ut_x / UT_X_NUM;
    bz = ut_z / UT_Z_NUM;
    ax = bx - 1;
    az = bz - 1;
    if (ax < 0 || ax >= PCFA_ACRE_X_NUM || az < 0 || az >= PCFA_ACRE_Z_NUM) {
        return 0;
    }

    if (acre != NULL) {
        *acre = az * PCFA_ACRE_X_NUM + ax;
    }
    if (tile != NULL) {
        *tile = (ut_z % UT_Z_NUM) * UT_X_NUM + (ut_x % UT_X_NUM);
    }
    return 1;
}

int pcfa_acre_tile_to_town_ut(int acre, int tile, int* ut_x, int* ut_z) {
    if (!pcfa_acre_ok(acre) || !pcfa_tile_ok(tile)) {
        return 0;
    }
    if (ut_x != NULL) {
        *ut_x = (acre % PCFA_ACRE_X_NUM + 1) * UT_X_NUM + (tile % UT_X_NUM);
    }
    if (ut_z != NULL) {
        *ut_z = (acre / PCFA_ACRE_X_NUM + 1) * UT_Z_NUM + (tile / UT_X_NUM);
    }
    return 1;
}

/* ---- reads ------------------------------------------------------------------------------- */
int pcfa_get_tile(int acre, int tile, uint16_t* out_value) {
    if (!pcfa_acre_ok(acre) || !pcfa_tile_ok(tile) || out_value == NULL) {
        return 0;
    }
    *out_value = pcfa_acre_items(acre)[tile];
    return 1;
}

int pcfa_get_deposit(int acre, int tile) {
    if (!pcfa_acre_ok(acre) || !pcfa_tile_ok(tile)) {
        return -1;
    }
    return (pcfa_acre_deposit(acre)[tile / UT_X_NUM] >> (tile % UT_X_NUM)) & 1;
}

int pcfa_read_acre(int acre, uint16_t items[256], uint16_t deposit[16]) {
    if (!pcfa_acre_ok(acre) || (items == NULL && deposit == NULL)) {
        return 0;
    }
    if (items != NULL) {
        memcpy(items, pcfa_acre_items(acre), PCFA_TILE_NUM * sizeof(uint16_t));
    }
    if (deposit != NULL) {
        memcpy(deposit, pcfa_acre_deposit(acre), PCFA_DEPOSIT_ROWS * sizeof(uint16_t));
    }
    return 1;
}

/* ---- writes ------------------------------------------------------------------------------ */
void pcfa_refresh_loaded_view(void) {
    if (pcfa_scene_is_town()) {
        mFI_SetFGUpData();
    }
}

int pcfa_set_tile(int acre, int tile, uint16_t value) {
    mActor_name_t* items;

    if (!pcfa_acre_ok(acre) || !pcfa_tile_ok(tile) || !pcfa_save_ready()) {
        return 0;
    }

    items = pcfa_acre_items(acre);
    if (items[tile] != (mActor_name_t)value) {
        /* Direct Save write. In the town scene this slot IS the loaded grid slot (verified by
         * pcfa_scene_is_town()), so this plus the refresh below is exactly what
         * mFI_UtNumtoFGSet_common(value, ut_x, ut_z, TRUE) would do -- without that function's
         * scene-relative lookup, its FG_TYPE_EMPTY acre filter, or the guard hook. */
        items[tile] = (mActor_name_t)value;
        pcfa_mark_acre_dirty(acre);
        pcfa_refresh_loaded_view();
    }
    return 1;
}

int pcfa_set_deposit(int acre, int tile, int on) {
    u16* row;
    u16 bit;
    u16 new_bits;

    if (!pcfa_acre_ok(acre) || !pcfa_tile_ok(tile) || !pcfa_save_ready()) {
        return 0;
    }

    row = &pcfa_acre_deposit(acre)[tile / UT_X_NUM];
    bit = (u16)(1u << (tile % UT_X_NUM));
    new_bits = on ? (u16)(*row | bit) : (u16)(*row & (u16)~bit);
    if (new_bits != *row) {
        *row = new_bits;
        pcfa_mark_acre_dirty(acre);
        pcfa_refresh_loaded_view(); /* lets bg_item re-run its shine/hole checks; harmless otherwise */
    }
    return 1;
}

int pcfa_write_acre(int acre, const uint16_t items[256], const uint16_t deposit[16]) {
    int changed = 0;

    if (!pcfa_acre_ok(acre) || (items == NULL && deposit == NULL) || !pcfa_save_ready()) {
        return 0;
    }

    if (items != NULL) {
        mActor_name_t* dst = pcfa_acre_items(acre);

        if (memcmp(dst, items, PCFA_TILE_NUM * sizeof(uint16_t)) != 0) {
            memcpy(dst, items, PCFA_TILE_NUM * sizeof(uint16_t));
            changed = 1;
        }
    }
    if (deposit != NULL) {
        u16* dst = pcfa_acre_deposit(acre);

        if (memcmp(dst, deposit, PCFA_DEPOSIT_ROWS * sizeof(uint16_t)) != 0) {
            memcpy(dst, deposit, PCFA_DEPOSIT_ROWS * sizeof(uint16_t));
            changed = 1;
        }
    }

    if (changed) {
        pcfa_mark_acre_dirty(acre);
        pcfa_refresh_loaded_view();
    }
    return 1;
}

/* ---- dirty tracking ---------------------------------------------------------------------- */
void pcfa_mark_acre_dirty(int acre) {
    if (pcfa_acre_ok(acre)) {
        s_dirty_mask |= (uint32_t)1u << acre;
    }
}

void pcfa_mark_all_dirty(void) {
    s_dirty_mask = PCFA_DIRTY_ALL_MASK;
}

uint32_t pcfa_take_dirty_acres(void) {
    uint32_t mask = s_dirty_mask & PCFA_DIRTY_ALL_MASK;

    s_dirty_mask = 0;
    return mask;
}

uint32_t pcfa_peek_dirty_acres(void) {
    return s_dirty_mask & PCFA_DIRTY_ALL_MASK;
}

void pcfa_set_net_apply_active(int active) {
    s_net_apply_active = active ? 1 : 0;
}

int pcfa_net_apply_active(void) {
    return s_net_apply_active;
}

/* ---- hooks (called from src/game/m_field_info.c, PC build only) --------------------------- */
void pcfa_note_tile_write(const uint16_t* slot, uint16_t old_value, uint16_t new_value) {
    int acre;
    int tile;

    if (old_value == new_value) {
        return; /* no state change: nothing to broadcast, nothing to warn about */
    }
    if (!pcfa_fg_slot_to_acre_tile(slot, &acre, &tile)) {
        return; /* not the persistent town field (room floor, island, interior grid, border fill) */
    }
    pcfa_mark_acre_dirty(acre);
    pcfa_guard_log("tile", acre, tile, old_value, new_value);
}

void pcfa_note_deposit_write(const uint16_t* row, uint16_t old_bits, uint16_t new_bits) {
    int acre;
    int uz;

    if (old_bits == new_bits) {
        return;
    }
    if (!pcfa_deposit_row_to_acre(row, &acre, &uz)) {
        return; /* e.g. Save island.deposit rows */
    }
    pcfa_mark_acre_dirty(acre);
    pcfa_guard_log("deposit", acre, uz, old_bits, new_bits);
}

/* ---- transient values -------------------------------------------------------------------- */
_Static_assert(BURIED_PITFALL_HOLE_RSV_START == 0x0043 && BURIED_PITFALL_HOLE_RSV_END == 0x005B, "pitfall RSV range");
_Static_assert(BURIED_PITFALL_HOLE_RSV_START - BURIED_PITFALL_HOLE_START == 0x19, "pit_fall/pit_fall_stop offset");
_Static_assert(MONEY_FLOWER_SEED == 0x006F && MONEY_ROCK_A - ROCK_A == 7, "money rock hit window");
_Static_assert(DUMMY_MAILBOX0 == 0xF001 && DUMMY_BOAT == 0xF128, "DUMMY_* actor placeholder range");
_Static_assert(RSV_NO == 0xFFFF, "RSV_NO");
_Static_assert(RSV_SIGNBOARD == 0xFE30, "RSV_SIGNBOARD (m_name_table.h:3975)");

/* Source-cited classification (bg_item_common.c_inc line numbers at HEAD 79b28f1):
 *
 * PCFA_VALUE_TRANSIENT -- only ever present while a live actor / bg_item sequence owns the tile,
 * and always rewritten by that owner (on completion AND on teardown). Never persistent.
 *  - BURIED_PITFALL_HOLE_RSV00..24 (0x0043-0x005B): bIT_actor_pit_fall writes pitfall+0x19 when a
 *    player triggers a buried pitfall (3024-3025); the pit sequence it starts overwrites it with
 *    RSV_NO after one tick (2433-2440) and then HOLE_START+n (2465-2466); bIT_actor_pit_fall_stop
 *    reverts it to the buried pitfall (value-0x19, 3035-3038); pit teardown writes EMPTY_NO
 *    (2505-2513). Read by the player fall state (m_player_common.c_inc:6548).
 *  - MONEY_FLOWER_SEED (0x006F): written when a money rock is hit (228); reverted to
 *    fg_item-7 (MONEY_ROCK_x -> ROCK_x) when the 386..446-frame window ends (225, 335-337) or on
 *    bg_item teardown (394-402). The pre-hit value MONEY_ROCK_x is the last stable value; ROCK_x
 *    is the settled value. 5 ten-coin slots (bg_item.h:19) >= the at most 4 money rocks per day
 *    (mAGrw_SetMoneyStone_player), so the slot-steal path (176-196) cannot strand one.
 *  - DUMMY_* (0xF001 DUMMY_MAILBOX0 .. 0xF128 DUMMY_BOAT): "actor alive here" placeholders written
 *    by structure/prop actor init (e.g. ac_house_move.c_inc:503, ac_mailbox_move.c_inc:206,
 *    ac_reserve_move.c_inc:68, ac_haniwa_move.c_inc:781) and replaced with the real spawner name
 *    (actor->npc_id) by restore_fgdata on actor delete and before every save
 *    (m_actor.c:841-856, 898-914). Sending one would stop the receiver from ever spawning that
 *    structure. (0xF128 is also RSV_POLICE_ITEM_0, an indoor-only value.)
 *
 * PCFA_VALUE_AMBIGUOUS -- RSV_NO (0xFFFF). Transient in every bg_item sequence (in-flight drop
 * target 239/961/1350/1379, dig 1159, bury/fill 1195/1220, pit 2415/2437; replaced on landing /
 * completion / teardown) and structure door fronts while the actor lives (ac_house_move.c_inc:25-33,
 * EMPTY_NO on dt; same in ac_buggy/br_shop/kamakura/tent/toudai). BUT it is also PERSISTENT
 * footprint fill: villager houses (m_npc.c:2651-2685, 7 RSV_NO tiles, Save-direct), event/structure
 * placement (mFI_SetFGStructure_common fill table, m_field_info.c:~2687), shop upgrade ground clean
 * (ac_shop_level.c:187); and a dig entry that fails because the single hole slot (bIT_HOLE_NUM 1)
 * is busy leaves its RSV_NO behind (1158-1163). The contract lists RSV_NO as transient, so
 * pcfa_is_transient_value() keeps returning 1 for it; see the Workstream C report for the
 * recommended settle-timeout handling.
 *
 * PCFA_VALUE_AMBIGUOUS -- RSV_SIGNBOARD (0xFE30, m_name_table.h:3975). Written while a planted sign
 * wobbles (ac_sign.c:635), replaced by the placed signboard value after ~20-30 frames (ac_sign.c:715),
 * but aSIGN_actor_dt (ac_sign.c:72) does not finalize, so a mid-wobble teardown leaves it in Save
 * permanently. Same bounded-settle treatment as RSV_NO.
 *
 * Deliberately STABLE (persistent world state): HOLE00-24 (0x11-0x29), HOLE_SHINE, SHINE_SPOT,
 * BURIED_PITFALL_HOLE00-24 (0x2A-0x42), ROCK_*, MONEY_ROCK_*, SIGN reserve values
 * (mFM_RenewalReserve), RSV_BRIDGE0/1 / RSV_DOOR (static field markers), structure / prop spawner
 * names (0x5xxx, 0xAxxx), RSV_WALL_NO/RSV_FE1C/RSV_FE1F (rooms only). */
int pcfa_transient_kind(uint16_t v) {
    if (v == RSV_NO || v == RSV_SIGNBOARD) {
        return PCFA_VALUE_AMBIGUOUS;
    }
    if (v >= BURIED_PITFALL_HOLE_RSV_START && v <= BURIED_PITFALL_HOLE_RSV_END) {
        return PCFA_VALUE_TRANSIENT;
    }
    if (v == MONEY_FLOWER_SEED) {
        return PCFA_VALUE_TRANSIENT;
    }
    if (v >= DUMMY_MAILBOX0 && v <= DUMMY_BOAT) {
        return PCFA_VALUE_TRANSIENT;
    }
    return PCFA_VALUE_STABLE;
}

int pcfa_is_transient_value(uint16_t v) {
    return pcfa_transient_kind(v) != PCFA_VALUE_STABLE;
}
