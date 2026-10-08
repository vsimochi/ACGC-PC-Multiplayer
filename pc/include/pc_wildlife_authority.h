/* pc_wildlife_authority.h - host-authoritative wildlife (fish/bug) spawn-decision table
 * (multiplayer World Ecology milestone, Wildlife Sync T0: authority seam foundation only).
 *
 * SCOPE (T0 foundation only -- see pc_wildlife_authority.c's top-of-file doc for the full design
 * rationale and the audit findings this is built on): this module owns the HOST's authoritative
 * record of which fish/bug spawn-decisions currently exist in the shared town, keyed by a
 * host-issued wildlife_entity_id that is NEVER derived from a vanilla actor pointer or local pool
 * slot index. It does NOT implement catching, tournament sync, a full late-join/reconnect wildlife
 * snapshot, latent-bug gameplay sync, or bee/ant special-case capture sync -- all of that is later
 * Wildlife Sync stages (T1-T5), out of scope here by explicit brief.
 *
 * Only the HOST ever calls pcwld_host_spawn_trigger() / mutates the table. A client only calls the
 * read-only lookup pcwld_find_by_id() (e.g. to recognize a stale/unknown entity_id once catch-style
 * requests exist in a later stage -- see PC_NETGAME_MSG_WILDLIFE_SPAWN's own doc, pc_net_game.c).
 *
 * Decomp-independent header: <stdint.h> types only, mirroring pc_field_authority.h's own
 * convention -- position is plain float x/y/z (the exact xyz_t world position the vanilla
 * aINS_Init_c/aGYO_Init_c decision output already carries), never a decomp xyz_t by value, so this
 * header stays includable from pc_net_game.h without pulling in the whole decomp tree.
 */
#ifndef PC_WILDLIFE_AUTHORITY_H
#define PC_WILDLIFE_AUTHORITY_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
    PC_WILDLIFE_KIND_FISH = 0,
    PC_WILDLIFE_KIND_BUG  = 1,

    PC_WILDLIFE_KIND_NUM
};

/* Minimum authoritative fields only -- see pc_wildlife_authority.c's design doc for why nothing
 * more (no group/index: neither aINS_Init_c nor aGYO_Init_c's decision output carries a multi-spawn
 * batch index the table would need to distinguish; a firefly/red-dragonfly multi-spawn is several
 * independent aSOI_ins_make_sub() calls, each producing its own separate insert here). */
typedef struct PcWildlifeRecord {
    uint32_t entity_id; /* host-issued, monotonic within the session; 0 is never a valid id */
    int      kind;      /* PC_WILDLIFE_KIND_* */
    int      species;   /* the vanilla gyo_type (fish) / insect_type (bug) enum value, verbatim */
    int      bx, bz;     /* raw SET_MANAGER block-number acre, i.e. ax = bx - 1, az = bz - 1
                            (PCFA_ACRE_X_NUM/PCFA_ACRE_Z_NUM, pc_field_authority.h) */
    float    pos_x, pos_y, pos_z; /* the exact xyz_t world position the decision produced */
} PcWildlifeRecord;

/* Clears every record and BEGINS A NEW AUTHORITATIVE WILDLIFE SESSION, which MAY restart the
 * entity_id sequence back at 1 (s_next_entity_id, pc_wildlife_authority.c) -- entity_id values are
 * therefore unique only WITHIN the current session, NEVER a stable cross-session identifier. Host-only
 * bookkeeping; safe to call regardless of role. Called at the same two sites
 * s_host_tree_cut_count/s_host_money_rock are cleared at (pc_net_game.c): a detected town change on
 * an already-running host, and a fresh hosting session's full state reset -- both of which, today,
 * do restart the id sequence -- a wildlife record's bx/bz is only meaningful for the town it was
 * created in. */
void pcwld_reset(void);

/* 1 and fills *out (if non-NULL) if entity_id currently names a live record; 0 (unknown/stale,
 * e.g. already removed, or never issued, or from a previous session after pcwld_reset()) otherwise.
 * Safe to call from any role -- this is the "can a client recognize a stale id" lookup the brief
 * calls out for a later stage's catch-request validation. entity_id 0 always returns 0. */
int pcwld_find_by_id(uint32_t entity_id, PcWildlifeRecord* out);

