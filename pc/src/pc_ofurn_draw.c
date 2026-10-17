/* pc_ofurn_draw.c - OUTDOOR FURNITURE (stages C1 + C2): the PC-only renderer + collision + input seams + the GRAB driver of PLACED furniture (pc_outdoor_furniture.h).
 *
 * What it does, all from the field-item actor (bg_item_common.c_inc hooks) and the player's wait / hold / rotate states, in the TOWN field only:
 *   - for every placed piece of the VIEW (host: the authoritative marker table; client: the mirror of message 102, or 98 with a C1 host) near the local player whose
 *     origin tile still holds exactly base | rotation, ONE private FTR_ACTOR + dma bank (ac_my_room.c aMR_PC_Standalone*: the catalog's construction, a trimmed
 *     aMR_DrawOneFurniture, NEVER a move_proc: no furniture behaviour, no my_room_clip, no music / drawers / sitting), at the model position of its footprint (C2: the
 *     indoor engine's aMR_UnitNumber2Position: 1x1 / 2x1 the origin cell centre, 2x2 the shared corner);
 *   - the vanilla leaf of that origin tile is not drawn (pc_ofurn_hide_leaf; a leaf without a marker is untouched; the filler cells are never drawn by vanilla);
 *   - a moving-background collision box (mCoBG_RegistMoveBg, the room furniture's own shape type + angle: 2x1 / 2x2 boxes as indoors) for the nearest pieces;
 *   - C2 GRAB (the vanilla indoor scheme, ac_my_room_move.c_inc aMR_ManageMoveBottun / aMR_MngPush/PullDirectTimer / aMR_PlaceKurukuruFurniture): A in front of a placed
 *     piece = grab (the real HOLD state, mPlib_request_main_hold_type1); while A is held: the stick full TOWARDS the piece for 16 frames = push one tile (PUSH state),
 *     full AWAY = pull one tile (PULL state), full sideways (after the stick was neutral) = rotate 90 degrees (ROTATE_FURNITURE, LTURN / RTURN); releasing A lets go.
 *     Each step is ONE host transaction (kind 22 MOVE / ROTATE): the model moves at once (optimistic) and snaps back with the vanilla "bubu" puff when the host refuses.
 *     B in front of a piece (not grabbing) = pick it up (vanilla: B picks furniture up, A never does).
 *   - remote moves (message 102 MOVE): the model slides / turns from its old pose.
 * NOTHING here writes a field tile, a deposit bit or a house: the tiles are the host's (FIELD_UPDATE), the markers come from pc_net_game.c. */
#include "pc_outdoor_furniture.h"
#include "pc_field_authority.h"
#include "pc_net_game.h"
#include "pc_test_hooks.h"

#include "m_actor.h"
#include "m_play.h"
#include "m_player.h"
#include "m_player_lib.h"
#include "m_name_table.h"
#include "m_field_info.h"
#include "m_collision_bg.h"
#include "m_controller.h"
#include "m_common_data.h"
#include "m_room_type.h"
#include "m_ftr_def.h" /* FTR_NUM */
#include "ac_furniture.h"
#include "ef_effect_control.h"
#include "audio.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ac_my_room.c (TARGET_PC block) */
extern int  aMR_PC_StandaloneCt(FTR_ACTOR* ftr_actor, int item, const xyz_t* pos, u8* bank);
extern void aMR_PC_StandaloneAnimate(FTR_ACTOR* ftr_actor);
extern void aMR_PC_StandaloneDraw(FTR_ACTOR* ftr_actor, GAME* game, u8* bank);
extern void aMR_PC_StandaloneDt(FTR_ACTOR* ftr_actor, u8* bank);

#define PC_OFURN_SLOTS       160  /* models drawn at once (the nearest pieces within PC_OFURN_RANGE_UT) */
#define PC_OFURN_COLLIDE_MAX 24   /* collision boxes at once (the move-bg registry holds 64 for the whole scene) */
#define PC_OFURN_RANGE_UT    24   /* tiles from the player (1.5 acres): what the camera can show */
#define PC_OFURN_COLLIDE_SQ  (160.0f * 160.0f)
#define PC_OFURN_BANK        0x800
#define PC_OFURN_SLIDE_MS    800u /* a push / pull slide: the vanilla PUSH1 / PULL1 clip (25 keyframes at speed 0.5, ~50 frames) */
#define PC_OFURN_TURN_MS     270u /* a 90 degree turn: aMR_FtrRotate's 5.6 degrees a frame (~16 frames) */

typedef struct PCOfurnSlot {
    int      used;
    uint8_t  ux, uz;
    uint16_t item;
    uint8_t  shape;
    int      collide_idx;
    xyz_t    target;          /* the model position of the footprint */
    s16      target_angle;
    int      sliding;         /* a remote MOVE: from the old pose */
    xyz_t    from;
    s16      from_angle;
    uint32_t t0, dur;
    FTR_ACTOR ftr;
    u8       bank[PC_OFURN_BANK] __attribute__((aligned(32)));
} PCOfurnSlot;

static PCOfurnSlot s_slot[PC_OFURN_SLOTS];
static PCOfurnSlot s_test_slot; /* TEST-ONLY draw cycle */
static GAME_PLAY*  s_play_seen = NULL;
static uint32_t    s_view_gen_seen = 0xFFFFFFFFu;
static uint32_t    s_frame = 0;
static int         s_bank_ok = -1;

