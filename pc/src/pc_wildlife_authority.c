/* pc_wildlife_authority.c - host-authoritative wildlife (fish/bug) spawn-decision table.
 * See pc_wildlife_authority.h for the public contract and scope statement.
 *
 * ================================================================================================
 * DESIGN, based on two prior read-only source audits (trusted, carefully source-traced):
 * ================================================================================================
 *
 * Trigger chain (unmodified by this file): aSetMgr_move_set() (ac_set_manager.c) runs one overlay
 * per call from proc_table = {aSOI_insect_set, aSOG_gyoei_set}, advancing set_ovl_type each call --
 * insects fire on frame N and fish on frame N+1 of the same wade-entry sequence. The network trigger
 * point this milestone hooks is exactly that proc_table invocation (ac_set_manager.c,
 * aSetMgr_move_set()) -- see pc_net_game_request_wildlife_spawn_trigger()'s own doc for the client
 * side of that seam.
 *
 * aSOI_insect_set()/aSOG_gyoei_set() both take (SET_MANAGER*, GAME_PLAY*) and read only
 * set_manager->player_pos.next_bx/next_bz (a plain int pair) and set_manager->keep (a scene-global
 * decision cache, NOT per-player -- one keep buffer is safe to reuse across sequential, distinct
 * acres). Neither function dereferences a player actor or camera in the decision path itself. Field/
 * collision/FG/deposit data for the ENTIRE town is resident in memory simultaneously (only rendering
 * is streamed), so these functions are safe to invoke for ANY acre, not just one the host's own
 * local player is standing in.
 *
 * The only genuine local-process coupling is 5 CLIP FUNCTION POINTERS used for "already alive in
 * this acre" checks and actual actor creation: chk_live_gyoei_proc, make_gyoei_proc (fish),
 * chk_live_insect_proc, make_insect_proc, make_ant_proc (bugs). Vanilla, these query/mutate the
 * process's own fixed-size local actor pools. This file's whole job is to SHIM those 5 pointers,
 * for the duration of one synchronous host-side call, to instead (a) answer "already alive" from
 * THIS module's own network-authoritative table, and (b) capture the decision's species/position
 * output INTO this table instead of constructing a real local ACTOR -- the host must not silently
 * grow a real local fish/bug actor every time it resolves another peer's wade event, since T0 has no
 * client-presentation path yet for a network-driven wildlife record (see pc_wildlife_authority.h /
 * the milestone brief's own explicit "stop before rendering" scope boundary).
 *
 * Side-effect writes during the decision (Save gyoei_term/gyoei_term_transition_offset,
 * Save insect_term/offset, hitodama_block_data) are untouched by this file -- they happen INSIDE
 * aSOI_insect_set()/aSOG_gyoei_set() exactly as vanilla, since those functions are called completely
 * unmodified. hitodama_block_data (spirit/ghost-event reroll) is explicitly unmirrored shared state,
 * out of scope for T0 per the audit -- not fixed here.
 *
 * RNG: aSOI_insect_set()/aSOG_gyoei_set() consume the SAME legacy global RNG stream exactly as
 * vanilla always has. This file changes only WHO calls them (host-only, for any acre) and WHY
 * (triggered by any connected player's wade event) -- never adds a new RNG domain, never wraps the
 * calls in QRAND_DOMAIN_FISH_SPAWN/QRAND_DOMAIN_BUG_SPAWN (explicitly out of scope, see the
 * milestone brief).
 *
 * Deviation from a byte-exact wade replay (documented, low risk): a real uninterrupted wade calls
 * aSOI_insect_set() on frame N and aSOG_gyoei_set() on frame N+1, with a wade-END in between
 * skipping the fish roll. A network wildlife-spawn-trigger request is one discrete client event with
 * no matching "wade-end" signal, so pcwld_host_spawn_trigger() runs BOTH overlays synchronously, in
 * the same insect-then-fish order vanilla uses, once per call. This preserves RNG-consumption order
 * and decision independence; the only behavioral difference is that a network trigger can never
 * "interrupt" the fish half the way a very fast wade-in/wade-out could locally. This does not touch
 * RNG domain/stream choice, only how many of the two decisions run per trigger.
 *
 * ================================================================================================
 * T1 ADDITION: local presentation (real vanilla actor from an already-decided record)
 * ================================================================================================
 * Source-traced findings this design is built on (ac_gyoei_clip.c_inc / ac_insect_clip.c_inc):
 *
 *  - aGYO_make_gyoei()/aINS_make_insect() ARE the creation-only step, not a decide+create pair --
 *    they take an already-fully-decided aGYO_Init_c/aINS_Init_c (species + position, nothing else)
 *    and do nothing but allocate a free local pool slot and call aGYO_setupActor()/aINS_setupActor().
 *    Neither one rolls any RNG. Both are reachable, UNMODIFIED, through the exact same clip function
 *    pointers this file's own shims temporarily replace (make_gyoei_proc / make_insect_proc) --
 *    calling through those pointers OUTSIDE the shimmed window (i.e. once this file has restored
 *    them to their real vanilla values, exactly as pcwld_host_spawn_trigger() already does at the
 *    end of each overlay) is calling the real, original function directly. No second, narrower entry
 *    point is needed.
 *  - Neither setup function recomputes ground/water height from x/z -- aSOG_gyoei_make() and
 *    aSOI_insect_set() (ac_set_ovl_gyoei.c/ac_set_ovl_insect.c) only ever adjust position.x/z
 *    (aSOG_get_water_attribute_position/aSOG_get_fall_attribute_position hunt for a nearby water/
 *    fall tile in the XZ plane only); position.y is left at whatever mFI_BkandUtNum2Wpos()
 *    initialized it to and is passed straight through to Actor_init_actor_class() untouched. This
 *    is exactly why PcWildlifeRecord.pos_y is always 0.0 in T0 -- it is not a T0 gap, it is
 *    byte-for-byte what vanilla's own decision functions already do. No height correction is added
 *    here; giving aGYO_make_gyoei()/aINS_make_insect() the SAME y (0.0, verbatim from the record) a
 *    real wade would have handed them is the only behavior consistent with "replicate the already-
 *    decided record, don't re-derive anything."
 *  - Ants are NOT a same-shape creation call: aINS_make_ant() (the REAL, unshimmed function behind
 *    make_ant_proc) does not create an actor at all -- it only queues one pending ant spawn
 *    (aINS_CLIP->ant_spawn_pending/ant_spawn_info/ant_bx/ant_bz for later, separate consumption).
 *    Materializing an ant safely would mean replicating that queue-and-later-consume shape, not a
 *    single synchronous call -- explicitly deferred, see pcwld_presentation_create()'s own doc.
 *    Bees are an ordinary aINS_INSECT_TYPE_BEE value that flows through aINS_make_insect() exactly
 *    like any other species (aINS_setupActor() handles it in the same shadow-size switch as the
 *    butterflies/cicadas/dragonflies) -- T1 does NOT special-case bees out of ordinary presentation;
 *    only ants get the synchronous-call treatment they structurally can't support yet.
 *  - The reserved 9th insect pool slot (aINS_MAKE_EXIST, ac_insect_clip.c_inc's aINS_searchRegistSpace)
 *    is used only by aINS_make_actor() (mPlib/ghost-catch-label net-catch flow, aINS_INIT_RELEASE) --
 *    an ambient wildlife-spawn decision never asks for it (aSOI_insect_set() always calls
 *    make_insect_proc with the implicit aINS_MAKE_NEW shape); T1's presentation call always goes
 *    through aINS_MAKE_NEW too, so it never touches that reserved slot -- out of scope, untouched.
 *  - Latent bugs (mole cricket / bagworm+spider / pill bug) and museum-catch flows are a LOCAL
 *    player-tool-action reveal/consume state layered on top of an ordinary insect actor, not a
 *    different creation call -- T1 creates them exactly like any other ordinary bug (no special
 *    case needed for creation itself), but does not attempt anything about their reveal/latent
 *    behavior, catching, or museum display -- all explicitly out of T1's presentation-only scope.
 *
 * Host self-presentation without network loopback: pcwld_table_insert() (below) pushes every newly
 * inserted record onto a small pending-presentation queue; pcwld_host_spawn_trigger() drains that
 * queue itself, once, AFTER it has restored BOTH clip structs to their real vanilla function
 * pointers (so pcwld_presentation_create()'s calls through Common_Get(clip...)->make_*_proc reach
 * the real creation functions, never this file's own shims) -- this is a plain local function call,
 * never a self-addressed network message.
 *
 * Client presentation: a client's PC_NETGAME_MSG_WILDLIFE_SPAWN handler (pc_net_game.c) calls the
 * exact same pcwld_presentation_create() with the data straight off the wire -- the client's own
 * clip pointers are NEVER shimmed by anything in this file (only a HOST's pcwld_host_spawn_trigger()
 * touches them, and only for the duration of one synchronous call), so Common_Get(clip...)-
 * >make_*_proc is already the real vanilla function on a client at all times.
 *
 * entity_id -> local actor identity: deliberately NOT a raw actor pointer or vanilla pool slot index
 * (same discipline T0 already applies to the authoritative side) -- s_presentation[] below only
 * records entity_id/kind/species/whether a real actor was made, keyed by entity_id, matching
 * PcWildlifeSlot's own shape. This is enough for T1's only two consumers (pcwld_presentation_has()
 * for duplicate-spawn protection, and this module's own doc/reporting) -- see this file's own
 * despawn-safety doc below for why a stale reverse (actor-> entity) link is deliberately NOT built.
 *
 * Despawn safety (guard only, no policy -- T1 explicitly does not implement a despawn policy): T1
 * adds NO destruction hook of any kind (no dt_gyoei_proc/dt_proc shim). A real actor this file
 * creates is destroyed by vanilla's own ordinary lifetime rules (culling, life_time countdown,
 * scene teardown) completely untouched and unobserved by this file -- which also means
 * s_presentation[]'s "active" bit can, in principle, outlive the real actor once vanilla despawns it
 * on its own. That is intentional and safe for T1's scope: s_presentation[] is used only for
 * duplicate-SPAWN-message suppression, never dereferenced as a pointer and never fed back into the
 * authoritative table (pcwld_remove_by_id() is not called from anywhere this file's presentation
 * code touches) -- a stale "already present" entry can, at worst, cause a rare future duplicate
 * suppression to be slightly too eager, never a crash, never authoritative-table corruption, and
 * never a use-after-free (no pointer is ever kept). A real despawn/expiration policy that reconciles
 * this is explicitly later work.
 */
#include "pc_wildlife_authority.h"

