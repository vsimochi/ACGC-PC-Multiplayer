/* pc_handover.h - HAND-OVER ANIMATIONS in multiplayer (Nook Work Mode, villager gifts / takes): the pure logic shared by the game and
 * pc/tools/net_spike/handover_selftest.c (no engine dependencies).
 *
 * The LOCAL hand-over (the player's RECIEVE_WAIT / RECIEVE_STRETCH / RECIEVE / RECIEVE_PUTAWAY / GIVE / GIVE_WAIT states 66-71 and the NPC's
 * TRANSFER / GET clips with the item in hand) is always the VANILLA machinery: the handOverItem actor (ac_handOverItem*), the NPC's aNPC_ACT_TRANS /
 * aNPC_ACT_GET (requested through the vanilla mDemo order values), Nook's own TRANSFER / GET clips, mPlib_request_main_give_type1(). The PC seams
 * (ac_quest_talk_work.c_inc: the Work Mode villager talk, ac_npc_shop_common.c: the Work Mode Nook rows) only START that machinery once the HOST
 * APPLIED the work transaction (a refusal never plays it). The pockets are the host's post-image, applied by the transaction core at APPLIED
 * exactly as before: nothing here writes a pocket, a wallet or a job.
 *
 * What OTHER players see:
 *   - the player: ACTION_EXT (96) already carries 67-71 (the real local states now run, so the generic sender reports them);
 *   - the villager: HANDOVER (103) client -> host: "my local villager <slot> hands <item> to me / takes it from me". The host, which holds that
 *     villager still for the conversation (talk hold), plays the matching ACTION TYPES on ITS actor (pc_ho_script_*), and the existing NPC_MOVE
 *     stream (action_type, N3 Channel A) carries them to every other client, whose consumer villager plays the clips through aNPC_setupAction;
 *   - Nook (and the other shop keepers: one local actor per process): HANDOVER (103) owner -> host -> every OTHER READY peer in the owner's
 *     announced scene; an idle shop keeper there plays the same clip sequence (pc_ho_script_*) once.
 * Old peers: no branch for 103 -> dropped silently (outside the violation-counting ranges 47-50 / 59-61). PRESENTATION ONLY.
 * Kill switch: AC_MP_HANDOVER_ANIM=0 or settings.ini [Gameplay] handover_anim = 0 (the old instant pocket change, no animation, no 103). */
#ifndef PC_HANDOVER_H
#define PC_HANDOVER_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_HO_MSG_ID 103u

enum { PC_HO_KIND_NPC_GIVES = 1, PC_HO_KIND_NPC_TAKES = 2 };     /* npc -> player / player -> npc */
enum { PC_HO_CLASS_VILLAGER = 1, PC_HO_CLASS_SHOP = 2 };          /* an outdoor town villager (Save_t.animals[] slot) / a shop keeper of the sender's shop scene */
#define PC_HO_FL_FISH 0x01u                                       /* the item is a fish (the _F clips) */
#define PC_HO_FL_RESERVED 0xFEu
#define PC_HO_SLOT_NONE 0xFFu

typedef struct PCHandoverMsg {
    uint8_t  msg_type;  /* PC_HO_MSG_ID */
    uint8_t  kind;      /* PC_HO_KIND_* */
    uint8_t  npc_class; /* PC_HO_CLASS_* */
    uint8_t  slot;      /* VILLAGER: the animals[] slot; SHOP: PC_HO_SLOT_NONE */
    uint16_t npc_id;    /* VILLAGER: Animal_c.id.npc_id (slot-reuse guard); SHOP: the keeper actor's npc_id */
    uint16_t item;      /* the exchanged item (presentation: only the fish flag / logs use it) */
    uint8_t  origin;    /* client -> host: 0; host -> client: the owner's wire id (stamped by the host) */
    uint8_t  flags;     /* PC_HO_FL_* */
    uint16_t seq;       /* the sender's own counter (logs) */
    uint32_t rsv;       /* 0 */
} PCHandoverMsg;
typedef char pc_ho_msg_size_check[(sizeof(PCHandoverMsg) == 16) ? 1 : -1];

/* shape check (every receiver): 1 = well-formed. slot_max = ANIMAL_NUM_MAX of the caller. */
static inline int pc_ho_msg_ok(const PCHandoverMsg* m, int slot_max) {
    if (m->msg_type != PC_HO_MSG_ID || (m->kind != PC_HO_KIND_NPC_GIVES && m->kind != PC_HO_KIND_NPC_TAKES) || (m->flags & PC_HO_FL_RESERVED) != 0 || m->rsv != 0 ||
        m->npc_id == 0 || m->item == 0) {
        return 0;
    }
    if (m->npc_class == PC_HO_CLASS_VILLAGER) {
        return (int)m->slot < slot_max;
    }
    if (m->npc_class == PC_HO_CLASS_SHOP) {
        return m->slot == PC_HO_SLOT_NONE;
    }
    return 0;
}