/* ---- the GRAB driver state (C2) ---- */
#define PCOG_FTR_NO 0x7F0F /* the ftr_no the player's hold / push / pull / rotate states carry back to pc_ofurn_grab_judge (no My_Room furniture index) */
static struct {
    int           active;
    PCOfurnMarker mk;          /* the grabbed piece as the driver last knew it */
    int           face;        /* the player's facing (dir4) = the direction from the player to the piece */
    int           rotate_only; /* a C1 host: no MOVE */
    int           phase;       /* 0 holding, 2 pushing, 3 pulling, 4 / 5 rotating (the judge codes) */
    float         push_t, pull_t; /* frames the stick has been held full towards / away */
    int           bubu_dir;    /* no retry until the stick leaves this direction (-1 none) */
    int           allow_rot;   /* re-armed by a neutral stick */
    uint32_t      start_ms;
    int           seen_state;  /* the player entered HOLD at least once */
    /* the op in flight */
    int           op;          /* 0 none, PC_OFURN_OP_MOVE / ROTATE */
    uint32_t      op_serial;
    PCOfurnMarker op_to;       /* the marker as it will be when APPLIED */
    int           release;     /* end the grab at the next judge (a refusal after a move) */
    /* the optimistic presentation: the model of `pres_from` / `pres_to` (origin + item) is drawn at the interpolated pose */
    int           pres;
    uint8_t       pf_x, pf_z, pt_x, pt_z;
    uint16_t      pf_item, pt_item;
    xyz_t         p_from, p_to;
    s16           a_from, a_to;
    uint32_t      p_t0, p_dur;
    xyz_t         pull_target; /* the player's end position of a pull */
    /* TEST-ONLY input override (AC_TEST_OFURN_GRAB) */
    int           test_on, test_a;
    float         test_x, test_y;
} s_grab;

static int pc_ofurn_draw_enabled(void) {
    if (s_bank_ok < 0) {
        s_bank_ok = aMR_PC_StandaloneBankSize() <= PC_OFURN_BANK ? 1 : 0;
        if (!s_bank_ok) {
            printf("[OFURN][DRAW] *** the furniture bank (%d B) is larger than the renderer's (%d B): placed furniture stays as leaves ***\n", aMR_PC_StandaloneBankSize(),
                   PC_OFURN_BANK);
        }
    }
    return s_bank_ok == 1;
}

static s16 pc_ofurn_rot_angle(int rot) {
    return (s16)(uint16_t)((unsigned)(rot & 3) << 14); /* aMR_angle_table: rot * 90 degrees */
}

/* the model pose of a footprint (x / z from pc_ofurn_fp_model_pos, y = the ground there) */
static int pc_ofurn_model_pose(int fp, int ux, int uz, xyz_t* pos) {
    float x, z;
    xyz_t c;
    if (!mFI_UtNum2CenterWpos(&c, ux, uz)) {
        return 0;
    }
    pc_ofurn_fp_model_pos(fp, ux, uz, &x, &z);
    pos->x = x;
    pos->z = z;
    pos->y = mCoBG_GetBgY_OnlyCenter_FromWpos(c, 0.0f); /* the static ground (never the top of a placed piece's own move-bg box) */
    return 1;
}

static void pc_ofurn_slot_release(PCOfurnSlot* s, const char* why) {
    if (!s->used) {
        return;
    }
    if (s->collide_idx >= 0) {
        mCoBG_CrossOffMoveBg(s->collide_idx);
        s->collide_idx = -1;
    }
    aMR_PC_StandaloneDt(&s->ftr, s->bank);
    if (why != NULL) {
        printf("[OFURN][DRAW] model released: tile (%u,%u) item 0x%04X (%s)\n", (unsigned)s->ux, (unsigned)s->uz, (unsigned)s->item, why);
    }
    s->used = 0;
}

static void pc_ofurn_release_all(const char* why) {
    int i;
    for (i = 0; i < PC_OFURN_SLOTS; i++) {
        pc_ofurn_slot_release(&s_slot[i], why);
    }
    pc_ofurn_slot_release(&s_test_slot, NULL);
}

static int pc_ofurn_slot_ct(PCOfurnSlot* s, const PCOfurnMarker* m, uint16_t item) {
    xyz_t pos;
    float fx, fz;
    int frot;
    uint32_t age;
    if (!pc_ofurn_model_pose((int)m->shape, (int)m->ut_x, (int)m->ut_z, &pos)) {
        return 0;
    }
    memset(s, 0, sizeof(*s));
    s->collide_idx = -1;
    if (!aMR_PC_StandaloneCt(&s->ftr, (int)item, &pos, s->bank)) {
        return 0;
    }
    s->used = 1;
    s->ux = m->ut_x;
    s->uz = m->ut_z;
    s->item = item;
    s->shape = m->shape;
    s->target = pos;
    s->target_angle = s->ftr.s_angle_y;
    if (pc_net_game_ofurn_take_slide((int)m->ut_x, (int)m->ut_z, (int)item, &fx, &fz, &frot, &age)) {
        /* a remote MOVE / ROTATE: start at the old pose (unless this player's own optimistic presentation drives the model anyway) */
        s->sliding = 1;
        s->from.x = fx;
        s->from.z = fz;
        s->from.y = pos.y;
        s->from_angle = pc_ofurn_rot_angle(frot);
        s->t0 = pc_net_game_now_ms() - age;
        s->dur = (fx == pos.x && fz == pos.z) ? PC_OFURN_TURN_MS : PC_OFURN_SLIDE_MS;
    }
    return 1;
}

/* 1 = the view holds a placed piece k whose origin tile holds exactly `item` and whose model can be drawn */
static int pc_ofurn_view_match(const PCOfurnTable* t, int k, uint16_t* item_out) {
    int acre, tile;
    uint16_t v = 0;
    const PCOfurnMarker* m = &t->m[k];
    if (!pcfa_town_ut_to_acre_tile(m->ut_x, m->ut_z, &acre, &tile) || !pcfa_get_tile(acre, tile, &v) || v != pc_ofurn_tile_value(m)) {
        return 0;
    }
    if (!aMR_PC_StandaloneDrawable((int)v)) {
        return 0;
    }
    *item_out = v;
    return 1;
}

int pc_ofurn_hide_leaf(int ut_x, int ut_z, int item) {
    const PCOfurnTable* t;
    int k;
    uint16_t v;
    if (!pcfa_scene_is_town() || !pc_ofurn_draw_enabled() || (t = pc_net_game_ofurn_view()) == NULL || (k = pc_ofurn_find(t, ut_x, ut_z)) < 0) {
        return 0;
    }
    return pc_ofurn_view_match(t, k, &v) && v == (uint16_t)item;
}

static int pc_ofurn_local_player(GAME_PLAY* play, float* x, float* z) {
    PLAYER_ACTOR* pl = GET_PLAYER_ACTOR(play);
    if (pl == NULL) {
        return 0;
    }
    *x = pl->actor_class.world.position.x;
    *z = pl->actor_class.world.position.z;
    return 1;
}