#include "types.h"
#include "game.h"
#include "m_play.h"
#include "m_common_data.h"
#include "m_clip.h"
#include "m_player_lib.h"
#include "ac_set_manager.h"
#include "ac_set_ovl_insect.h"
#include "ac_set_ovl_gyoei.h"
#include "ac_insect_h.h"
#include "ac_gyoei_h.h"
#include "ac_gyoei.h" /* World Ecology Wildlife Sync T-catch: aGYO_pc_stamp_entity_id()/
                         aGYO_pc_handle_wildlife_despawn() (TARGET_PC-gated, ac_gyoei.h's own doc) */

#include "pc_field_authority.h"
#include "pc_net_game.h"
#include "pc_log.h"
#include "pc_test_hooks.h" /* pc_test_hook_getenv(): AC_TEST_WILDLIFE_REROLL */

#include <string.h>
#include <math.h>
#include "ac_uki.h" /* host-authoritative simulation: the remote bobber proxy is a UKI_ACTOR */
#include <stdio.h>
#include <time.h>

/* Conservative fixed capacity: PCFA_ACRE_NUM (30) addressable town acres, generously bounded to 4
 * concurrently-tracked wildlife records per acre. Vanilla itself caps concurrent LOCAL fish actors
 * at aGYO_MAX_GYOEI == 2 (process-wide, not even per-acre) and usable insect slots at 8 (also
 * process-wide); 4-per-acre x 30 acres is already a large multiple of anything vanilla ever
 * materializes at once, with headroom for a firefly/red-dragonfly multi-spawn batch (several
 * aSOI_ins_make_sub() calls in one decision). A small fixed array with entity_id as the real
 * identity (never the array index) is a deliberately simple match for this project's other
 * host-side tables (s_host_bury_state/s_host_money_rock/s_host_tree_cut_count, pc_net_game.c). */
#define PCWLD_MAX_ENTITIES (PCFA_ACRE_NUM * 4)
_Static_assert(PCWLD_MAX_ENTITIES == PCWLD_PUBLIC_MAX_ENTITIES,
               "PCWLD_MAX_ENTITIES drifted from pc_wildlife_authority.h's own public mirror "
               "(PCWLD_PUBLIC_MAX_ENTITIES) -- update both together, see that header's own doc for why "
               "the public one is a repeated literal rather than sharing this formula directly");

/* T2: host-side "current authoritative wildlife session" generation -- see pcwld_session_generation()'s
 * own doc (pc_wildlife_authority.h) for the full contract. 0 is reserved (client side: "no session
 * observed yet"); this file never produces 0 (the time(NULL)-based seed is OR'd with 1, and the plain
 * increment path skips back to 1 on wraparound, mirroring pcwld_alloc_entity_id()'s own 0-avoidance). */
static uint32_t s_wildlife_session_gen = 0;

typedef struct PcWildlifeSlot {
    int              active;
    PcWildlifeRecord rec;
    float            age_accum; /* T1 review fix: 60fps-frame-unit age since insertion -- see
                                    pcwld_host_check_idle()'s own doc below for why this exists and
                                    why a time-based expiry (rather than an exist-flag-based one) was
                                    chosen. */
    uint8_t          recreates;     /* how many times pcwld_host_ensure_actors() brought this record's host actor back (capped: a creature that dies at once every time is left alone) */
    float            absent_frames; /* consecutive 60 fps frames in which no player attended this record (pcwld_host_collect_unattended()) */
} PcWildlifeSlot;

static PcWildlifeSlot s_table[PCWLD_MAX_ENTITIES];

/* Host-issued, monotonic within the running session. 0 is reserved (never issued, always means
 * "unknown/no entity") -- see pcwld_alloc_entity_id()'s wraparound handling below. */
static uint32_t s_next_entity_id = 1;

/* Re-entrancy guard for pcwld_host_spawn_trigger() -- the vanilla decision functions use static
 * scratch buffers that are safe for SEQUENTIAL calls but not for re-entrant/interleaved ones (per
 * the audit). Single-threaded host loop, so a simple flag is sufficient. */
static int s_in_progress = 0;

/* Scratch SET_MANAGER passed to aSOI_insect_set()/aSOG_gyoei_set() for a host-driven, out-of-band
 * decision (i.e. NOT the host's own real, ticking SET_MANAGER actor, which keeps running its own
 * local wade sequence for the host's own local player completely undisturbed by this file). Only
 * .player_pos.next_bx/next_bz and .keep are ever read by those two functions (confirmed by the
 * audit -- neither dereferences .actor_class or any other ACTOR field), so a persistent static
 * instance zero-initialized once is enough; .keep persists across calls exactly like the audit's
 * "one keep is safe for sequential multi-acre evaluation" finding intends. */
static SET_MANAGER s_scratch_set_manager;

/* ---- T1: local presentation map + host self-presentation pending queue -------------------- */

/* Small fixed capacity: only ever populated by THIS process's own real local pool slots -- vanilla
 * caps those at aGYO_MAX_GYOEI (2) + aINS_ACTOR_NUM (9) == 11 concurrent actors, process-wide. 16
 * gives headroom without pretending to track anything beyond what a single process could ever
 * actually materialize at once.
 *
 * T1 REVIEW FIX (bug: fixed-size map with no free/reuse path permanently exhausts within a session):
 * bumped 16 -> 64 as a stopgap capacity increase (Option A), ON TOP OF the real per-slot idle expiry
 * added below (Option B variant) -- see pcwld_presentation_check_idle()'s own doc for why a
 * time-based expiry was chosen over an exist-flag-based one. The capacity bump alone would NOT have
 * fixed the underlying "never freed" problem (still purely cosmetic headroom), so it is documented
 * here as a belt-and-suspenders margin on top of the real fix, not the fix itself. */
#define PCWLDP_MAX_LOCAL 64

typedef struct PcWildlifePresentationSlot {
    int      active;
    uint32_t entity_id;
    int      kind;
    int      species;
    int      has_actor; /* 0 for a deferred species (currently: ants) recorded as "handled" with no
                            real actor -- see pcwld_presentation_create()'s own doc */
    float    age_accum; /* T1 review fix: 60fps-frame-unit age since this slot was populated -- see
                            pcwld_presentation_check_idle()'s own doc below */
    void*    local_actor; /* T4 (ordinary bug catching): PC_WILDLIFE_KIND_BUG only -- the exact
                              aINS_INSECT_ACTOR* pointer make_insect_proc() returned for this entity_id,
                              opaque here (void*) so this file's own kind-agnostic bookkeeping never
                              needs to include ac_insect_h.h's full type just to store it. Fish use a
                              different mechanism (aGYO_pc_stamp_entity_id(), ac_gyoei.h/.c) -- see
                              pcwld_bug_local_actor_for_entity()'s own doc (pc_wildlife_authority.h) for
                              why bugs can't share that same in-struct-stamp approach. NULL for fish, for
                              a deferred ant, or once pcwld_bug_controller_torn_down() invalidates it. */
} PcWildlifePresentationSlot;

static PcWildlifePresentationSlot s_presentation[PCWLDP_MAX_LOCAL];

/* Populated by pcwld_table_insert() (host-only -- see that function's own doc), drained by
 * pcwld_host_spawn_trigger() once both clip structs are back to their real vanilla pointers. Bounded
 * to the same small size as s_presentation[] -- a single trigger call can produce at most a handful
 * of records (ordinary insect/fish decisions are 0-1 each; a firefly/red-dragonfly multi-spawn batch
 * is the only case producing more than one insect record per call). */
#define PCWLD_MAX_PENDING_PRESENTATION 8

typedef struct PcWildlifePendingPresentation {
    uint32_t entity_id;
    int      kind, species, bx, bz;
    float    x, y, z;
} PcWildlifePendingPresentation;

static PcWildlifePendingPresentation s_pending[PCWLD_MAX_PENDING_PRESENTATION];
static int s_pending_count;

/* ---- table -------------------------------------------------------------------------------- */

void pcwld_reset(void) {
    memset(s_table, 0, sizeof(s_table));
    s_next_entity_id = 1;
    /* Deliberately do NOT reset s_scratch_set_manager.keep here: the term/keep cache it holds is a
     * time-of-day/season-derived decision cache, not town-specific data -- resetting it on a town
     * change would just force one extra (harmless, idempotent) keep rebuild on the next trigger. */
    pcwld_presentation_reset(); /* host-side local presentation map is just as town-specific as the
                                    authoritative table itself -- see that function's own doc */
    pcwld_clear_local_actor_stamps(); /* Bug fix (post-T3 review, Bug C): entity_id counter restarts at
                                          1 below -- any stale _1F8 stamp left on a live local actor
                                          from the PREVIOUS session must not survive to be mismatched
                                          against a new, same-numbered entity_id */
    memset(s_pending, 0, sizeof(s_pending));
    s_pending_count = 0;

    /* T2: a fresh authoritative wildlife session begins here (fresh hosting session, or a detected
     * town change on an already-running host -- the two call sites, pc_net_game.c) -- see
     * pcwld_session_generation()'s own doc (pc_wildlife_authority.h) for the full contract this
     * advances. First-ever call this process: seed from wall-clock time (never a small, predictable,
     * easily-cross-host-colliding fixed value) so a reconnecting client comparing this value against a
     * DIFFERENT host process's own independently-seeded generation is overwhelmingly unlikely to see a
     * false match (a coincidental match would make that client wrongly treat the new host's session as
     * a continuation of the old one for one snapshot's reconciliation pass -- see
     * pcwld_presentation_reconcile()'s own doc for the bounded, self-correcting consequence of that
     * rare case). `| 1u` guarantees a non-zero seed regardless of what time() returns. Every
     * subsequent call (same process) just increments -- a mere client disconnect/reconnect to this
     * SAME still-running host never calls pcwld_reset() at all, so the generation stays stable across
     * that, which is exactly the signal a reconnecting client needs. */
    if (s_wildlife_session_gen == 0) {
        s_wildlife_session_gen = ((uint32_t)time(NULL)) | 1u;
    } else {
        s_wildlife_session_gen++;
        if (s_wildlife_session_gen == 0) {
            s_wildlife_session_gen = 1; /* wrapped past UINT32_MAX -- 0 stays reserved */
        }
    }
}

uint32_t pcwld_session_generation(void) {
    /* Lazily seed even if somehow read before the first pcwld_reset() (defensive only -- every real
     * host path calls pcwld_reset() well before any snapshot could be pumped) so this never returns
     * the reserved 0. */
    if (s_wildlife_session_gen == 0) {
        s_wildlife_session_gen = ((uint32_t)time(NULL)) | 1u;
    }
    return s_wildlife_session_gen;
}

static int pcwld_find_slot_by_id(uint32_t entity_id) {
    int i;
    if (entity_id == 0) {
        return -1;
    }
    for (i = 0; i < PCWLD_MAX_ENTITIES; i++) {
        if (s_table[i].active && s_table[i].rec.entity_id == entity_id) {
            return i;
        }
    }
    return -1;
}

int pcwld_find_by_id(uint32_t entity_id, PcWildlifeRecord* out) {
    int slot = pcwld_find_slot_by_id(entity_id);
    if (slot < 0) {
        return 0;
    }
    if (out != NULL) {
        *out = s_table[slot].rec;
    }
    return 1;
}

int pcwld_acre_has_kind(int bx, int bz, int kind) {
    int i;
    for (i = 0; i < PCWLD_MAX_ENTITIES; i++) {
        if (s_table[i].active && s_table[i].rec.kind == kind && s_table[i].rec.bx == bx &&
            s_table[i].rec.bz == bz) {
            return 1;
        }
    }
    return 0;
}

int pcwld_count_in_acre(int bx, int bz) {
    int i, count = 0;
    for (i = 0; i < PCWLD_MAX_ENTITIES; i++) {
        if (s_table[i].active && s_table[i].rec.bx == bx && s_table[i].rec.bz == bz) {
            count++;
        }
    }
    return count;
}

/* T2: host-side snapshot-pump iteration primitives -- see pc_wildlife_authority.h's own doc for both. */
int pcwld_active_count(void) {
    int i, count = 0;
    for (i = 0; i < PCWLD_MAX_ENTITIES; i++) {
        if (s_table[i].active) {
            count++;
        }
    }
    return count;
}

int pcwld_get_by_slot(int idx, PcWildlifeRecord* out) {
    if (idx < 0 || idx >= PCWLD_MAX_ENTITIES || !s_table[idx].active) {
        return 0;
    }
    if (out != NULL) {
        *out = s_table[idx].rec;
    }
    return 1;
}

int pcwld_remove_by_id(uint32_t entity_id) {
    int slot = pcwld_find_slot_by_id(entity_id);
    if (slot < 0) {
        return 0;
    }
    memset(&s_table[slot], 0, sizeof(s_table[slot]));
    return 1;
}

/* T1 review fix (BUG: neither this table nor s_presentation[] had ANY caller of pcwld_remove_by_id()
 * or any other free/reuse path -- once a fish/bug decision is recorded in an acre, pcwld_acre_has_kind()
 * keeps returning true for that acre+kind FOREVER, so that acre can never spawn wildlife of that kind
 * again for the rest of the hosting session; separately, s_presentation[] (now PCWLDP_MAX_LOCAL slots,
 * see its own doc) would eventually fill and refuse every further local materialization, permanently,
 * on every peer. Together this makes wildlife dry up permanently within a session.
 *
 * CHOICE OF FIX: a time-based idle expiry, mirroring the EXACT precedent already used for tree-cut
 * cut-count bookkeeping (PCNetGameTreeCutSlot/pcnetgame_host_check_tree_cut_idle(), above in
 * pc_net_game.c) -- age_accum in 60fps-frame units, incremented once per poll by gamePT->graph->
 * dt_num_60fps_frames, freeing the slot once past a conservative threshold. This is a REAL removal
 * path (not just a capacity bump), but it is explicitly a STOPGAP, not a byte-exact despawn policy:
 * a full policy is still later work (per this milestone's own explicit brief). Rejected alternative
 * (Option B as literally suggested: detect via the underlying vanilla actor's exist flag): NOT used
 * here because (a) this file's own top-of-file T1 doc deliberately avoids keeping any actor pointer
 * or reverse actor->entity_id link in s_presentation[]/s_table (an intentional safety choice --
 * see "Despawn safety" above -- reversing it to add a back-link is a materially bigger, riskier
 * change than this narrow bugfix pass should make), and (b) even if the HOST checked its OWN local
 * pool's exist flags, that can only ever observe the HOST's own local actor -- it has no way to know
 * whether a REMOTE peer's presentation actor for that same entity_id is still alive, so it is not
 * actually a more correct signal for the AUTHORITATIVE table (which must stay valid for every peer,
 * not just the host) than a plain elapsed-time bound is. A time-based bound is peer-agnostic and
 * uniformly conservative instead.
 *
 * THRESHOLD: 10 real-world minutes (continuous, NOT gated on culling/visibility the way vanilla's own
 * insect life_time is -- see aINS_calc_life_time()/ac_insect_move.c_inc, which only decrements while
 * NOT culled, making its 216000-frame constant equivalent to up to 60 minutes of ACTIVE/visible time,
 * context-dependent and not practically reproducible here without new coupling into vanilla's culling
 * state). 10 minutes is a deliberately conservative, simple approximation -- long enough that a still
 * "in play" record is very unlikely to be recycled out from under a live actor, short enough that an
 * acre's spawn gate reliably clears well within an ordinary play session. Documented here as a
 * stopgap value, not a tuned/validated one.
 *
 * KNOWN TRADEOFF (spawn density, not fixed here): this record blocks re-spawn of that kind in that
 * acre for up to the full 10-minute TTL regardless of whether the underlying actor already despawned
 * -- unlike vanilla, which re-rolls chk_live_*_proc fresh on every wade-in after a despawn. So under
 * --authoritative-wildlife, effective spawn density/variety in a given acre can be noticeably LOWER
 * than vanilla for as long as this stopgap TTL stands between despawn and record expiry. Acceptable
 * for this stopgap pass; a real despawn-driven removal path would close the gap. */
#define PCWLD_RECORD_MAX_AGE_60FPS_FRAMES (60.0f * 60.0f * 10.0f) /* ~10 minutes */

/* T8 audit verification, TEST-ONLY: --diag-bug-ttl-lookup <frames> (pc_platform.h/pc_main.c) needs a way
 * to cross the idle-expiry threshold within a short automated test run, without permanently touching the
 * real ~10-minute production constant above. 0 (the default) means "no override -- use the real constant
 * unmodified"; set only by pcnetgame_run_bug_ttl_lookup_diag_host() (pc_net_game.c), gated behind that same
 * CLI flag, never touched by any normal code path. */
static float s_ttl_override_frames = 0.0f;

void pcwld_test_set_ttl_override_frames(float frames) {
    s_ttl_override_frames = frames;
}

static float pcwld_effective_ttl_frames(void) {
    return (s_ttl_override_frames > 0.0f) ? s_ttl_override_frames : PCWLD_RECORD_MAX_AGE_60FPS_FRAMES;
}

/* HOST-ONLY. Per-poll idle-expiry tick for the authoritative table -- see this function's own
 * "review fix" doc above (pcwld_remove_by_id()) for the bug this closes and why this specific fix was
 * chosen. Called from pc_net_game_poll(), host branch only, alongside
 * pcnetgame_host_check_tree_cut()/pcnetgame_host_check_field_action_money_rock() (pc_net_game.c) --
 * same "run once per poll, before this poll's events are handled" placement. Never mutates anything
 * other than this table; freeing a slot here only means a FUTURE spawn decision for that acre/kind is
 * no longer blocked -- any real actor a peer already materialized for the expired record is completely
 * unaffected (see this file's own top-of-file despawn-safety doc: this module never destroys a real
 * actor). */
void pcwld_host_check_idle(void) {
    int i;
    float dt;

    if (gamePT == NULL) {
        return;
    }
    dt = (float)gamePT->graph->dt_num_60fps_frames;

    for (i = 0; i < PCWLD_MAX_ENTITIES; i++) {
        if (!s_table[i].active) {
            continue;
        }
        s_table[i].age_accum += dt;
        if (s_table[i].age_accum >= pcwld_effective_ttl_frames()) {
            printf("[NET][WILDLIFE] host: entity %u (kind %d species %d acre %d,%d) idle-expired -- "
                   "acre/kind spawn gate released (stopgap TTL, see pc_wildlife_authority.c's own "
                   "doc)\n",
                   (unsigned)s_table[i].rec.entity_id, s_table[i].rec.kind, s_table[i].rec.species,
                   s_table[i].rec.bx, s_table[i].rec.bz);
            memset(&s_table[i], 0, sizeof(s_table[i]));
        }
    }
}

#define PCWLD_UNATTENDED_GRACE_FRAMES (60.0f * 12.0f) /* 12 s without any player in / near the record's acre */

int pcwld_host_acre_records(int bx, int bz, PcWildlifeRecord* out, int max) {
    int i, n = 0;
    for (i = 0; i < PCWLD_MAX_ENTITIES && n < max; i++) {
        if (s_table[i].active && s_table[i].rec.bx == bx && s_table[i].rec.bz == bz) {
            out[n++] = s_table[i].rec;
        }
    }
    return n;
}

int pcwld_host_collect_unattended(int (*attended)(const PcWildlifeRecord*), float dt_frames, uint32_t* out_ids, int max) {
    int i, n = 0;
    for (i = 0; i < PCWLD_MAX_ENTITIES; i++) {
        if (!s_table[i].active) {
            continue;
        }
        if (attended(&s_table[i].rec)) {
            s_table[i].absent_frames = 0.0f;
            continue;
        }
        s_table[i].absent_frames += dt_frames;
        if (s_table[i].absent_frames >= PCWLD_UNATTENDED_GRACE_FRAMES && n < max) {
            printf("[NET][WILDLIFE] host: entity %u (kind %d species %d acre %d,%d) released -- no player in or near its acre for 12 s (its acre re-rolls on the next entry)\n",
                   (unsigned)s_table[i].rec.entity_id, s_table[i].rec.kind, s_table[i].rec.species, s_table[i].rec.bx, s_table[i].rec.bz);
            out_ids[n++] = s_table[i].rec.entity_id;
            memset(&s_table[i], 0, sizeof(s_table[i]));
        }
    }
    return n;
}

/* Allocates a never-before-issued, never-still-in-use id. A u32 counter is enormous headroom for a
 * play session (over 4 billion spawns); the only real wraparound concern is landing back on an id
 * some ancient, still-live record happens to still hold (impossible in practice given
 * PCWLD_MAX_ENTITIES is tiny next to 2^32, but checked anyway rather than assumed) -- skip forward
 * past any id still active. 0 is never issued (reserved as "unknown"). Returns 0 only in the
 * pathological case where no free id could be found in a bounded number of tries (defensive; should
 * never actually happen given the table itself is far smaller than the id space). */
static uint32_t pcwld_alloc_entity_id(void) {
    int tries;
    for (tries = 0; tries < PCWLD_MAX_ENTITIES + 4; tries++) {
        uint32_t id = s_next_entity_id++;
        if (s_next_entity_id == 0) {
            s_next_entity_id = 1; /* wrapped past UINT32_MAX -- 0 stays reserved */
        }
        if (id != 0 && pcwld_find_slot_by_id(id) < 0) {
            return id;
        }
    }
    return 0;
}

static uint32_t pcwld_table_insert(int kind, int species, int bx, int bz, float x, float y, float z) {
    int slot = -1;
    uint32_t id;
    int i;

    for (i = 0; i < PCWLD_MAX_ENTITIES; i++) {
        if (!s_table[i].active) {
            slot = i;
            break;
        }
    }
    if (slot < 0) {
        printf("[NET][WILDLIFE] host: table full (%d slots) -- spawn decision result dropped, not "
               "tracked (acre %d,%d kind %d species %d)\n",
               PCWLD_MAX_ENTITIES, bx, bz, kind, species);
        return 0;
    }

    id = pcwld_alloc_entity_id();
    if (id == 0) {
        printf("[NET][WILDLIFE] host: entity id allocation failed -- spawn decision result dropped\n");
        return 0;
    }

    s_table[slot].active         = 1;
    s_table[slot].age_accum      = 0.0f; /* T1 review fix: fresh record starts un-aged -- see
                                             pcwld_host_check_idle()'s own doc */
    s_table[slot].absent_frames  = 0.0f;
    s_table[slot].rec.entity_id  = id;
    s_table[slot].rec.kind       = kind;
    s_table[slot].rec.species    = species;
    s_table[slot].rec.bx         = bx;
    s_table[slot].rec.bz         = bz;
    s_table[slot].rec.pos_x      = x;
    s_table[slot].rec.pos_y      = y;
    s_table[slot].rec.pos_z      = z;

    printf("[NET][WILDLIFE] host: spawn decision entity %u kind %d species %d acre(%d,%d) pos(%.1f,%.1f,%.1f)\n",
           (unsigned)id, kind, species, bx, bz, x, y, z);

    /* T1: queue this brand-new record for the host's OWN local presentation actor -- drained by
     * pcwld_host_spawn_trigger() itself once both clip structs are back to their real vanilla
     * pointers (this function runs WHILE they are still shimmed -- see this file's own top-of-file
     * T1 doc for why draining happens after, not here). This is always host-only: pcwld_table_insert()
     * is only ever reached from inside pcwld_host_spawn_trigger()'s shims, which is itself only ever
     * invoked from the two host-only call sites in pc_net_game.c. A full pending queue only means
     * this one host loses its OWN local presentation actor for the overflow record -- the
     * authoritative record itself (above) and the broadcast below are entirely unaffected, and every
     * client still gets a correct WILDLIFE_SPAWN and materializes its own actor normally. */
    if (s_pending_count < PCWLD_MAX_PENDING_PRESENTATION) {
        s_pending[s_pending_count].entity_id = id;
        s_pending[s_pending_count].kind      = kind;
        s_pending[s_pending_count].species   = species;
        s_pending[s_pending_count].bx        = bx;
        s_pending[s_pending_count].bz        = bz;
        s_pending[s_pending_count].x         = x;
        s_pending[s_pending_count].y         = y;
        s_pending[s_pending_count].z         = z;
        s_pending_count++;
    } else {
        printf("[NET][WILDLIFE] host: presentation queue full -- entity %u will not get a HOST-local "
               "actor this call (authoritative record + client broadcast unaffected)\n",
               (unsigned)id);
    }

    pc_net_game_notify_wildlife_spawn(id, kind, species, bx, bz, x, y, z);
    return id;
}

/* ---- clip shims ----------------------------------------------------------------------------
 * These stand in for the 5 vanilla clip function pointers for the duration of one synchronous
 * pcwld_host_spawn_trigger() call. They read the current target acre off s_scratch_set_manager
 * (the exact struct passed to aSOI_insect_set()/aSOG_gyoei_set() for this call) rather than
 * threading it through separately -- correct because pcwld_host_spawn_trigger() is never
 * re-entrant (enforced by s_in_progress) and s_scratch_set_manager.player_pos.next_bx/next_bz are
 * set immediately before each overlay call and never touched by anything else in between. */

static int pcwld_shim_chk_live_insect_proc(int bx, int bz, GAME* game) {
    (void)game;
    return pcwld_acre_has_kind(bx, bz, PC_WILDLIFE_KIND_BUG) ? TRUE : FALSE;
}

/* Sentinel non-NULL return -- NEVER dereferenced by aSOI_ins_make_sub()/aSOI_ins_make()
 * (ac_set_ovl_insect.c), which only test the pointer's truthiness to decide whether to continue a
 * multi-birth loop. No real local ACTOR is constructed here -- this shim only runs during the
 * HOST's authoritative spawn-decision trigger (pcwld_host_spawn_trigger()), which records the
 * decision into the wire table; the actual client-side ACTOR (on host and remote clients alike) is
 * materialized separately by pcwld_presentation_create(), added in T1 -- see this file's top-of-file
 * doc. */
static ACTOR* pcwld_shim_make_insect_proc(aINS_Init_c* init, int flag) {
    (void)flag;
    pcwld_table_insert(PC_WILDLIFE_KIND_BUG, init->insect_type, s_scratch_set_manager.player_pos.next_bx,
                       s_scratch_set_manager.player_pos.next_bz, init->position.x, init->position.y,
                       init->position.z);
    return (ACTOR*)1;
}

static void pcwld_shim_make_ant_proc(aINS_Init_c* init, s8 bx, s8 bz) {
    pcwld_table_insert(PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_ANT, (int)bx, (int)bz, init->position.x,
                       init->position.y, init->position.z);
}

static int pcwld_shim_chk_live_gyoei_proc(int bx, int bz, GAME* game) {
    (void)game;
    return pcwld_acre_has_kind(bx, bz, PC_WILDLIFE_KIND_FISH) ? TRUE : FALSE;
}

/* Sentinel success return (1) -- aSOG_gyoei_make() (ac_set_ovl_gyoei.c) only tests truthiness. */
static int pcwld_shim_make_gyoei_proc(aGYO_Init_c* init) {
    pcwld_table_insert(PC_WILDLIFE_KIND_FISH, init->fish_type, s_scratch_set_manager.player_pos.next_bx,
                       s_scratch_set_manager.player_pos.next_bz, init->position.x, init->position.y,
                       init->position.z);
    return 1;
}

/* ---- adapter -------------------------------------------------------------------------------- */

/* TEST-ONLY (AC_TEST_HOOKS=1, AC_TEST_WILDLIFE_REROLL=<n>): an automated gameplay test cannot wait for the vanilla roll to produce a bug / a fish (it often produces nothing). With this set
 * the host repeats the UNMODIFIED vanilla decision (aSOI_insect_set / aSOG_gyoei_set, same RNG stream, same rules) up to n times per kind until the acre holds a creature of that kind. Nothing
 * is rolled or chosen by this file; 1 (the default, and always in normal play) is exactly one decision per kind. */
static int pcwld_test_reroll_count(void) {
    const char* e = pc_test_hook_getenv("AC_TEST_WILDLIFE_REROLL");
    int n = (e != NULL) ? atoi(e) : 1;
    return n < 1 ? 1 : (n > 200 ? 200 : n);
}

int pcwld_host_spawn_trigger(int bx, int bz) {
    int reroll, tries;
    aINS_Clip_c* insect_clip;
    aGYO_Clip_c* gyo_clip;
    aINS_Clip_c saved_insect_clip;
    aGYO_Clip_c saved_gyo_clip;
    GAME_PLAY* play;

    if (s_in_progress) {
        return 0; /* never re-entrant/recursive -- see this file's top-of-file doc */
    }
    if (!pcfa_scene_is_town()) {
        return 0; /* the host's own loaded scene must be the town for the clip pointers below to be
                     valid/meaningful -- mirrors pc_net_game_host_local_tree_shake()'s own precedent
                     of requiring pcfa_scene_is_town() before touching town-scene-only state */
    }
    if (gamePT == NULL) {
        return 0;
    }
    /* Addressable acre range, same convention as pc_field_authority.h's ax = bx - 1, az = bz - 1. */
    if (bx - 1 < 0 || bx - 1 >= PCFA_ACRE_X_NUM || bz - 1 < 0 || bz - 1 >= PCFA_ACRE_Z_NUM) {
        return 0;
    }

    insect_clip = Common_Get(clip).insect_clip;
    gyo_clip    = Common_Get(clip.gyo_clip);
    if (insect_clip == NULL || gyo_clip == NULL) {
        return 0; /* clip not (yet) wired up -- treat as not-ready rather than crash */
    }

    play = (GAME_PLAY*)gamePT;

    s_in_progress = 1;

    s_scratch_set_manager.player_pos.next_bx = bx;
    s_scratch_set_manager.player_pos.next_bz = bz;

    /* ---- insects (frame N in a real wade sequence) ---- */
    saved_insect_clip = *insect_clip;
    reroll = pcwld_test_reroll_count();
    for (tries = 0; tries < reroll; tries++) {
        insect_clip->chk_live_insect_proc = pcwld_shim_chk_live_insect_proc;
        insect_clip->make_insect_proc     = pcwld_shim_make_insect_proc;
        insect_clip->make_ant_proc        = pcwld_shim_make_ant_proc;

        aSOI_insect_set(&s_scratch_set_manager, play);

        insect_clip->chk_live_insect_proc = saved_insect_clip.chk_live_insect_proc;
        insect_clip->make_insect_proc     = saved_insect_clip.make_insect_proc;
        insect_clip->make_ant_proc        = saved_insect_clip.make_ant_proc;
        if (pcwld_acre_has_kind(bx, bz, PC_WILDLIFE_KIND_BUG)) {
            break;
        }
    }

    /* ---- fish (frame N+1 in a real wade sequence -- see this file's own deviation doc) ---- */
    saved_gyo_clip = *gyo_clip;
    for (tries = 0; tries < reroll; tries++) {
        gyo_clip->chk_live_gyoei_proc = pcwld_shim_chk_live_gyoei_proc;
        gyo_clip->make_gyoei_proc     = pcwld_shim_make_gyoei_proc;

        aSOG_gyoei_set(&s_scratch_set_manager, play);

        gyo_clip->chk_live_gyoei_proc = saved_gyo_clip.chk_live_gyoei_proc;
        gyo_clip->make_gyoei_proc     = saved_gyo_clip.make_gyoei_proc;
        if (pcwld_acre_has_kind(bx, bz, PC_WILDLIFE_KIND_FISH)) {
            break;
        }
    }

    /* T1: host self-presentation. Both clip structs are back to their real vanilla function
     * pointers at this point -- drain whatever pcwld_table_insert() queued during either shim
     * window and materialize the host's OWN local actor for each, via the exact same
     * pcwld_presentation_create() a remote client uses on WILDLIFE_SPAWN. Plain local function
     * calls -- no network loopback of any kind. */
    {
        int i;
        for (i = 0; i < s_pending_count; i++) {
            pcwld_presentation_create(s_pending[i].entity_id, s_pending[i].kind, s_pending[i].species,
                                      s_pending[i].bx, s_pending[i].bz, s_pending[i].x, s_pending[i].y,
                                      s_pending[i].z);
        }
        s_pending_count = 0;
    }

    s_in_progress = 0;
    PC_LOG_RL(PCL_WILDLIFE, 8, 16, "host wildlife spawn trigger done: acre (%d,%d)\n", bx, bz);
    return 1;
}

/* ---- T1: local presentation ------------------------------------------------------------------ */

int pcwld_presentation_has(uint32_t entity_id) {
    int i;
    if (entity_id == 0) {
        return 0;
    }
    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        if (s_presentation[i].active && s_presentation[i].entity_id == entity_id) {
            return 1;
        }
    }
    return 0;
}

