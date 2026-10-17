/* pc_ofurn_draw.c - OUTDOOR FURNITURE (stage C1): the PC-only renderer + collision + input seams of PLACED furniture (pc_outdoor_furniture.h).
 *
 * What it does, all from the field-item actor (bg_item_common.c_inc hooks), in the TOWN field only:
 *   - for every placed piece of the VIEW (host: the authoritative marker table; client: the mirror of message 98) near the local player whose tile still holds exactly
 *     base | rotation, ONE private FTR_ACTOR + dma bank (ac_my_room.c aMR_PC_Standalone*: the catalog's construction, a trimmed aMR_DrawOneFurniture, NEVER a
 *     move_proc: no furniture behaviour, no my_room_clip, no music / drawers / sitting);
 *   - the vanilla leaf of that tile is not drawn (pc_ofurn_hide_leaf; a leaf without a marker is untouched);
 *   - a moving-background collision box (mCoBG_RegistMoveBg, the room furniture's own shape type) for the pieces nearest to the local player;
 *   - A in front of a placed piece = Rotate, B = Pick up (pc_ofurn_front_press, the wait-state seam of m_player_main_wait.c_inc).
 * NOTHING here writes a field tile, a deposit bit or a house: the tile is the host's (FIELD_UPDATE), the markers come from pc_net_game.c. */
#include "pc_outdoor_furniture.h"
#include "pc_field_authority.h"
#include "pc_net_game.h"
#include "pc_test_hooks.h"

#include "m_actor.h"
#include "m_play.h"
#include "m_player_lib.h"
#include "m_name_table.h"
#include "m_field_info.h"
#include "m_collision_bg.h"
#include "m_room_type.h"
#include "m_ftr_def.h" /* FTR_NUM */
#include "ac_furniture.h"

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

typedef struct PCOfurnSlot {
    int      used;
    uint8_t  ux, uz;
    uint16_t item;
    int      collide_idx;
    FTR_ACTOR ftr;
    u8       bank[PC_OFURN_BANK] __attribute__((aligned(32)));
} PCOfurnSlot;

static PCOfurnSlot s_slot[PC_OFURN_SLOTS];
static PCOfurnSlot s_test_slot; /* TEST-ONLY draw cycle */
static GAME_PLAY*  s_play_seen = NULL;
static uint32_t    s_view_gen_seen = 0xFFFFFFFFu;
static uint32_t    s_frame = 0;
static int         s_bank_ok = -1;

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

static int pc_ofurn_slot_ct(PCOfurnSlot* s, int ux, int uz, uint16_t item) {
    xyz_t pos;
    if (!mFI_UtNum2CenterWpos(&pos, ux, uz)) {
        return 0;
    }
    pos.y = mCoBG_GetBgY_AngleS_FromWpos(NULL, pos, 0.0f);
    memset(s, 0, sizeof(*s));
    s->collide_idx = -1;
    if (!aMR_PC_StandaloneCt(&s->ftr, (int)item, &pos, s->bank)) {
        return 0;
    }
    s->used = 1;
    s->ux = (uint8_t)ux;
    s->uz = (uint8_t)uz;
    s->item = item;
    return 1;
}

/* 1 = the view holds a placed piece on (ux, uz) whose tile holds exactly `item` and whose model can be drawn */
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

static void pc_ofurn_draw_test_cycle(GAME_PLAY* play, float px, float pz);

void pc_ofurn_bgitem_move(GAME_PLAY* play) {
    const PCOfurnTable* t;
    float px, pz;
    int i, k, n_col = 0;
    static int want_k[PC_OFURN_SLOTS];
    static uint16_t want_item[PC_OFURN_SLOTS];
    static float want_d[PC_OFURN_SLOTS];
    int n_want = 0;
    if (!pcfa_scene_is_town() || !pc_ofurn_draw_enabled() || !pc_ofurn_local_player(play, &px, &pz)) {
        pc_ofurn_release_all(NULL);
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
    }
    s_frame++;
    pc_ofurn_draw_test_cycle(play, px, pz);
    t = pc_net_game_ofurn_view();
    if (t == NULL) {
        pc_ofurn_release_all("no view");
        return;
    }
    if (t->gen != s_view_gen_seen) {
        s_view_gen_seen = t->gen;
        mFI_SetFGUpData(); /* the leaf-hide decision of the field-item draw tables changed */
    }
    if ((s_frame % 6u) == 0u) {
        const int pux = (int)(px / mFI_UT_WORLDSIZE_X_F), puz = (int)(pz / mFI_UT_WORLDSIZE_Z_F);
        /* the wanted set: the nearest placed pieces in range whose tile still holds them */
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
                keep = t->m[want_k[j]].ut_x == s_slot[i].ux && t->m[want_k[j]].ut_z == s_slot[i].uz && want_item[j] == s_slot[i].item;
            }
            if (!keep) {
                pc_ofurn_slot_release(&s_slot[i], "gone / changed / out of range");
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
                if (pc_ofurn_slot_ct(&s_slot[free_i], m->ut_x, m->ut_z, want_item[k])) {
                    printf("[OFURN][DRAW] model built: tile (%u,%u) item 0x%04X rot %u shape %d slot %d\n", (unsigned)m->ut_x, (unsigned)m->ut_z, (unsigned)want_item[k],
                           (unsigned)(want_item[k] & 3u), aMR_PC_FtrItemShape((int)want_item[k]), free_i);
                }
            }
        }
    }
    /* collision: the nearest pieces only; the presentation tick (skeleton / texture animation) */
    for (i = 0; i < PC_OFURN_SLOTS; i++) {
        PCOfurnSlot* s = &s_slot[i];
        float dx, dz;
        if (!s->used) {
            continue;
        }
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
                    printf("[OFURN][COL] collision %s: tile (%u,%u) item 0x%04X type %d move_bg %d\n", s->collide_idx >= 0 ? "registered" : "REFUSED (registry full)",
                           (unsigned)s->ux, (unsigned)s->uz, (unsigned)s->item, type, s->collide_idx);
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
}

/* A / B in front of a placed piece (the wait state): 1 = consumed */
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
    r = a_trigger ? pc_net_game_ofurn_rotate(ux, uz) : pc_net_game_ofurn_pickup(ux, uz);
    printf("[OFURN][INPUT] %s in front of the placed piece at (%d,%d) -> %s\n", a_trigger ? "A (rotate)" : "B (pick up)", ux, uz, r ? "handled" : "not handled");
    return r;
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
