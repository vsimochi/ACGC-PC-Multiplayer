/* pc_pvp_authority.h - player-vs-player tool hits (axe / shovel / net), host-authoritative REACTIONS ONLY.
 *
 * Pure logic, no engine dependencies (compiled into the game and into pc/tools/net_spike/pvp_selftest.c):
 *  - the two fixed-size wire structs (PC_NETGAME_MSG_PVP_HIT_REQ = 91 client -> host, PC_NETGAME_MSG_PVP_HIT = 92 host -> the victim
 *    only; both RELIABLE; protocol 8 extended in place, no version bump; an old peer ignores both ids),
 *  - the attacker-side geometry (the vanilla axe triangle, a shovel circle and the net head segment vs a puppet pipe),
 *  - the host validation (both players in the town field, a real target, the attacker's recent swing state, distance / facing cone from the
 *    host's last-known positions, per-attacker rate, per-victim immunity, arrival / scene-change protection, seq ordering),
 *  - the victim rules (state allowlist, reaction per tool, balloon guard, pending-hit TTL and a local minimum interval).
 * A hit is a REACTION only: a tumble or a shock animation with a small knockback. Nothing here (or in its glue) writes an item, a pocket,
 * the wallet, the axe wear or the field. Always on: there is no host switch and no per-player opt-out. */
#ifndef PC_PVP_AUTHORITY_H
#define PC_PVP_AUTHORITY_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- tools / reactions ---- */
enum { PC_PVP_TOOL_AXE = 1, PC_PVP_TOOL_SHOVEL = 2, PC_PVP_TOOL_NET = 3 };
enum { PC_PVP_REACT_TUMBLE = 1, PC_PVP_REACT_SHOCK = 2, PC_PVP_REACT_FLINCH = 3 };
/* axe -> TUMBLE (trip + slide away), shovel -> SHOCK (the GAAAN dismay, in place), net -> FLINCH (the short STUNG_MOSQUITO arm-slap clip, in place;
 * the PvP entry skips the vanilla mosquito dialogue and returns to WAIT). SHOCK / FLINCH never turn the victim (it keeps its own facing). */
#define PC_PVP_SHOCK_FRAMES 84.0f /* shovel SHOCK length in 60 fps frames (vanilla end_time = start + 44): GAAAN1 then GAAAN2 held, ~1.4 s */
#define PC_PVP_FLINCH_LABEL 0x50565031u /* the stung_mosquito label of a PvP flinch (no mosquito actor has it): its end goes to WAIT, not NOTICE_MOSQUITO */

/* vanilla mPlayer_INDEX_* values the logic needs (pc_net_game.c _Static_asserts each against include/m_player.h) */
enum {
    PC_PVP_IDX_WAIT = 7, PC_PVP_IDX_WALK = 8, PC_PVP_IDX_RUN = 9, PC_PVP_IDX_DASH = 10, PC_PVP_IDX_TUMBLE = 11, PC_PVP_IDX_TUMBLE_GETUP = 12,
    PC_PVP_IDX_TURN_DASH = 13, PC_PVP_IDX_SWING_AXE = 37, PC_PVP_IDX_AIR_AXE = 38, PC_PVP_IDX_REFLECT_AXE = 39, PC_PVP_IDX_READY_NET = 42,
    PC_PVP_IDX_READY_WALK_NET = 43, PC_PVP_IDX_SWING_NET = 44, PC_PVP_IDX_PULL_NET = 45, PC_PVP_IDX_STOP_NET = 46, PC_PVP_IDX_DIG_SCOOP = 58,
    PC_PVP_IDX_FILL_SCOOP = 59, PC_PVP_IDX_REFLECT_SCOOP = 60, PC_PVP_IDX_AIR_SCOOP = 61, PC_PVP_IDX_SHOCK = 99, PC_PVP_IDX_STUNG_MOSQUITO = 107,
};