void pcwld_presentation_reset(void) {
    memset(s_presentation, 0, sizeof(s_presentation));
}

static int pcwld_presentation_alloc_slot(void) {
    int i;
    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        if (!s_presentation[i].active) {
            return i;
        }
    }
    return -1;
}

/* Coarse finite/sane-magnitude check, NaN-safe without <math.h>: a comparison against NaN is always
 * false, so !(v > lo && v < hi) is true (rejected) for NaN just like for anything genuinely out of
 * range. 100000.0f is generously larger than any real GameCube town world coordinate. */
static int pcwld_position_component_sane(float v) {
    return v > -100000.0f && v < 100000.0f;
}

static int pcwld_bug_presentation_actor_still_alive(const PcWildlifePresentationSlot* slot);

/* 1 iff the local actor of this presentation entry is still there. A deferred ant has no actor and is "handled" for good. */
static int pcwld_presentation_actor_alive(const PcWildlifePresentationSlot* s) {
    if (s->kind == PC_WILDLIFE_KIND_BUG && s->species == aINS_INSECT_TYPE_ANT) {
        return 1; /* an ant never has an actor: it is "handled" for good */
    }
    if (!s->has_actor || !pcfa_scene_is_town()) {
        return 0; /* its actor was lost (pool slot reused, scene unloaded...) */
    }
    if (!pcfa_scene_is_town()) {
        return 0;
    }
    return s->kind == PC_WILDLIFE_KIND_BUG ? pcwld_bug_presentation_actor_still_alive(s) : aGYO_pc_entity_alive((u32)s->entity_id);
}

