/* pc_fishing_view.h - fishing / bug-catching presentation on the other players' puppets (worker P3). PRESENTATION ONLY.
 *
 * Pure logic, no engine dependencies (compiled into the game and into pc/tools/net_spike/fishing_view_selftest.c):
 *  - the fixed 24-byte wire struct of BOBBER_VIEW (PC_NETGAME_MSG_BOBBER_VIEW = 97, host -> READY peers in the angler's announced scene,
 *    UNRELIABLE while the bobber exists (<= 10 Hz per angler), RELIABLE once when it is gone; protocol 8 extended in place, no version bump;
 *    an old peer ignores the unknown id),
 *  - the host relay state per angler (validation of the BOBBER_STATE the host already receives / of the host's own bobber, reordering guard,
 *    a fixed send interval, a stale timeout, a "gone" on reel-in / disconnect / scene change),
 *  - the receiver view per angler (shape check, scene filter, wrap-safe seq, expiry).
 * The view is drawn by pc_remote_player.c (the vanilla tol_uki model at the relayed position). It is NOT a UKI actor: nothing here or in its glue
 * creates an actor the fish AI could see, and nothing writes wildlife / fish / catch / item state. The host's fish AI keeps using the
 * pcwld proxy fed by BOBBER_STATE exactly as before (this module only reads the same message after it was handled).
 *
 * PLAYER_ACTION (id 46) kind 2 CATCH (PC_NETGAME_PLAYER_ACTION_KIND_CATCH, pc_net_game.c) is the other half: the host emits it after IT accepted a
 * catch (the X3 catch transaction of a client, or the host's own local catch), with the host-derived item; the puppet shows that creature during
 * NOTICE_NET / NOTICE_ROD (and its release in RELEASE_CREATURE). */
#ifndef PC_FISHING_VIEW_H
#define PC_FISHING_VIEW_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_FV_MSG_BOBBER_VIEW 97u          /* == PC_NETGAME_MSG_BOBBER_VIEW (pc_net_game.c _Static_asserts it) */
#define PC_FV_ORIGINS 256                  /* indexed by the wire player id (uint8_t): peer ids + the host id */
#define PC_FV_SEND_INTERVAL_MS 100u        /* host: at most one active BOBBER_VIEW per angler per 100 ms (<= 10 Hz) */
#define PC_FV_HOST_STALE_MS 1000u          /* host: no BOBBER_STATE / own sample for this long = the bobber is gone */
#define PC_FV_VIEW_STALE_MS 1500u          /* receiver: no BOBBER_VIEW for this long = hide it */
#define PC_FV_SEQ_FORGET_MS 3000u          /* receiver: a seq memory older than this is forgotten (a restarted host counts from 1 again) */
#define PC_FV_MAX_REACH 480.0f             /* host: bobber farther than this (xz) from the angler's last MOVE position = rejected */
#define PC_FV_MAX_DY 200.0f
#define PC_FV_UKI_STATUS_MAX 7             /* aUKI_STATUS_CATCH (ac_uki.h: 0..7) */
#define PC_FV_UKI_STATUS_CAST 3            /* aUKI_STATUS_CAST: the first status pcwld_client_collect_bobber() reports */

/* BOBBER_VIEW, id 97, host -> client, 24 bytes. origin = the angler's wire player id (host-assigned, never a client claim). active 0 = "gone"
 * (the receiver hides it at once). seq = host-assigned per origin (wrap-safe, active and gone share it, so a late unreliable 'active' that
 * arrives after the reliable 'gone' is dropped). rsv must be 0. */
typedef struct PCBobberViewMsg {
    uint8_t  msg_type;   /* PC_NETGAME_MSG_BOBBER_VIEW */
    uint8_t  origin;
    uint8_t  active;
    uint8_t  scene_id;   /* the angler's announced scene: a receiver in another scene ignores it */
    uint8_t  uki_status; /* aUKI_STATUS_* (CAST .. CATCH) */
    int8_t   gyo_status; /* the bobber's own fish stage (1 idle ... 4 bite, 5 hooked, 6 catch) */
    uint8_t  rod_type;   /* 0 normal rod (tol_uki1), 1 golden rod (tol_uki2) */
    uint8_t  rsv;        /* 0 */
    float    x, y, z;    /* absolute world position */
    int16_t  angle;      /* the bobber's world.angle.y */
    uint16_t seq;
} PCBobberViewMsg;
_Static_assert(sizeof(PCBobberViewMsg) == 24, "PCBobberViewMsg wire size drifted");