static float pc_ofurn_ease(float t) {
    if (t <= 0.0f) {
        return 0.0f;
    }
    if (t >= 1.0f) {
        return 1.0f;
    }
    return t * t * (3.0f - 2.0f * t);
}

static s16 pc_ofurn_lerp_angle(s16 a, s16 b, float t) {
    const s16 d = (s16)(b - a); /* the shortest way round */
    return (s16)(a + (s16)((float)d * t));
}

/* the pose a slot is drawn at this frame: the grab presentation, a remote slide, or its footprint */
static void pc_ofurn_slot_pose(PCOfurnSlot* s, uint32_t now) {
    xyz_t p = s->target;
    s16 a = s->target_angle;
    if (s_grab.pres && ((s->ux == s_grab.pf_x && s->uz == s_grab.pf_z && s->item == s_grab.pf_item) ||
                        (s->ux == s_grab.pt_x && s->uz == s_grab.pt_z && s->item == s_grab.pt_item))) {
        const float t = pc_ofurn_ease((float)(uint32_t)(now - s_grab.p_t0) / (float)(s_grab.p_dur ? s_grab.p_dur : 1u));
        p.x = s_grab.p_from.x + (s_grab.p_to.x - s_grab.p_from.x) * t;
        p.z = s_grab.p_from.z + (s_grab.p_to.z - s_grab.p_from.z) * t;
        p.y = s_grab.p_from.y + (s_grab.p_to.y - s_grab.p_from.y) * t;
        a = pc_ofurn_lerp_angle(s_grab.a_from, s_grab.a_to, t);
        s->sliding = 0;
    } else if (s->sliding) {
        const float t = pc_ofurn_ease((float)(uint32_t)(now - s->t0) / (float)(s->dur ? s->dur : 1u));
        p.x = s->from.x + (s->target.x - s->from.x) * t;
        p.z = s->from.z + (s->target.z - s->from.z) * t;
        a = pc_ofurn_lerp_angle(s->from_angle, s->target_angle, t);
        if (t >= 1.0f) {
            s->sliding = 0;
        }
    }
    s->ftr.last_position = s->ftr.position;
    s->ftr.position = p;
    s->ftr.s_angle_y = a;
}

static void pc_ofurn_draw_test_cycle(GAME_PLAY* play, float px, float pz);
static void pc_ofurn_grab_tick(GAME_PLAY* play);

void pc_ofurn_bgitem_move(GAME_PLAY* play) {
    const PCOfurnTable* t;
    float px, pz;
    int i, k, n_col = 0;
    uint32_t now;
    static int want_k[PC_OFURN_SLOTS];
    static uint16_t want_item[PC_OFURN_SLOTS];
    static float want_d[PC_OFURN_SLOTS];
    int n_want = 0;
    if (!pcfa_scene_is_town() || !pc_ofurn_draw_enabled() || !pc_ofurn_local_player(play, &px, &pz)) {
        pc_ofurn_release_all(NULL);
        s_grab.active = 0;
        s_grab.pres = 0;
        return;
    }
    if (play != s_play_seen) {
        int j;
        for (j = 0; j < PC_OFURN_SLOTS; j++) {
            s_slot[j].collide_idx = -1; /* a new play scene re-initialised the move-bg registry (m_play.c mCoBG_InitMoveBgData): nothing of ours is in it */
            s_slot[j].used = 0;
        }
        s_test_slot.used = 0;
        s_play_seen = play;
        s_view_gen_seen = 0xFFFFFFFFu;
        memset(&s_grab, 0, sizeof(s_grab));
    }
    s_frame++;
    now = pc_net_game_now_ms();
    pc_ofurn_draw_test_cycle(play, px, pz);
    pc_ofurn_grab_tick(play);
    t = pc_net_game_ofurn_view();
    if (t == NULL) {
        pc_ofurn_release_all("no view");
        return;
    }
    if (t->gen != s_view_gen_seen) {
        s_view_gen_seen = t->gen;
        mFI_SetFGUpData(); /* the leaf-hide decision of the field-item draw tables changed */
    }
    if ((s_frame % 6u) == 0u || s_grab.pres) {
        const int pux = (int)(px / mFI_UT_WORLDSIZE_X_F), puz = (int)(pz / mFI_UT_WORLDSIZE_Z_F);
        /* the wanted set: the nearest placed pieces in range whose origin tile still holds them */
        for (k = 0; k < t->n; k++) {
            uint16_t v;
            const int dx = (int)t->m[k].ut_x - pux, dz = (int)t->m[k].ut_z - puz;
            float d;
            if (dx < -PC_OFURN_RANGE_UT || dx > PC_OFURN_RANGE_UT || dz < -PC_OFURN_RANGE_UT || dz > PC_OFURN_RANGE_UT || !pc_ofurn_view_match(t, k, &v)) {
                continue;
            }
            d = (float)(dx * dx + dz * dz);
            if (n_want < PC_OFURN_SLOTS) {
                want_k[n_want] = k;
                want_item[n_want] = v;
                want_d[n_want] = d;
                n_want++;
            } else {
                int far = 0, j;
                for (j = 1; j < n_want; j++) {
                    if (want_d[j] > want_d[far]) {
                        far = j;
                    }
                }
                if (d < want_d[far]) {
                    want_k[far] = k;
                    want_item[far] = v;
                    want_d[far] = d;
                }
            }
        }
        /* release what is no longer wanted / changed */
        for (i = 0; i < PC_OFURN_SLOTS; i++) {
            int keep = 0, j;
            if (!s_slot[i].used) {
                continue;
            }
            for (j = 0; j < n_want && !keep; j++) {
                keep = t->m[want_k[j]].ut_x == s_slot[i].ux && t->m[want_k[j]].ut_z == s_slot[i].uz && want_item[j] == s_slot[i].item &&
                       t->m[want_k[j]].shape == s_slot[i].shape;
            }
            if (!keep) {
                pc_ofurn_slot_release(&s_slot[i], "gone / moved / changed / out of range");
            }
        }
        /* construct what is new */
        for (k = 0; k < n_want; k++) {
            int have = 0, free_i = -1;
            const PCOfurnMarker* m = &t->m[want_k[k]];
            for (i = 0; i < PC_OFURN_SLOTS; i++) {
                if (s_slot[i].used && s_slot[i].ux == m->ut_x && s_slot[i].uz == m->ut_z) {
                    have = 1;
                    break;
                }
                if (!s_slot[i].used && free_i < 0) {
                    free_i = i;
                }
            }
            if (!have && free_i >= 0) {
                if (pc_ofurn_slot_ct(&s_slot[free_i], m, want_item[k])) {
                    printf("[OFURN][DRAW] model built: tile (%u,%u) item 0x%04X rot %u shape %d fp %u slot %d%s\n", (unsigned)m->ut_x, (unsigned)m->ut_z,
                           (unsigned)want_item[k], (unsigned)(want_item[k] & 3u), aMR_PC_FtrItemShape((int)want_item[k]), (unsigned)m->shape, free_i,
                           s_slot[free_i].sliding ? " (slides from its old pose)" : "");
                }
            }
        }
    }
    /* the pose (presentation / slide), collision: the nearest pieces only; the presentation tick (skeleton / texture animation) */
    for (i = 0; i < PC_OFURN_SLOTS; i++) {
        PCOfurnSlot* s = &s_slot[i];
        float dx, dz;
        if (!s->used) {
            continue;
        }
        pc_ofurn_slot_pose(s, now);
        aMR_PC_StandaloneAnimate(&s->ftr);
        dx = s->ftr.position.x - px;
        dz = s->ftr.position.z - pz;
        if (dx * dx + dz * dz <= PC_OFURN_COLLIDE_SQ && n_col < PC_OFURN_COLLIDE_MAX) {
            n_col++;
            if (s->collide_idx == -1) {
                const int type = aMR_PC_StandaloneMoveBgType((int)s->item);
                if (type >= 0 && type < mCoBG_FTR_TYPE_NUM) {
                    s->collide_idx = mCoBG_RegistMoveBg(&s->ftr.bg_register, &s->ftr.position, &s->ftr.last_position, &s->ftr.s_angle_y, 100.0f, NULL, &s->ftr.collision_scale,
                                                        &s->ftr.bg_contact, &s->ftr.base_position, type, mCoBG_ATTRIBUTE_STONE, 81.0f);
                    printf("[OFURN][COL] collision %s: tile (%u,%u) item 0x%04X type %d fp %u move_bg %d\n", s->collide_idx >= 0 ? "registered" : "REFUSED (registry full)",
                           (unsigned)s->ux, (unsigned)s->uz, (unsigned)s->item, type, (unsigned)s->shape, s->collide_idx);
                    if (s->collide_idx == -1) {
                        s->collide_idx = -2; /* do not retry every frame; a later release resets it */
                    }
                }
            }
        } else if (s->collide_idx >= 0) {
            mCoBG_CrossOffMoveBg(s->collide_idx);
            s->collide_idx = -1;
        }
    }
    if ((s_frame % 1800u) == 1u) {
        int used = 0, col = 0;
        for (i = 0; i < PC_OFURN_SLOTS; i++) {
            used += s_slot[i].used ? 1 : 0;
            col += (s_slot[i].used && s_slot[i].collide_idx >= 0) ? 1 : 0;
        }
        if (used > 0 || t->n > 0) {
            printf("[OFURN][DRAW] status: %d placed piece(s) known, %d model(s) drawn, %d collision box(es)\n", t->n, used, col);
        }
    }
}

