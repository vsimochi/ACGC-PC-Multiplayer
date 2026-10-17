/* pc_emote.h - EMOTE WHEEL: pure logic (no engine dependencies; compiled into the game and into pc/tools/net_spike/emote_selftest.c).
 *
 * The emote wheel (pc_emote_wheel.c: hold F / hold the pad Emote button, D-pad Up by default, point with the mouse / the right stick, release)
 * plays a short emote on the LOCAL player. NO new player state and NO new wire message:
 *  - LOCAL: an "emote overlay" inside the vanilla WAIT state (src/game/m_player_pc_emote.c_inc, hooked into m_player_main_wait.c_inc): the
 *    clip is bound with the vanilla Player_actor_SetupItem_Base1 + InitAnimation_Base2 (a held tool keeps its upper-body pose), the face
 *    follows the clip's eye / mouth tables, effects / sounds fire at keyframe cues. Any movement / button input, any state change, the clip
 *    end or the time cap ends it. "Shocked" requests the vanilla SHOCK state instead (it returns to WAIT by itself).
 *  - NETWORK: while the overlay plays, the MOVE action_state and the ACTION_EXT sampler report the state as DEMO_WAIT (74) with a fresh
 *    entry counter (bumped at the start and at the end), the emote id in ACTION_EXT `variant` and its constant face in `face`; receivers bind
 *    the clip as a dynamic row and fire the emote's cue rows (pc_remote_player.c). MOVE move_state keeps the real class (WAIT -> IDLE).
 *    Not masked in the train-station zone (an arriving guest would read a DEMO_WAIT there as "the train stands at the station").
 * Gating: free play on foot only (WAIT..DASH, no demo, on the ground, not in water, no menu / message / chat / pause / tool wheel, not the
 * host observer); full-body emotes need empty hands (the held tool is NEVER stowed: that would change the inventory); 1 s cooldown.
 * Kill switch: settings.ini [Gameplay] emotes = 0, env AC_EMOTES=0. */
#ifndef PC_EMOTE_H
#define PC_EMOTE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
    PC_EMOTE_NONE = 0,
    PC_EMOTE_HOORAY = 1,
    PC_EMOTE_SHOCKED,
    PC_EMOTE_SURPRISED,
    PC_EMOTE_PRAY,
    PC_EMOTE_PHEW,
    PC_EMOTE_BOO,
    PC_EMOTE_TWIRL,
    PC_EMOTE_EXERCISE,
    PC_EMOTE_NUM /* 9: ids 1..8 = the wheel slices clockwise from the top */
};
#define PC_EMOTE_SLICES (PC_EMOTE_NUM - 1)

/* vanilla clip ids (mPlayer_ANIM_*; m_player_pc_emote.c_inc _Static_asserts each against include/m_player.h) */
#define PC_EMOTE_ANIM_WAIT1 0
#define PC_EMOTE_ANIM_MENU_CHANGE1 73
#define PC_EMOTE_ANIM_TIRED1 97
#define PC_EMOTE_ANIM_BIKU1 118
#define PC_EMOTE_ANIM_ITAZURA1 125
#define PC_EMOTE_ANIM_YATTA1 128
#define PC_EMOTE_ANIM_TAISOU1 144 /* TAISOU1 .. TAISOU7_2 = 144 .. 155 */
#define PC_EMOTE_ANIM_OMAIRI_US1 156
#define PC_EMOTE_ANIM_NUM 157

/* vanilla main indices (static asserted in m_player_pc_emote.c_inc) */
#define PC_EMOTE_IDX_WAIT 7
#define PC_EMOTE_IDX_DASH 10
#define PC_EMOTE_IDX_DEMO_WAIT 74

enum { PC_EMOTE_KIND_CLIP = 0, PC_EMOTE_KIND_SHOCK = 1 };
#define PC_EMOTE_FACE_NONE 0xFFu /* = PC_AEXT_FACE_NONE: eye / mouth from the clip's tables, normal blinking where a clip has none */

typedef struct PCEmoteStep {
    uint8_t anim;  /* PC_EMOTE_ANIM_* (always played STOP, morph -5) */
    float   speed; /* keyframe speed (0.5 = the vanilla speed of most of these clips) */
} PCEmoteStep;

typedef struct PCEmoteDef {
    const char*        name;      /* wheel label */
    unsigned char      glyph;     /* game font symbol drawn before the name (include/m_font.h CHAR_SYMBOL_*) */
    uint8_t            kind;      /* PC_EMOTE_KIND_* */
    uint8_t            full_body; /* 1 = needs empty hands (the clip moves the arms) */
    uint8_t            face;      /* eye << 4 | mouth applied where the clip has no table, or PC_EMOTE_FACE_NONE */
    uint8_t            loop;      /* 1 = the step list repeats until the cap */
    uint8_t            n_steps;
    const PCEmoteStep* steps;
    uint32_t           cap_ms;    /* hard cap of the whole emote (wall-clock: accumulated frame time, fps independent) */
} PCEmoteDef;

