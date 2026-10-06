/* pc_platform.h - SDL2/OpenGL platform layer and global state */
#ifndef PC_PLATFORM_H
#define PC_PLATFORM_H

/* 32-bit required: decomp code (JSystem, emu64) casts pointers to u32 */
#include <stdint.h>
/* PC_LOW_ADDRESS_64 (experimental, set by CMake via -DPC_ALLOW_64BIT=ON) builds a 64-bit
 * process linked so every game pointer stays below 4 GB; see pc_lowaddr.h. */
#if UINTPTR_MAX != 0xFFFFFFFFu && !defined(PC_LOW_ADDRESS_64)
#error "This project must be compiled as 32-bit (pointer size != 4 bytes)"
#endif

#define SDL_MAIN_HANDLED
#include <SDL.h>
#include <glad/gl.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>
#include <time.h>

#include "pc_types.h"
#include "pc_lowaddr.h"

/* --- Configuration --- */
#define PC_GC_WIDTH       640
#define PC_GC_HEIGHT      480
#define PC_SCREEN_WIDTH   PC_GC_WIDTH
#define PC_SCREEN_HEIGHT  PC_GC_HEIGHT
#define PC_WINDOW_TITLE   "Animal Crossing"

#define PC_MAIN_MEMORY_SIZE   (24 * 1024 * 1024)
#define PC_ARAM_SIZE          (16 * 1024 * 1024)
#define PC_FIFO_SIZE          (256 * 1024)

#define PC_SPEEDHACK_MULTIPLIER 10.0

#define PC_PI  3.14159265358979323846
#define PC_PIf 3.14159265358979323846f
#define PC_DEG_TO_RAD (PC_PI / 180.0)
#define PC_DEG_TO_RADf (PC_PIf / 180.0f)

/* GC hardware clocks */
#define GC_BUS_CLOCK          162000000u
#define GC_CORE_CLOCK         486000000u
#define GC_TIMER_CLOCK        (GC_BUS_CLOCK / 4)

/* --- Platform headers --- */
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#undef near
#undef far
#else
#include <sys/mman.h>
#include <dlfcn.h>
#include <elf.h>
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* --- Global state --- */
extern SDL_Window*   g_pc_window;
extern SDL_GLContext  g_pc_gl_context;
/* g_pc_running is written from the SetConsoleCtrlHandler thread and from signal(SIGINT/SIGTERM)
 * handlers (pc/src/pc_main.c) and read every frame by the main loop (graph_proc(), src/graph.c).
 * It is declared atomic_int (C11 <stdatomic.h>) so that ordinary reads and plain `= 0` writes at
 * every existing call site compile to atomic load/store with no source changes required anywhere
 * except this header, the definition in pc_main.c, and graph.c's own separate extern.
 *
 * C++ translation units (pc_stubs_cpp.cpp, which includes this header transitively) never read or
 * write this flag; it is declared here as plain `int` under __cplusplus only because this header
 * must still parse under C++, which has no <stdatomic.h> in the C++17 mode this project builds
 * with. On this toolchain (GCC/MinGW-w64, x86/x64) a lock-free atomic_int -- guaranteed lock-free
 * for int-sized objects on this target -- has the same size, alignment, and object representation
 * as a plain int (there is no wrapper struct), so this declaration stays ABI-compatible with the
 * atomic_int definition it refers to via extern "C" linkage. */
#ifdef __cplusplus
extern int           g_pc_running;
#else
#include <stdatomic.h>
extern atomic_int    g_pc_running;
#endif
extern int           g_pc_verbose;
extern int           g_pc_pickup_test_seed; /* Stage 5A.1: --pickup-test-seed -- see pc_net_game.c */
extern int           g_pc_field_action_test_seed; /* World Ecology Stage 1: --field-action-test-seed --
                                                     * see pc_net_game.c's
                                                     * pcnetgame_run_field_action_test_seed() */
extern int           g_pc_bury_test_seed; /* World Ecology T3: --bury-test-seed -- see pc_net_game.c's
                                             * pcnetgame_run_bury_test_seed() */
/* World Ecology Wildlife Sync T0/T1 gate: --authoritative-wildlife. UNLIKE the *_test_seed/force-*
 * hooks above (one-shot TEST triggers), this is a persistent MODE flag -- it stays in effect for the
 * whole process lifetime, the same way --host/--connect select a persistent role. Off (0) by
 * default: in that default state, aSetMgr_move_set() (ac_set_manager.c) and
 * pc_net_game_request_wildlife_spawn_trigger()/pc_net_game_host_local_wildlife_spawn_trigger()
 * (pc_net_game.c) all fall through to EXACTLY the pre-T0 vanilla behavior (every role calls
 * aSOI_insect_set()/aSOG_gyoei_set() locally, unmodified, on every tick) -- the new host-authoritative
 * wildlife adapter (pc_wildlife_authority.c) is reachable ONLY when this flag is explicitly passed,
 * since running that adapter and its T1 client-side presentation path
 * (pcwld_presentation_create(), which materializes a REAL local vanilla fish/bug actor from a host's
 * decision) on one side while the other side is still doing ordinary vanilla local spawning would
 * double-spawn wildlife -- see pcnetgame_handle_host_wildlife_spawn_trigger_request()'s and
 * pcnetgame_handle_client_wildlife_spawn()'s own gate checks (pc_net_game.c) for the enforcement.
 * See pc_main.c's CLI parsing and ac_set_manager.c's own gate doc for the exact fallthrough shape. */