/* ---- tuning ---- */
#define PC_PVP_AXE_REACH 35.0f          /* m_player_common.c_inc:2101-2106: triangle tips 35 units ahead at +-8 degrees */
#define PC_PVP_AXE_HALF_ANGLE_DEG 8.0255126953125f
#define PC_PVP_PUPPET_RADIUS 16.0f      /* the puppet pipe (pc_remote_player.c s_remote_player_pipe_data) */
#define PC_PVP_PUPPET_HEIGHT 60.0f
#define PC_PVP_SHOVEL_AHEAD 40.0f       /* the shovel point: one tile ahead of the player in its facing */
#define PC_PVP_SHOVEL_RADIUS 39.0f      /* vanilla refuses to dig within 39 units of a villager (m_player_lib.c AIR_SCOOP) */
#define PC_PVP_NET_RADIUS 10.0f         /* the net head segment's own thickness on top of the puppet pipe */
#define PC_PVP_AXE_CONTACT_FRAME 15.0f  /* swing_axe.c_inc / reflect_axe.c_inc contact frame */
#define PC_PVP_SHOVEL_CONTACT_FRAME 13.0f /* air_scoop.c_inc / reflect_scoop.c_inc contact frame */
#define PC_PVP_MAX_DIST 60.0f           /* host: attacker <-> target from the host's last-known positions (latency tolerance) */
#define PC_PVP_MAX_DY 50.0f
#define PC_PVP_CLAIM_DIST 40.0f         /* host: the claimed attacker position vs. its last MOVE position */
#define PC_PVP_CONE_DEG 60.0f           /* host: target inside +-60 degrees of the claimed facing */
#define PC_PVP_CONE_MIN_DIST 12.0f      /* closer than this the cone is not evaluated (overlapping bodies) */
#define PC_PVP_SWING_WINDOW_MS 600u     /* host: the attacker's MOVE showed a matching swing state at most this long ago */
#define PC_PVP_RATE_MS 700u             /* host: one accepted hit per attacker per 700 ms */
#define PC_PVP_IMMUNE_MS 2000u          /* host: a victim cannot be hit again for 2 s */
#define PC_PVP_PROTECT_MS 10000u        /* host: no hits on / by a player within 10 s of entering the town field (arrival / scene change) */
#define PC_PVP_VICTIM_TTL_MS 700u       /* victim: a received hit not applied within this is dropped (menu / talk / cutscene) */
#define PC_PVP_VICTIM_MIN_MS 1500u      /* victim: local minimum interval between two applied reactions */
#define PC_PVP_KNOCK_AXE 7.5f           /* tumble knockback START speed (a dash trip), eased to 0 over PC_PVP_KNOCK_FRAMES 60 fps frames by the victim hook: ~45 units at ANY frame rate */
#define PC_PVP_KNOCK_FRAMES 24.0f       /* (vanilla brakes the tumble by 0.175 per RENDER frame, m_player_main_tumble.c_inc:82, so its own slide shrinks with the fps) */
#define PC_PVP_KNOCK_SHOVEL 0.0f        /* the shock state brakes at 0.326 and turns toward the attacker: no push */
#define PC_PVP_KNOCK_MAX 8.0f
#define PC_PVP_ATTACK_MIN_MS 600u       /* attacker: local minimum interval between two requests */
#define PC_PVP_MAX_PLAYERS 256          /* wire ids 0..254 (+ 0xFF unused) */

/* ---- wire ---- */
typedef struct PCPvpHitReqMsg {
    uint8_t  msg_type; /* PC_NETGAME_MSG_PVP_HIT_REQ (91) */
    uint8_t  target;   /* the victim's wire id as the attacker's process knows it (8 = the host) */
    uint8_t  tool;     /* PC_PVP_TOOL_* */
    uint8_t  rsv;      /* 0 */
    uint16_t seq;      /* per-attacker swing counter (wraps) */
    int16_t  facing;   /* the attacker's shape_info.rotation.y at the contact frame */
    float    pos_x, pos_z; /* the attacker's world position at the contact frame */
} PCPvpHitReqMsg;
_Static_assert(sizeof(PCPvpHitReqMsg) == 16, "PCPvpHitReqMsg is a fixed 16-byte wire struct");

typedef struct PCPvpHitMsg {
    uint8_t  msg_type;    /* PC_NETGAME_MSG_PVP_HIT (92) */
    uint8_t  attacker;    /* the attacker's wire id as the VICTIM knows it (8 = the host) */
    uint8_t  tool;        /* PC_PVP_TOOL_* */
    uint8_t  reaction;    /* PC_PVP_REACT_* (the host's choice; the victim may still downgrade TUMBLE to SHOCK: balloon guard) */
    int16_t  knock_angle; /* engine angle the victim slides toward (away from the attacker) */
    uint16_t seq;         /* host hit counter (diagnostics / duplicate filter) */
    float    knock_speed; /* 0..PC_PVP_KNOCK_MAX */
} PCPvpHitMsg;
_Static_assert(sizeof(PCPvpHitMsg) == 12, "PCPvpHitMsg is a fixed 12-byte wire struct");

int pc_pvp_req_msg_valid(const PCPvpHitReqMsg* m);
int pc_pvp_hit_msg_valid(const PCPvpHitMsg* m);