/* 1 iff at least one record of `kind` currently exists in acre (bx, bz). This is what the host
 * spawn adapter's clip shims (pcwld_host_spawn_trigger()'s internal chk_live_* replacements) use in
 * place of the vanilla local-pool "already alive in this acre" scan. */
int pcwld_acre_has_kind(int bx, int bz, int kind);

/* Number of currently-live records in acre (bx, bz), any kind. Test/diagnostic helper. */
int pcwld_count_in_acre(int bx, int bz);

/* Removes the record named by entity_id, if any (freeing its slot for reuse -- entity_id values are
 * unique only WITHIN the current authoritative wildlife session; see pcwld_reset()'s own doc for
 * when a new session begins and the id sequence may restart). Returns 1 if a record was removed, 0
 * if entity_id was already unknown. Not used anywhere yet in T0 (no despawn policy is implemented --
 * see this module's own top-of-file doc): nothing on the network can currently reach it, and
 * test_wildlife_spawn_wire.py does NOT call or exercise it -- that test covers only spawn-trigger
 * wire behavior (valid/out-of-range acres, multi-acre entity uniqueness, and same-acre/same-kind
 * re-trigger de-duplication). Provided here as a foundation primitive for a later stage's despawn
 * policy. */
int pcwld_remove_by_id(uint32_t entity_id);

/* HOST-ONLY. T1 review fix: per-poll idle-expiry tick for the authoritative table -- closes the bug
 * where NOTHING ever called pcwld_remove_by_id(), so an acre's "already has a fish/bug of this kind"
 * gate (pcwld_acre_has_kind()) never cleared and that acre/kind combo could never spawn again for the
 * rest of the hosting session. A conservative, time-based (NOT vanilla-despawn-accurate) stopgap --
 * see pc_wildlife_authority.c's own doc at this function's definition for the full rationale and why
 * a time-based bound was chosen over inspecting the underlying vanilla actor's exist flag. Call once
 * per poll, host branch only, same placement as pcnetgame_host_check_tree_cut()/
 * pcnetgame_host_check_field_action_money_rock() (pc_net_game.c). No-op if gamePT is not yet valid. */
void pcwld_host_check_idle(void);

/* HOST-ONLY. The narrow adapter around the existing, UNMODIFIED aSOI_insect_set()/aSOG_gyoei_set()
 * vanilla decision functions -- see pc_wildlife_authority.c for the full design (clip-shim
 * strategy, re-entrancy guard, why both overlays run synchronously per call). `bx`/`bz` are the
 * SAME raw block-number acre convention SET_MANAGER's own player_pos.next_bx/next_bz use (an
 * authoritative target acre supplied by the CALLER -- never derived from any local player state
 * here). Returns 1 if the decision actually ran (regardless of whether the vanilla logic itself
 * decided "nothing spawns this time", which is an entirely ordinary, expected outcome -- NOT a
 * failure); 0 if rejected outright: already running re-entrantly, the host's own loaded scene is
 * not the town (field/clip data validity precondition -- see doc), or bx/bz is out of the
 * addressable acre range. Never blocks. Must not be called re-entrantly/recursively (enforced
 * internally with a guard flag) -- safe to call sequentially for any number of distinct acres. */
int pcwld_host_spawn_trigger(int bx, int bz);

/* ================================================================================================
 * T1: LOCAL PRESENTATION (real vanilla fish/bug actor from an already-decided record)
 * ================================================================================================
 * Callable from ANY role. Never rolls RNG, never re-runs aSOI_insect_set()/aSOG_gyoei_set(), never
 * chooses its own species/position/entity_id -- purely constructs a real local actor from data a
 * host decision has already produced (either this process's OWN host decision -- see
 * pcwld_host_spawn_trigger()'s own doc for how the host drains its pending-presentation queue after
 * that decision -- or a remote PC_NETGAME_MSG_WILDLIFE_SPAWN a client received, pc_net_game.c).
 */

/* 1 if entity_id already has a locally-materialized presentation entry (a real actor, OR a species
 * this stage explicitly defers -- see pcwld_presentation_create()'s own doc); 0 otherwise. */
int pcwld_presentation_has(uint32_t entity_id);