int pcwld_presentation_create(uint32_t entity_id, int kind, int species, int bx, int bz, float x,
                              float y, float z) {
    int slot;
    aINS_Clip_c* insect_clip;
    aGYO_Clip_c* gyo_clip;
    void* local_actor_out = NULL; /* T4: see the PC_WILDLIFE_KIND_BUG branch below */

    if (entity_id == 0) {
        return 0;
    }
    if (pcwld_presentation_has(entity_id)) {
        /* A duplicate of an actor that is still alive is ignored (never replace a live actor). A duplicate whose actor is GONE is a creature the host still holds and this process has
         * lost (vanilla destroys an insect / fish the player walked away from): the host replays an acre's records to a player entering it, so create the actor again. */
        int k;
        for (k = 0; k < PCWLDP_MAX_LOCAL; k++) {
            if (s_presentation[k].active && s_presentation[k].entity_id == entity_id) {
                break;
            }
        }
        if (k >= PCWLDP_MAX_LOCAL || pcwld_presentation_actor_alive(&s_presentation[k])) {
            return 1;
        }
        memset(&s_presentation[k], 0, sizeof(s_presentation[k]));
        printf("[NET][WILDLIFE] presentation: entity %u has no local actor any more -- re-materializing it for the player entering its acre\n", (unsigned)entity_id);
    }
    if (kind < 0 || kind >= PC_WILDLIFE_KIND_NUM) {
        printf("[NET][WILDLIFE] presentation: entity %u rejected -- unknown kind %d\n",
               (unsigned)entity_id, kind);
        return 0;
    }
    if (bx - 1 < 0 || bx - 1 >= PCFA_ACRE_X_NUM || bz - 1 < 0 || bz - 1 >= PCFA_ACRE_Z_NUM) {
        printf("[NET][WILDLIFE] presentation: entity %u rejected -- acre (%d,%d) out of range\n",
               (unsigned)entity_id, bx, bz);
        return 0;
    }
    if (!pcwld_position_component_sane(x) || !pcwld_position_component_sane(y) ||
        !pcwld_position_component_sane(z)) {
        printf("[NET][WILDLIFE] presentation: entity %u rejected -- position (%.1f,%.1f,%.1f) "
               "out of range/non-finite\n",
               (unsigned)entity_id, x, y, z);
        return 0;
    }
    if (kind == PC_WILDLIFE_KIND_FISH) {
        if (species < 0 || species >= aGYO_TYPE_EXTENDED_NUM) {
            printf("[NET][WILDLIFE] presentation: entity %u rejected -- fish species %d out of range\n",
                   (unsigned)entity_id, species);
            return 0;
        }
    } else {
        /* BUG FIX (T1 review): aINS_INSECT_TYPE_SPIRIT (ac_insect_h.h) is defined as EXACTLY
         * aINS_INSECT_TYPE_NUM (a deliberate alias, not a coincidence -- aINS_INSECT_TYPE_NONE ==
         * aINS_INSECT_TYPE_NUM + 1 and aINS_INSECT_TYPE_EXTENDED_NUM == aINS_INSECT_TYPE_NUM + 2 sit
         * right after it), so a plain `species >= aINS_INSECT_TYPE_NUM` bound WRONGLY rejects the one
         * value one past it that is actually still a fully legitimate, fully-supported species --
         * confirmed by tracing aINS_setupActor()'s own program-table lookup (ac_insect_clip.c_inc):
         * aINS_program_type[]/aINS_program_dlftbl[] (ac_insect_data.c_inc) are sized to include an
         * entry at index aINS_INSECT_TYPE_NUM itself (aINS_PROGRAM_HITODAMA, i.e. exactly the
         * spirit/hitodama ghost-event program), and aSOI_insect_set()'s own ghost-event path
         * (aSOI_SPAWN_TYPE_SPIRIT, ac_set_ovl_insect.c) can legitimately decide this exact value.
         * aINS_INSECT_TYPE_NONE (NUM + 1) is a real "no insect" sentinel, never a spawnable species,
         * and correctly stays rejected -- so the fix is `> aINS_INSECT_TYPE_NUM`, not switching to
         * aINS_INSECT_TYPE_EXTENDED_NUM (which would wrongly also accept NONE). */
        if (species < 0 || species > aINS_INSECT_TYPE_NUM) {
            printf("[NET][WILDLIFE] presentation: entity %u rejected -- bug species %d out of range\n",
                   (unsigned)entity_id, species);
            return 0;
        }
    }

    if (!pcfa_scene_is_town() || gamePT == NULL) {
        /* Not a protocol error -- e.g. this process's own town scene hasn't finished loading yet.
         * Nothing to retry: the one-shot WILDLIFE_SPAWN is simply missed locally, same accepted-gap
         * shape as every other "arrived before we were ready" case in this codebase. Not recorded in
         * s_presentation[] -- deliberately left available for a hypothetical future retry path. */
        printf("[NET][WILDLIFE] presentation: entity %u dropped -- local scene not ready (not the "
               "town)\n",
               (unsigned)entity_id);
        return 0;
    }

    insect_clip = Common_Get(clip).insect_clip;
    gyo_clip    = Common_Get(clip.gyo_clip);
    if (insect_clip == NULL || gyo_clip == NULL) {
        printf("[NET][WILDLIFE] presentation: entity %u dropped -- clip not (yet) wired up\n",
               (unsigned)entity_id);
        return 0;
    }

    slot = pcwld_presentation_alloc_slot();
    if (slot < 0) {
        printf("[NET][WILDLIFE] presentation: local table full (%d) -- entity %u dropped\n",
               PCWLDP_MAX_LOCAL, (unsigned)entity_id);
        return 0;
    }

    if (kind == PC_WILDLIFE_KIND_BUG && species == aINS_INSECT_TYPE_ANT) {
        /* T1 SCOPE (see this file's own top-of-file doc): the REAL make_ant_proc only queues a
         * pending spawn for later, separate consumption -- it does not synchronously produce an
         * ACTOR* the way aGYO_make_gyoei()/aINS_make_insect() do, so it cannot safely share this
         * call's shape yet. Recorded as "handled" (no actor) so a duplicate SPAWN for this id stays
         * a no-op; deferred to a later special-case stage rather than attempted here. */
        s_presentation[slot].active    = 1;
        s_presentation[slot].entity_id = entity_id;
        s_presentation[slot].kind      = kind;
        s_presentation[slot].species   = species;
        s_presentation[slot].has_actor = 0;
        s_presentation[slot].age_accum = 0.0f; /* T1 review fix -- see pcwld_presentation_check_idle() */
        printf("[NET][WILDLIFE] presentation: entity %u species ANT DEFERRED (T1 does not "
               "materialize ants -- see pc_wildlife_authority.c's own doc)\n",
               (unsigned)entity_id);
        return 1;
    }

    {
        int ok = 0;
        ACTOR* created_actor = NULL;

        if (kind == PC_WILDLIFE_KIND_FISH) {
            if (gyo_clip->make_gyoei_proc != NULL) {
                aGYO_Init_c init;
                init.fish_type     = species;
                init.position.x    = x;
                init.position.y    = y;
                init.position.z    = z;
                init.extra_data    = 0;
                init.game          = (GAME*)gamePT;
                ok = gyo_clip->make_gyoei_proc(&init) ? 1 : 0;
                if (ok && gyo_clip->search_near_gyoei_proc != NULL) {
                    /* aGYO_make_gyoei() (the real, unshimmed function behind make_gyoei_proc outside
                     * pcwld_host_spawn_trigger()'s own shim window) returns only a bool, never the
                     * ACTOR* it created -- search_near_gyoei_proc(x, z) recovers it: this actor was
                     * just placed at EXACTLY (x, z), so it is guaranteed nearest to itself (distance
                     * 0) among the at-most aGYO_MAX_GYOEI==2 live fish actors, unless another already-
                     * live fish coincidentally sits at the exact same float position (astronomically
                     * unlikely). Needed for the block_x/block_z correction below. */
                    created_actor = gyo_clip->search_near_gyoei_proc(x, z);
                }
            }
        } else {
            if (insect_clip->make_insect_proc != NULL) {
                aINS_Init_c init;
                init.insect_type = species;
                init.position.x  = x;
                init.position.y  = y;
                init.position.z  = z;
                /* KNOWN LIMITATION (T1 review, not fixed -- deliberately deferred): vanilla gives a
                 * cockroach (aINS_INSECT_TYPE_COCKROACH) extra_data 4 or 6 at spawn time depending on
                 * whether it spawned on a tree or a spoiled turnip (ac_set_ovl_insect.c), which affects
                 * its initial behavior/state -- that value is not currently captured during the host's
                 * shim-based decision, not part of PcWildlifeRecord/the wire PCNetGameWildlifeSpawnMsg,
                 * and so always defaults to aINS_INIT_NORMAL here regardless of species. Fixing this
                 * properly would need a wire-format change (a new field on the authoritative record
                 * and the WILDLIFE_SPAWN message) that is out of this narrow bugfix pass's scope --
                 * left as a documented gap rather than a rushed partial fix. */
                init.extra_data  = aINS_INIT_NORMAL;
                init.game        = (GAME*)gamePT;
                created_actor    = insect_clip->make_insect_proc(&init, aINS_MAKE_NEW);
                ok = created_actor != NULL;
            }
        }

        if (!ok) {
            printf("[NET][WILDLIFE] presentation: entity %u local actor creation failed (pool full "
                   "or clip not ready)\n",
                   (unsigned)entity_id);
            return 0;
        }

        /* BUG FIX (T1 review): aGYO_setupActor()/aINS_setupActor() (ac_gyoei_clip.c_inc /
         * ac_insect_clip.c_inc), called via make_gyoei_proc/make_insect_proc just above, stamp the
         * new actor's block_x/block_z from play->block_table.block_x/block_z -- i.e. the LOCAL
         * PLAYER's own current acre -- never from the acre this spawn actually belongs to. Since this
         * whole function exists to materialize actors for acres OTHER than wherever the local player
         * happens to be standing, every presentation actor would otherwise be mis-stamped with the
         * WRONG acre: vanilla's own despawn/cull check (culled + far away + a DIFFERENT block than
         * play->block_table) can then never fire for it while the local player stays put (the actor's
         * stored block already matches wherever the player is standing, so the "different block" half
         * of that check never trips), permanently pinning a real pool slot; fish OFFING/ocean-acre
         * collision checks read the wrong acre's data for the same reason. Overwrite immediately
         * after creation, before anything else observes the actor, with the CORRECT spawn acre --
         * bx/bz UNMODIFIED, with NO -1 offset. actor->block_x/block_z use the SAME full-field block
         * numbering as play->block_table.block_x/block_z and as this trigger's own bx/bz parameter
         * (mFI_Wpos2BlockNum() writes that same numbering into play->block_table.block_x directly,
         * m_field_info.c; aGYO_chk_live_gyoei() compares actor.block_x against a bare bx with no
         * offset, ac_gyoei_clip.c_inc). That numbering is NOT the same thing as this module's own
         * separate 0-based ax/az array-index convention (ax = bx - 1) used only for indexing pcfa's
         * internal per-acre arrays elsewhere in this file -- applying that -1 offset here would stamp
         * the actor with a different, still-wrong acre instead of the real spawn acre.
         *
         * NOTE (residual, not fixed by this write): this does NOT retroactively fix the OFFING
         * col_flags bit aGYO_setupActor() itself computes SYNCHRONOUSLY during creation
         * (ac_gyoei_clip.c_inc, immediately after its own Actor_init_actor_class() call, using
         * ctrl->tools_class.actor_class.block_x/block_z as THEY stood at that instant) -- that
         * one-time check already ran against the WRONG (local-player) acre before control returns
         * here and cannot be redone from outside without re-deriving vanilla's own OFFING logic. Only
         * the stored block_x/block_z fields themselves (read by every SUBSEQUENT cull/despawn check,
         * and by ordinary gameplay code that queries an actor's acre after creation) are corrected by
         * this write. A fish actor materialized this way may therefore start with an OFFING col_flag
         * bit computed against the wrong acre's water/land classification -- a narrower residual gap
         * than the one this fix closes, left as a known limitation. */
        if (created_actor != NULL) {
            created_actor->block_x = (int8_t)bx;
            created_actor->block_z = (int8_t)bz;

            /* World Ecology Wildlife Sync T-catch: stamp entity_id into the fish actor's own
             * otherwise-unused _1F8 field (ac_gyoei.h) so a later WILDLIFE_DESPAWN can find this exact
             * local actor again -- see aGYO_pc_stamp_entity_id()'s own doc. Fish only (kind ==
             * PC_WILDLIFE_KIND_FISH): bugs have no despawn-by-catch concept in this milestone (ordinary
             * fish catching only, per this milestone's own explicit scope limit), so aINS_CTRL_ACTOR is
             * never touched here. A no-op for a kind-FISH actor recovery failure (created_actor == NULL,
             * logged separately below) -- entity_id simply stays unstamped, exactly like every other
             * "arrived before we were ready" gap in this codebase; the fish is still fully playable
             * locally, it just cannot be individually despawned-by-catch-elsewhere for THIS process
             * (an ordinary WILDLIFE_DESPAWN Case 1 fallback still can't fire either, since there is no
             * stamped actor to find -- documented residual gap, matching this same recovery failure's
             * existing block_x/block_z correction gap immediately above). */
            if (kind == PC_WILDLIFE_KIND_FISH) {
                aGYO_pc_stamp_entity_id(created_actor, entity_id);
            }
        } else {
            printf("[NET][WILDLIFE] presentation: entity %u WARNING -- could not recover the created "
                   "actor to correct its acre stamp (block_x/block_z left at the local player's "
                   "acre)\n",
                   (unsigned)entity_id);
        }

        /* T4 (ordinary bug catching): unlike fish, bugs have no in-struct field to stamp (see this
         * module's own top-of-file doc and pcwld_bug_local_actor_for_entity()'s doc, pc_wildlife_
         * authority.h, for the due-diligence grep that established no field of aINS_INSECT_ACTOR is
         * genuinely unused) -- so the mapping is kept here instead, in this module's own bookkeeping.
         * created_actor IS already the exact per-slot aINS_INSECT_ACTOR* (aINS_make_insect(),
         * ac_insect_clip.c_inc, returns `&aINS_ctrlActor->insect_actor[free_idx]` directly -- unlike
         * fish, no separate search_near_insect_proc recovery step is needed here at all). NULL for a
         * FISH record (fish use the aGYO_pc_stamp_entity_id() path above instead) or a recovery
         * failure (created_actor == NULL, logged just above) -- either way pcwld_bug_local_actor_for_
         * entity() simply reports "not locally materialized" for this entity_id, the same documented
         * residual gap fish already have for their own recovery-failure case. */
        local_actor_out = (kind == PC_WILDLIFE_KIND_BUG) ? (void*)created_actor : NULL;

        /* TRACE (diagnostics only): where the actor really is right after the vanilla constructor and the acre stamp, against the position the record asked for */
        if (created_actor != NULL) {
            printf("[NET][WILDLIFE][TRACE] entity %u kind %d species %d record_acre(%d,%d) requested(%.1f,%.1f,%.1f) -> actor %p world(%.1f,%.1f,%.1f) home(%.1f,%.1f,%.1f) block(%d,%d)\n",
                   (unsigned)entity_id, kind, species, bx, bz, x, y, z, (void*)created_actor, created_actor->world.position.x, created_actor->world.position.y,
                   created_actor->world.position.z, created_actor->home.position.x, created_actor->home.position.y, created_actor->home.position.z, (int)created_actor->block_x,
                   (int)created_actor->block_z);
        }
    }

    /* T8 audit fix (Bug 2 part a): a bug's local_actor pointer identifies a POOL SLOT, not a specific bug
     * -- aINS_searchRegistSpace() (ac_insect_clip.c_inc) hands out the FIRST FREE slot, so a brand-new bug
     * can land at the EXACT SAME address a previous bug's now-stale s_presentation[] entry still
     * remembers. This is now the ONLY place that mapping is invalidated for a genuinely dead actor (see
     * pcwld_bug_handle_wildlife_despawn()'s own doc, below, for why an immediate despawn-time clear was
     * tried and then reverted as a T8-review regression) -- a pointer collision unambiguously means that
     * OTHER entry's actor is dead, since the game would never hand out a slot still backing a live one, so
     * invalidate it here, before this brand new mapping is recorded, rather than leaving two entity_ids
     * pointing at the same live slot. Combined with Bug 1's "never age out / reconcile away a still-alive
     * bug" guard (pcwld_bug_presentation_actor_still_alive(), used below by pcwld_presentation_check_idle()
     * and pcwld_presentation_reconcile()), a stale entry is guaranteed to be cleaned up either here (as
     * soon as its slot is actually reused) or by ordinary idle-expiry (once its actor actually dies) --
     * whichever comes first -- without ever needing to clear it synchronously at despawn time. */
    if (kind == PC_WILDLIFE_KIND_BUG && local_actor_out != NULL) {
        int k;
        for (k = 0; k < PCWLDP_MAX_LOCAL; k++) {
            if (!s_presentation[k].active || k == slot) {
                continue;
            }
            if (s_presentation[k].kind == PC_WILDLIFE_KIND_BUG && s_presentation[k].has_actor &&
                s_presentation[k].local_actor == local_actor_out) {
                printf("[NET][WILDLIFE] presentation: entity %u had a STALE mapping to a bug pool slot "
                       "now reused by entity %u -- old mapping invalidated (Bug 2 fix, see pc_wildlife_"
                       "authority.c's own doc)\n",
                       (unsigned)s_presentation[k].entity_id, (unsigned)entity_id);
                s_presentation[k].local_actor = NULL;
                s_presentation[k].has_actor   = 0;
            }
        }
    }

    s_presentation[slot].active      = 1;
    s_presentation[slot].entity_id   = entity_id;
    s_presentation[slot].kind        = kind;
    s_presentation[slot].species     = species;
    s_presentation[slot].has_actor   = 1;
    s_presentation[slot].local_actor = local_actor_out;
    s_presentation[slot].age_accum = 0.0f; /* T1 review fix -- see pcwld_presentation_check_idle() */

    /* T8 audit verification aid: local_actor logged for BUG entities specifically (NULL for fish/ants,
       where it is never meaningful) so a pool-slot reuse collision (Bug 2) is directly grep-able from an
       ordinary host/client log -- two different entity_id "materialized locally" lines sharing the exact
       same local_actor= address is precisely the condition pcwld_presentation_create()'s own Bug 2 fix
       (just below) now detects and invalidates. */
    printf("[NET][WILDLIFE] presentation: entity %u kind %d species %d materialized locally at "
           "acre(%d,%d) pos(%.1f,%.1f,%.1f) local_actor=%p\n",
           (unsigned)entity_id, kind, species, bx, bz, x, y, z, local_actor_out);
    return 1;
}