/* ---- angles / geometry (engine angle: x += sin(a) * d, z += cos(a) * d) ---- */
int16_t pc_pvp_angle_to(float from_x, float from_z, float to_x, float to_z);
int     pc_pvp_angle_diff_deg(int16_t a, int16_t b); /* |a - b| in degrees, 0..180 */
/* tool of a vanilla main index (PC_PVP_TOOL_*), 0 = not a swing state of a hitting tool */
int pc_pvp_tool_of_index(int main_index);
/* 1 when a puppet pipe at (tx, ty, tz) is hit by the axe triangle of a player at (px, py, pz) facing `facing` */
int pc_pvp_axe_hits(float px, float py, float pz, int16_t facing, float tx, float ty, float tz);
/* 1 when the shovel point (PC_PVP_SHOVEL_AHEAD ahead) is within PC_PVP_SHOVEL_RADIUS of the puppet */
int pc_pvp_shovel_hits(float px, float py, float pz, int16_t facing, float tx, float ty, float tz);
/* 1 when the net head segment a -> b (xyz) passes within pipe radius + PC_PVP_NET_RADIUS of the puppet and the heights overlap */
int pc_pvp_net_hits(const float a[3], const float b[3], float tx, float ty, float tz);

/* ---- host ---- */
enum {
    PC_PVP_OK = 0,
    PC_PVP_REJ_MALFORMED = 1,
    PC_PVP_REJ_BAD_ATTACKER = 2,   /* not READY / observer / out of range */
    PC_PVP_REJ_BAD_TARGET = 3,     /* self / not READY / the dedicated observer / unknown */
    PC_PVP_REJ_SCENE = 4,          /* attacker or target not in the town field */
    PC_PVP_REJ_NO_POS = 5,         /* no last-known position for attacker or target */
    PC_PVP_REJ_NO_SWING = 6,       /* the attacker's MOVE never showed a matching swing within the window */
    PC_PVP_REJ_CLAIM_FAR = 7,      /* claimed position far from the attacker's last MOVE position */
    PC_PVP_REJ_RANGE = 8,          /* attacker <-> target too far (xz or height) */
    PC_PVP_REJ_CONE = 9,           /* target outside the facing cone */
    PC_PVP_REJ_RATE = 10,          /* attacker rate limit */
    PC_PVP_REJ_IMMUNE = 11,        /* victim was hit within PC_PVP_IMMUNE_MS */
    PC_PVP_REJ_PROTECTED = 12,     /* attacker or victim entered the town field within PC_PVP_PROTECT_MS */
    PC_PVP_REJ_OLD_SEQ = 13,       /* replayed / old swing seq */
};

/* what the host knows about one player at validation time (filled by the game glue) */
typedef struct PCPvpPlayerView {
    uint8_t valid;    /* a real, READY player (the host's own player: not an observer) */
    uint8_t in_town;  /* announced / live scene is the town field */
    uint8_t has_pos;
    float   x, y, z;  /* last-known position (MOVE for a client, the live actor for the host) */
} PCPvpPlayerView;

typedef struct PCPvpHost {
    uint32_t last_hit_ms[PC_PVP_MAX_PLAYERS];   /* attacker: last accepted hit */
    uint32_t immune_until[PC_PVP_MAX_PLAYERS];  /* victim */
    uint16_t last_seq[PC_PVP_MAX_PLAYERS];
    uint8_t  has_hit[PC_PVP_MAX_PLAYERS];
    uint8_t  has_immune[PC_PVP_MAX_PLAYERS];
    uint32_t swing_ms[PC_PVP_MAX_PLAYERS][4];   /* per tool: last time the player's MOVE showed a swing state of it */
    uint8_t  has_swing[PC_PVP_MAX_PLAYERS];     /* bit per tool */
    uint8_t  town_known[PC_PVP_MAX_PLAYERS];    /* scene tracking: 1 = seen */
    uint8_t  town_state[PC_PVP_MAX_PLAYERS];    /* last in_town value */
    uint32_t town_since[PC_PVP_MAX_PLAYERS];    /* when in_town last became 1 (or the player was first seen in town) */
    uint16_t hit_seq;                           /* PVP_HIT counter */
} PCPvpHost;

void pc_pvp_host_reset(PCPvpHost* h);
void pc_pvp_host_forget(PCPvpHost* h, int player); /* disconnect / slot reuse */
/* MOVE observation: a swing-state main index of a tool records its time */
void pc_pvp_host_note_motion(PCPvpHost* h, int player, int main_index, uint32_t now_ms);
/* scene observation (call every host tick for every READY player and the host's own) */
void pc_pvp_host_note_scene(PCPvpHost* h, int player, int in_town, uint32_t now_ms);
/* Validates a request from `attacker` (wire id) against the views. On PC_PVP_OK the rate / immunity / seq records are updated and
 * *out is filled (msg_type, attacker = the attacker wire id, tool, reaction, knock angle / speed, seq). */