void pc_ofurn_bgitem_draw(GAME_PLAY* play) {
    int i;
    GAME* game = (GAME*)play;
    if (!pcfa_scene_is_town() || play != s_play_seen) {
        return;
    }
    for (i = 0; i < PC_OFURN_SLOTS; i++) {
        if (s_slot[i].used) {
            aMR_PC_StandaloneDraw(&s_slot[i].ftr, game, s_slot[i].bank);
        }
    }
    if (s_test_slot.used) {
        aMR_PC_StandaloneDraw(&s_test_slot.ftr, game, s_test_slot.bank);
    }
}

void pc_ofurn_bgitem_destruct(void) {
    pc_ofurn_release_all(NULL);
    s_play_seen = NULL;
    memset(&s_grab, 0, sizeof(s_grab));
}

/* ================================================================ C2: the GRAB driver ================================================================ */

static int pc_ofurn_a_held(void) {
    return s_grab.test_on ? s_grab.test_a : chkButton(BUTTON_A) != 0;
}

/* the stick in WORLD directions (the town camera looks north: stick up = north = -z): the full direction (dir4) or -1; *neutral = 1 when centred */
static int pc_ofurn_stick_dir(int* neutral) {
    float x, y, r;
    if (s_grab.test_on) {
        x = s_grab.test_x;
        y = s_grab.test_y;
        r = sqrtf(x * x + y * y);
    } else if (gamePT != NULL) {
        x = gamePT->mcon.move_pX;
        y = gamePT->mcon.move_pY;
        r = gamePT->mcon.move_pR;
    } else {
        *neutral = 1;
        return -1;
    }
    *neutral = fabsf(x) <= 0.086f && fabsf(y) <= 0.086f; /* aMR_CheckControllerNeutral */
    if (r < 0.8f) {
        return -1; /* aMR_JudgeStickFull: move_pR >= 0.8 and the axis past 0.8 */
    }
    if (x >= 0.8f) {
        return 1;
    }
    if (x <= -0.8f) {
        return 3;
    }
    if (y >= 0.8f) {
        return 2;
    }
    if (y <= -0.8f) {
        return 0;
    }
    return -1;
}

static void pc_ofurn_grab_bubu(GAME* game, PLAYER_ACTOR* pl, int dir) {
    if (eEC_CLIP != NULL && pl != NULL) {
        eEC_CLIP->effect_make_proc(eEC_EFFECT_BUBU, pl->actor_class.world.position, 2, 0, game, RSV_NO, 0, 0); /* aMR_SetBubu: the vanilla "won't budge" puff */
    }
    s_grab.push_t = 0.0f;
    s_grab.pull_t = 0.0f;
    s_grab.bubu_dir = dir;
}

