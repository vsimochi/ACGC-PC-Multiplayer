/* pc_shop_keeper_target.h - SHOP KEEPER TARGET SYNC (Tom Nook / Timmy & Tommy follow the NEAREST player of the shop): the pure target picker shared by
 * the game (pc_net_game.c pc_net_game_shop_keeper_target) and pc/tools/net_spike/shop_keeper_target_selftest.c (no engine dependencies).
 *
 * Every process has its own keeper actor in its own shop interior (the keepers are not in Save animals[], nothing networks them). Vanilla makes the
 * keeper turn to / walk after GET_PLAYER_ACTOR only, so each process' keeper looked at ITS OWN player. Now every process feeds the same candidate set
 * to this picker: every player standing in that shop scene (its own player + the puppets announced in the same scene, residents and guests; never the
 * hidden --host-observer, which has no puppet and is skipped on the host) with the global wire id, the position and whether the player is in a
 * conversation state (mPlayer_INDEX_TALK .. GIVE_WAIT, carried by every MOVE sample). The picker is deterministic (ties: the lower wire id), so all
 * processes agree up to the network delay of the puppet positions; the hysteresis below absorbs that delay. NO wire change.
 *
 * Rules (pc_skt_pick):
 *   1. a TALKING player within PC_SKT_TALK_RANGE of the keeper wins at once (the nearest such; the keeper's vanilla talk partner on the talker's own
 *      process, mirrored on every other process);
 *   2. otherwise the nearest player; the current target is kept until another player is at least PC_SKT_MARGIN (one tile) nearer for PC_SKT_DWELL
 *      consecutive frames (no flicker when two players stand at a similar distance);
 *   3. a current target that left the candidate set (left the shop, disconnected) is replaced at once.
 * Kill switch: AC_MP_SHOP_NOOK_SYNC=0 or settings.ini [Gameplay] shop_nook_sync = 0 (vanilla: every keeper follows the local player). */
#ifndef PC_SHOP_KEEPER_TARGET_H
#define PC_SHOP_KEEPER_TARGET_H

#ifdef __cplusplus
extern "C" {
#endif

#define PC_SKT_MAX_CAND 32
#define PC_SKT_MARGIN 40.0f      /* world units (one tile): how much nearer a challenger must be */
#define PC_SKT_DWELL 20.0f       /* 60 fps frames the challenger must keep that margin (~1/3 s) */
#define PC_SKT_TALK_RANGE 110.0f /* a conversation state counts only this close to the keeper (vanilla talks at < 85 / 110 units) */
#define PC_SKT_NONE (-1)

typedef struct PCSktCand {
    int   id;      /* global wire id (host player 0, clients their assigned id) */
    float x, z;    /* world position */
    int   talking; /* the player is in a conversation state */
} PCSktCand;

typedef struct PCSktState {
    int   cur;         /* the current target's wire id, PC_SKT_NONE = none */
    int   chal;        /* the challenger being timed, PC_SKT_NONE = none */
    float chal_frames; /* how long it has kept the margin */
    int   cur_talk;    /* the current target was picked by rule 1 */
} PCSktState;

static inline void pc_skt_reset(PCSktState* s) {
    s->cur = PC_SKT_NONE;
    s->chal = PC_SKT_NONE;
    s->chal_frames = 0.0f;
    s->cur_talk = 0;
}

static inline float pc_skt_d2(const PCSktCand* c, float kx, float kz) {
    return (c->x - kx) * (c->x - kx) + (c->z - kz) * (c->z - kz);
}

/* is a better than b (nearer, ties: the lower id) */
static inline int pc_skt_better(float da, int ida, float db, int idb) {
    return da < db || (da == db && ida < idb);
}

/* pick the keeper's target for this frame; dt = 60 fps frames since the last call. Returns the target's wire id (PC_SKT_NONE when there is nobody). */
static inline int pc_skt_pick(PCSktState* s, float kx, float kz, const PCSktCand* c, int n, float dt) {
    int i, near = -1, talk = -1, cur_i = -1;
    float dn = 0.0f, dt2 = 0.0f;
    if (n > PC_SKT_MAX_CAND) {
        n = PC_SKT_MAX_CAND;
    }
    for (i = 0; i < n; i++) {
        float d = pc_skt_d2(&c[i], kx, kz);
        if (near < 0 || pc_skt_better(d, c[i].id, dn, c[near].id)) {
            near = i;
            dn = d;
        }
        if (c[i].talking && d < PC_SKT_TALK_RANGE * PC_SKT_TALK_RANGE && (talk < 0 || pc_skt_better(d, c[i].id, dt2, c[talk].id))) {
            talk = i;
            dt2 = d;
        }
        if (c[i].id == s->cur) {
            cur_i = i;
        }
    }
    if (talk >= 0) { /* rule 1 */
        s->cur = c[talk].id;
        s->cur_talk = 1;
        s->chal = PC_SKT_NONE;
        s->chal_frames = 0.0f;
        return s->cur;
    }
    if (near < 0) {
        pc_skt_reset(s);
        return PC_SKT_NONE;
    }
    if (cur_i < 0) { /* rule 3 / first pick */
        s->cur = c[near].id;
        s->cur_talk = 0;
        s->chal = PC_SKT_NONE;
        s->chal_frames = 0.0f;
        return s->cur;
    }
    s->cur_talk = 0;
    if (near == cur_i) {
        s->chal = PC_SKT_NONE;
        s->chal_frames = 0.0f;
        return s->cur;
    }
    { /* rule 2: distances, not squares, for the margin */
        float dc = pc_skt_d2(&c[cur_i], kx, kz);
        float ln = 0.0f;
        /* |cur| - |near| >= M  <=>  dc >= (|near| + M)^2; |near| = sqrt(dn) by a short Newton iteration (keeps the header libm-free) */
        if (dn > 0.0f) {
            int k;
            ln = dn > 1.0f ? dn * 0.5f : 1.0f;
            for (k = 0; k < 24; k++) {
                ln = 0.5f * (ln + dn / ln);
            }
        }
        if (dc >= (ln + PC_SKT_MARGIN) * (ln + PC_SKT_MARGIN)) {
            if (s->chal == c[near].id) {
                s->chal_frames += dt;
            } else {
                s->chal = c[near].id;
                s->chal_frames = dt;
            }
            if (s->chal_frames >= PC_SKT_DWELL) {
                s->cur = c[near].id;
                s->chal = PC_SKT_NONE;
                s->chal_frames = 0.0f;
            }
        } else {
            s->chal = PC_SKT_NONE;
            s->chal_frames = 0.0f;
        }
    }
    return s->cur;
}

#ifdef __cplusplus
}
#endif

#endif /* PC_SHOP_KEEPER_TARGET_H */