/* Creates a REAL local vanilla fish/bug actor for an already-decided record. kind/species/bx,bz/
 * position are a TRUST BOUNDARY on a client (received over the network) and are fully validated
 * here (range-checked; rejected with a log line and return 0, never a crash). A duplicate
 * entity_id (pcwld_presentation_has() already true) is a successful no-op (1) -- never replaces a
 * live actor. Ants (PC_WILDLIFE_KIND_BUG, aINS_INSECT_TYPE_ANT) are accepted but deliberately
 * produce NO actor (recorded as "handled" so a duplicate is still a no-op) -- see
 * pc_wildlife_authority.c's own doc for why ants can't safely share this same synchronous path in
 * T1. Never blocks; a local scene/clip that isn't ready yet (e.g. this process hasn't finished
 * loading the town) is treated as an ordinary, retryable-by-nothing failure (0), not a crash. */
int pcwld_presentation_create(uint32_t entity_id, int kind, int species, int bx, int bz, float x,
                              float y, float z);

/* Clears the local presentation map ONLY -- never touches the authoritative table (pcwld_reset()
 * for that). Safe from any role; called at the same reset points pcwld_reset() and
 * pcnetgame_reset_client_session_state() (pc_net_game.c) already use for other per-town/
 * per-session local state. A real actor this module created is NOT destroyed by this call -- see
 * this module's own top-of-file despawn-safety doc: local actor lifetime is intentionally decoupled
 * from this bookkeeping map. */
void pcwld_presentation_reset(void);

/* ================================================================================================
 * T2: LATE-JOIN / RECONNECT SNAPSHOT SUPPORT
 * ================================================================================================
 * See pc_net_game.c's PCNetGameWildlifeSnapshot{Begin,Entry,End}Msg for the wire side (host ->
 * one client, nested inside the existing SNAPSHOT_BEGIN/.../SNAPSHOT_END pump, mirroring the
 * SNOWMAN_STATE/VILLAGER_SNAPSHOT/FRIENDSHIP_SNAPSHOT_ENTRY precedent already established there).
 * This module only exposes the host-side table ITERATION and the client-side RECONCILIATION
 * primitive; the wire framing, staleness/epoch gating, and session-generation comparison all live
 * in pc_net_game.c, exactly like every other snapshot stage's own split of responsibility. */

/* Public mirror of pc_wildlife_authority.c's own PCWLD_MAX_ENTITIES (PCFA_ACRE_NUM(30) * 4 == 120)
 * -- repeated here as a plain literal, NOT the PCFA_ACRE_NUM-based formula, so this header keeps its
 * own "decomp-independent, <stdint.h> only" contract (see this header's own top-of-file doc) and
 * never needs to include pc_field_authority.h. Verified identical to the real, private
 * PCWLD_MAX_ENTITIES via a _Static_assert in pc_wildlife_authority.c. Used by pc_net_game.c to size
 * its own bounded per-snapshot "entities named as still-live in this snapshot" tracking array for
 * client-side reconciliation (see pcwld_presentation_reconcile() below) -- never invents a second,
 * different capacity number. */
#define PCWLD_PUBLIC_MAX_ENTITIES 120

/* HOST-ONLY (meaningful only there; harmless/always-0-progressing-from-a-seed if ever called
 * elsewhere). Number of currently-active records in the authoritative table, any acre/kind. Used by
 * the host's snapshot pump to fill WILDLIFE_SNAPSHOT_BEGIN's `count` field (a late-joiner/reconnecting
 * client's advance notice of how many WILDLIFE_SNAPSHOT_ENTRY messages will follow -- purely a
 * framing/diagnostic aid, matching PCNetGameSnapshotBeginMsg's own acre_count field's role for
 * FIELD_BLOCK). */
int pcwld_active_count(void);

/* HOST-ONLY iteration primitive: fills *out and returns 1 if authoritative table slot `idx` (0 <=
 * idx < PCWLD_PUBLIC_MAX_ENTITIES) is currently active; 0 otherwise (inactive slot, or idx out of
 * range). Used by the host's snapshot pump to flat-iterate the WHOLE table (every acre, unfiltered --
 * see the T2 milestone brief's own explicit "multi-acre snapshot: the full authoritative table, not
 * one client's local area" requirement) exactly once per WILDLIFE_SNAPSHOT_ENTRY sent, mirroring
 * pcnetgame_build_friendship_snapshot_entry()'s own flat-iteration-with-skip convention. */
int pcwld_get_by_slot(int idx, PcWildlifeRecord* out);