extern int           g_pc_authoritative_wildlife;
/* Furniture sync (Stage 1, protocol v8 ids 59..61): --house-sync is a HOST opt-in MODE flag (announced in HOST_CONFIG byte 1 bit 0; a client follows the
 * host and ignores its own copy). Off by default. g_pc_house_test_host_in_house / g_pc_house_test_host_edit are TEST-ONLY host hooks (default -1 / NULL,
 * never active in normal play): see pc_main.c and pc_net_game.c (pcnetgame_local_house_unsafe / pcnetgame_house_test_host_edit). */
extern int           g_pc_house_sync;
extern int           g_pc_house_test_host_in_house;
extern const char*   g_pc_house_test_host_edit;
/* TEST-ONLY --house-test-fidelity: at every player-house room teardown log whether the live export (aMR_pc_export_home) matched what the teardown wrote. Default off. */
extern int           g_pc_house_test_fidelity;
/* Villager population/is_home milestone, TEST-ONLY: --force-villager-grow / --force-villager-remove.
 * Fire mNpc_DebugForceGrow()/mNpc_DebugForceRemove() (m_npc.c) once, as soon as the host world is
 * ready -- see pc_net_game.c's pcnetgame_run_villager_test_triggers(). Off by default; never active
 * in normal single-player or hosted play. Bypasses only mNpc_CheckGrow()'s/mNpc_ForceRemove()'s real
 * (multi-day) trigger CONDITIONS, never the RNG selection logic itself, and never the host-authority
 * gate -- exercises the exact same gate/notify/wire path a real (rare) trigger would. */
extern int           g_pc_force_villager_grow;
extern int           g_pc_force_villager_remove;
/* Friendship/mail sync milestone, TEST-ONLY: --force-friendship-delta N / --force-mail-send. Fire
 * mNpc_DebugForceFriendshipDelta()/mNpc_DebugForceMailSend() (m_npc.c) once, as soon as this
 * process's own local world is ready (and, on a CLIENT, its link is READY) -- see pc_net_game.c's
 * pcnetgame_run_friendship_mail_test_triggers(). Unlike the villager-population hooks above, these
 * are NOT host-only: they exercise mNpc_AddFriendship()/mNpc_SendMailtoNpc()'s real
 * client-intercept-and-request path exactly as well as the host-apply-and-broadcast path, on
 * whichever role this process is. Off by default; never active in normal single-player or hosted
 * play. g_pc_force_friendship_delta is the signed delta to apply (0 = not requested). */
extern int           g_pc_force_friendship_delta;
extern int           g_pc_force_mail_send;
/* World Ecology Stage 1 money-rock review, TEST-ONLY: --force-money-rock-hit / --force-money-bag-pickup.
 * Give REAL C-executable coverage to the two money-rock paths no other test hook can reach: the HOST's
 * own local hit (pc_net_game_host_local_money_rock_hit()) and the real CLIENT-side wallet-credit/
 * pocket-grant handler (pcnetgame_handle_client_pickup_result()'s money-bag branch). Mirror
 * g_pc_force_villager_grow/g_pc_force_friendship_delta's own exact pattern: off by default, never
 * active in normal single-player or hosted play, fire exactly once as soon as their own precondition
 * is met -- see pc_net_game.c's pcnetgame_run_money_rock_test_triggers().
 *   g_pc_force_money_rock_hit    HOST-only. Once the host world is ready, calls
 *                                 pc_net_game_host_local_money_rock_hit() directly at the
 *                                 --field-action-test-seed fixture's own money-rock tile (24,104) --
 *                                 exactly the real decomp hit seam a local player's tool swing would
 *                                 call, bypassing only the swing/animation itself. Requires
 *                                 --field-action-test-seed too (no seeded rock, no tile to hit).
 *   g_pc_force_money_bag_pickup  CLIENT-only. Once this client's world is synced, scans the fixed
 *                                 9-tile neighborhood around (24,104) (pcnetgame_find_money_rock_drop_tile()'s
 *                                 own candidate set) via the client's own already-applied local field
 *                                 copy (pcfa_get_tile() -- kept current by real FIELD_UPDATE messages,
 *                                 the same data this client's real vanilla pickup targeting would read)
 *                                 for a money-bag item, and calls pc_net_game_request_pickup() on it --
 *                                 the exact real seam m_player_main_pickup.c_inc uses, bypassing only
 *                                 the player's own walk-up-and-swing input. Retries every poll until a
 *                                 bag is found (the host-side hit may not have landed yet).
 *                                 X1b: it additionally waits for the resident record to be SYNCED and, like
 *                                 --force-dig-hole, first moves the local actor onto the bag's tile centre and
 *                                 waits 1.5 s for the MOVE to reach the host (else the host's reach check
 *                                 rejects a client standing far from the fixture). */