/* ---- the clip script: the NPC side of one hand-over, played on an actor that does NOT run the vanilla hand-over itself ----
 * (the host's talk-held villager, an observer's idle shop keeper). Steps advance when the one-shot clip of the step stopped (after a minimum)
 * or at the step's cap; the wait step is timed. The caller maps the step to its clip (villager: aNPC_ACTION_TYPE_*, keeper: aNPC_ANIM_*). */
enum {
    PC_HO_ST_NONE = 0,
    PC_HO_ST_TRANSFER,    /* TRANSFER1 / TRANSFER_F1: the NPC holds the item out */
    PC_HO_ST_TRANS_WAIT,  /* TRANS_WAIT1 / _F1: waiting for the player to take it */
    PC_HO_ST_GET,         /* GET1 / GET_F1: reaching for the player's item */
    PC_HO_ST_GET_PULL,    /* GET_PULL1 / _F1 */
    PC_HO_ST_GET_PUTAWAY, /* GET_PUTAWAY1 / _F1 (+ the GASAGOSO sound in vanilla) */
    PC_HO_ST_DONE         /* restore the previous clip */
};
#define PC_HO_MIN_FRAMES 8.0f        /* a step never ends before (the clip's STOPPED flag of the previous clip may still be set on the first frame) */
#define PC_HO_TRANSFER_CAP 120.0f    /* caps (60 fps frames) in case the clip never reports STOPPED */
#define PC_HO_TRANS_WAIT_FRAMES 70.0f /* the vanilla wait lasts until the player took the item: ~ the player's RECIEVE_STRETCH + RECIEVE clips */
#define PC_HO_GET_CAP 90.0f
#define PC_HO_SCRIPT_TTL_FRAMES 600.0f /* a whole script never runs longer than 10 s */

typedef struct PCHoScript {
    uint8_t  active;
    uint8_t  kind;
    uint8_t  step;
    uint8_t  fish;
    uint16_t npc_id;
    float    step_frames;
    float    total_frames;
} PCHoScript;

static inline void pc_ho_script_start(PCHoScript* s, int kind, uint16_t npc_id, int fish) {
    s->active = 1;
    s->kind = (uint8_t)kind;
    s->step = (uint8_t)(kind == PC_HO_KIND_NPC_GIVES ? PC_HO_ST_TRANSFER : PC_HO_ST_GET);
    s->fish = (uint8_t)(fish ? 1 : 0);
    s->npc_id = npc_id;
    s->step_frames = 0.0f;
    s->total_frames = 0.0f;
}

/* advance by dt frames; clip_stopped = the actor's main clip of the CURRENT step has stopped. Returns the step to show now (PC_HO_ST_DONE once:
 * the caller restores its clip, the script is then inactive; PC_HO_ST_NONE = inactive). *changed = 1 when the step changed this call. */
static inline int pc_ho_script_tick(PCHoScript* s, int clip_stopped, float dt, int* changed) {
    float cap = 0.0f;
    int next;
    *changed = 0;
    if (!s->active) {
        return PC_HO_ST_NONE;
    }
    s->step_frames += dt;
    s->total_frames += dt;
    switch (s->step) {
        case PC_HO_ST_TRANSFER: cap = PC_HO_TRANSFER_CAP; next = PC_HO_ST_TRANS_WAIT; break;
        case PC_HO_ST_TRANS_WAIT: cap = PC_HO_TRANS_WAIT_FRAMES; next = PC_HO_ST_DONE; clip_stopped = 0; break; /* a looping clip: timed only */
        case PC_HO_ST_GET: cap = PC_HO_GET_CAP; next = PC_HO_ST_GET_PULL; break;
        case PC_HO_ST_GET_PULL: cap = PC_HO_GET_CAP; next = PC_HO_ST_GET_PUTAWAY; break;
        case PC_HO_ST_GET_PUTAWAY: cap = PC_HO_GET_CAP; next = PC_HO_ST_DONE; break;
        default: s->active = 0; *changed = 1; return PC_HO_ST_DONE;
    }
    if (s->total_frames >= PC_HO_SCRIPT_TTL_FRAMES) {
        next = PC_HO_ST_DONE;
    } else if (!((clip_stopped && s->step_frames >= PC_HO_MIN_FRAMES) || s->step_frames >= cap)) {
        return s->step;
    }
    s->step = (uint8_t)next;
    s->step_frames = 0.0f;
    *changed = 1;
    if (next == PC_HO_ST_DONE) {
        s->active = 0;
    }
    return next;
}

/* ---- the host's per-peer rate limit: at most PC_HO_RATE_N messages per PC_HO_RATE_MS (a real conversation sends 1..3) ---- */
#define PC_HO_RATE_N 4u
#define PC_HO_RATE_MS 2000u
typedef struct PCHoRate {
    uint32_t window_ms;
    uint32_t count;
} PCHoRate;
static inline int pc_ho_rate_ok(PCHoRate* r, uint32_t now_ms) {
    if (r->count == 0u || (uint32_t)(now_ms - r->window_ms) >= PC_HO_RATE_MS) {
        r->window_ms = now_ms;
        r->count = 0u;
    }
    if (r->count >= PC_HO_RATE_N) {
        return 0;
    }
    r->count++;
    return 1;
}