/* The host's current AUTHORITATIVE WILDLIFE SESSION generation -- changes exactly when pcwld_reset()
 * runs (fresh hosting session, or a detected town change on an already-running host), stays constant
 * across everything else (in particular, across any number of peers connecting/disconnecting/
 * reconnecting while the SAME hosting session keeps running). Seeded from a process-startup-time
 * value (never a small, easily-colliding fixed constant like 1) the FIRST time pcwld_reset() ever
 * runs, then a plain increment on every subsequent call -- see pcwld_reset()'s own doc for why: a
 * reconnecting CLIENT compares this value against what it saw on its previous connection (to the
 * same or a possibly-different host process) to decide whether its local wildlife bookkeeping is
 * still trustworthy (same generation -- reconcile against it) or must be fully discarded first (a
 * new/different generation -- see PCNetGameWildlifeSnapshotBeginMsg's own doc, pc_net_game.c, and
 * pcwld_presentation_reconcile() below). 0 is never returned (reserved as "no session yet" on the
 * client side, mirroring entity_id's own 0-reserved convention) -- read-only from any role; only a
 * HOST ever advances it (via pcwld_reset()). */
uint32_t pcwld_session_generation(void);

/* CLIENT-side (or any role) reconciliation primitive, called ONCE per completed wildlife snapshot
 * (see pcnetgame_handle_client_wildlife_snapshot_end(), pc_net_game.c) -- NEVER incrementally per
 * entry, since the correct "stale" set can only be known once the FULL snapshot has been received.
 * `keep_ids`/`keep_count` is the list of every entity_id actually named by the snapshot just
 * completed (already created/refreshed via pcwld_presentation_create() as each
 * WILDLIFE_SNAPSHOT_ENTRY arrived). Any presentation-map entry whose entity_id is NOT in that list is
 * STALE (the host's current authoritative table no longer contains it, or it belongs to a
 * superseded/previous session already handled by the generation check above) and is removed from the
 * LOCAL bookkeeping map ONLY -- see pcwld_presentation_reset()'s own doc and this module's top-of-file
 * "Despawn safety" doc for why this deliberately does NOT attempt to destroy the real local actor
 * (T2 is explicitly NOT a despawn policy -- the milestone brief's own ABSOLUTE SCOPE LIMIT excludes
 * "final despawn policy"). This is purely local, client-side bookkeeping cleanup: it never calls back
 * into, or otherwise mutates, the HOST's authoritative table (the host has no way to even receive
 * such a call from here) -- a client has no authority to declare something despawned; the host
 * already independently decided that on its own (that is WHY the entity is absent from the
 * snapshot). Returns the number of stale entries removed (0 is an entirely ordinary, expected
 * outcome -- e.g. every fresh late-join, whose local map already started empty). Safe to call with
 * keep_count == 0 or keep_ids == NULL (treated as an empty keep-list -- every currently-tracked entry
 * is stale). */
int pcwld_presentation_reconcile(const uint32_t* keep_ids, int keep_count);

/* T1 review fix: per-poll idle-expiry tick for the local presentation map -- callable from ANY role.
 * Closes the bug where this fixed-size map (PCWLDP_MAX_LOCAL slots) had no free/reuse path at all, so
 * it would eventually fill permanently within a session and refuse every further local wildlife
 * materialization from then on. Same conservative time-based stopgap as pcwld_host_check_idle() --
 * see that function's own doc and pc_wildlife_authority.c's definition of this function for the full
 * rationale. Call once per poll from any role (both a host and a client maintain their own local
 * presentation map). No-op if gamePT is not yet valid. */
void pcwld_presentation_check_idle(void);

/* ================================================================================================
 * T-catch: ordinary fish catching only (see this milestone's own ABSOLUTE SCOPE LIMIT -- no bug
 * catching, no tournaments, no ant/bee special-case handling, no general despawn policy beyond what
 * catching itself needs).
 * ================================================================================================ */