static void pc_ofurn_grab_end(const char* why) {
    if (s_grab.active) {
        printf("[OFURN][GRAB] released the piece at (%u,%u) (%s)\n", (unsigned)s_grab.mk.ut_x, (unsigned)s_grab.mk.ut_z, why);
    }
    s_grab.active = 0;
    s_grab.phase = 0;
}

/* starts the optimistic presentation of `from` -> `to` (the model of either key is drawn at the interpolated pose) */
static void pc_ofurn_pres_start(const PCOfurnMarker* from, const PCOfurnMarker* to, uint32_t dur) {
    s_grab.pres = 1;
    s_grab.pf_x = from->ut_x;
    s_grab.pf_z = from->ut_z;
    s_grab.pf_item = pc_ofurn_tile_value(from);
    s_grab.pt_x = to->ut_x;
    s_grab.pt_z = to->ut_z;
    s_grab.pt_item = pc_ofurn_tile_value(to);
    (void)pc_ofurn_model_pose((int)from->shape, (int)from->ut_x, (int)from->ut_z, &s_grab.p_from);
    (void)pc_ofurn_model_pose((int)to->shape, (int)to->ut_x, (int)to->ut_z, &s_grab.p_to);
    s_grab.a_from = pc_ofurn_rot_angle((int)from->rot);
    s_grab.a_to = pc_ofurn_rot_angle((int)to->rot);
    s_grab.p_t0 = pc_net_game_now_ms();
    s_grab.p_dur = dur;
}

/* a refusal: the model goes back where it was (the current pose -> the old footprint, quickly) */
static void pc_ofurn_pres_rollback(void) {
    const uint32_t now = pc_net_game_now_ms();
    const float t = pc_ofurn_ease((float)(uint32_t)(now - s_grab.p_t0) / (float)(s_grab.p_dur ? s_grab.p_dur : 1u));
    xyz_t cur;
    s16 a;
    cur.x = s_grab.p_from.x + (s_grab.p_to.x - s_grab.p_from.x) * t;
    cur.y = s_grab.p_from.y + (s_grab.p_to.y - s_grab.p_from.y) * t;
    cur.z = s_grab.p_from.z + (s_grab.p_to.z - s_grab.p_from.z) * t;
    a = pc_ofurn_lerp_angle(s_grab.a_from, s_grab.a_to, t);
    s_grab.p_to = s_grab.p_from;
    s_grab.a_to = s_grab.a_from;
    s_grab.p_from = cur;
    s_grab.a_from = a;
    s_grab.pt_x = s_grab.pf_x;
    s_grab.pt_z = s_grab.pf_z;
    s_grab.pt_item = s_grab.pf_item;
    s_grab.p_t0 = now;
    s_grab.p_dur = 150u;
}

/* the per-frame part (from the field-item actor): the op outcome, the end of a presentation, a grab the player state left */
static void pc_ofurn_grab_tick(GAME_PLAY* play) {
    const uint32_t now = pc_net_game_now_ms();
    if (s_grab.op != 0) {
        int reason = 0;
        const int oc = pc_net_game_ofurn_op_outcome(s_grab.op_serial, &reason);
        if (oc == 1) {
            printf("[OFURN][GRAB] %s APPLIED: (%u,%u) rot %u -> (%u,%u) rot %u\n", s_grab.op == PC_OFURN_OP_MOVE ? "MOVE" : "ROTATE", (unsigned)s_grab.mk.ut_x,
                   (unsigned)s_grab.mk.ut_z, (unsigned)s_grab.mk.rot, (unsigned)s_grab.op_to.ut_x, (unsigned)s_grab.op_to.ut_z, (unsigned)s_grab.op_to.rot);
            s_grab.mk = s_grab.op_to;
            s_grab.op = 0;
        } else if (oc == 2) {
            printf("[OFURN][GRAB] %s REFUSED by the host (reason %d): the piece snaps back\n", s_grab.op == PC_OFURN_OP_MOVE ? "MOVE" : "ROTATE", reason);
            if (s_grab.pres) {
                pc_ofurn_pres_rollback();
            }
            pc_net_game_ofurn_notice_text(s_grab.op == PC_OFURN_OP_MOVE ? "It won't budge" : "It won't turn there");
            if (s_grab.op == PC_OFURN_OP_MOVE) {
                s_grab.release = 1; /* the player already stepped: let go (the piece is back on its old cells) */
            }
            s_grab.op = 0;
        }
    }
    if (s_grab.pres && s_grab.op == 0 && (uint32_t)(now - s_grab.p_t0) >= s_grab.p_dur + 400u) {
        s_grab.pres = 0; /* the view (FIELD_UPDATE + the 102 MOVE) has the new footprint by now; a slow link: the model then jumps to it */
    }
    if (s_grab.pres && (uint32_t)(now - s_grab.p_t0) >= 4000u) {
        s_grab.pres = 0; /* never held forever */
    }
    if (s_grab.active) {
        const int idx = mPlib_get_player_actor_main_index((GAME*)play);
        const int in_grab_state =
            idx == mPlayer_INDEX_HOLD || idx == mPlayer_INDEX_PUSH || idx == mPlayer_INDEX_PULL || idx == mPlayer_INDEX_ROTATE_FURNITURE;
        if (in_grab_state) {
            s_grab.seen_state = 1;
        } else if (s_grab.seen_state || (uint32_t)(now - s_grab.start_ms) > 1000u) {
            pc_ofurn_grab_end("the player left the hold state");
        }
    }
}

