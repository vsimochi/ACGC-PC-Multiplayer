#ifndef PC_REMOTE_ARRIVAL_LOGIC_H
#define PC_REMOTE_ARRIVAL_LOGIC_H

/* Guest-arrival presentation T3 / T4: the PURE decision logic (header-only, libc-free, natively unit-testable:
 * pc/tools/net_spike/test_guest_arrival.py + guest_arrival_selftest.c). The game code (pc/src/pc_remote_player.c, src/game/m_train_control.c)
 * only feeds it values read from the game and acts on the answer.
 *
 * T4: when THIS process sees a remote puppet ENTER the 'standing in the train' state (mPlayer_INDEX_DEMO_STANDING_TRAIN, the vanilla ride-off
 * demo state of an arriving player) in the town, it starts the vanilla arrival train locally exactly like the arriving client does
 * (train_coming_flag = 3, consumed by mTRC_schedule -> mTRC_demo_init). The train is purely local state (every process runs its own): it
 * is called only when this process has no train activity of its own, is not itself playing an arrival / title demo, and at most once per
 * contiguous 'standing in the train' period of one puppet. */

enum {
    PCARR_REMOTE_TRAIN_OK = 0,        /* call the arrival train now */
    PCARR_REMOTE_TRAIN_LATCHED,       /* this puppet already triggered (or was already evaluated for) this arrival */
    PCARR_REMOTE_TRAIN_PUPPET_SCENE,  /* the puppet is not in the town scene this process is showing */
    PCARR_REMOTE_TRAIN_LOCAL_SCENE,   /* this process is not in the town (FG field) scene */
    PCARR_REMOTE_TRAIN_NO_PLAYER,     /* no local player actor yet: mTRC_move would not run the state machine either */
    PCARR_REMOTE_TRAIN_TITLE,         /* a title demo is running (mTRC_move would treat the train as the parked title train) */
    PCARR_REMOTE_TRAIN_PRE_GAME,      /* player-select / train interior draw type: the vanilla Start flow owns the train */
    PCARR_REMOTE_TRAIN_LOCAL_DEMO,    /* the LOCAL player is arriving / boarding (train main index or ride-off / intro demo actor) */
    PCARR_REMOTE_TRAIN_COMING_FLAG,   /* a train request (coming_flag != 0) is pending: never overwrite it */
    PCARR_REMOTE_TRAIN_BUSY           /* a train is already arriving / stopped / departing (train_action != NONE) */
};

/* Returns PCARR_REMOTE_TRAIN_OK (0) only when ALL guards hold; otherwise the FIRST failing reason (in the order of the enum, which is the
 * order of evaluation). The caller latches the puppet's standing period on any answer other than PUPPET_SCENE (a not yet known scene may
 * still resolve), so a refusal is never retried every frame.
 *   already_latched      the puppet's contiguous standing period was already evaluated
 *   puppet_in_local_town the puppet's announced scene is the town field this process is showing
 *   local_is_town        this process is in the town (FG field) scene
 *   have_player          a local player actor exists
 *   title_demo_active    mEv_CheckTitleDemo() != NONE
 *   pre_game_draw_type   field_draw_type is the player-select / train interior
 *   local_train_demo     local player's main index is a train demo, or a ride-off / intro demo actor exists
 *   coming_flag          Common train_coming_flag
 *   train_action         Common train_action (0 = none) */
static inline int pcarr_remote_arrival_train_decide(int already_latched, int puppet_in_local_town, int local_is_town, int have_player,
                                                    int title_demo_active, int pre_game_draw_type, int local_train_demo,
                                                    unsigned coming_flag, unsigned train_action) {
    if (already_latched) {
        return PCARR_REMOTE_TRAIN_LATCHED;
    }
    if (!puppet_in_local_town) {
        return PCARR_REMOTE_TRAIN_PUPPET_SCENE;
    }
    if (!local_is_town) {
        return PCARR_REMOTE_TRAIN_LOCAL_SCENE;
    }
    if (!have_player) {
        return PCARR_REMOTE_TRAIN_NO_PLAYER;
    }
    if (title_demo_active) {
        return PCARR_REMOTE_TRAIN_TITLE;
    }
    if (pre_game_draw_type) {
        return PCARR_REMOTE_TRAIN_PRE_GAME;
    }
    if (local_train_demo) {
        return PCARR_REMOTE_TRAIN_LOCAL_DEMO;
    }
    if (coming_flag != 0u) {
        return PCARR_REMOTE_TRAIN_COMING_FLAG;
    }
    if (train_action != 0u) {
        return PCARR_REMOTE_TRAIN_BUSY;
    }
    return PCARR_REMOTE_TRAIN_OK;
}