/* HOST-ONLY species-claim check for an accepted CATCH_REQUEST (pc_net_game.c). `record_species` is the
 * authoritative table's own PcWildlifeRecord.species for the entity being claimed (PRE the vanilla
 * SALMON2->SALMON conversion aGYO_setupActor() applies at creation time -- see
 * pcwld_shim_make_gyoei_proc()'s own doc: the record stores aGYO_Init_c.fish_type verbatim, straight
 * off aSOG_gyoei_set()'s decision, before any conversion). `claimed_species` is the requester's own
 * claimed uki->gyo_type (a TRUST BOUNDARY value off the wire). Returns 1 iff the claim is legitimate:
 * an exact match, the vanilla SALMON2->SALMON conversion, or any recognized trash substitution
 * (aGYO_IS_FISH_TRASH, ac_gyoei.h) -- trash is TRUSTED rather than re-derived host-side, since the
 * 1/20 substitution roll (aUKI_get_fish_type(), ac_uki_move.c_inc) is already settled client-side
 * before aUKI_bite()'s point of no return, per this milestone's own design brief. 0 otherwise
 * (rejected -- never silently accepted "close enough"). */
int pcwld_fish_species_matches_claim(int record_species, int claimed_species);

/* Callable from any role. WILDLIFE_DESPAWN reconciliation entry point for THIS process's own local
 * fish presentation -- thin wrapper around aGYO_pc_handle_wildlife_despawn() (ac_gyoei.h/.c,
 * TARGET_PC-gated) so pc_net_game.c never needs to include decomp fish-actor headers directly (same
 * separation of concerns as every other pcwld_* entry point in this header). See that function's own
 * doc for the full 3-case contract. Returns 1 if a locally-stamped actor was found for entity_id
 * (whether or not anything needed to change), 0 if this process never materialized one locally. */
int pcwld_handle_wildlife_despawn(uint32_t entity_id);

/* Bug fix (post-T3 review, Bug C -- stale actor stamps surviving a generation change). Thin wrapper
 * around aGYO_pc_clear_all_entity_stamps() (ac_gyoei.h/.c) that adds the same pcfa_scene_is_town()
 * precondition check pcwld_host_spawn_trigger() already uses before touching the local fish-actor
 * pool -- a no-op if this process's own town scene is not currently loaded. Called at every point this
 * process treats wildlife bookkeeping as belonging to a brand-new authoritative session (alongside
 * pcwld_presentation_reset(): pcwld_reset() here, and the client-side generation-mismatch branch of
 * pcnetgame_handle_client_wildlife_snapshot_begin(), pc_net_game.c) so a leftover local actor's _1F8
 * entity_id stamp from a superseded session can never be mismatched against a same-numbered entity_id
 * in the new one. */
void pcwld_clear_local_actor_stamps(void);

/* ================================================================================================
 * T4: ordinary bug catching (see pc_net_game.c's own T4 top-of-section doc for the full design).
 * ================================================================================================ */

/* HOST-ONLY species-claim check for an accepted CATCH_REQUEST/host-local catch of a PC_WILDLIFE_KIND_
 * BUG record. Unlike pcwld_fish_species_matches_claim(), there is no vanilla trash-substitution or
 * type-conversion equivalent for bugs (verified: aINS_setupActor()/make_insect_proc() never rewrite
 * insect_type the way aGYO_setupActor() rewrites SALMON2->SALMON, and there is no bug equivalent of
 * aUKI_get_fish_type()'s 1/20 trash roll) -- an exact match is the only accepted claim. Returns 1 iff
 * record_species == claimed_species, 0 otherwise. */
int pcwld_bug_species_matches_claim(int record_species, int claimed_species);

/* Opaque local-actor lookup for a BUG-kind entity_id, used by pc_net_game.c to build a CATCH_REQUEST
 * from the local player's own already-engaged item_net_catch_label (an ordinary net catch only -- see
 * Player_actor_CheckCapture_forNet()'s own doc, m_player_main_swing_net.c_inc). Returns a non-NULL
 * opaque pointer (the SAME pointer pcwld_presentation_create() received back from make_insect_proc(),
 * cast to void*; the caller alone knows to cast it to aINS_INSECT_ACTOR*) and fills *out_species with
 * the recorded species iff entity_id currently names a live, locally-materialized BUG presentation
 * entry; NULL otherwise (unknown entity_id, a different kind, an ant -- deliberately never
 * materialized, see pcwld_presentation_create()'s own doc -- or this process's own scene is not
 * currently the town). See pc_wildlife_authority.c's own doc for why this module stores the pointer
 * itself, in its own bookkeeping, rather than stamping it into the vanilla aINS_INSECT_ACTOR struct the
 * way fish are stamped (aGYO_pc_stamp_entity_id()): no field of aINS_INSECT_ACTOR is genuinely unused
 * across every insect species (verified by grep across every ac_ins_*.c file before this decision was
 * made, mirroring the same due-diligence T3 already applied to _1F8) -- repurposing any of them would
 * silently corrupt an unrelated species' own behavior state. */