/* T8 audit fix (Bug 1): a BUG's s_presentation[] entry must never be expired/reconciled-away while its
 * local_actor pointer still refers to a genuinely LIVE actor. Unlike fish (identity stamped directly onto
 * the actor itself via _1F8, ac_gyoei.h -- see this file's own top-of-file doc), a bug's entity_id<->actor
 * mapping lives ONLY in this side table; losing it while the actor is still alive makes
 * pcwld_bug_entity_id_for_local_actor() return 0 for a still-live bug, which Player_actor_setup_main_
 * Notice_net() (m_player_main_notice_net.c_inc) treats as "untracked" and grants via plain vanilla -- an
 * undetected, unauthoritative duplicate catch, reachable in ordinary play (a stationary player standing
 * near a bug for the 10-minute idle TTL, or a reconnect racing a snapshot that no longer lists an entity
 * a DIFFERENT peer already caught while this client was away). Reuses the EXACT "is this actor still
 * alive" check pcwld_bug_handle_wildlife_despawn() already established below (exist_flag == TRUE and
 * species still matches the slot's own recorded species) -- a dead, fled, culled, or pool-slot-reused
 * actor fails that check and is correctly still eligible for normal expiry/reconciliation. */
static int pcwld_bug_presentation_actor_still_alive(const PcWildlifePresentationSlot* slot) {
    const aINS_INSECT_ACTOR* insect;

    if (slot->kind != PC_WILDLIFE_KIND_BUG || !slot->has_actor || slot->local_actor == NULL) {
        return 0;
    }
    if (!pcfa_scene_is_town()) {
        /* No live local bug-actor pool to dereference right now (mirrors pcwld_bug_handle_wildlife_
           despawn()'s own precondition) -- by the time this process's scene actually leaves town,
           pcwld_bug_controller_torn_down() (ac_insect.c's aINS_actor_dt()) has already cleared
           has_actor/local_actor for every bug slot anyway, so this branch is defensive only. */
        return 0;
    }
    insect = (const aINS_INSECT_ACTOR*)slot->local_actor;
    return insect->exist_flag == TRUE && insect->type == slot->species;
}

/* TEST-ONLY (the autopilot's `bugs` command, pc_net_game.c): the idx-th (0-based) bug entity that has a LIVE local actor right now, with the actor's position. 0 when there is none. */
/* TEST-ONLY (the autopilot's `pres` command): one log line per presentation entry with the state of its local actor, to see why a creature is (not) catchable. */
void pcwld_test_dump_presentation(void) {
    int i;
    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        const PcWildlifePresentationSlot* s = &s_presentation[i];
        if (!s->active) {
            continue;
        }
        if (s->kind == PC_WILDLIFE_KIND_BUG && s->has_actor && s->local_actor != NULL && pcfa_scene_is_town()) {
            const aINS_INSECT_ACTOR* in = (const aINS_INSECT_ACTOR*)s->local_actor;
            printf("[AUTO] pres entity %u bug species %d actor %p exist=%d type=%d pos=(%.0f,%.0f)\n", (unsigned)s->entity_id, s->species, s->local_actor, in->exist_flag, in->type,
                   (double)in->tools_actor.actor_class.world.position.x, (double)in->tools_actor.actor_class.world.position.z);
        } else {
            printf("[AUTO] pres entity %u kind %d species %d has_actor=%d alive=%d\n", (unsigned)s->entity_id, s->kind, s->species, s->has_actor, pcwld_presentation_actor_alive(s));
        }
    }
}

