/* pc_host_observer.h - the opt-in HIDDEN SERVER OBSERVER (--host-observer, HOST role only).
 *
 * A hosted process normally plays one of the four residents of the town it serves, which makes that resident unclaimable for clients. With
 * --host-observer the host instead binds a PC-owned, static, reserved identity (pc_m_card.c s_pc_observer_private, a Private_c that lives
 * OUTSIDE Save_t.private_data[] and is therefore never serialised) with Common.player_no == mPr_FOREIGNER (4, exactly: every larger value
 * would index into homes[] / past Save_t), keeps a hidden, inert, uncontrollable avatar parked in the town field (SCENE_FG) so the vanilla
 * play loop keeps running, and is excluded from every network presence message (no roster entry, MOVE, scene, appearance, identity).
 * All four residents are then claimable by clients. Plain --host, --bootstrap-resident and single-player are unchanged.
 *
 * Everything here is a plain C predicate usable from the decomp sources (src/game/*, guarded by TARGET_PC) and from the PC layer. */
#ifndef PC_HOST_OBSERVER_H
#define PC_HOST_OBSERVER_H

#ifdef __cplusplus
extern "C" {
#endif

/* pc_main.c: 1 iff --host-observer was passed (validated at startup: only with --host, never with --bootstrap-resident / --bootstrap-guest). */
extern int g_pc_host_observer;

/* 1 iff the HOST role AND the flag AND Now_Private is the observer's static record AND player_no == mPr_FOREIGNER. False in every other
 * process (single-player, plain --host, --bootstrap-resident, clients) and before the observer was bound. Cheap; callable every frame. */
int pc_host_observer_active(void);

/* pc_host_observer_active() AND the one-way latch "the town field was loaded once with the observer in it" (first settled frame in SCENE_FG,
 * no wipe, pcfa_scene_is_town()). Used ONLY by pcfa_save_ready()'s foreigner branch; never flickers off during later scene reloads. */
int pc_host_observer_ready(void);

/* 1 iff `personal_id` (a PersonalID_c*) byte-equals the observer's reserved PersonalID (and the observer is active). The guest-key conflict
 * check uses it so that no guest can ever claim the observer identity. */
int pc_host_observer_id_matches(const void* personal_id);

#ifdef __cplusplus
}
#endif

#endif /* PC_HOST_OBSERVER_H */