void* pcwld_bug_local_actor_for_entity(uint32_t entity_id, int* out_species);

/* Reverse direction of pcwld_bug_local_actor_for_entity(): given the local player's own already-
 * engaged item_net_catch_label pointer and its own insect_type (both read directly off PLAYER_ACTOR by
 * the caller, m_player_main_notice_net.c_inc / m_player_main_putaway_net.c_inc), returns the entity_id
 * this module issued for it, or 0 if this exact pointer+species combination is not currently tracked as
 * a live local BUG presentation entry. The species cross-check is a safety margin against a stale
 * pointer left over from before an indoor round-trip (see pcwld_bug_controller_torn_down()'s own doc)
 * coincidentally matching a slot the SAME memory now holds for a genuinely different, same-species bug
 * -- an exact pointer match alone is not trusted. */
uint32_t pcwld_bug_entity_id_for_local_actor(const void* local_actor, int species);

/* PC-only, additive: invalidates the local_actor pointer of every currently-tracked BUG presentation
 * entry (entity_id/species/kind bookkeeping itself is untouched, and stays subject to the ordinary
 * idle-expiry/reconcile paths). Call exactly once, from aINS_actor_dt() (ac_insect.c, TARGET_PC-gated,
 * mirroring ac_gyoei.c's own defensive `aGYO_ctrlActor = NULL` fix), whenever the single shared
 * aINS_CTRL_ACTOR controller actor -- and, with it, every one of its aINS_ACTOR_NUM insect_actor[]
 * slots -- is torn down. That happens on every ordinary town-scene unload (entering a house/shop/
 * museum), not only a full town change, so without this hook a stale local_actor pointer could, in the
 * narrow case where the very same memory is reused for a brand-new same-species bug after the player
 * returns to town, misidentify that unrelated new bug as the old entity_id. Safe to call with no active
 * BUG presentation entries (a no-op). */
void pcwld_bug_controller_torn_down(void);

/* Callable from any role. WILDLIFE_DESPAWN reconciliation entry point for THIS process's own local BUG
 * presentation -- companion to pcwld_handle_wildlife_despawn() (which already dispatches to this
 * internally; not normally called directly). Sets aINS_INSECT_ACTOR::insect_flags.destruct on the
 * locally-tracked actor for entity_id, iff its exist_flag/species still match what was recorded at
 * materialization time. No case-branch on engagement state is needed the way fish's despawn handler
 * needs one: ac_insect_move.c_inc's own move loop already refuses to run aINS_destruct() while the
 * target actor is the local player's own current item_net_catch_label (see that file's own check) --
 * i.e. vanilla itself already protects an actively-engaged catch from a concurrent deferred-destroy
 * request. Returns 1 if a locally-tracked, still-matching actor was found (whether or not anything
 * needed to change), 0 otherwise. */
int pcwld_bug_handle_wildlife_despawn(uint32_t entity_id);

/* T8 audit fix (Bug 3): pc_net_game_world_is_host_authoritative() (pc_net_game.h) gates on LINK STATE
 * (s_client_link == READY), not ROLE -- while disconnected, s_role stays PC_NETGAME_ROLE_CLIENT but
 * s_client_link leaves READY, so that shared gate reads FALSE during a disconnect. Many OTHER systems
 * (villager movement/population, weather, snowmen, NPC think -- every other caller of that shared
 * function, pc_net_game.c) correctly WANT exactly that link-state semantics: while disconnected, THOSE
 * systems should fall back to local authority rather than freeze waiting on a host that isn't there, so
 * changing the shared function's own definition to check role instead of link state would regress every
 * one of those callers. This wildlife-specific helper instead narrowly answers "should local ambient
 * wildlife spawning be suppressed / should a local catch grant be denied", which must stay true across a
 * disconnect (role stays CLIENT the whole time) -- unlike the shared gate. Always FALSE for a HOST or for
 * single-player (role NONE), exactly like today, so neither of those roles is affected by this fix. Used
 * by the SET_MANAGER spawn-suppression gate (ac_set_manager.c) and both catch-interception seams
 * (m_player_main_notice_rod.c_inc / m_player_main_notice_net.c_inc) in place of (never instead of the
 * definition of) pc_net_game_world_is_host_authoritative(). */