int pc_pvp_host_validate(PCPvpHost* h, int attacker, const PCPvpHitReqMsg* m, const PCPvpPlayerView* av, const PCPvpPlayerView* tv,
                         uint32_t now_ms, uint8_t hit_msg_type, PCPvpHitMsg* out);
const char* pc_pvp_reason_name(int rc);

/* ---- victim ---- */
/* 1 when a player in this vanilla main index may be interrupted (WAIT / WALK / RUN / DASH / TURN_DASH / READY_NET / READY_WALK_NET) */
int pc_pvp_victim_state_ok(int main_index);
/* the reaction actually applied: TUMBLE becomes SHOCK while a balloon is held (TUMBLE_GETUP would release it) */
int pc_pvp_victim_reaction(int reaction, int holding_balloon);

typedef struct PCPvpVictim {
    PCPvpHitMsg pending;
    uint32_t    pending_ms;
    uint8_t     has_pending;
    uint8_t     applied_any;
    uint32_t    last_applied_ms;
    uint16_t    last_seq;
    uint8_t     has_seq;
} PCPvpVictim;

void pc_pvp_victim_reset(PCPvpVictim* v);
/* queue a host PVP_HIT (1 = queued, 0 = malformed / duplicate seq) */
int pc_pvp_victim_offer(PCPvpVictim* v, const PCPvpHitMsg* m, uint32_t now_ms);
/* 1 = apply *out now (consumes it): the pending hit is fresh, the local interval passed and the state is allowed. A stale hit is
 * dropped (returns 0, *dropped = 1). With a disallowed state the hit stays pending until its TTL. */
int pc_pvp_victim_take(PCPvpVictim* v, uint32_t now_ms, int main_index, int extra_block, PCPvpHitMsg* out, int* dropped);

/* ---- attacker (local) ---- */
typedef struct PCPvpAttacker {
    uint16_t seq;
    uint32_t last_ms;
    uint8_t  sent_any;
    int      armed_index;   /* the swing entry being watched (-1 none) */
    uint8_t  fired;         /* this entry already produced its one test */
} PCPvpAttacker;
void pc_pvp_attacker_reset(PCPvpAttacker* a);
/* new main-index entry (or re-entry): arms one contact test when it is a swing state of a hitting tool */
void pc_pvp_attacker_on_entry(PCPvpAttacker* a, int main_index);
/* 1 once per armed entry when the animation frame reached the tool's contact frame (axe 15, shovel 13; the net is tested by its own hook) */
int pc_pvp_attacker_contact(PCPvpAttacker* a, int main_index, float frame);
/* local rate limit; 1 and *seq_out filled when a request may be sent now */
int pc_pvp_attacker_next(PCPvpAttacker* a, uint32_t now_ms, uint16_t* seq_out);

/* ---- game glue (pc_net_game.c, called from src/game/m_player*.c under TARGET_PC) ---- */
/* victim: once per local player frame BEFORE the request processing: 1 = apply now (reaction / angle / speed / the angle to face) */
int  pc_net_game_pvp_victim_take(int main_index, int holding_balloon, int* reaction, int16_t* knock_angle, float* knock_speed, int16_t* face_angle);
/* victim: the reaction state was entered (or not) after the request: logs the result, starts the knockback measurement */
void pc_net_game_pvp_victim_applied(int reaction, int entered, int main_index, const float pos[3], int16_t facing);
/* TEST-ONLY diagnostics (PC_NET_TEST_HOOKS builds): the victim's speed / position right after an applied reaction; a no-op otherwise */
void pc_net_game_pvp_trace(int main_index, float speed, const float pos[3], int16_t facing);
/* attacker: once per local player frame AFTER the request processing (axe / shovel contact detection). entered = a main index was
 * entered this frame (player->changed_main_index). */
void pc_net_game_pvp_attacker_frame(int main_index, int entered, float frame, const float pos[3], int16_t facing);
/* attacker: the net hit test, from Player_actor_HitBGCheck_Swing_net (frame > 6): 1 = a puppet was hit (the swing stops, vanilla STOP_NET + sound) */
int  pc_net_game_pvp_net_hit(const float net_start[3], const float net_end[3], const float pos[3], int16_t facing);
/* ---- puppet queries (pc_remote_player.c) ---- */
/* 1 when the actor is another player's puppet (Check_axe_after must not reflect / wear the axe on it). Any role, also single player (0). */
int  pc_pvp_is_puppet_actor(const void* actor);
/* 1 when a visible, same-field puppet stands within `radius` (xz) of (x, z): the shovel must not dig under another player */
int  pc_pvp_puppet_near(float x, float z, float radius);
/* the visible, same-field puppets: wire ids and current (drawn) positions; returns the count (<= max) */
int  pc_pvp_puppet_targets(int* ids, float (*pos)[3], int max);

#ifdef __cplusplus
}
#endif

#endif