extern int           g_pc_force_money_rock_hit;
extern int           g_pc_force_money_bag_pickup;
/* P1 (World Ecology T-dig) real-gameplay verification, TEST-ONLY: --force-dig-hole. CLIENT-only,
 * mirrors g_pc_force_money_bag_pickup's own exact pattern (off by default, fires exactly once as soon
 * as its own precondition is met -- see pc_net_game.c's pcnetgame_run_dig_hole_test_trigger()). Unlike
 * every other force-* hook above, this one does NOT call an existing pc_net_game_*() seam directly --
 * it calls PC_Test_ForceRequestDigScoop() (m_player.c, declared in m_player_lib.h), a thin exported
 * wrapper around the REAL, completely unmodified static Player_actor_request_main_dig_scoop_all()
 * (m_player_main_dig_scoop.c_inc) -- the same request-boundary entry point genuine controller input
 * reaches via Player_actor_CheckAndRequest_main_scoop_all(). Once the request is accepted, the
 * player's own ordinary per-frame main-index update takes over completely (unmodified
 * Player_actor_setup_main_Dig_scoop() / Player_actor_main_Dig_scoop() / Player_actor_Put_Hole_Dig_scoop()),
 * driving the real animation state machine to its real commit point, which (for a host-authoritative
 * client) calls the real pc_net_game_request_dig_hole() network seam exactly as it would from a real
 * shovel swing. Targets the --field-action-test-seed fixture's own DIG_HOLE tile (56,105) -- requires
 * --field-action-test-seed too. Bypasses only raw controller polling and shovel-equip state (this
 * request path does not require a shovel to be out), never any part of the dig/fill/commit logic
 * itself. */
extern int           g_pc_force_dig_hole;
/* WEEDS real-client verification, TEST-ONLY: --force-weed-pull. CLIENT-only, default off, fires exactly once (pc_net_game.c's
 * pcnetgame_run_weed_pull_test_trigger()): moves the local actor onto the --field-action-test-seed weed fixture (24,108) and forces the REAL
 * Player_actor_request_main_remove_grass() through PC_Test_ForceRequestRemoveGrass() (m_player.c); the real pull animation then reaches the
 * real WEED_PULL network seam. Requires --field-action-test-seed on the host. */
extern int           g_pc_force_weed_pull;
/* World Ecology Wildlife Sync T1 real-gameplay verification, TEST-ONLY: --force-wildlife-trigger.
 * CLIENT-only, mirrors g_pc_force_dig_hole's own exact pattern (off by default, fires exactly once
 * -- see pc_net_game.c's pcnetgame_run_wildlife_trigger_test_trigger()). Unlike force-dig-hole, no
 * teleport or reach-check wait is needed: calls pc_net_game_request_wildlife_spawn_trigger() directly,
 * once per acre across a fixed burst of 10 different addressable acres in a single frame (NOT a
 * single fixed acre -- see pcnetgame_run_wildlife_trigger_test_trigger()'s own s_acres[] table,
 * pc_net_game.c, for the exact list and why more than one acre is fired), the exact real client-side
 * network seam a genuine wade-entry sequence (aSetMgr_move_set()/mFI_CheckPlayerWade(),
 * ac_set_manager.c) already calls -- bypassing only the real wade/water-tile detection itself, never
 * any part of the host decision, broadcast, or presentation logic that follows. Requires
 * --authoritative-wildlife to have any effect. */
extern int           g_pc_force_wildlife_trigger;
/* World Ecology Wildlife Sync T-catch real-gameplay verification, TEST-ONLY: --force-fish-catch.
 * BOTH roles (unlike every other force-* hook above, which is host-only or client-only) -- mirrors
 * g_pc_force_wildlife_trigger's own "no teleport/reach-check needed" pattern, but bypasses fishing's
 * own multi-stage cast/float/bite/hook state machine entirely (judged too complex and non-deterministic
 * to drive via a simple force-flag the way a single dig-scoop request or wade-trigger send can be --
 * see pcnetgame_run_fish_catch_test_trigger_host()/_client()'s own doc, pc_net_game.c, for the full
 * rationale). HOST: once at least one live FISH entity exists in the authoritative table (typically
 * seeded via --force-wildlife-trigger first), calls pc_net_game_host_local_wildlife_catch() directly
 * with that record's own species and, on accept, grants the item exactly like the real
 * Player_actor_setup_main_Notice_rod() seam would -- bypassing only the requirement that a real UKI
 * (fishing rod/bobber) actor be cast, floated, and bitten first, never any part of the host validation,
 * removal, or despawn-broadcast logic itself. CLIENT: once this client has observed at least one
 * WILDLIFE_SPAWN broadcast for a FISH entity, calls the REAL pc_net_game_request_catch_fish() network
 * seam directly with that entity's own species -- the exact same function the real client-side seam
 * calls -- and the REAL pcnetgame_handle_client_catch_result() then grants the item on accept, exactly
 * as a genuine catch would. Fires exactly once per role. Requires --authoritative-wildlife. */