int pcwld_should_suppress_local_wildlife(void);

/* T8 audit verification, TEST-ONLY: overrides the idle-expiry TTL both pcwld_host_check_idle() and
 * pcwld_presentation_check_idle() use (normally the fixed ~10-minute PCWLD_RECORD_MAX_AGE_60FPS_FRAMES
 * constant, pc_wildlife_authority.c) to `frames` 60fps-frame-units instead, so a short automated test run
 * can actually cross the threshold -- see --diag-bug-ttl-lookup's own doc, pc_platform.h. Pass 0 (or never
 * call this) to use the real production constant unmodified; this is the default. Never called from any
 * normal (non-test-flag-gated) code path. */
void pcwld_test_set_ttl_override_frames(float frames);

/* TEST-ONLY (the autopilot's `bugs` command): the idx-th (0-based) bug entity with a LIVE local actor, plus the actor's world position; 0 when there is none. */
int pcwld_test_live_bug(int idx, uint32_t* entity_id, int* species, float* x, float* z);
/* TEST-ONLY (the autopilot's `pres` command): logs every presentation entry and the state of its local actor. */
void pcwld_test_dump_presentation(void);

/* ================================================================================================
 * Record lifetime = actor lifetime (attendance), and entry replay
 * ================================================================================================
 * A vanilla insect / fish actor lives only while a player is near it (aINS_cull_check(): destroyed once the player is more than 600 units away, in another acre and it is off screen; fish
 * likewise). The host's record used to outlive that by a blind 10 minutes, and while it stood the acre's "already has this kind" gate blocked every re-roll: after the first spawn in an
 * acre nobody ever saw wildlife there again, and a player who walked in later saw nothing of what another player was looking at. Two small, additive pieces fix it: */

/* HOST-ONLY. Fills `out` with the live records of acre (bx, bz) (up to `max`), returns the count. */
int pcwld_host_acre_records(int bx, int bz, PcWildlifeRecord* out, int max);

/* HOST-ONLY. One tick of the attendance rule. `attended(rec)` says whether any player is near / in the record's acre (the caller knows the players; this module does not). A record nobody
 * attends for PCWLD_UNATTENDED_GRACE (12 s) is removed (its acre re-rolls on the next entry, like vanilla); its entity_id is stored in out_ids (up to `max`) so the caller can broadcast the
 * despawn. Returns the number removed. `dt_frames` = 60 fps frames since the last call. */
int pcwld_host_collect_unattended(int (*attended)(const PcWildlifeRecord*), float dt_frames, uint32_t* out_ids, int max);

/* Callable from any role. (Re)creates the LOCAL actor of every live record of acre (bx, bz) whose actor is gone (or never existed here) - the host's own player entering an acre. A record
 * whose actor is still alive is left alone. */
void pcwld_host_replay_acre_local(int bx, int bz);

/* ================================================================================================
 * HOST-AUTHORITATIVE WILDLIFE SIMULATION
 * ================================================================================================
 * The host runs the vanilla fish / insect AI for every record; clients show the host's state. See pc_net_game.c (WILDLIFE_STATE / BOBBER_STATE / BOBBER_EVENT) for the wire side.
 *  - host inputs: the vanilla AI looks at ONE local player and ONE local bobber; the adapters below add the connected players (their position, dash, tool use) and their bobbers.
 *  - client output: a creature the host simulates is pulled toward the host's position / heading after the local actor ran its (cosmetic) animation tick; a client never decides a
 *    wildlife outcome (no self-targeting of its bobber, no bite decision): it applies the host's BOBBER_EVENTs to its own bobber and fish copy, which then play the vanilla animation. */

/* 1 = this process is a CLIENT following a host that simulates the wildlife / 1 = this process is the HOST of authoritative wildlife. */
int pcwld_sim_is_client(void);
int pcwld_sim_is_host(void);

/* a connected player as the host's AI sees it (positions of the puppets) */
typedef struct PcWldRemotePlayer {
    float   x, y, z;
    int16_t angle;
    uint8_t peer;
    uint8_t dash; /* running flat out: scares fish within 110 units */
    uint8_t tool; /* 0 none, 1 net swing, 2 axe, 3 scoop / shovel: scares within 150 units */
    uint8_t _pad;
} PcWldRemotePlayer;
#define PCWLD_MAX_REMOTE_PLAYERS 16