/* Per-puppet standing-period latch (T4): `standing` = the puppet's current main index is DEMO_STANDING_TRAIN. *latched is the puppet's
 * state. Returns 1 when the puppet is in a standing period that has NOT been evaluated yet (the caller evaluates the guard now), else 0.
 * The latch is cleared when the puppet leaves the state, so a later arrival of the same puppet is a new period. `scene_known`:
 * the puppet's scene is the local town; the period is only consumed once that holds (a late SCENE packet must not lose the arrival). */
static inline int pcarr_remote_standing_should_evaluate(int* latched, int standing, int scene_known) {
    if (!standing) {
        *latched = 0;
        return 0;
    }
    if (*latched || !scene_known) {
        return 0;
    }
    *latched = 1;
    return 1;
}

/* Train-on-residents fix: reasons 7 (LOCAL_DEMO), 8 (COMING_FLAG), 9 (BUSY) are TRANSIENT local conditions (a resident in the first-job intro,
 * a pending train request, an own train in progress) that can clear while the guest is still standing in the train, so they must not consume
 * the per-period latch: the caller re-polls every frame. Only OK (the train was called) and the structural reasons 1-6 consume it. */
static inline int pcarr_remote_reason_is_transient(int reason) {
    return reason == PCARR_REMOTE_TRAIN_LOCAL_DEMO || reason == PCARR_REMOTE_TRAIN_COMING_FLAG || reason == PCARR_REMOTE_TRAIN_BUSY;
}

/* Call after pcarr_remote_standing_should_evaluate() returned 1 and the guard answered `reason`: a transient refusal releases the latch so the
 * next frame evaluates again (while the puppet still stands); OK / structural reasons keep it (once per standing period). */
static inline void pcarr_remote_standing_settle(int* latched, int reason) {
    if (pcarr_remote_reason_is_transient(reason)) {
        *latched = 0;
    }
}

/* Intro demo actor counts as 'the LOCAL player is arriving' only while the first-job intro runs (the actor lives the whole first job). */
static inline int pcarr_local_intro_arriving(int intro_actor_exists, int first_intro_active) {
    return intro_actor_exists && first_intro_active;
}

/* T3 (puppet rows): nonzero when the wire main index is the DEMO_WALK state, which the sender classifies as OTHER (WAIT1 on the fallback
 * path): the puppet then plays the walk clip instead of sliding in idle. `move_state_is_other`: the sender's coarse class is OTHER. */
static inline int pcarr_demo_walk_plays_walk(int action_valid, int action_index, int demo_walk_index, int move_state_is_other) {
    return action_valid && action_index == demo_walk_index && move_state_is_other;
}

/* ---- SHARED train arrival (a guest that connects while another player is still arriving joins THAT arrival instead of starting a second, independent one) ----
 * Every process runs its own local train (nothing about it is synced), so "the same train" means: the joining guest's local train is started at the phase the other passenger is in,
 * read from the other player's streamed state, and the guest takes a different passenger spot. The streamed MOVE state + position is all the information used:
 *   STANDING_TRAIN (riding)                          the other player's caboose is at its x: PCARR_JOIN_RIDING, train x = passenger x + 190 (caboose = train - 250, passenger = caboose + 60)
 *   GETOFF_TRAIN / DEMO_WALK, or TALK / DEMO_WAIT on the station platform (Porter's welcome, joined mid-welcome)    the train stands at the station: PCARR_JOIN_STOPPED
 * Anything else (ordinary field movement, boarding, a DEMO_WAIT elsewhere) is not an arrival. */