/* one bobber sample (host input / receiver view) */
typedef struct PCFvSample {
    uint8_t active;
    uint8_t uki_status;
    int8_t  gyo_status;
    uint8_t rod_type;
    uint8_t scene_id;
    float   x, y, z;
    int16_t angle;
} PCFvSample;

/* ---- host relay ---- */
typedef struct PCFvHostSlot {
    uint8_t    have;          /* an active bobber is known */
    uint8_t    sent_any;      /* an active view went out since the last 'gone' (only then a 'gone' is owed) */
    uint8_t    gone_pending;  /* a 'gone' must go out at the next poll */
    uint8_t    gone_scene;    /* ... to this scene */
    uint8_t    in_seq_valid;
    uint8_t    last_sent_scene;
    uint8_t    last_sent_uki;
    int8_t     last_sent_gyo;
    int16_t    in_seq;        /* the newest BOBBER_STATE.seq applied */
    uint16_t   seq_out;
    uint32_t   last_in_ms;
    uint32_t   last_sent_ms;
    uint32_t   accepted, rejected, reordered, sent_active, sent_gone;
    PCFvSample s;
} PCFvHostSlot;

typedef struct PCFvHost {
    PCFvHostSlot o[PC_FV_ORIGINS];
} PCFvHost;

/* 1 = a plausible bobber (finite, inside the town coordinate range, a known uki / gyo status, rod 0/1) and, when the angler's position is
 * known (player_pos != NULL), within PC_FV_MAX_REACH / PC_FV_MAX_DY of it. An inactive sample is always ok (it only clears). */
int pc_fv_sample_ok(const PCFvSample* s, const float* player_pos);

/* Host: one input for `origin` (a client's BOBBER_STATE after the host handled it, or the host's own bobber). has_seq / seq = the
 * BOBBER_STATE ordering (a sample older than one already applied is dropped). Returns 1 accepted, 0 dropped (bad / reordered / origin). */
int pc_fv_host_note(PCFvHost* h, int origin, const PCFvSample* s, const float* player_pos, int has_seq, int16_t seq, uint32_t now);

/* Host: the angler disconnected / left the scene / reeled in without a final state: owe a 'gone' if a view went out. */
void pc_fv_host_drop(PCFvHost* h, int origin);

/* Host: once per poll per origin. Returns 1 and fills *out (+ *reliable) when a message must go out now: a pending 'gone' (reliable, once),
 * a scene change of an active bobber (first a 'gone' for the old scene), or the active view when PC_FV_SEND_INTERVAL_MS passed since the
 * last one. A bobber with no input for PC_FV_HOST_STALE_MS turns into a 'gone'. */
int pc_fv_host_poll(PCFvHost* h, int origin, uint32_t now, PCBobberViewMsg* out, int* reliable);

/* ---- receiver view ---- */
typedef struct PCFvView {
    uint8_t    active;
    uint8_t    seq_valid;
    uint16_t   seq;
    uint32_t   last_rx_ms;
    uint32_t   since_ms;      /* when this activation started (the receiver snaps the drawn position on a new one) */
    uint32_t   applied, ignored;
    PCFvSample s;
} PCFvView;

/* 1 = the message is well formed (type, rsv 0, active 0/1, status ranges, finite, coordinate range). */
int pc_fv_msg_ok(const PCBobberViewMsg* m);

/* Receiver: apply a well-formed message to the origin's view. my_scene_valid / my_scene = the receiver's own announced scene: an active view of
 * another scene is ignored (and clears a shown one). Stale / duplicate seq is ignored. Returns 1 applied (shown or cleared), 0 ignored. */
int pc_fv_view_apply(PCFvView* v, const PCBobberViewMsg* m, int my_scene_valid, uint8_t my_scene, uint32_t now);

/* Receiver: 1 = the view is active and fresh (no message for PC_FV_VIEW_STALE_MS hides it); fills *out. */
int pc_fv_view_get(PCFvView* v, uint32_t now, PCFvSample* out);

/* ---- glue implemented in pc_net_game.c / pc_remote_player.c (game build only) ---- */
/* receiver side (pc_net_game.c): the current bobber of player `origin` as THIS process should draw it; 1 = shown. age_ms = since the last update. */
int pc_net_game_bobber_view_get(int origin, PCFvSample* out, uint32_t* active_since_ms);
/* pc_remote_player.c: a host-accepted catch of player `origin` (PLAYER_ACTION kind 2): item = the host-derived creature item. */
void pc_remote_player_on_catch(int origin, uint16_t item, uint16_t seq);

#ifdef __cplusplus
}
#endif

#endif /* PC_FISHING_VIEW_H */