/* HOST: 1 iff some connected player scares a fish at (x, z) (dash within 110, a tool within 150); *angle_to_player = the angle from (x, z) to that player. */
int pcwld_remote_scare(float x, float z, int16_t* angle_to_player);
/* HOST: the connected player nearest to (x, z); 0 when there is none. */
int pcwld_nearest_remote_player(float x, float z, float* px, float* py, float* pz);
/* HOST: 1 iff a connected player stands in acre block (bx, bz). */
int pcwld_remote_player_in_block(int bx, int bz);

/* a remote player's bobber as the host's fish AI sees it (a real UKI_ACTOR-shaped proxy, filled from BOBBER_STATE). */
typedef struct PcWldBobber {
    uint8_t peer;
    uint8_t active;
    uint8_t uki_status; /* aUKI_STATUS_* */
    int8_t  gyo_status;
    int8_t  command;    /* the player's command to the bobber (6 = hook / reel in) */
    uint8_t hit_water;
    uint8_t cast_timer;
    uint8_t rod_type;
    float   x, y, z;    /* the bobber */
    float   ux, uy, uz; /* uki_pos */
    int16_t angle;
} PcWldBobber;

/* an event the host's fish AI produced for a remote bobber, to be sent to that peer. */
enum { PCWLD_BEV_NEAR_TOUCH = 1, PCWLD_BEV_NUDGE = 2, PCWLD_BEV_BITE = 3, PCWLD_BEV_RELEASE = 4 };
typedef struct PcWldBobberEvent {
    uint8_t  peer;
    uint8_t  ev;
    int16_t  gyo_type;
    uint32_t entity_id;
    float    x, y, z; /* the fish */
    int16_t  angle;
} PcWldBobberEvent;
#define PCWLD_MAX_BOBBER_EVENTS 8

void pcwld_host_set_bobber(const PcWldBobber* b);
void pcwld_host_clear_bobber(int peer);
/* HOST, once per poll: fills the events the fish AI produced since the last call (a bobber's gyo_command changed, the fish nibbled); returns the count. */
int pcwld_host_bobber_events(PcWldBobberEvent* out, int max);
/* the proxies the fish AI may target (UKI_ACTOR*), and whether a UKI is one of them */
int pcwld_remote_bobbers(void** out, int max);
int pcwld_uki_is_proxy(const void* uki);

/* the periodic state of a creature the host simulates */
typedef struct PcWldStateEntry {
    uint32_t entity_id;
    float    x, y, z;
    int16_t  angle;
    uint8_t  action;
    uint8_t  engaged; /* 0xFF nobody, 0xFE the host's own bobber, else the peer whose bobber the fish follows */
} PcWldStateEntry;
#define PCWLD_STATE_MAX 40

/* HOST: the state of every record the host has a live actor for (and refreshes the record's position from it). */
int pcwld_host_collect_state(PcWldStateEntry* out, int max);
/* HOST, about once a second: a record somebody attends whose host actor is gone (vanilla destroys a fish whose angler lost it, an insect that wandered off...) gets its actor back at the
 * record's last known position, so the host keeps simulating every creature that is alive. */
void pcwld_host_ensure_actors(int (*attended)(const PcWildlifeRecord*));
/* CLIENT: remember the host's latest state of a creature. */
void pcwld_client_set_state(const PcWldStateEntry* e);
/* CLIENT: pull the actor of a driven creature toward the host's state (after its local animation tick). */
void pcwld_drive_fish(void* fish_actor);
void pcwld_drive_insect(void* insect_actor);
/* CLIENT: the host sent a fish / bobber event for this client's own bobber. */
void pcwld_client_bobber_event(uint32_t entity_id, int ev, int gyo_type, float x, float y, float z, int16_t angle);
/* CLIENT: the local bobber as BOBBER_STATE wants it; returns 1 when a bobber exists. */
int pcwld_client_collect_bobber(PcWldBobber* out);
/* 1 iff this CLIENT's fish actor is a stamped creature of the host (its AI must not decide wildlife outcomes). */
int pcwld_fish_is_driven(const void* fish_actor);

#ifdef __cplusplus
}
#endif

#endif /* PC_WILDLIFE_AUTHORITY_H */