/* A in front of a placed piece (the wait state): grab it. 1 = consumed */
static int pc_ofurn_grab_try(GAME* game, PLAYER_ACTOR* pl, int ux, int uz) {
    PCOfurnMarker mk;
    xyz_t pos;
    int rotate_only = 0, pux, puz;
    const int face = pc_ofurn_dir4((int)pl->actor_class.shape_info.rotation.y);
    if (!pc_net_game_ofurn_marker_at(ux, uz, &mk)) {
        return 0;
    }
    if (!pc_net_game_ofurn_may_move(&rotate_only)) {
        pc_net_game_ofurn_notice_text(pc_ofurn_reason_text(PC_OFURN_REASON_NOT_ALLOWED));
        return 1; /* consumed: a guest's A press does nothing else on a placed piece */
    }
    if (s_grab.op != 0) {
        return 1; /* the previous step is still on its way */
    }
    pux = (int)(pl->actor_class.world.position.x / mFI_UT_WORLDSIZE_X_F);
    puz = (int)(pl->actor_class.world.position.z / mFI_UT_WORLDSIZE_Z_F);
    if (!mFI_UtNum2CenterWpos(&pos, pux, puz)) {
        return 0;
    }
    pos.y = pl->actor_class.world.position.y;
    /* the vanilla grab: the player snaps to its tile centre facing the piece (aMR_SetNicePos) and holds it (index = Player_actor_wall_angle: N 0, W 1, S 2, E 3) */
    if (!mPlib_request_main_hold_type1(game, PCOG_FTR_NO, (face + 2) & 3, &pos)) {
        return 0;
    }
    memset(&s_grab.mk, 0, sizeof(s_grab.mk));
    s_grab.active = 1;
    s_grab.mk = mk;
    s_grab.face = face;
    s_grab.rotate_only = rotate_only;
    s_grab.phase = 0;
    s_grab.push_t = s_grab.pull_t = 0.0f;
    s_grab.bubu_dir = -1;
    s_grab.allow_rot = 0;
    s_grab.start_ms = pc_net_game_now_ms();
    s_grab.seen_state = 0;
    s_grab.release = 0;
    printf("[OFURN][GRAB] grabbed the piece at (%u,%u) item 0x%04X fp %u facing %d%s\n", (unsigned)mk.ut_x, (unsigned)mk.ut_z, (unsigned)pc_ofurn_tile_value(&mk),
           (unsigned)mk.shape, face, rotate_only ? " (a C1 host: rotate only)" : "");
    return 1;
}

static int pc_ofurn_grab_step_move(GAME* game, PLAYER_ACTOR* pl, int pull, xyz_t* wpos0, xyz_t* wpos1) {
    const int dir = pull ? (s_grab.face + 2) & 3 : s_grab.face;
    int nox, noz, fx, fz, r, dx, dz;
    const int pux = (int)(pl->actor_class.world.position.x / mFI_UT_WORLDSIZE_X_F), puz = (int)(pl->actor_class.world.position.z / mFI_UT_WORLDSIZE_Z_F);
    PCOfurnMarker to = s_grab.mk;
    s_grab.test_x = s_grab.test_y = 0.0f; /* TEST-ONLY input: one step per scripted step */
    if (s_grab.rotate_only || !pc_ofurn_move_geometry((int)s_grab.mk.shape, (int)s_grab.mk.rot, (int)s_grab.mk.ut_x, (int)s_grab.mk.ut_z, dir, pull, pux, puz, &nox, &noz,
                                                       &fx, &fz) ||
        !pc_net_game_ofurn_cells_free(&s_grab.mk, (int)s_grab.mk.rot, nox, noz)) {
        printf("[OFURN][GRAB] %s dir %d: blocked locally (won't budge)\n", pull ? "PULL" : "PUSH", dir);
        pc_ofurn_grab_bubu(game, pl, pull ? (s_grab.face + 2) & 3 : s_grab.face);
        return 1;
    }
    if (pull) {
        /* the player steps back one tile: its end position (the vanilla pull_target_pos; no slip correction outdoors: the tile behind was checked to be free of the piece) */
        xyz_t c;
        int acre, tile, walkable = 0;
        uint16_t v = 0;
        if (pcfa_town_ut_to_acre_tile(fx, fz, &acre, &tile) && pcfa_get_tile(acre, tile, &v)) {
            /* empty ground, a dropped item or a furniture leaf: nothing the player would bump into (trees, rocks, fences, flowers, structures: blocked) */
            walkable = v == (uint16_t)EMPTY_NO || ITEM_NAME_GET_TYPE((mActor_name_t)v) == NAME_TYPE_ITEM1 ||
                       (pc_ofurn_is_ftr(v) && !pc_net_game_ofurn_placed_at(fx, fz));
        }
        if (!walkable || !mFI_UtNum2CenterWpos(&c, fx, fz) ||
            fabsf(mCoBG_GetBgY_OnlyCenter_FromWpos(c, 0.0f) - pl->actor_class.world.position.y) > 10.0f) {
            printf("[OFURN][GRAB] PULL: the tile behind the player (%d,%d) is blocked\n", fx, fz);
            pc_ofurn_grab_bubu(game, pl, (s_grab.face + 2) & 3);
            return 1;
        }
    }
    r = pc_net_game_ofurn_move((int)s_grab.mk.ut_x, (int)s_grab.mk.ut_z, dir, pull);
    if (r < 0) {
        return 1; /* busy: try again next frame */
    }
    if (r == 0) {
        pc_ofurn_grab_bubu(game, pl, pull ? (s_grab.face + 2) & 3 : s_grab.face);
        return 1;
    }
    to.ut_x = (uint8_t)nox;
    to.ut_z = (uint8_t)noz;
    s_grab.op = PC_OFURN_OP_MOVE;
    s_grab.op_serial = pc_net_game_ofurn_op_serial();
    s_grab.op_to = to;
    pc_ofurn_pres_start(&s_grab.mk, &to, PC_OFURN_SLIDE_MS);
    sAdo_OngenTrgStart(NA_SE_STONECOIN_DRAG, &s_grab.p_from); /* outdoors there is no room floor (aMR_SetMoveSE's sAdo_FloorTrgStart): the drag sound */
    pc_ofurn_dir_delta(dir, &dx, &dz);
    if (wpos0 != NULL && wpos1 != NULL) {
        wpos0->x = pl->actor_class.world.position.x + (float)dx * mFI_UT_WORLDSIZE_X_F;
        wpos0->y = pl->actor_class.world.position.y;
        wpos0->z = pl->actor_class.world.position.z + (float)dz * mFI_UT_WORLDSIZE_Z_F;
        *wpos1 = *wpos0; /* no slip: ofs = wpos1 - wpos0 = 0, the PULL1 root motion moves the player one tile, settle snaps it to wpos1 */
    }
    s_grab.phase = pull ? 3 : 2;
    s_grab.push_t = s_grab.pull_t = 0.0f;
    printf("[OFURN][GRAB] %s dir %d: (%u,%u) -> (%d,%d) (request sent, the model slides now)\n", pull ? "PULL" : "PUSH", dir, (unsigned)s_grab.mk.ut_x,
           (unsigned)s_grab.mk.ut_z, nox, noz);
    return pull ? 3 : 2;
}