/* ---- the Work Mode plan: which hand-overs a successful (APPLIED) work op plays, in order ----
 * op numbers = PC_WORK_OP_* (pc_nook_house.h): 2 DELIVER (player -> Nook: the objective), 4 TAKE_PARCEL (Nook -> player: the parcel),
 * 5 VILLAGER_GIVE (villager -> player: the fetched item), 6 VILLAGER_RECEIVE (player -> villager: the parcel, then an ITEM tip villager -> player).
 * ENTER / LEAVE / REPORT move no item by hand (REPORT pays Bells into the wallet, as vanilla Nook's payments do). tip_kind 2 = an item tip
 * (PC_WORK_TIP_ITEM). Returns the number of steps (0..2); kinds[i] = PC_HO_KIND_*, items[i] = the item. A step with no item is dropped. */
static inline int pc_ho_work_plan(int op, int applied, int obj_item, int carried_item, int tip_kind, int tip_value, int* kinds, int* items) {
    int n = 0;
    if (!applied) {
        return 0; /* a refusal never plays a hand-over */
    }
    switch (op) {
        case 2: if (obj_item != 0) { kinds[n] = PC_HO_KIND_NPC_TAKES; items[n++] = obj_item; } break;
        case 4: if (carried_item != 0) { kinds[n] = PC_HO_KIND_NPC_GIVES; items[n++] = carried_item; } break;
        case 5: if (obj_item != 0) { kinds[n] = PC_HO_KIND_NPC_GIVES; items[n++] = obj_item; } break;
        case 6:
            if (carried_item != 0) { kinds[n] = PC_HO_KIND_NPC_TAKES; items[n++] = carried_item; }
            if (tip_kind == 2 && tip_value != 0) { kinds[n] = PC_HO_KIND_NPC_GIVES; items[n++] = tip_value; }
            break;
        default: break;
    }
    return n;
}

/* ---- the local hand-over waiter (the seams poll the vanilla handOverItem clip once per frame) ----
 * started = the item actor got a master (the hand-over is running); finished when it has none again (and the player left the receive states);
 * a hand-over that never starts / never ends is abandoned at the cap (the seam then unlocks the message window: the dialogue never hangs). */
#define PC_HO_WAIT_START_CAP 240.0f  /* 4 s for the vanilla actors to start it */
#define PC_HO_WAIT_TOTAL_CAP 900.0f  /* 15 s in all */
enum { PC_HO_WAIT_RUNNING = 0, PC_HO_WAIT_DONE = 1, PC_HO_WAIT_TIMEOUT = 2 };
typedef struct PCHoWait {
    uint8_t started;
    float   frames;
} PCHoWait;
static inline void pc_ho_wait_start(PCHoWait* w) {
    w->started = 0;
    w->frames = 0.0f;
}
static inline int pc_ho_wait_tick(PCHoWait* w, int master_set, int player_busy, float dt) {
    w->frames += dt;
    if (master_set) {
        w->started = 1;
    } else if (w->started && !player_busy) {
        return PC_HO_WAIT_DONE;
    }
    if ((!w->started && w->frames >= PC_HO_WAIT_START_CAP) || w->frames >= PC_HO_WAIT_TOTAL_CAP) {
        return PC_HO_WAIT_TIMEOUT;
    }
    return PC_HO_WAIT_RUNNING;
}

/* ---- game-side API (pc_handover.c / pc_net_game.c; not used by the selftest) ---- */
int pc_handover_enabled(void); /* AC_MP_HANDOVER_ANIM (env, 0 = off) && settings.ini [Gameplay] handover_anim (default 1) */
/* a LOCAL NPC started a hand-over with the local player (the vanilla actor code, any role): logs, and a READY client tells the host (103).
 * npc_class PC_HO_CLASS_*, slot = the villager slot (-1 for a keeper), npc_id, kind PC_HO_KIND_*, item, fish 0/1. */
void pc_net_game_handover_local(int npc_class, int slot, int npc_id, int kind, int item, int fish);
/* HOST: the clip step to show on its talk-held villager `slot` this frame (PC_HO_ST_*; NONE = no script); clip_stopped = its main clip stopped. */
int pc_net_game_host_handover_step(int slot, int npc_id, int clip_stopped, float dt, int* changed, int* fish);
/* every process: the clip step for an idle shop keeper `npc_id` (an observer's view of another player's hand-over in this shop). */
int pc_net_game_shop_handover_step(int npc_id, int clip_stopped, float dt, int* changed, int* fish);

#ifdef __cplusplus
}
#endif

#endif /* PC_HANDOVER_H */