/* NULL for an invalid id. */
const PCEmoteDef* pc_emote_def(int id);

/* ---- wheel slice picking (dx, dy relative to the centre, y down). 0 = centre (no emote), 1..n = slices clockwise from the top. `prev` = the
 * current selection: kept a little beyond its own sector edge / the centre deadzone (hysteresis, no flicker on a boundary). */
int pc_emote_pick(float dx, float dy, float dead, int n, int prev);

/* ---- gating */
typedef struct PCEmoteGateIn {
    int enabled;        /* settings / env kill switch */
    int paused;         /* the PC pause menu */
    int menu_open;      /* a submenu is open / opening (start_refuse, menu types) */
    int title_demo;     /* the title scene's demo town */
    int transition;     /* a scene wipe */
    int observer;       /* the hidden host observer */
    int msg_or_demo;    /* a message window / a cutscene (mDemo) */
    int chat_open;      /* the chat line is open */
    int tool_wheel;     /* the tool wheel is up */
    int no_player;      /* no live player actor */
    int main_index;     /* the player's now_main_index */
    int demo_mode;      /* Player_actor_Check_is_demo_mode(now or requested) */
    int on_ground;
    int in_water;
    int hands_empty;    /* nothing in hand (Now_Private->equipment == EMPTY_NO) */
    uint32_t since_last_ms; /* since the last emote started (0xFFFFFFFF = never) */
} PCEmoteGateIn;

enum {
    PC_EMOTE_OK = 0,
    PC_EMOTE_R_DISABLED,
    PC_EMOTE_R_PAUSED,
    PC_EMOTE_R_MENU,
    PC_EMOTE_R_TITLE,
    PC_EMOTE_R_TRANSITION,
    PC_EMOTE_R_OBSERVER,
    PC_EMOTE_R_MSG,
    PC_EMOTE_R_CHAT,
    PC_EMOTE_R_TOOL_WHEEL,
    PC_EMOTE_R_NO_PLAYER,
    PC_EMOTE_R_STATE,      /* not standing / walking / running (talk, seated, wading, door, fishing, a tool action ...) */
    PC_EMOTE_R_DEMO,
    PC_EMOTE_R_AIR,
    PC_EMOTE_R_WATER,
    PC_EMOTE_R_HANDS,      /* full-body emote with a tool in hand */
    PC_EMOTE_R_COOLDOWN,
    PC_EMOTE_R_BAD_ID,
    PC_EMOTE_R_NUM
};
#define PC_EMOTE_COOLDOWN_MS 1000u

/* emote id 0 = may the wheel OPEN (everything except the per-emote rules and the cooldown); 1..8 = may this emote START now. */
int pc_emote_gate(const PCEmoteGateIn* in, int id);
const char* pc_emote_reason_name(int reason);

/* ---- overlay sequencer (the engine binds steps[s->step] whenever tick returns NEXT) */
typedef struct PCEmoteSeq {
    int   active, id, step;
    float elapsed_ms;
    int   end_reason; /* PC_EMOTE_END_* of the last END */
} PCEmoteSeq;

enum { PC_EMOTE_T_CONTINUE = 0, PC_EMOTE_T_NEXT = 1, PC_EMOTE_T_END = 2 };
enum { PC_EMOTE_END_NONE = 0, PC_EMOTE_END_DONE, PC_EMOTE_END_CAP, PC_EMOTE_END_INPUT, PC_EMOTE_END_STATE };

/* 1 = a CLIP emote started (bind steps[0]); 0 = not a clip emote (SHOCK / invalid): nothing started. */
int pc_emote_seq_begin(PCEmoteSeq* s, int id);
/* Once per game frame while active: clip_ended = the bound STOP clip reached its end this frame; dt_ms = this frame's time; input = a movement /
 * button input this frame. */
int pc_emote_seq_tick(PCEmoteSeq* s, int clip_ended, float dt_ms, int input);
void pc_emote_seq_stop(PCEmoteSeq* s, int reason);
const char* pc_emote_end_name(int reason);

/* ---- network masking (local player only) */
/* The wire main index of the local player: DEMO_WAIT while the overlay plays in WAIT outside the station zone, else the real index. */
int pc_emote_wire_index(int real_index, int overlay_active, int in_station_zone);
/* The station platform zone (= pc_remote_arrival_logic.h PCARR_STATION_*: x 2200 z 820 +-120, town field only). */
int pc_emote_station_zone(int in_town_field, float x, float z);

/* global net state written by the overlay, read by pc_net_game.c's samplers */
void pc_emote_net_set(int active, int id, uint8_t face);
int  pc_emote_net_active(int* id, uint8_t* face); /* 1 while the overlay plays */

#ifdef __cplusplus
}
#endif

#endif /* PC_EMOTE_H */