int pcwld_test_live_bug(int idx, uint32_t* entity_id, int* species, float* x, float* z) {
    int i, n = 0;
    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        if (s_presentation[i].active && pcwld_bug_presentation_actor_still_alive(&s_presentation[i])) {
            if (n++ == idx) {
                const ACTOR* a = (const ACTOR*)s_presentation[i].local_actor;
                *entity_id = s_presentation[i].entity_id;
                *species = s_presentation[i].species;
                *x = a->world.position.x;
                *z = a->world.position.z;
                return 1;
            }
        }
    }
    return 0;
}

void pcwld_host_replay_acre_local(int bx, int bz) {
    PcWildlifeRecord recs[16];
    int i, n = pcwld_host_acre_records(bx, bz, recs, 16);
    for (i = 0; i < n; i++) {
        (void)pcwld_presentation_create(recs[i].entity_id, recs[i].kind, recs[i].species, recs[i].bx, recs[i].bz, recs[i].pos_x, recs[i].pos_y, recs[i].pos_z);
    }
}

/* T2: late-join/reconnect snapshot reconciliation -- see pc_wildlife_authority.h's own doc for the
 * full contract. Purely local bookkeeping cleanup: clears the slot exactly like pcwld_presentation_
 * check_idle()'s own idle-expiry path does (never touches a real actor, never calls back into the
 * authoritative table -- see this file's top-of-file "Despawn safety" doc, which applies identically
 * here). O(PCWLDP_MAX_LOCAL * keep_count); both bounds are small (64 and PCWLD_PUBLIC_MAX_ENTITIES ==
 * 120), so a linear scan is plenty for a once-per-snapshot call. */
int pcwld_presentation_reconcile(const uint32_t* keep_ids, int keep_count) {
    int i, j, removed = 0;

    if (keep_ids == NULL) {
        keep_count = 0;
    }

    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        int keep = 0;

        if (!s_presentation[i].active) {
            continue;
        }
        for (j = 0; j < keep_count; j++) {
            if (keep_ids[j] == s_presentation[i].entity_id) {
                keep = 1;
                break;
            }
        }
        if (!keep) {
            /* T8 audit fix (Bug 1): a missing-from-snapshot bug whose local actor is STILL ALIVE must not
               be reconciled away -- see pcwld_bug_presentation_actor_still_alive()'s own doc just above. */
            if (pcwld_bug_presentation_actor_still_alive(&s_presentation[i])) {
                printf("[NET][WILDLIFE] presentation: entity %u missing from snapshot but its local bug "
                       "actor is still alive -- reconciliation DEFERRED (Bug 1 fix, see pc_wildlife_"
                       "authority.c's own doc)\n",
                       (unsigned)s_presentation[i].entity_id);
                continue;
            }
            printf("[NET][WILDLIFE] presentation: entity %u stale after snapshot reconciliation -- "
                   "local bookkeeping cleared (real actor, if any, left untouched -- see "
                   "pcwld_presentation_reconcile()'s own doc)\n",
                   (unsigned)s_presentation[i].entity_id);
            memset(&s_presentation[i], 0, sizeof(s_presentation[i]));
            removed++;
        }
    }
    return removed;
}

/* T1 review fix: per-poll idle-expiry tick for the local presentation map -- see this file's own
 * "review fix" doc at pcwld_host_check_idle() (above) for the full bug/choice-of-fix rationale, which
 * applies identically here (this map has exactly the same "no free/reuse path" problem the
 * authoritative table has). Callable from ANY role: unlike the authoritative table, s_presentation[]
 * is ordinary per-process local bookkeeping that exists on a host AND a client alike (populated by
 * pcwld_presentation_create(), which either role calls -- see that function's own doc). Freeing a
 * slot here only means pcwld_presentation_has() may return 0 for that entity_id again; it never
 * touches a real actor (see this file's own top-of-file despawn-safety doc) and, in the ordinary case
 * (no message redelivery), never causes a double-materialization -- see this file's own choice-of-fix
 * doc for the one acknowledged low-probability edge case (a redelivered WILDLIFE_SPAWN for an
 * already-expired-but-still-actually-alive entity_id would be treated as new and materialize a
 * second actor). Safe to call with no active town/gamePT (a no-op, same "no work to do yet" shape as
 * every other per-poll tick in this codebase). */
/* TRACE (diagnostics only): every ~5 s the current world position of every live presentation actor, to compare one entity between processes */
static void pcwld_trace_positions(float dt) {
    static float accum = 0.0f;
    static float interval = -1.0f;
    int i;
    if (interval < 0.0f) {
        const char* e = pc_test_hook_getenv("AC_TEST_WILDLIFE_TRACE_FRAMES"); /* TEST-ONLY: finer trace interval for a diagnosis run */
        interval = (e != NULL && atoi(e) > 0) ? (float)atoi(e) : 300.0f;
    }
    accum += dt;
    if (accum < interval) {
        return;
    }
    accum = 0.0f;
    if (!pcfa_scene_is_town()) {
        return;
    }
    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        const PcWildlifePresentationSlot* s = &s_presentation[i];
        if (!s->active || !s->has_actor) {
            continue;
        }
        if (s->kind == PC_WILDLIFE_KIND_BUG && pcwld_bug_presentation_actor_still_alive(s)) {
            const ACTOR* a = (const ACTOR*)s->local_actor;
            printf("[NET][WILDLIFE][TRACE] now entity %u bug species %d world(%.1f,%.1f,%.1f) block(%d,%d)\n", (unsigned)s->entity_id, s->species, a->world.position.x, a->world.position.y,
                   a->world.position.z, (int)a->block_x, (int)a->block_z);
        } else if (s->kind == PC_WILDLIFE_KIND_FISH) {
            float p[8];
            int fbx = 0, fbz = 0;
            if (aGYO_pc_entity_position((u32)s->entity_id, p, &fbx, &fbz)) {
                const PLAYER_ACTOR* pl = (gamePT != NULL) ? get_player_actor_withoutCheck((GAME_PLAY*)gamePT) : NULL;
                printf("[NET][WILDLIFE][TRACE] now entity %u fish species %d world(%.1f,%.1f,%.1f) home(%.1f,%.1f,%.1f) block(%d,%d) action=%d state=0x%X localplayer(%.0f,%.0f)\n",
                       (unsigned)s->entity_id, s->species, p[0], p[1], p[2], p[3], p[4], p[5], fbx, fbz, (int)p[6], (unsigned)p[7],
                       pl != NULL ? (double)pl->actor_class.world.position.x : 0.0, pl != NULL ? (double)pl->actor_class.world.position.z : 0.0);
            }
        }
    }
}

void pcwld_presentation_check_idle(void) {
    int i;
    float dt;

    if (gamePT == NULL) {
        return;
    }
    dt = (float)gamePT->graph->dt_num_60fps_frames;
    pcwld_trace_positions(dt);

    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        if (!s_presentation[i].active) {
            continue;
        }
        /* T8 audit fix (Bug 1): never age out a BUG entry whose local actor is still genuinely alive --
           see pcwld_bug_presentation_actor_still_alive()'s own doc above pcwld_presentation_reconcile().
           The age timer is simply paused (not reset) while the actor stays alive, so a bug that later
           does despawn/flee/get culled starts aging from its true remaining budget, not from zero. */
        if (pcwld_bug_presentation_actor_still_alive(&s_presentation[i])) {
            if (s_presentation[i].age_accum >= pcwld_effective_ttl_frames()) {
                /* Would have expired under the pre-fix logic -- logged only past the threshold (not
                   every poll) so this stays a rare, targeted diagnostic line rather than log spam. */
                printf("[NET][WILDLIFE] presentation: entity %u past idle-expiry threshold but its local "
                       "bug actor is still alive -- expiry SKIPPED (Bug 1 fix, see pc_wildlife_"
                       "authority.c's own doc)\n",
                       (unsigned)s_presentation[i].entity_id);
            }
            continue;
        }
        s_presentation[i].age_accum += dt;
        if (s_presentation[i].age_accum >= pcwld_effective_ttl_frames()) {
            printf("[NET][WILDLIFE] presentation: entity %u idle-expired -- local slot freed for "
                   "reuse (stopgap TTL, see pc_wildlife_authority.c's own doc)\n",
                   (unsigned)s_presentation[i].entity_id);
            memset(&s_presentation[i], 0, sizeof(s_presentation[i]));
        }
    }
}

/* ---- T-catch: ordinary fish catching only -------------------------------------------------- */

int pcwld_fish_species_matches_claim(int record_species, int claimed_species) {
    if (record_species == claimed_species) {
        return 1;
    }
    /* Vanilla SALMON2 -> SALMON conversion (aGYO_setupActor(), ac_gyoei_clip.c_inc) -- the
       authoritative record stores aGYO_Init_c.fish_type VERBATIM, straight off aSOG_gyoei_set()'s own
       decision (pcwld_shim_make_gyoei_proc(), above), i.e. BEFORE that conversion ever runs -- so a
       genuine SALMON2 record is legitimately claimed as plain SALMON by the catching client (whose own
       local actor already went through the real, unmodified aGYO_setupActor()). */
    if (record_species == aGYO_TYPE_SALMON2 && claimed_species == aGYO_TYPE_SALMON) {
        return 1;
    }
    /* Trash substitution (aUKI_get_fish_type(), ac_uki_move.c_inc's own 1/20 roll) -- already settled
       client-side, on the BOBBER's own copy of gyo_type, before aUKI_bite()'s point of no return; per
       this milestone's own design brief, trusted here rather than re-derived host-side (the host has no
       access to the bobber's own per-catch roll state at all). Any recognized trash item id is accepted
       regardless of the record's own (pre-trash-roll) species. */
    if (aGYO_IS_FISH_TRASH(claimed_species)) {
        return 1;
    }
    return 0;
}

void pcwld_clear_local_actor_stamps(void) {
    if (!pcfa_scene_is_town()) {
        return; /* no live local fish-actor pool to touch right now -- same precondition
                   pcwld_handle_wildlife_despawn() enforces just below */
    }
    aGYO_pc_clear_all_entity_stamps();
}

int pcwld_handle_wildlife_despawn(uint32_t entity_id) {
    /* Bug fix (post-T3 review): aGYO_actor_dt() (ac_gyoei.c) never resets aGYO_ctrlActor to NULL nor
     * clears ctrl[].exist when the GYOEI controller actor itself is torn down -- which happens whenever
     * THIS process's own town scene is unloaded (entering a house/shop/museum etc). Without this gate,
     * a despawn broadcast arriving while this client is indoors would make
     * aGYO_pc_handle_wildlife_despawn() scan a dangling aGYO_ctrlActor pointing at freed memory, and
     * potentially WRITE into it (gyo_flags |= 0x20) if the stale bytes happen to look like a match.
     * Gate on pcfa_scene_is_town() exactly like every other host/client wildlife entry point in this
     * file (see pcwld_host_spawn_trigger()'s own identical precedent above) -- a despawn is only ever
     * meaningful against a local fish-actor pool that is actually alive, which only holds while this
     * process's own scene is the town. */
    if (!pcfa_scene_is_town()) {
        return 0;
    }
    /* T4: dispatch to both the fish and the bug local-reconciliation paths unconditionally -- each is
     * fully self-contained and independently recognizes only entities it actually tracks (the fish path
     * scans aGYO_ctrlActor's own ctrl[]._1F8 stamps; the bug path scans this module's own s_presentation[]
     * for kind == PC_WILDLIFE_KIND_BUG), so calling the "wrong" one for a given entity_id is always a
     * harmless, cheap no-op (found < 0 / no matching slot) rather than something that needs a kind lookup
     * first -- entity_id's own authoritative record has, in fact, ALREADY been removed from the table by
     * the time a despawn is ever broadcast (pcnetgame_commit_catch_despawn() removes-then-broadcasts), so
     * there is no authoritative kind to look up here even if this function wanted to. */
    return aGYO_pc_handle_wildlife_despawn(entity_id) || pcwld_bug_handle_wildlife_despawn(entity_id);
}

/* ---- T4: ordinary bug catching -------------------------------------------------------------- */

int pcwld_bug_species_matches_claim(int record_species, int claimed_species) {
    /* No trash-substitution or type-conversion equivalent for bugs exists in vanilla (verified:
       aINS_setupActor()/make_insect_proc() never rewrite insect_type the way aGYO_setupActor() rewrites
       SALMON2->SALMON, and there is no bug equivalent of aUKI_get_fish_type()'s 1/20 trash roll) -- an
       exact match is the only legitimate claim. */
    return record_species == claimed_species;
}

void* pcwld_bug_local_actor_for_entity(uint32_t entity_id, int* out_species) {
    int i;

    if (entity_id == 0 || !pcfa_scene_is_town()) {
        return NULL;
    }
    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        if (s_presentation[i].active && s_presentation[i].kind == PC_WILDLIFE_KIND_BUG &&
            s_presentation[i].entity_id == entity_id) {
            if (!s_presentation[i].has_actor || s_presentation[i].local_actor == NULL) {
                return NULL; /* deferred (ant) or a recovery failure -- nothing to hand back */
            }
            if (out_species != NULL) {
                *out_species = s_presentation[i].species;
            }
            return s_presentation[i].local_actor;
        }
    }
    return NULL;
}

uint32_t pcwld_bug_entity_id_for_local_actor(const void* local_actor, int species) {
    int i;

    if (local_actor == NULL || !pcfa_scene_is_town()) {
        return 0;
    }
    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        if (s_presentation[i].active && s_presentation[i].kind == PC_WILDLIFE_KIND_BUG &&
            s_presentation[i].has_actor && s_presentation[i].local_actor == local_actor &&
            s_presentation[i].species == species) {
            return s_presentation[i].entity_id;
        }
    }
    return 0;
}

