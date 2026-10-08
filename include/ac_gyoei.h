#ifndef AC_GYOEI_H
#define AC_GYOEI_H

#include "types.h"
#include "ac_tools.h"
#include "ac_gyoei_h.h"

#ifdef __cplusplus
extern "C" {
#endif

#define aGYO_MAX_GYOEI 2
#define aGYO_EXIST_MAX 4

enum {
    aGYO_GYO_TYPE_TEST,
    aGYO_GYO_TYPE_KASEKI,

    aGYO_GYO_TYPE_NUM
};

enum {
    aGYO_ROD_NORMAL,
    aGYO_ROD_GOLDEN,

    aGYO_ROD_NUM
};

enum {
    aGYO_DRAW_TYPE_GYOEI,
    aGYO_DRAW_TYPE_FISH,

    aGYO_DRAW_TYPE_NUM
};

/* TODO: should we use the internal names for fish? */
enum fish_type {
    aGYO_TYPE_CRUCIAN_CARP,
    aGYO_TYPE_BROOK_TROUT,
    aGYO_TYPE_CARP,
    aGYO_TYPE_KOI,
    aGYO_TYPE_CATFISH,
    aGYO_TYPE_SMALL_BASS,
    aGYO_TYPE_BASS,
    aGYO_TYPE_LARGE_BASS,
    aGYO_TYPE_BLUEGILL,
    aGYO_TYPE_GIANT_CATFISH,
    aGYO_TYPE_GIANT_SNAKEHEAD,
    aGYO_TYPE_BARBEL_STEED,
    aGYO_TYPE_DACE,
    aGYO_TYPE_PALE_CHUB,
    aGYO_TYPE_BITTERLING,
    aGYO_TYPE_LOACH,
    aGYO_TYPE_POND_SMELT,
    aGYO_TYPE_SWEETFISH,
    aGYO_TYPE_CHERRY_SALMON,
    aGYO_TYPE_LARGE_CHAR,
    aGYO_TYPE_RAINBOW_TROUT,
    aGYO_TYPE_STRINGFISH,
    aGYO_TYPE_SALMON,
    aGYO_TYPE_GOLDFISH,
    aGYO_TYPE_PIRANHA,
    aGYO_TYPE_AROWANA,
    aGYO_TYPE_EEL,
    aGYO_TYPE_FRESHWATER_GOBY,
    aGYO_TYPE_ANGELFISH,
    aGYO_TYPE_GUPPY,
    aGYO_TYPE_POPEYED_GOLDFISH,
    aGYO_TYPE_COELACANTH,
    aGYO_TYPE_CRAWFISH,
    aGYO_TYPE_FROG,
    aGYO_TYPE_KILLIFISH,
    aGYO_TYPE_JELLYFISH,
    aGYO_TYPE_SEA_BASS,
    aGYO_TYPE_RED_SNAPPER,
    aGYO_TYPE_BARRED_KNIFEJAW,
    aGYO_TYPE_ARAPAIMA,

    aGYO_TYPE_NUM,

    /* non-fish fish */
    aGYO_TYPE_WHALE = aGYO_TYPE_NUM,
    aGYO_TYPE_EMPTY_CAN,
    aGYO_TYPE_BOOT,
    aGYO_TYPE_OLD_TIRE,
    aGYO_TYPE_SALMON2,

    aGYO_TYPE_EXTENDED_NUM
};

#define aGYO_TYPE_INVALID -1

#define aGYO_IS_FISH_TRASH(type) ((type) >= aGYO_TYPE_EMPTY_CAN && (type) <= aGYO_TYPE_OLD_TIRE)

enum {
    aGYO_SIZE_XXS,
    aGYO_SIZE_XS,
    aGYO_SIZE_S,
    aGYO_SIZE_M,
    aGYO_SIZE_L,
    aGYO_SIZE_XL,
    aGYO_SIZE_XXL,
    aGYO_SIZE_WHALE,

    aGYO_SIZE_NUM
};

typedef struct gyo_ctrl_actor_s aGYO_CTRL_ACTOR;

typedef void (*aGYO_ACT_PROC)(ACTOR*, GAME*);

/* sizeof(aGYO_CTRL_ACTOR) == 0x254 */
struct gyo_ctrl_actor_s {
    /* 0x000 */ TOOLS_ACTOR tools_class;
    /* 0x1CC */ ACTOR* linked_actor; /* Generally UKI_ACTOR */
    /* 0x1D0 */ int exist;
    /* 0x1D4 */ int draw_type;
    /* 0x1D8 */ int gyo_type;
    /* 0x1DC */ s16 size_type;
    /* 0x1E0 */ int action;
    /* 0x1E4 */ aGYO_ACT_PROC act_proc;
    /* 0x1E8 */ int anim_frame;
    /* 0x1EC */ f32 speed_step;
    /* 0x1F0 */ f32 speed;
    /* 0x1F4 */ f32 range;
    /* 0x1F8 */ int _1F8;
    /* 0x1FC */ ClObjPipe_c col_pipe;
    /* 0x218 */ int work0;
    /* 0x21C */ f32 fwork0;
    /* 0x220 */ f32 fwork1;
    /* 0x224 */ f32 fwork2;
    /* 0x228 */ f32 fwork3;
    /* 0x22C */ s16 swork0;
    /* 0x22E */ s16 swork1;
    /* 0x230 */ s16 swork2;
    /* 0x232 */ s16 swork3;
    /* 0x234 */ s16 swork4;
    /* 0x236 */ s16 move_counter;
    /* 0x238 */ s16 move_angle;
    /* 0x23A */ s16 pattern_subcounter;
    /* 0x23C */ s16 pattern_counter;
    /* 0x23E */ s16 touch_counter;
    /* 0x240 */ u16 gyo_flags;
    /* 0x242 */ u8 swim_flag;
    /* 0x243 */ u8 escape_flag;
    /* 0x244 */ int alpha;
    /* 0x248 */ int col_flags;
    /* 0x24C */ u8* overlay_p;
    /* 0x250 */ f32 draw_accum;
};

typedef struct gyoei_actor_s GYOEI_ACTOR;

/* sizeof(GYOEI_ACTOR) == 0x634 */
struct gyoei_actor_s {
    /* 0x000 */ ACTOR actor_class;
    /* 0x174 */ aGYO_CTRL_ACTOR ctrl[aGYO_MAX_GYOEI];
    /* 0x61C */ u8 exist[aGYO_EXIST_MAX];
    /* 0x620 */ int segment_type[aGYO_EXIST_MAX];
    /* 0x630 */ f32 logic_accum;
};

extern void aGTT_actor_init(ACTOR* actorx, GAME* game); // ac_gyo_test.c
extern void aGKK_actor_init(ACTOR* actorx, GAME* game); // ac_gyo_kaseki

extern ACTOR_PROFILE Gyoei_Profile;

#ifdef TARGET_PC
/* World Ecology Wildlife Sync T-catch (multiplayer only -- see pc_wildlife_authority.c's own doc):
 * these two functions are ADDITIVE, PC-only surface on top of the unmodified vanilla GYOEI actor --
 * they never change any vanilla code path or struct layout (aGYO_CTRL_ACTOR::_1F8 is repurposed, not
 * resized: confirmed unused anywhere else in this codebase). Defined in ac_gyoei.c, TARGET_PC-gated. */

/* Stamps `entity_id` (the host-authoritative wildlife entity id -- pc_wildlife_authority.h) into a
 * just-created fish actor's otherwise-unused _1F8 field, so a later WILDLIFE_DESPAWN can find this
 * exact local actor again (see aGYO_pc_handle_wildlife_despawn() below). Called once, immediately
 * after pcwld_presentation_create() recovers the newly-created actor via search_near_gyoei_proc() (both
 * host self-presentation and a real client's WILDLIFE_SPAWN/snapshot-entry path go through that same
 * function). A no-op if `actorx` is NULL. entity_id 0 is never stamped (0 always means "no entity"
 * throughout this milestone, so a freshly bzero()'d, unstamped actor's _1F8 == 0 is indistinguishable
 * from "never stamped", which is exactly the desired default). */
void aGYO_pc_stamp_entity_id(ACTOR* actorx, u32 entity_id);

/* Reads back the stamp aGYO_pc_stamp_entity_id() wrote (0 if `actorx` is NULL, is not a live fish actor,
 * or was never stamped). Used by the client-side catch-interception seam
 * (Player_actor_setup_main_Notice_rod(), m_player_main_notice_rod.c_inc) to recover the entity_id for
 * `uki->child_actor` (the hooked fish actor) at the moment of catching. */
u32 aGYO_pc_get_entity_id_stamp(ACTOR* actorx);

/* WILDLIFE_DESPAWN reconciliation (World Ecology Wildlife Sync T-catch) -- see the milestone's own
 * case doc. Finds the live fish actor (if any) THIS process itself materialized for `entity_id` (via
 * the _1F8 stamp above) among the aGYO_MAX_GYOEI local fish slots, and applies exactly one of:
 *   1. Not engaged by this process's own fishing rod (bobber) actor, and not even in the "approaching
 *      the bobber" state (gyo_flags & 2, see Case 3 below): sets the SAME deferred-destroy flag
 *      (gyo_flags |= 0x20) vanilla's own aGYO_actor_move() already checks every tick -- clean, ordinary
 *      removal on the very next tick, no new destruction path invented.
 *   2. Engaged by this process's own bobber, but BEFORE aUKI_bite()'s point of no return (gyo_status <
 *      5): the exact same pair of writes vanilla's own bite-timeout escape path already uses
 *      (ac_gyo_test.c) -- uki->gyo_command = 0 and gyo_flags |= 0x20.
 *   3. (Bug D partial fix, post-T3 review) Not engaged by this process's own bobber, but IS in the
 *      "approaching the bobber" state (gyo_flags & 2 set by aGTT_near_init(), before child_actor is
 *      actually assigned to this fish actor): treated exactly like Case 1 (gyo_flags |= 0x20) -- no
 *      bobber references it yet, so destroying it next tick is just as safe. Without this case such a
 *      fish matched neither Case 1 nor Case 2 and was left as a harmless but visually-persisting
 *      "ghost" until a later, unrelated cull.
 *   4. Engaged AND at/past the point of no return (gyo_status >= 5), or the defensive
 *      engaged-but-flag-mismatch edge case: left COMPLETELY untouched -- a catch already in progress on
 *      this exact process (whether it is the one being accepted, or a now-moot race loser) must never
 *      be interrupted; the host's own accept/reject decision for whichever specific request arrives is
 *      what actually resolves the race, not this reconciliation.
 * Returns 1 if a locally-stamped actor was found for entity_id (whether or not anything needed to
 * change), 0 if this process never materialized one (already gone, or this entity_id was never seen
 * here) -- informational only; never treated as an error by any caller. entity_id 0 always returns 0. */
int aGYO_pc_handle_wildlife_despawn(u32 entity_id);

/* Bug fix (post-T3 review, Bug C): zeroes the _1F8 entity_id stamp on every ctrl[] slot in the local
 * fish-actor pool, unconditionally (whether or not that slot is currently `exist`). Needed because a
 * generation change (pcwld_reset()/a new authoritative wildlife session -- pc_wildlife_authority.h)
 * restarts the host's entity_id counter at 1, but does NOT itself touch any _1F8 stamp already sitting
 * on a live actor from the PREVIOUS session -- without this call, a leftover old-session actor stamped
 * entity_id=N could be mismatched against a brand-new session's unrelated entity that happens to also
 * be assigned id=N. Callers MUST have already established that this process's own aGYO_ctrlActor is
 * live (i.e. the town scene is actually loaded, pcfa_scene_is_town()) before calling this -- exactly
 * the same precondition aGYO_pc_handle_wildlife_despawn() requires of its own callers, see that
 * function's caller (pcwld_handle_wildlife_despawn(), pc_wildlife_authority.c) for the exact pattern.
 * A no-op if aGYO_ctrlActor is NULL (no fish-actor pool exists at all right now). */
void aGYO_pc_clear_all_entity_stamps(void);

/* 1 iff a live local fish actor currently carries entity_id's stamp (aGYO_pc_stamp_entity_id()). Used to decide whether a replayed WILDLIFE_SPAWN for an entity this process already
 * knows must re-create the actor (vanilla culls a fish that is far from the player) or is a duplicate of a fish that is still there. */
int aGYO_pc_entity_alive(u32 entity_id);

/* diagnostics: out6 = world xyz + home xyz of the live fish actor stamped with entity_id, plus its block_x / block_z; 0 when there is none. */
int aGYO_pc_entity_position(u32 entity_id, float* out6, int* bx, int* bz);

/* host-authoritative wildlife simulation (see pc_wildlife_authority.h): the live fish actor of an entity, its state (xyz, heading, vanilla action), and the bobber-event entry (ac_gyo_test.c). */
int aGYO_pc_find_entity(u32 entity_id, ACTOR** out);
int aGYO_pc_entity_state(u32 entity_id, float* xyz, s16* angle, int* action);
int aGTT_pc_action(const ACTOR* fish);
int aGTT_pc_apply_bobber_event(ACTOR* fish, ACTOR* uki, int ev, int gyo_type, float x, float y, float z, s16 angle);
#endif /* TARGET_PC */

#ifdef __cplusplus
}
#endif

#endif