static int pc_ofurn_grab_step_rotate(GAME* game, PLAYER_ACTOR* pl, int left) {
    /* vanilla aMR_PlaceKurukuruFurniture: L = +90 (judge 5, LTURN1), R = -90 (judge 4, RTURN1) */
    const int nrot = ((int)s_grab.mk.rot + (left ? 1 : 3)) & 3;
    PCOfurnMarker to = s_grab.mk;
    int r;
    const int pux = (int)(pl->actor_class.world.position.x / mFI_UT_WORLDSIZE_X_F), puz = (int)(pl->actor_class.world.position.z / mFI_UT_WORLDSIZE_Z_F);
    s_grab.allow_rot = 0;
    s_grab.test_x = s_grab.test_y = 0.0f; /* TEST-ONLY input: one step per scripted step */
    if (pc_ofurn_fp_covers((int)s_grab.mk.shape, nrot, (int)s_grab.mk.ut_x, (int)s_grab.mk.ut_z, pux, puz) ||
        !pc_net_game_ofurn_cells_free(&s_grab.mk, nrot, (int)s_grab.mk.ut_x, (int)s_grab.mk.ut_z)) {
        /* vanilla aMR_RotatePermission: never into the player's own cell, never onto an occupied one */
        printf("[OFURN][GRAB] ROTATE: blocked locally (no room to turn)\n");
        pc_ofurn_grab_bubu(game, pl, -1);
        return 1;
    }
    r = pc_net_game_ofurn_rotate_to((int)s_grab.mk.ut_x, (int)s_grab.mk.ut_z, nrot);
    if (r <= 0) {
        if (r == 0) {
            pc_ofurn_grab_bubu(game, pl, -1);
        }
        return 1;
    }
    to.rot = (uint8_t)nrot;
    s_grab.op = PC_OFURN_OP_ROTATE;
    s_grab.op_serial = pc_net_game_ofurn_op_serial();
    s_grab.op_to = to;
    pc_ofurn_pres_start(&s_grab.mk, &to, PC_OFURN_TURN_MS);
    sAdo_OngenTrgStart(NA_SE_ROTATE, &s_grab.p_from);
    s_grab.phase = left ? 5 : 4;
    printf("[OFURN][GRAB] ROTATE %s: (%u,%u) rot %u -> %d (request sent, the model turns now)\n", left ? "left (+90)" : "right (-90)", (unsigned)s_grab.mk.ut_x,
           (unsigned)s_grab.mk.ut_z, (unsigned)s_grab.mk.rot, nrot);
    return s_grab.phase;
}

/* THE judge (m_player_main_hold.c_inc / m_player_main_rotate_furniture.c_inc when there is no My_Room actor): the vanilla aMR_JudgePlayerAction codes
 * 0 release (-> wait), 1 hold, 2 push, 3 pull (wpos0 / wpos1 = the player's start / end), 4 rotate right, 5 rotate left */
int pc_ofurn_grab_judge(xyz_t* wpos0, xyz_t* wpos1, int ftr_no) {
    GAME* game = gamePT;
    PLAYER_ACTOR* pl;
    int dir, neutral = 1;
    float dt;
    if (!s_grab.active || ftr_no != PCOG_FTR_NO || game == NULL || !pcfa_scene_is_town()) {
        return 0;
    }
    pl = GET_PLAYER_ACTOR((GAME_PLAY*)game);
    if (pl == NULL) {
        return 0;
    }
    dt = (float)game->graph->dt_num_60fps_frames;
    if (s_grab.phase == 4 || s_grab.phase == 5) {
        if (s_grab.pres && (uint32_t)(pc_net_game_now_ms() - s_grab.p_t0) < s_grab.p_dur) {
            return s_grab.phase; /* the turn is still under way (the rotate state waits for a different answer) */
        }
        s_grab.phase = 0;
        return 1;
    }
    if (s_grab.phase == 2 || s_grab.phase == 3) {
        s_grab.phase = 0; /* the push / pull clip ended: the player is holding again (the step's answer may still be on its way) */
    }
    if (s_grab.release) {
        s_grab.release = 0;
        pc_ofurn_grab_end("the host refused the step");
        return 0;
    }
    if (!pc_ofurn_a_held()) {
        pc_ofurn_grab_end("A released");
        return 0;
    }
    if (s_grab.op == 0) {
        PCOfurnMarker now_mk;
        if (!pc_net_game_ofurn_marker_at((int)s_grab.mk.ut_x, (int)s_grab.mk.ut_z, &now_mk) || now_mk.ut_x != s_grab.mk.ut_x || now_mk.ut_z != s_grab.mk.ut_z) {
            if (!s_grab.pres) {
                pc_ofurn_grab_end("the piece is gone / was moved by somebody else");
                return 0;
            }
            return 1; /* the view has not caught up with our own step yet */
        }
        s_grab.mk = now_mk;
    } else {
        return 1; /* one step at a time: wait for the host's answer */
    }
    dir = pc_ofurn_stick_dir(&neutral);
    if (neutral) {
        s_grab.allow_rot = 1;
        s_grab.bubu_dir = -1;
    }
    if (dir != s_grab.bubu_dir) {
        s_grab.bubu_dir = -1;
    }
    if (dir == s_grab.face && s_grab.bubu_dir < 0) {
        s_grab.pull_t = 0.0f;
        s_grab.push_t += dt;
        if (s_grab.push_t > 16.0f) { /* aMR_MngPushDirectTimer: push_timer > 16 */
            return pc_ofurn_grab_step_move(game, pl, 0, wpos0, wpos1);
        }
        return 1;
    }
    if (dir == ((s_grab.face + 2) & 3) && s_grab.bubu_dir < 0) {
        s_grab.push_t = 0.0f;
        s_grab.pull_t += dt;
        if (s_grab.pull_t > 16.0f) {
            return pc_ofurn_grab_step_move(game, pl, 1, wpos0, wpos1);
        }
        return 1;
    }
    s_grab.push_t = s_grab.pull_t = 0.0f;
    if (dir >= 0 && s_grab.allow_rot && (dir == ((s_grab.face + 1) & 3) || dir == ((s_grab.face + 3) & 3))) {
        /* the player's left = facing + 1 (facing north, west is on the left) -> rotate left */
        return pc_ofurn_grab_step_rotate(game, pl, dir == ((s_grab.face + 1) & 3));
    }
    return 1;
}