void pcwld_bug_controller_torn_down(void) {
    int i;

    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        if (s_presentation[i].active && s_presentation[i].kind == PC_WILDLIFE_KIND_BUG) {
            s_presentation[i].local_actor = NULL;
            s_presentation[i].has_actor = 0;
        }
    }
}

int pcwld_bug_handle_wildlife_despawn(uint32_t entity_id) {
    int i;

    if (!pcfa_scene_is_town()) {
        return 0; /* same precondition pcwld_handle_wildlife_despawn() already enforces */
    }
    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        aINS_INSECT_ACTOR* insect;

        if (!s_presentation[i].active || s_presentation[i].kind != PC_WILDLIFE_KIND_BUG ||
            s_presentation[i].entity_id != entity_id) {
            continue;
        }
        if (!s_presentation[i].has_actor || s_presentation[i].local_actor == NULL) {
            return 0; /* deferred (ant) or already invalidated -- nothing to touch */
        }
        insect = (aINS_INSECT_ACTOR*)s_presentation[i].local_actor;
        if (insect->exist_flag != TRUE || insect->type != s_presentation[i].species) {
            /* Stale pointer (see pcwld_bug_controller_torn_down()'s own doc): either this slot's actor
               already naturally left on its own, or the underlying memory was reused for an unrelated
               bug after an indoor round-trip. Either way, touching it would be wrong -- treat exactly
               like "not found locally", the same as fish's own found < 0 case. */
            return 0;
        }
        /* Deferred destroy -- ac_insect_move.c_inc's own move loop already refuses to run aINS_destruct()
           while this exact actor is the local player's own current item_net_catch_label, so no
           engagement-state case-branch (unlike fish's Case 1/2/3) is needed here: it is always safe to
           set this flag unconditionally once the identity checks above have passed. */
        insect->insect_flags.destruct = TRUE;

        /* T8 review fix (regression in Bug 2 part b, post-T8): do NOT clear this entity's local_actor
         * mapping here. The earlier "clear immediately on despawn" fix (removed) reintroduced a genuine
         * double-award race: ac_insect_move.c_inc's aINS_cull_check() deliberately refuses to run
         * aINS_destruct() on an actor that is still THIS process's own current item_net_catch_label
         * (mPlib_Get_item_net_catch_label(), see the check just above insect->insect_flags.destruct is
         * set) -- so when a DIFFERENT peer's catch on this same entity_id is accepted by the host FIRST,
         * the resulting WILDLIFE_DESPAWN can arrive at the LOSING peer while ITS OWN local actor is still
         * genuinely alive and mid pull/notice/putaway animation, still holding the catch label. That
         * losing peer's own Notice_net/putaway_net seam still needs pcwld_bug_entity_id_for_local_actor()
         * to resolve the real entity_id for that label later in the sequence, in order to receive a
         * proper host REJECT; clearing the mapping here would make that lookup return 0 ("untracked") and
         * fall through to a plain vanilla grant -- reopening exactly the "exactly one winner" guarantee
         * T4's own race test was built to verify.
         *
         * This is safe to leave unclear: pcwld_presentation_create()'s own defense-in-depth collision
         * scan (Bug 2 part a, just above its slot assignment) already invalidates any OTHER entry still
         * pointing at this same pool-slot address the moment a NEW bug is actually materialized there, and
         * Bug 1's fix (pcwld_bug_presentation_actor_still_alive(), used by both pcwld_presentation_check_
         * idle() and pcwld_presentation_reconcile()) already keeps this slot from being aged out or
         * reconciled away while the actor is still genuinely alive -- once the actor really dies (exist_
         * flag goes FALSE, e.g. once the label finally releases and the deferred destruct above actually
         * runs), idle-expiry resumes and reaps this entry normally, exactly like fish's own stamp-based
         * identity already implicitly relies on "stale entries get cleaned up eventually, not
         * synchronously" reasoning. entity_id/kind/species bookkeeping is left alone either way, so a
         * redelivered WILDLIFE_DESPAWN for this same entity_id still returns 0 harmlessly above once the
         * actor genuinely stops existing (the exist_flag/species identity check just above). */
        return 1;
    }
    return 0;
}

/* T8 audit fix (Bug 3): see this function's own doc, pc_wildlife_authority.h. */
int pcwld_should_suppress_local_wildlife(void) {
    return pc_net_game_role() == PC_NETGAME_ROLE_CLIENT;
}

/* ================================================================================================
 * HOST-AUTHORITATIVE WILDLIFE SIMULATION (see pc_wildlife_authority.h)
 * ================================================================================================ */

int pcwld_sim_is_client(void) {
    return pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && pc_net_game_authoritative_wildlife_enabled();
}

int pcwld_sim_is_host(void) {
    return pc_net_game_role() == PC_NETGAME_ROLE_HOST && pc_net_game_authoritative_wildlife_enabled();
}

/* ---- the host's AI sees the connected players ---- */

int pcwld_remote_scare(float x, float z, int16_t* angle_to_player) {
    PcWldRemotePlayer rp[PCWLD_MAX_REMOTE_PLAYERS];
    int i, n = pc_net_game_wildlife_remote_players(rp, PCWLD_MAX_REMOTE_PLAYERS);
    xyz_t from, to;

    from.x = x;
    from.y = 0.0f;
    from.z = z;
    for (i = 0; i < n; i++) {
        const float dx = rp[i].x - x, dz = rp[i].z - z;
        const float dist = sqrtf(dx * dx + dz * dz);
        if ((rp[i].dash && dist < 110.0f) || (rp[i].tool != 0 && dist < 150.0f)) {
            to.x = rp[i].x;
            to.y = rp[i].y;
            to.z = rp[i].z;
            *angle_to_player = search_position_angleY(&from, &to);
            return 1;
        }
    }
    return 0;
}

int pcwld_nearest_remote_player(float x, float z, float* px, float* py, float* pz) {
    PcWldRemotePlayer rp[PCWLD_MAX_REMOTE_PLAYERS];
    int i, best = -1, n = pc_net_game_wildlife_remote_players(rp, PCWLD_MAX_REMOTE_PLAYERS);
    float best_d = 0.0f;

    for (i = 0; i < n; i++) {
        const float d = (rp[i].x - x) * (rp[i].x - x) + (rp[i].z - z) * (rp[i].z - z);
        if (best < 0 || d < best_d) {
            best = i;
            best_d = d;
        }
    }
    if (best < 0) {
        return 0;
    }
    *px = rp[best].x;
    *py = rp[best].y;
    *pz = rp[best].z;
    return 1;
}

int pcwld_remote_player_in_block(int bx, int bz) {
    PcWldRemotePlayer rp[PCWLD_MAX_REMOTE_PLAYERS];
    int i, n = pc_net_game_wildlife_remote_players(rp, PCWLD_MAX_REMOTE_PLAYERS);

    for (i = 0; i < n; i++) {
        if ((int)(rp[i].x / 640.0f) == bx && (int)(rp[i].z / 640.0f) == bz) {
            return 1;
        }
    }
    return 0;
}

/* ---- a remote angler's bobber, as a real UKI_ACTOR-shaped proxy the unmodified fish AI can read and write ---- */

#define PCWLD_PROXY_MAX 8
#define PCWLD_PROXY_LIFE_MS 800u /* a BOBBER_STATE stream stops: the proxy stops being a target after this */

typedef struct PcWldProxy {
    int        used;
    int        peer;
    uint32_t   until_ms;
    int        active;
    int        prev_cmd;
    uint32_t   prev_entity;
    float      last_fish[3];
    int16_t    last_angle;
    UKI_ACTOR  uki;
} PcWldProxy;

static PcWldProxy s_proxy[PCWLD_PROXY_MAX];

static PcWldProxy* pcwld_proxy_find(int peer, int create) {
    int i, free_i = -1;
    for (i = 0; i < PCWLD_PROXY_MAX; i++) {
        if (s_proxy[i].used && s_proxy[i].peer == peer) {
            return &s_proxy[i];
        }
        if (!s_proxy[i].used && free_i < 0) {
            free_i = i;
        }
    }
    if (create && free_i >= 0) {
        memset(&s_proxy[free_i], 0, sizeof(s_proxy[free_i]));
        s_proxy[free_i].used = 1;
        s_proxy[free_i].peer = peer;
        return &s_proxy[free_i];
    }
    return NULL;
}

void pcwld_host_set_bobber(const PcWldBobber* b) {
    PcWldProxy* p;
    UKI_ACTOR* u;

    if (!pcwld_sim_is_host() || b == NULL) {
        return;
    }
    p = pcwld_proxy_find(b->peer, b->active != 0);
    if (p == NULL) {
        return;
    }
    u = &p->uki;
    if (!b->active) { /* the bobber is gone: a fish still tied to it lets go (the vanilla "player reeled in" path: status COMEBACK) */
        u->status = aUKI_STATUS_COMEBACK;
        u->gyo_status = 0;
        p->active = 0;
        p->until_ms = pc_net_game_now_ms() + PCWLD_PROXY_LIFE_MS;
        return;
    }
    u->actor_class.world.position.x = b->x;
    u->actor_class.world.position.y = b->y;
    u->actor_class.world.position.z = b->z;
    u->actor_class.world.angle.y = b->angle;
    u->actor_class.shape_info.rotation.y = b->angle;
    u->status = b->uki_status;
    u->gyo_status = b->gyo_status;
    u->command = b->command;
    u->hit_water_flag = b->hit_water;
    u->cast_timer = b->cast_timer;
    u->uki_pos.x = b->ux;
    u->uki_pos.y = b->uy;
    u->uki_pos.z = b->uz;
    u->actor_class.bg_collision_check.result.unit_attribute = mCoBG_Wpos2Attribute(u->actor_class.world.position, NULL);
    p->active = 1;
    p->until_ms = pc_net_game_now_ms() + PCWLD_PROXY_LIFE_MS;
}

void pcwld_host_clear_bobber(int peer) {
    PcWldProxy* p = pcwld_proxy_find(peer, 0);
    if (p != NULL) {
        memset(p, 0, sizeof(*p));
    }
}

int pcwld_remote_bobbers(void** out, int max) {
    int i, n = 0;
    const uint32_t now = pc_net_game_now_ms();

    for (i = 0; i < PCWLD_PROXY_MAX && n < max; i++) {
        if (s_proxy[i].used && s_proxy[i].active && (int32_t)(s_proxy[i].until_ms - now) > 0) {
            out[n++] = &s_proxy[i].uki;
        }
    }
    return n;
}

int pcwld_uki_is_proxy(const void* uki) {
    int i;
    for (i = 0; i < PCWLD_PROXY_MAX; i++) {
        if (uki == (const void*)&s_proxy[i].uki) {
            return 1;
        }
    }
    return 0;
}

int pcwld_host_bobber_events(PcWldBobberEvent* out, int max) {
    int i, n = 0;

    for (i = 0; i < PCWLD_PROXY_MAX; i++) {
        PcWldProxy* p = &s_proxy[i];
        UKI_ACTOR* u = &p->uki;
        const int cmd = u->gyo_command;

        if (!p->used) {
            continue;
        }
        if (cmd != p->prev_cmd && n < max) {
            PcWldBobberEvent* e = &out[n];
            ACTOR* fish = u->child_actor;
            memset(e, 0, sizeof(*e));
            e->peer = (uint8_t)p->peer;
            if (cmd != 0 && fish != NULL) {
                p->prev_entity = aGYO_pc_get_entity_id_stamp(fish);
                p->last_fish[0] = fish->world.position.x;
                p->last_fish[1] = fish->world.position.y;
                p->last_fish[2] = fish->world.position.z;
                p->last_angle = fish->world.angle.y;
            }
            e->entity_id = p->prev_entity;
            e->gyo_type = (int16_t)u->gyo_type;
            e->x = p->last_fish[0];
            e->y = p->last_fish[1];
            e->z = p->last_fish[2];
            e->angle = p->last_angle;
            e->ev = (cmd == 1) ? PCWLD_BEV_NEAR_TOUCH : (cmd == 2) ? PCWLD_BEV_BITE : (p->prev_cmd != 0 ? PCWLD_BEV_RELEASE : 0);
            if (e->ev != 0 && e->entity_id != 0) {
                printf("[NET][WILDLIFE][BOBBER] host: peer %d bobber event %d entity %u (gyo_command %d -> %d, fish type %d)\n", p->peer, e->ev, (unsigned)e->entity_id, p->prev_cmd, cmd,
                       (int)e->gyo_type);
                n++;
            }
            p->prev_cmd = cmd;
        }
        if (u->touched_flag) { /* the fish nibbled: nobody consumes this flag on the proxy */
            u->touched_flag = FALSE;
            if (u->child_actor != NULL && n < max) {
                PcWldBobberEvent* e = &out[n];
                memset(e, 0, sizeof(*e));
                e->peer = (uint8_t)p->peer;
                e->ev = PCWLD_BEV_NUDGE;
                e->entity_id = aGYO_pc_get_entity_id_stamp(u->child_actor);
                if (e->entity_id != 0) {
                    n++;
                }
            }
        }
        if (p->used && !p->active && (int32_t)(p->until_ms - pc_net_game_now_ms()) < 0 && u->gyo_command == 0) {
            memset(p, 0, sizeof(*p)); /* idle and silent: free the slot */
        }
    }
    return n;
}

/* ---- what the host tells everybody ---- */