extern int           g_pc_force_fish_catch;
/* World Ecology Wildlife Sync T4 real-gameplay verification, TEST-ONLY: --force-bug-catch.
 * Faithful mirror of g_pc_force_fish_catch's own exact pattern above, adapted for ordinary BUG catching
 * (T4's extension of the same shared pcnetgame_validate_and_commit_catch()/pcnetgame_request_catch_common()
 * core to PC_WILDLIFE_KIND_BUG -- see pc_net_game_request_catch_bug()'s own doc, pc_net_game.h). BOTH
 * roles, same as --force-fish-catch. HOST: once at least one live BUG entity exists in the authoritative
 * table whose species is NOT aINS_INSECT_TYPE_ANT (ants are deliberately excluded by
 * pcnetgame_validate_and_commit_catch() itself -- see that function's own doc, pc_net_game.c -- so the
 * host trigger skips them rather than wasting its one-shot fire on a claim guaranteed to be rejected),
 * calls pc_net_game_host_local_wildlife_catch() directly with that record's own species and, on accept,
 * grants the item exactly like the real Player_actor_setup_main_Notice_net() seam would (m_player_main_
 * notice_net.c_inc) -- bypassing only the requirement that a real net be swung and connect with a live
 * insect actor first, never any part of the host validation, removal, or despawn-broadcast logic itself.
 * CLIENT: once this client has observed at least one WILDLIFE_SPAWN broadcast (or late-join snapshot
 * entry) for a non-ant BUG entity, calls the REAL pc_net_game_request_catch_bug() network seam directly
 * with that entity's own species -- the exact same function the real client-side seam calls -- and the
 * REAL pcnetgame_handle_client_catch_result() then grants the item on accept, exactly as a genuine catch
 * would. Shares the exact same teleport/move-sync-wait timing --force-fish-catch's own client trigger
 * already established (240 frames -- see pcnetgame_run_fish_catch_test_trigger_client()'s own doc for why),
 * since this is the identical MOVE-then-CATCH_REQUEST race-timing concern, not something specific to
 * fish. Fires exactly once per role. Requires --authoritative-wildlife. */
extern int           g_pc_scene_test_enter_shop; /* M9-A TEST-ONLY: --scene-test-enter-shop. Off by default. When
                                                  * set, once this process is announced as standing in the
                                                  * town field it requests a REAL goto_other_scene() into
                                                  * SCENE_SHOP0 (see pc_net_game.c's
                                                  * pcnetgame_run_scene_test_hook()). Never active otherwise. */
extern int           g_pc_scene_test_leave_after; /* M9-A TEST-ONLY: --scene-test-leave-after N: with the flag
                                                   * above, leave the shop again N polls after it was announced
                                                   * (0 = stay inside). */
extern int           g_pc_collide_test_overlap;  /* M9-B TEST-ONLY: --collide-test-overlap. Off by default. Once this
                                                   * process is READY, in the IN_TOWN field and a same-scene puppet
                                                   * exists, teleports the LOCAL player's world position to the first
                                                   * puppet's position + (10,0,0), exactly once (see pc_net_game.c's
                                                   * pcnetgame_run_collide_test_hook()). Also makes the
                                                   * [NET][COLLIDE][DIAG] line print every contact frame. */
extern int           g_pc_collide_test_approach; /* M9-B TEST-ONLY: --collide-test-approach N (N > 0): steps the LOCAL
                                                   * player 2 world units per game frame toward the first puppet for N
                                                   * frames, then stops. Off (0) by default. */
extern int           g_pc_force_bug_catch;
/* T8 audit verification, TEST-ONLY: --diag-bug-ttl-lookup <frames>. Added specifically to re-verify Bug
 * 1's fix (pcwld_presentation_check_idle()/pcwld_presentation_reconcile(), pc_wildlife_authority.c)
 * through the REAL net->side-table lookup path -- the audit explicitly flagged that --force-bug-catch
 * does NOT exercise this path (its host branch reads the AUTHORITATIVE table's own entity_id directly and
 * never calls pcwld_bug_entity_id_for_local_actor(), and its client branch never runs on the host at all).
 * When set (frames > 0), HOST role only: (1) temporarily overrides the normally-fixed, ~10-minute
 * PCWLD_RECORD_MAX_AGE_60FPS_FRAMES idle-expiry threshold (pc_wildlife_authority.c) to this many 60fps
 * frames, for BOTH pcwld_host_check_idle() and pcwld_presentation_check_idle(), so a short test run can
 * actually cross the threshold; (2) once at least one live, non-ant BUG entity exists, repeatedly calls
 * pc_net_game_bug_entity_id_for_label() -- the EXACT SAME public wrapper around pcwld_bug_entity_id_
 * for_local_actor() that Player_actor_setup_main_Notice_net() (m_player_main_notice_net.c_inc) calls for
 * a real net-swing catch -- with that entity's own recorded local_actor pointer and species, and logs the
 * result every ~0.5s until well past the (overridden) TTL. This exercises the REAL production lookup
 * function end-to-end; it does NOT simulate an actual net-swing collision (judged impractical to automate
 * throughout this codebase, see test_wildlife_bug_catch.py's own doc) -- reported as PROTOCOL+HOST-LOGIC
 * TESTED, not REAL GAMEPLAY TESTED. A complete no-op when 0 (the default) or when authoritative wildlife
 * is not enabled. */
