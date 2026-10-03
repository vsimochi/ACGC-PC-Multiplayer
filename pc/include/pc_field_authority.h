/* pc_field_authority.h - persistent town-field addressing + authority helpers (multiplayer
 * foundation, Workstream C).
 *
 * Every function here operates on the PERSISTENT town field held in the save:
 *   Save_t.fg[6][5]        (mFM_fg_c, 16x16 mActor_name_t tiles each)  include/m_common_data.h:95
 *   Save_t.deposit[30][16] (u16 bit rows, "item buried here" flags)      include/m_common_data.h:117
 * and never on whatever scene-relative grid (g_fdinfo) happens to be loaded -- except that when
 * the loaded grid ALIASES Save fg (the outdoor town scene, see pcfa_scene_is_town()) a write is
 * followed by a redraw request so the visible world stays consistent.
 *
 * Addressing (identical on the wire and in every API below):
 *   acre = az * 5 + ax   ax 0..4, az 0..5   == FGBLOCKXZ_2_FGIDX(ax, az) == Save fg[az][ax]
 *                                            == Save deposit[acre]
 *   tile = uz * 16 + ux  ux, uz 0..15        == mFM_fg_c.items[uz][ux]
 *   deposit bit: row deposit[acre][uz], bit (1 << ux)   (m_field_info.c mFI_LineDepositON/OFF)
 *   town-scene absolute unit coords: ut_x = (ax + 1) * 16 + ux, ut_z = (az + 1) * 16 + uz
 *     (block (0,*), (6,*), (*,0) and (*,7..9) of the 7x10 town scene are border / beach / ocean /
 *      island blocks that are NOT backed by Save fg and are rejected; m_field_make.c:161-184)
 * Island acres and house-room floors are out of scope for this phase (not addressable).
 *
 * Decomp-independent header: <stdint.h> types only. Safe to call in any game state (title
 * screen, indoors, no save loaded, networking never started): all functions bounds-check their
 * arguments and never dereference a scene pointer they have not verified.
 */
#ifndef PC_FIELD_AUTHORITY_H
#define PC_FIELD_AUTHORITY_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PCFA_ACRE_X_NUM   5
#define PCFA_ACRE_Z_NUM   6
#define PCFA_ACRE_NUM     30
#define PCFA_TILE_NUM     256
#define PCFA_DEPOSIT_ROWS 16

/* ---- Section 2 contract API ------------------------------------------------------------- */

/* 1 once a real save is loaded and Save fg/deposit hold the live town of the current gameplay
 * session. Stateless; see pc_field_authority.c for the exact conditions. Goes to 0 on the title
 * screen, in the player-select / save scenes (where the game (re)loads or (re)initialises Save),
 * and while playing as a travelling foreigner (Save is then another town) -- except for a network CLIENT playing a guest in the host's town
 * (guests G2: the client never travels, its Save is the host's town copy). */
int      pcfa_save_ready(void);

/* 1 iff the currently loaded field grid aliases Save fg (outdoor town scene). Structural check:
 * every one of the 30 town FG blocks of g_fdinfo points exactly at its Save fg acre. */
int      pcfa_scene_is_town(void);

/* Pure coordinate math, independent of the loaded scene. 0 if (ut_x, ut_z) is not a town FG tile
 * (border/beach/ocean/island blocks, negative or out-of-range coordinates). Out pointers may be
 * NULL. */
int      pcfa_town_ut_to_acre_tile(int ut_x, int ut_z, int* acre, int* tile);
int      pcfa_acre_tile_to_town_ut(int acre, int tile, int* ut_x, int* ut_z);

/* Reads Save fg / Save deposit. Reads are allowed regardless of pcfa_save_ready() (the arrays
 * always exist in memory); callers that need meaningful data must check pcfa_save_ready().
 * pcfa_get_tile: 0 if acre/tile invalid or out_value NULL. pcfa_get_deposit: 0/1, -1 if invalid. */
int      pcfa_get_tile(int acre, int tile, uint16_t* out_value);
int      pcfa_get_deposit(int acre, int tile);