static int pcwld_find_presentation(uint32_t entity_id) {
    int i;
    for (i = 0; i < PCWLDP_MAX_LOCAL; i++) {
        if (s_presentation[i].active && s_presentation[i].entity_id == entity_id) {
            return i;
        }
    }
    return -1;
}

int pcwld_host_collect_state(PcWldStateEntry* out, int max) {
    int i, n = 0;

    if (!pcwld_sim_is_host() || !pcfa_scene_is_town()) {
        return 0;
    }
    for (i = 0; i < PCWLD_MAX_ENTITIES && n < max; i++) {
        PcWldStateEntry* e = &out[n];
        const PcWildlifeRecord* r;
        int ps;
        if (!s_table[i].active) {
            continue;
        }
        r = &s_table[i].rec;
        memset(e, 0, sizeof(*e));
        e->entity_id = r->entity_id;
        e->engaged = 0xFF;
        if (r->kind == PC_WILDLIFE_KIND_FISH) {
            float xyz[3];
            int16_t ang;
            int act;
            ACTOR* fish = NULL;
            if (!aGYO_pc_entity_state(r->entity_id, xyz, &ang, &act)) {
                continue; /* no live host actor: nothing simulated for it */
            }
            e->x = xyz[0];
            e->y = xyz[1];
            e->z = xyz[2];
            e->angle = ang;
            e->action = (uint8_t)act;
            if (act >= 3 && aGYO_pc_find_entity(r->entity_id, &fish) && ((aGYO_CTRL_ACTOR*)fish)->linked_actor != NULL) {
                int k;
                e->engaged = 0xFE;
                for (k = 0; k < PCWLD_PROXY_MAX; k++) {
                    if (s_proxy[k].used && (ACTOR*)&s_proxy[k].uki == ((aGYO_CTRL_ACTOR*)fish)->linked_actor) {
                        e->engaged = (uint8_t)s_proxy[k].peer;
                    }
                }
            }
        } else {
            const aINS_INSECT_ACTOR* in;
            ps = pcwld_find_presentation(r->entity_id);
            if (ps < 0 || !pcwld_bug_presentation_actor_still_alive(&s_presentation[ps])) {
                continue;
            }
            in = (const aINS_INSECT_ACTOR*)s_presentation[ps].local_actor;
            e->x = in->tools_actor.actor_class.world.position.x;
            e->y = in->tools_actor.actor_class.world.position.y;
            e->z = in->tools_actor.actor_class.world.position.z;
            e->angle = in->tools_actor.actor_class.shape_info.rotation.y;
            e->action = (uint8_t)in->action;
        }
        /* the record follows the creature: the catch reach check (pcnetgame_validate_catch) must measure against where it IS */
        s_table[i].rec.pos_x = e->x;
        s_table[i].rec.pos_y = e->y;
        s_table[i].rec.pos_z = e->z;
        n++;
    }
    return n;
}

void pcwld_host_ensure_actors(int (*attended)(const PcWildlifeRecord*)) {
    static float accum = 0.0f;
    int i;

    if (!pcwld_sim_is_host() || gamePT == NULL || !pcfa_scene_is_town()) {
        return;
    }
    accum += (float)gamePT->graph->dt_num_60fps_frames;
    if (accum < 60.0f) {
        return;
    }
    accum = 0.0f;
    for (i = 0; i < PCWLD_MAX_ENTITIES; i++) {
        const PcWildlifeRecord* r;
        int alive;
        if (!s_table[i].active) {
            continue;
        }
        r = &s_table[i].rec;
        if (r->kind == PC_WILDLIFE_KIND_BUG && r->species == aINS_INSECT_TYPE_ANT) {
            continue;
        }
        if (r->kind == PC_WILDLIFE_KIND_FISH) {
            alive = aGYO_pc_entity_alive((u32)r->entity_id);
        } else {
            const int ps = pcwld_find_presentation(r->entity_id);
            alive = ps >= 0 && pcwld_bug_presentation_actor_still_alive(&s_presentation[ps]);
        }
        if (!alive && s_table[i].recreates < 5 && attended(r)) {
            s_table[i].recreates++;
            printf("[NET][WILDLIFE] host: entity %u (kind %d) is attended but has no host actor -- re-creating it where it was (%.0f,%.0f)\n", (unsigned)r->entity_id, r->kind, r->pos_x, r->pos_z);
            (void)pcwld_presentation_create(r->entity_id, r->kind, r->species, r->bx, r->bz, r->pos_x, r->pos_y, r->pos_z);
        }
    }
}

/* ---- what a client does with it ---- */

#define PCWLD_TARGET_STALE_MS 2500u

typedef struct PcWldTarget {
    int            used;
    PcWldStateEntry e;
    uint32_t       ms;
} PcWldTarget;

static PcWldTarget s_target[PCWLD_PUBLIC_MAX_ENTITIES];

/* A creature the host still simulates whose local actor vanilla culled (the player drifted away from it) comes back as soon as the
 * player is near it again: the host keeps broadcasting its state, so the client re-creates the local copy where the host says it is. */
static void pcwld_client_rematerialize(const PcWldStateEntry* e) {
    static uint32_t s_last_try_ms;
    const uint32_t now = pc_net_game_now_ms();
    const PLAYER_ACTOR* pl;
    int k, bx = 0, bz = 0;
    xyz_t wp;
    float dx, dz;

    if (s_last_try_ms != 0 && (int32_t)(now - s_last_try_ms) < 1000) {
        return;
    }
    for (k = 0; k < PCWLDP_MAX_LOCAL; k++) {
        if (s_presentation[k].active && s_presentation[k].entity_id == e->entity_id) {
            break;
        }
    }
    if (k >= PCWLDP_MAX_LOCAL || !s_presentation[k].has_actor || pcwld_presentation_actor_alive(&s_presentation[k])) {
        return;
    }
    pl = (gamePT != NULL) ? get_player_actor_withoutCheck((GAME_PLAY*)gamePT) : NULL;
    if (pl == NULL) {
        return;
    }
    dx = e->x - pl->actor_class.world.position.x;
    dz = e->z - pl->actor_class.world.position.z;
    if (dx * dx + dz * dz > 450.0f * 450.0f) {
        return;
    }
    wp.x = e->x; wp.y = 0.0f; wp.z = e->z;
    if (!mFI_Wpos2BlockNum(&bx, &bz, wp)) {
        return;
    }
    s_last_try_ms = now;
    pcwld_presentation_create(e->entity_id, s_presentation[k].kind, s_presentation[k].species, bx, bz, e->x, e->y, e->z);
}

void pcwld_client_set_state(const PcWldStateEntry* e) {
    int i, free_i = -1;
    pcwld_client_rematerialize(e);
    for (i = 0; i < PCWLD_PUBLIC_MAX_ENTITIES; i++) {
        if (s_target[i].used && s_target[i].e.entity_id == e->entity_id) {
            s_target[i].e = *e;
            s_target[i].ms = pc_net_game_now_ms();
            return;
        }
        if (!s_target[i].used && free_i < 0) {
            free_i = i;
        }
    }
    if (free_i < 0) { /* full: reuse the stalest */
        uint32_t oldest = 0;
        const uint32_t now = pc_net_game_now_ms();
        free_i = 0;
        for (i = 0; i < PCWLD_PUBLIC_MAX_ENTITIES; i++) {
            if (now - s_target[i].ms > oldest) {
                oldest = now - s_target[i].ms;
                free_i = i;
            }
        }
    }
    s_target[free_i].used = 1;
    s_target[free_i].e = *e;
    s_target[free_i].ms = pc_net_game_now_ms();
}

static const PcWldTarget* pcwld_find_target(uint32_t entity_id) {
    int i;
    const uint32_t now = pc_net_game_now_ms();
    for (i = 0; i < PCWLD_PUBLIC_MAX_ENTITIES; i++) {
        if (s_target[i].used && s_target[i].e.entity_id == entity_id) {
            return (now - s_target[i].ms) <= PCWLD_TARGET_STALE_MS ? &s_target[i] : NULL; /* a host that went quiet: the local AI is free again */
        }
    }
    return NULL;
}

static void pcwld_ease_actor(ACTOR* a, const PcWldStateEntry* t, float k) {
    const float dx = t->x - a->world.position.x, dz = t->z - a->world.position.z;
    const float d = sqrtf(dx * dx + dz * dz);
    int bx, bz;
    xyz_t wp;

    if (d > 200.0f) {
        k = 1.0f; /* far off: snap */
    }
    a->world.position.x += dx * k;
    a->world.position.z += dz * k;
    a->world.position.y += (t->y - a->world.position.y) * k;
    a->world.angle.y = t->angle;
    a->shape_info.rotation.y = t->angle;
    wp = a->world.position;
    if (mFI_Wpos2BlockNum(&bx, &bz, wp)) {
        a->block_x = (int8_t)bx;
        a->block_z = (int8_t)bz;
    }
}

int pcwld_fish_is_driven(const void* fish_actor) {
    return pcwld_sim_is_client() && aGYO_pc_get_entity_id_stamp((ACTOR*)fish_actor) != 0;
}

void pcwld_drive_fish(void* fish_actor) {
    ACTOR* a = (ACTOR*)fish_actor;
    uint32_t entity;
    const PcWldTarget* t;
    int act;

    if (!pcwld_sim_is_client()) {
        return;
    }
    entity = aGYO_pc_get_entity_id_stamp(a);
    if (entity == 0 || (t = pcwld_find_target(entity)) == NULL) {
        return;
    }
    act = aGTT_pc_action(a);
    if (act >= 3 && act <= 6 && ((aGYO_CTRL_ACTOR*)a)->linked_actor != NULL) {
        return; /* engaged with THIS player's bobber (near / touch / bite / hooked): the vanilla bobber code plays it */
    }
    pcwld_ease_actor(a, &t->e, 0.2f);
}

void pcwld_drive_insect(void* insect_actor) {
    aINS_INSECT_ACTOR* in = (aINS_INSECT_ACTOR*)insect_actor;
    ACTOR* a = (ACTOR*)insect_actor;
    uint32_t entity;
    const PcWldTarget* t;
    float k;

    if (!pcwld_sim_is_client()) {
        return;
    }
    if ((ACTOR*)mPlib_Get_item_net_catch_label() == a) {
        return; /* in this player's net */
    }
    entity = pcwld_bug_entity_id_for_local_actor(insect_actor, in->type);
    if (entity == 0 || (t = pcwld_find_target(entity)) == NULL) {
        return;
    }
    k = 0.25f * (float)gamePT->graph->dt_num_60fps_frames;
    pcwld_ease_actor(a, &t->e, k > 1.0f ? 1.0f : k);
}

void pcwld_client_bobber_event(uint32_t entity_id, int ev, int gyo_type, float x, float y, float z, int16_t angle) {
    GAME_PLAY* play = (GAME_PLAY*)gamePT;
    ACTOR* fish = NULL;
    UKI_ACTOR* uki;

    if (!pcwld_sim_is_client() || play == NULL || !pcfa_scene_is_town()) {
        return;
    }
    uki = (UKI_ACTOR*)Actor_info_name_search(&play->actor_info, mAc_PROFILE_UKI, ACTOR_PART_BG);
    if (uki == NULL) {
        return;
    }
    if (!aGYO_pc_find_entity(entity_id, &fish)) {
        int bx = (int)(x / 640.0f), bz = (int)(z / 640.0f);
        if (ev != PCWLD_BEV_NEAR_TOUCH || !pcwld_presentation_create(entity_id, PC_WILDLIFE_KIND_FISH, gyo_type, bx, bz, x, y, z) || !aGYO_pc_find_entity(entity_id, &fish)) {
            printf("[NET][WILDLIFE][BOBBER] client: event %d for entity %u dropped -- no local fish actor\n", ev, (unsigned)entity_id);
            return;
        }
    }
    printf("[NET][WILDLIFE][BOBBER] client: event %d entity %u applied=%d (bobber gyo_status %d, fish action %d)\n", ev, (unsigned)entity_id,
           aGTT_pc_apply_bobber_event(fish, (ACTOR*)uki, ev, gyo_type, x, y, z, angle), (int)uki->gyo_status, aGTT_pc_action(fish));
}

int pcwld_client_collect_bobber(PcWldBobber* out) {
    GAME_PLAY* play = (GAME_PLAY*)gamePT;
    UKI_ACTOR* uki;

    if (play == NULL || !pcfa_scene_is_town()) {
        return 0;
    }
    uki = (UKI_ACTOR*)Actor_info_name_search(&play->actor_info, mAc_PROFILE_UKI, ACTOR_PART_BG);
    if (uki == NULL || uki->status < aUKI_STATUS_CAST) { /* the rod is out but not cast: the host's fish have nothing to look at */
        return 0;
    }
    memset(out, 0, sizeof(*out));
    out->active = 1;
    out->uki_status = (uint8_t)uki->status;
    out->gyo_status = (int8_t)uki->gyo_status;
    out->command = (int8_t)uki->command;
    out->hit_water = uki->hit_water_flag;
    out->cast_timer = (uint8_t)(uki->cast_timer > 255 ? 255 : (uki->cast_timer < 0 ? 0 : uki->cast_timer));
    out->rod_type = (Now_Private != NULL && Now_Private->equipment == ITM_GOLDEN_ROD) ? 1 : 0;
    out->x = uki->actor_class.world.position.x;
    out->y = uki->actor_class.world.position.y;
    out->z = uki->actor_class.world.position.z;
    out->ux = uki->uki_pos.x;
    out->uy = uki->uki_pos.y;
    out->uz = uki->uki_pos.z;
    out->angle = uki->actor_class.world.angle.y;
    return 1;
}