enum { PCARR_JOIN_NONE = 0, PCARR_JOIN_RIDING = 1, PCARR_JOIN_STOPPED = 2 };

#define PCARR_STATION_X 2200.0f /* the station platform around Porter, where an arriving guest gets off and is welcomed */
#define PCARR_STATION_Z 820.0f
#define PCARR_STATION_RADIUS 120.0f
#define PCARR_TRAIN_START_X 2037.0f /* mTRC_demo_init */
#define PCARR_TRAIN_STOP_X 2365.0f  /* where the arrival train stops (logged train x at action 4 / 5) */
#define PCARR_TRAIN_RIDE_MAX_X 2160.0f /* a riding join keeps BEGIN_SLOWDOWN (x <= 2165), so the vanilla stop distance still ends at the station */
#define PCARR_JOIN_PASSENGER_DX (-40.0f) /* the joiner stands this far BEHIND the first passenger (+60 from the caboose) */

static inline int pcarr_in_station_zone(float x, float z) {
    const float dx = x - PCARR_STATION_X;
    const float dz = z - PCARR_STATION_Z;
    return dx > -PCARR_STATION_RADIUS && dx < PCARR_STATION_RADIUS && dz > -PCARR_STATION_RADIUS && dz < PCARR_STATION_RADIUS;
}

static inline int pcarr_join_class(int action_valid, int action_index, int standing_index, int getoff_index, int walk_index, int talk_index, int demo_wait_index, float x, float z) {
    if (!action_valid) {
        return PCARR_JOIN_NONE;
    }
    if (action_index == standing_index) {
        return PCARR_JOIN_RIDING;
    }
    if (action_index == getoff_index || action_index == walk_index) {
        return PCARR_JOIN_STOPPED;
    }
    if ((action_index == talk_index || action_index == demo_wait_index) && pcarr_in_station_zone(x, z)) {
        return PCARR_JOIN_STOPPED;
    }
    return PCARR_JOIN_NONE;
}

/* the x of the train engine a passenger at world x `passenger_x` rides, kept in the part of the approach where the vanilla slowdown / stop still ends at the station */
static inline float pcarr_join_train_x(float passenger_x) {
    float x = passenger_x + 190.0f;
    if (x < PCARR_TRAIN_START_X) {
        x = PCARR_TRAIN_START_X;
    }
    if (x > PCARR_TRAIN_RIDE_MAX_X) {
        x = PCARR_TRAIN_RIDE_MAX_X;
    }
    return x;
}

/* The decision of a guest that is about to start its arrival: -1 = not decidable yet (keep the demo parked this frame), else the PCARR_JOIN_* class to start with.
 *   link_handshake   transport-connected, READY not reached yet (READY needs the town entered, i.e. it completes in the first frames of the arrival scene)
 *   link_ready       READY
 *   roster_pending   READY but the first world snapshot is not applied yet (the host's join-time scene replay precedes it on the same ordered channel)
 *   unknown_slots    town players whose first MOVE has not arrived yet (bounded by the puppet code's existing 'no MOVE for 90 frames' silence convention)
 *   n_stopped / n_riding   other players in each arrival class
 * Not a connected client: decided at once, NONE (the vanilla arrival, untouched). The joiner never waits for the other arrival to END: only until it knows what the others are doing. */
static inline int pcarr_join_decide(int link_handshake, int link_ready, int roster_pending, int unknown_slots, int n_stopped, int n_riding) {
    if (link_handshake) {
        return -1;
    }
    if (!link_ready) {
        return PCARR_JOIN_NONE;
    }
    if (roster_pending || unknown_slots > 0) {
        return -1;
    }
    return n_stopped > 0 ? PCARR_JOIN_STOPPED : (n_riding > 0 ? PCARR_JOIN_RIDING : PCARR_JOIN_NONE);
}

#endif /* PC_REMOTE_ARRIVAL_LOGIC_H */