extern int           g_pc_diag_bug_ttl_lookup_frames;
/* T8 review fix verification, TEST-ONLY: --diag-bug-despawn-label-race. Added specifically to reproduce
 * and verify the fix for the regression an independent review found in Bug 2 part (b)'s original
 * "clear local_actor immediately on despawn" change (pcwld_bug_handle_wildlife_despawn(),
 * pc_wildlife_authority.c): vanilla deliberately keeps a netted bug's actor ALIVE while it is the local
 * player's own current item_net_catch_label (mPlib_Get_item_net_catch_label(), ac_insect_move.c_inc's own
 * aINS_cull_check() refuses to run aINS_destruct() on it) -- so when a DIFFERENT peer's catch on the SAME
 * entity_id is accepted FIRST, the resulting WILDLIFE_DESPAWN can be reconciled on THIS process while its
 * own local actor is still genuinely alive and still the active catch label. The removed part (b) would
 * have wiped the entity_id<->local_actor mapping right then anyway, so the label-holder's own later
 * catch-completion lookup (pc_net_game_bug_entity_id_for_label(), which Player_actor_setup_main_
 * Notice_net()/the putaway-net exchange gate both call) would incorrectly see "untracked" (0) instead of
 * the real, losing entity_id.
 * HOST role only (host is the easier role to force this scenario onto a live bug actor without a second
 * process -- the property under test, "does the despawn-time reconciliation respect a still-alive local
 * actor", is purely local per-process bookkeeping identical on host and client, exactly like --diag-bug-
 * ttl-lookup's own precedent): (1) once at least one live, non-ant BUG entity exists, latches its
 * entity_id/species/local_actor, teleports THIS process's own local player directly on top of it (the same
 * safe teleport pattern pcnetgame_run_bug_catch_test_trigger_client() already uses for --force-bug-catch)
 * so aINS_cull_check()'s own distance/visibility cull rules never call aINS_destruct() on it during this
 * diagnostic's race window -- keeping exist_flag genuinely TRUE regardless of the label -- and, best-
 * effort, also calls mPlib_Change_item_net_catch_label() to point this process's own catch label at the
 * exact actor (confirmed BY TESTING to be a harmless no-op here, since Player_actor_Get_item_net_catch_
 * label() only ever honors that field while player->now_main_index is one of the four real net states,
 * which this diagnostic deliberately does not force this process's own player into -- driving that FSM
 * state directly was judged unsafe to fake outside real net-swing input); (2) waits for pcwld_find_by_id()
 * to report the entity removed from the authoritative table (driven externally -- see test_wildlife_bug_
 * catch.py's own TEST for this flag -- by a raced CATCH_REQUEST for the SAME entity_id from a second peer,
 * which the host accepts since it never issued its own catch attempt); (3) the instant the removal is
 * observed, queries pc_net_game_bug_entity_id_for_label() for the SAME (local_actor, species) pair and logs
 * PASS (still resolves to the original entity_id -- the fix holds) or FAIL (0 -- the regression), together
 * with the actor's own exist_flag/insect_flags.destruct at that instant (the test script cross-checks
 * exist_flag == TRUE to confirm this run genuinely reproduced the "still alive at despawn time"
 * precondition, not a vacuous pass against an already-dead actor). A complete no-op when 0 (the default) or
 * when authoritative wildlife is not enabled. */
extern int           g_pc_diag_bug_despawn_label_race;
/* T8 audit verification, TEST-ONLY: --diag-role-link-state. Added specifically to re-verify Bug 3's fix
 * (pcwld_should_suppress_local_wildlife(), pc_wildlife_authority.c) across a REAL host-connection-loss
 * event. Any role: once per ~1s, prints pc_net_game_role(), pc_net_game_client_link_state(),
 * pc_net_game_world_is_host_authoritative(), and pcwld_should_suppress_local_wildlife() together on one
 * line. The property under test: while a real CLIENT's transport connection is lost (a real
 * PC_NET_EVENT_PEER_DISCONNECTED, e.g. the host process dying), s_role stays PC_NETGAME_ROLE_CLIENT (only
 * an explicit return-to-menu resets it to NONE -- confirmed by reading pc_net_game.c's own event handler)
 * while s_client_link leaves PC_NETGAME_LINK_READY -- so pc_net_game_world_is_host_authoritative() drops
 * to 0 (identical to single-player) but pcwld_should_suppress_local_wildlife() correctly STAYS 1 (unlike
 * single-player, where it is always 0). A complete no-op when 0 (the default). */