/* the wait state (m_player_main_wait.c_inc): A in front of a placed piece = grab it, B = pick it up. 1 = consumed (the vanilla shake / pickup of this frame skipped) */
int pc_ofurn_front_press(GAME* game, int a_trigger, int b_trigger) {
    PLAYER_ACTOR* pl;
    int ux, uz, r;
    if ((!a_trigger && !b_trigger) || pc_net_game_role() == PC_NETGAME_ROLE_NONE || !pcfa_scene_is_town() || game == NULL) {
        return 0;
    }
    pl = GET_PLAYER_ACTOR((GAME_PLAY*)game);
    if (pl == NULL) {
        return 0;
    }
    pc_ofurn_front_tile(pl->actor_class.world.position.x, pl->actor_class.world.position.z, (int)pl->actor_class.shape_info.rotation.y, &ux, &uz);
    if (!pc_net_game_ofurn_placed_at(ux, uz)) {
        return 0;
    }
    r = a_trigger ? pc_ofurn_grab_try(game, pl, ux, uz) : pc_net_game_ofurn_pickup(ux, uz);
    printf("[OFURN][INPUT] %s in front of the placed piece at (%d,%d) -> %s\n", a_trigger ? "A (grab)" : "B (pick up)", ux, uz, r ? "handled" : "not handled");
    return r;
}

/* TEST-ONLY (AC_TEST_HOOKS=1): AC_TEST_OFURN_GRAB=<script> drives the grab through the REAL states with a synthetic A button / stick (the driver's input override,
 * never active in normal play): the script is a comma list of steps, each held ~1.2 s: grab (A down), push, pull, left, right, idle, release. 1 = a step is running. */
int pc_ofurn_test_grab_input(const char* script, int step) {
    char tok[16];
    const char* p = script != NULL ? script : "";
    int i = 0;
    s_grab.test_on = 1;
    s_grab.test_x = s_grab.test_y = 0.0f;
    while (*p != '\0') {
        size_t n = 0;
        while (p[n] != '\0' && p[n] != ',') {
            n++;
        }
        snprintf(tok, sizeof(tok), "%.*s", (int)(n < sizeof(tok) - 1 ? n : sizeof(tok) - 1), p);
        p += n;
        if (*p == ',') {
            p++;
        }
        if (i++ != step) {
            continue;
        }
        if (strcmp(tok, "release") == 0) {
            s_grab.test_a = 0;
            return 1;
        }
        s_grab.test_a = 1;
        if (strcmp(tok, "push") == 0 || strcmp(tok, "pull") == 0 || strcmp(tok, "left") == 0 || strcmp(tok, "right") == 0) {
            /* world direction of the step from the grab facing */
            int d = s_grab.face;
            if (strcmp(tok, "pull") == 0) {
                d = (d + 2) & 3;
            } else if (strcmp(tok, "left") == 0) {
                d = (d + 1) & 3;
            } else if (strcmp(tok, "right") == 0) {
                d = (d + 3) & 3;
            }
            s_grab.test_x = d == 1 ? 1.0f : d == 3 ? -1.0f : 0.0f;
            s_grab.test_y = d == 2 ? 1.0f : d == 0 ? -1.0f : 0.0f;
        }
        return 1;
    }
    s_grab.test_on = 0;
    s_grab.test_a = 0;
    return 0;
}

int pc_ofurn_test_grab_active(void) {
    return s_grab.active;
}

/* TEST-ONLY (AC_TEST_HOOKS=1, AC_TEST_OFURN_DRAWCYCLE=1, any role, town field): builds, draws (one frame) and tears down the model of EVERY furniture id in turn, two
 * tiles in front of the player (no field tile, no marker, no collision): ct / draw / dt of every profile incl. the ones with behaviour code must not crash. Never active
 * in normal play. */
static void pc_ofurn_draw_test_cycle(GAME_PLAY* play, float px, float pz) {
    static int idx = -1, done = 0, built = 0;
    xyz_t pos;
    if (done || pc_test_hook_getenv("AC_TEST_OFURN_DRAWCYCLE") == NULL) {
        return;
    }
    (void)play;
    if (s_test_slot.used) {
        aMR_PC_StandaloneDt(&s_test_slot.ftr, s_test_slot.bank); /* drawn for one frame */
        s_test_slot.used = 0;
    }
    if (idx < 0) {
        idx = 0;
        printf("[OFURN][DRAWTEST][TEST-ONLY] cycling the model of every furniture id (NOT active in normal play)\n");
    }
    for (; idx < FTR_NUM; idx++) {
        const int item = (int)mRmTp_FtrIdx2FtrItemNo(idx, idx & 3);
        if (aMR_PC_FtrItemShape(item) < 0) {
            continue;
        }
        pos.x = px;
        pos.z = pz + 80.0f;
        pos.y = mCoBG_GetBgY_AngleS_FromWpos(NULL, pos, 0.0f);
        memset(&s_test_slot, 0, sizeof(s_test_slot));
        s_test_slot.collide_idx = -1;
        if (aMR_PC_StandaloneCt(&s_test_slot.ftr, item, &pos, s_test_slot.bank)) {
            s_test_slot.used = 1;
            s_test_slot.item = (uint16_t)item;
            built++;
        }
        if ((idx % 100) == 0) {
            printf("[OFURN][DRAWTEST][TEST-ONLY] ftr index %d item 0x%04X shape %d drawable %d (built %d so far)\n", idx, (unsigned)item, aMR_PC_FtrItemShape(item),
                   aMR_PC_StandaloneDrawable(item), built);
        }
        idx++;
        return;
    }
    done = 1;
    printf("[OFURN][DRAWTEST][TEST-ONLY] done: %d furniture model(s) built, drawn and torn down without a crash\n", built);
}