/* Writes Save fg / Save deposit directly (never a scene-relative grid). Returns 0 (and writes
 * nothing) if acre/tile is invalid OR pcfa_save_ready() is 0 -- callers must defer, not drop.
 * On a value change the acre is marked dirty. If the loaded grid aliases Save fg, a redraw of the
 * loaded view is requested (mFI_SetFGUpData(); collision reads the grid live). These functions
 * never trigger the client write-guard diagnostic (they do not go through the hooked mFI paths). */
int      pcfa_set_tile(int acre, int tile, uint16_t value);
int      pcfa_set_deposit(int acre, int tile, int on);

/* Bulk copy of one acre. items = 256 tiles in tile order, deposit = 16 rows. Either pointer may be
 * NULL to skip that part (read: not filled; write: left unchanged). Returns 0 if acre invalid, both
 * pointers NULL, or (write only) pcfa_save_ready() is 0. write marks the acre dirty if anything
 * changed and refreshes the loaded view if in town. */
int      pcfa_read_acre(int acre, uint16_t items[256], uint16_t deposit[16]);
int      pcfa_write_acre(int acre, const uint16_t items[256], const uint16_t deposit[16]);

/* Requests a redraw/collision rebuild of the loaded town view; no-op if not in town. */
void     pcfa_refresh_loaded_view(void);

/* Dirty tracking: 30-bit mask, bit n = acre n. Invalid acres ignored. */
void     pcfa_mark_acre_dirty(int acre);
void     pcfa_mark_all_dirty(void);
uint32_t pcfa_take_dirty_acres(void);    /* returns the mask and clears it */

/* Set (1) / clear (0) by B around network-originated writes that go through the vanilla mFI_*
 * paths, so the client write-guard diagnostic stays quiet for them. Plain flag, not a counter. */
void     pcfa_set_net_apply_active(int active);

/* ---- Additions beyond the contract (Workstream C) --------------------------------------- */

#define PCFA_DIRTY_ALL_MASK ((uint32_t)((1u << PCFA_ACRE_NUM) - 1u))

int      pcfa_net_apply_active(void);    /* current value of the flag above */
uint32_t pcfa_peek_dirty_acres(void);    /* mask without clearing */

/* 1 if v is an in-flight / placeholder value that must never be sent as world state (contract
 * section 4) -- i.e. pcfa_transient_kind(v) != PCFA_VALUE_STABLE. See pc_field_authority.c for
 * the source-cited list. */
int      pcfa_is_transient_value(uint16_t v);

/* Finer classification for the snapshot/delta builder:
 *   PCFA_VALUE_STABLE    persistent world state, send as-is.
 *   PCFA_VALUE_TRANSIENT always reverted by its owning actor/sequence (buried-pitfall RSV range
 *                        0x43-0x5B, MONEY_FLOWER_SEED 0x6F, DUMMY_* actor placeholders
 *                        0xF001-0xF128): never send; substitute the last stable value.
 *   PCFA_VALUE_AMBIGUOUS RSV_NO 0xFFFF, RSV_SIGNBOARD 0xFE30: usually in-flight, but ALSO used as
 *                        persistent structure-footprint fill (villager houses, event
 *                        structures). Treat as transient only for a bounded settle time. */
#define PCFA_VALUE_STABLE    0
#define PCFA_VALUE_TRANSIENT 1
#define PCFA_VALUE_AMBIGUOUS 2
int      pcfa_transient_kind(uint16_t v);

/* Hook entry points called ONLY from src/game/m_field_info.c (PC build). `slot` / `row` is the
 * address that was just written. If it lies inside Save fg / Save deposit the owning acre is
 * marked dirty (only when the value actually changed) and, on a network CLIENT outside
 * net-apply, a rate-limited [NET][FIELD][GUARD] line is logged. Addresses anywhere else (house
 * floors, island, allocated interior grids, border fill) are ignored. Never modifies anything but
 * the dirty mask. */
void     pcfa_note_tile_write(const uint16_t* slot, uint16_t old_value, uint16_t new_value);
void     pcfa_note_deposit_write(const uint16_t* row, uint16_t old_bits, uint16_t new_bits);

#ifdef __cplusplus
}
#endif
#endif /* PC_FIELD_AUTHORITY_H */