extern int           g_pc_diag_role_link_state;
/* D3 (host-mirrored resident record) real-client verification, TEST-ONLY: --d3-test-wallet-add <N> (N may be negative).
 * CLIENT only: once, 3 s after the client adopted the host's resident record (state SYNCED), adds N bells to the local
 * Now_Private wallet (clamped to 0..99999) and logs a loud "[NET][REC][TEST-ONLY]" line, so a real client exercises the
 * record UPLOAD path (and the quit flush) without any gameplay input. A complete no-op when 0 (the default); NOT active in
 * normal play, a host or single-player. */
extern int           g_pc_d3_test_wallet_add;
/* D3 TEST-ONLY: --d3-test-wallet-add-late <N>. Like the flag above but a SECOND change, applied 900 ms after the previous
 * upload started (and was acknowledged), i.e. inside the client's 2 s upload gap, so that only the graceful-quit flush can
 * upload it. Default 0 = off. NOT active in normal play. */
extern int           g_pc_d3_test_wallet_add_late;
/* X1b (host-transactional pickup/drop/bury commit) REAL-CLIENT verification, TEST-ONLY: --txn-test-pickup-drop. CLIENT only (a
 * complete no-op for any other role), default OFF, never active in normal play; armed at start with a loud "[NET][TXN][TEST-ONLY]" log
 * and every step logs "[NET][TXN][TEST-ONLY]". Once the client's resident record is SYNCED (+4 s, so its MOVE has reached the host) it
 * performs ONE drop of its first droppable pocket item at the tile vanilla's drop search picks for the player's current position
 * (through the real pc_net_game_request_drop()), and 1.5 s after that transaction resolved a pickup of the same item from the same tile
 * (through the real pc_net_game_request_pickup(); like --force-dig-hole the local actor is first moved onto that tile's centre and
 * 1.5 s later the request is sent, so the host's pickup reach check accepts it). It bypasses only the inventory UI / pickup animation
 * input and the walk to the tile, never any part of the
 * request / provisional RESULT / TXN_COMMIT / TXN_RESULT chain. See pc_net_game.c pcnetgame_run_txn_test_hook(). */
extern int           g_pc_txn_test_pickup_drop;
/* X3 (host-transactional dig grants) REAL-CLIENT verification, TEST-ONLY: --txn-test-dig-grant. CLIENT only (a complete no-op for any other
 * role), default OFF, never active in normal play; armed with a loud "[NET][TXN][TEST-ONLY]" log, every step logs the same prefix. Needs the
 * host to run --field-action-test-seed. Once the client's resident record is SYNCED (+4 s) it moves the local actor onto each fixture tile
 * (so the host's reach check accepts), waits 1.5 s for the MOVE to reach the host and calls the REAL request functions in turn:
 * pc_net_game_request_dig_buried(40,104), pc_net_game_request_dig_hole_with_grant(56,105,0,ITM_MONEY_100) and
 * pc_net_game_request_dig_shine_with_grant(88,105,ITM_MONEY_1000) (the golden-shovel / shine rolls are fixed instead of random). It logs the
 * pockets before the request and after the transaction resolved. Bypasses only the shovel animation / input and the roll, never the
 * request / TXN_RESULT chain. See pc_net_game.c pcnetgame_run_txn_dig_test_hook(). */
extern int           g_pc_txn_test_dig_grant;
/* Town services milestone 1 TEST-ONLY hooks (all default OFF, never active in normal play, every step logs "[NET][TS][TEST-ONLY]"):
 *   --ts-test-seed-police=<hex>[,<hex>...]  HOST only: once the host world is ready, keep each listed item in the host's lost and found through the
 *                                           vanilla mPB_keep_item() (so the real FIFO / compaction rules apply). Refused for any other role.
 *   --ts-test-donate                        CLIENT only: once the record is SYNCED and the museum mirror arrived, place a fish the LOCAL mirror says the
 *                                           museum lacks into a free pocket slot (the hook's one local write), wait 5 s for the D3 upload and run ONE
 *                                           real MUSEUM_DONATE through pc_net_game_ts_begin_museum_donate() / _poll() (the dialogue seam's entry points).
 *   --ts-test-claim                         CLIENT only: claim the first item of the LOCAL lost-and-found mirror through pc_net_game_ts_begin_police_claim().
 * They bypass only the Blathers / Booker dialogue and the menu, never the TXN_COMMIT / TXN_RESULT chain. See pc_net_game.c pcnetgame_run_ts_test_hook(). */
extern const char*   g_pc_ts_test_seed_police;
extern int           g_pc_ts_test_donate;
extern int           g_pc_ts_test_claim;
/* World-state real-client TEST-ONLY hook (default OFF, never active in normal play, loud "[NET][WORLD][TEST-ONLY]" log):
 *   --world-test-force=W,I,T,P[,M[,E]]  HOST only: once the host world is ready, force known values into the host's own world state: weather type W
 *                                   (mEnv_WEATHER_*, 0..4), weather intensity I (0..3), Stalk Market trend T (0..4), daily turnip prices P+weekday
 *                                   (P 1..600), and optionally shift the host's game clock by M minutes (-180..180, via Save time_delta); E (month*100+day) pokes the host's Wisp date (event_save_common.ghost_day) 4 s after the first READY peer. The
 *                                   normal WORLD_META / CLOCK_SYNC machinery then carries them; the client's own log lines are compared by
 *                                   test_world_state_real_client.py. Refused for any other role. See pc_net_game.c pcnetgame_world_test_force(). */
extern const char*   g_pc_world_test_force;
/* Town services milestone 2 (shop) TEST-ONLY hooks (default OFF, never active in normal play, every step logs "[NET][SHOP][TEST-ONLY]"):
 *   --shop-test-buy   CLIENT only: once the record is SYNCED and the shop mirror arrived, raise the local wallet to 90000 (the hook's one local write), wait 5 s
 *                     for the D3 upload, then buy the cheapest buyable item of the LOCAL mirror through pc_net_game_ts_begin_shop_buy() / _poll().
 *   --shop-test-sell  CLIENT only: place a sellable fish into a free pocket slot (the hook's one local write), wait 5 s, then sell it through
 *                     pc_net_game_ts_begin_shop_sell() / _poll().
 * They bypass only the Nook dialogue and the menu, never the TXN_COMMIT / TXN_RESULT chain. See pc_net_game.c pcnetgame_run_shop_test_hook(). */
extern int           g_pc_shop_test_buy;
extern int           g_pc_shop_test_sell;
/* Guest-first town TEST-ONLY hook (default OFF, never active in normal play, every step logs "[NET][HOUSE][TEST-ONLY]"):
 *   --house-buy-test H|auto   CLIENT only (a GUEST client): once the record is SYNCED, raise the local wallet to 20000 when it is below the house price (the hook's one
 *                             local write), wait for the D3 upload, then buy house H (0..3) or `auto` through pc_net_game_ts_begin_house_purchase() / _poll(). On APPLIED the
 *                             host makes the guest a resident and the process re-joins as the resident in-process. See pc_net_game.c pcnetgame_run_house_buy_test_hook(). */
extern const char*   g_pc_house_buy_test;
/* Mail milestone 1 TEST-ONLY hooks (default OFF, never active in normal play, every step logs "[NET][MAIL][TEST-ONLY]"):
 *   --mail-test-send=<house>[,gift]   CLIENT only: once the record is SYNCED, write ONE send-font letter (sender = the local player, recipient = the
 *                                     owner of local house <house>) into the first free mail slot (the hook's one local write; with ",gift" the first
 *                                     pocket item is moved into the letter like the letter board's hand overlay does), then send it through
 *                                     pc_net_game_mail_begin_send() / _poll() (the post girl seam's entry points: AWAIT_CLEAN -> TXN_COMMIT kind 12).
 *   --mail-test-force-delivery        HOST only: every 2 s, while the post office holds player letters, run the vanilla mPO_delivery_one_address() for
 *                                     every house and log what is in every mailbox (hash, font, gift), so a test can see what landed where.
 * They bypass only the letter board / the postman's clock, never the TXN_COMMIT / TXN_RESULT chain. See pc_net_game.c pcnetgame_run_mail_test_hook(). */
extern const char*   g_pc_mail_test_send;
extern int           g_pc_mail_test_force_delivery;
/*   --mail-test-poke-museum=<resident idx>  HOST only (mail milestone R): once that resident's mirror is SYNCED, change its museum_record (stored_fossil_num + 1) the way the
 *                                     host's day-change fossil / result mail does, so the host-field watcher -> rev + 1 -> PUSH_HOSTFIELDS path is exercised.
 *                                     -1 = off (default). */
extern int           g_pc_mail_test_poke_museum;
/* Mail milestone 2 TEST-ONLY hooks (default OFF, never active in normal play, every step logs "[NET][MAIL][TEST-ONLY]"):
 *   --mail-test-take[=N]   CLIENT only: once the mailbox is host-fed and the record SYNCED, take up to N (default 10) letters out of the mailbox shadow, one
 *                          after the other, through pc_net_game_mail_take_step() (the mailbox overlay's entry point: TXN_COMMIT kind 13). Writes nothing itself.
 *   --mail-test-seed-mailbox=<resident>,<count>,<delay_ms>[,<gift hex>]   HOST only: <delay_ms> after the host world is ready, write <count> NPC-style letters
 *                          into the free slots of that resident's house mailbox (a simulated postman delivery); the first carries the gift when given.
 *   --mail-test-seed-reply=<resident>[,<resident>...]   HOST only: once the host world is ready, make the first villager owe each listed resident a reply
 *                          (letter dated a year ago), so the M2r pass generates it when that resident's peer is synced. */
extern int           g_pc_mail_test_take;
extern const char*   g_pc_mail_test_seed_mailbox;
extern const char*   g_pc_mail_test_seed_reply;
/* X1 (host-transactional PICKUP/DROP/BURY commit) host-side FAULT INJECTION, TEST-ONLY: --txn-fault=<mode>[:N[:K]]. HOST role ONLY:
 * pc_main.c refuses (exit code 2, message on stderr) any other role, and the C hook re-checks the role. OFF by default (mode 0),
 * never active in normal play; when armed the process logs "[NET][TXN][TEST-ONLY] FAULT INJECTION ENABLED mode=..." at start and
 * "[NET][TXN][TEST-ONLY] FAULT INJECTION FIRED ..." at every firing. The N-th visit of the mode's single site (default 1) and the
 * K-1 visits after it fire (K default 1):
 *   ignore_commit            discard the COMMIT right after the READY gate (nothing validated or journaled): a lost COMMIT
 *   fail_world               force the world re-validation to fail after the other validation passed (REJECTED WORLD_CHANGED)
 *   expire                   treat the COMMIT's PENDING reservation as aged >= 20 s (REJECTED EXPIRED; closes the L-2 gap)
 *   drop_result              do not send an APPLIED TXN_RESULT (first execution AND replays): the ack is lost on a live link
 *   kill_peer_after_commit   after the commit (world + mirror + journal) drop the peer WITHOUT a RESULT
 * (the crash modes are X4). g_pc_txn_fault_nth / _arg hold N and K. */
enum {
    PC_TXN_FAULT_NONE = 0,
    PC_TXN_FAULT_IGNORE_COMMIT = 1,
    PC_TXN_FAULT_FAIL_WORLD = 2,
    PC_TXN_FAULT_EXPIRE = 3,
    PC_TXN_FAULT_DROP_RESULT = 4,
    PC_TXN_FAULT_KILL_PEER_AFTER_COMMIT = 5
};
extern int           g_pc_txn_fault_mode;
extern int           g_pc_txn_fault_nth;
extern int           g_pc_txn_fault_arg;
#ifdef PC_NET_TEST_HOOKS
extern int           g_pc_promote_fault; /* --promote-fault (HOST, test builds only): 1 fail_save, 2 crash_after_members, 3 crash_after_gci */
#endif
extern int           g_pc_frame_limit_override;
extern int           g_pc_speedhack_enabled;
extern int           g_pc_time_override;
extern int           g_pc_min_override;
extern int           g_pc_sec_override;
extern int           g_pc_date_month;
extern int           g_pc_date_day;
extern int           g_pc_date_year;
extern int           g_pc_weather_override;
extern int           g_pc_weather_intensity_override;
extern u32           g_frame_limiter;

extern int g_pc_window_w;
extern int g_pc_window_h;
void pc_platform_update_window_size(void);

/* --- Widescreen mode (3-state) ---
 * 0 = hor+ (default): full-window viewport, FOV-corrected projection. Resets each frame.
 * 1 = stretch: full-window, no correction. For transitions/inventory BG.
 * 2 = pillarbox: centered 4:3 with black bars. For inventory UI alignment.
 *
 * m_play.c inserts NOOPTag markers in POLY_OPA to switch between states:
 *   ON (0xAC5701) -> 1, OFF (0xAC5700) -> 2. emu64 reads these during DL processing. */
#define PC_NOOP_WIDESCREEN_STRETCH     0xAC5701u
#define PC_NOOP_WIDESCREEN_STRETCH_OFF 0xAC5700u
extern int g_pc_widescreen_stretch;

/* --- Functions --- */
void pc_platform_init(void);
void pc_platform_shutdown(void);
void pc_platform_swap_buffers(void);
int  pc_platform_poll_events(void);

/* EXE image range for seg2k0 pointer disambiguation (vs N64 segment addresses) */
extern unsigned int pc_image_base;
extern unsigned int pc_image_end;

/* --- Model viewer --- */
extern int g_pc_model_viewer;
extern int g_pc_model_viewer_start;
extern int g_pc_model_viewer_no_cull;

/* --- Per-frame diagnostics --- */
extern int pc_emu64_frame_cmds;
extern int pc_emu64_frame_noop_cmds;
extern int pc_emu64_frame_tri_cmds;
extern int pc_emu64_frame_vtx_cmds;
extern int pc_emu64_frame_dl_cmds;
extern int pc_emu64_frame_cull_visible;
extern int pc_emu64_frame_cull_rejected;
extern int pc_gx_draw_call_count;

/* --- Audio --- */
extern int pc_save_loaded;
int  pc_audio_get_buffer_fill(void);
int  pc_audio_is_active(void);
void pc_audio_set_paused(int paused);
void pc_audio_shutdown(void);
void pc_audio_start_producer_thread(void);
void pc_audio_mq_init(void);
void pc_audio_mq_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* PC_PLATFORM_H */
