/* pc_player_preview.c - menu-only player model previews; see pc_player_preview.h. The setup mirrors pc_remote_player_apply_appearance() / pc_remote_player_visual_init() and the
 * draw mirrors pc_remote_player_dw() + the title logo's skeleton-in-FONT-display-list trick (ac_animal_logo.c aAL_title_draw), so the model is the very same asset path as the
 * in-game player, with no ACTOR and no Now_Private access. */
#include "pc_player_preview.h"

#include "m_play.h"
#include "m_player_lib.h"
#include "m_name_table.h"
#include "m_needlework.h"
#include "m_common_data.h"
#include "m_rcp.h"
#include "sys_matrix.h"
#include "libultra/libultra.h"
#include "pc_platform.h"
#include "pc_character.h"
#include "m_private.h"

extern u16 pc_guest_starter_shirt(const PersonalID_c* id, int gender); /* pc_m_card.c */
extern cKF_Skeleton_R_c cKF_bs_r_boy_1; /* compiled-in decomp skeletons (same externs as pc_remote_player.c) */
extern cKF_Skeleton_R_c cKF_bs_r_grl_1;

#include <stdio.h>
#include <string.h>

#define PCPV_FACE_TEX_SIZE 0xE00

typedef struct PCPlayerPreviewSlot {
    int                  ready;
    int                  gender, face, cloth;
    cKF_SkeletonInfo_R_c kf;
    s_xyz                joint_data[mPlayer_JOINT_NUM + 1];
    s_xyz                morph_data[mPlayer_JOINT_NUM + 1];
    u8                   face_tex[PCPV_FACE_TEX_SIZE] ATTRIBUTE_ALIGN(32);
    u16                  face_pallet[mNW_PALETTE_COUNT] ATTRIBUTE_ALIGN(32);
    u8                   cloth_tex[mNW_DESIGN_TEX_SIZE] ATTRIBUTE_ALIGN(32);
    u16                  cloth_pallet[mNW_PALETTE_COUNT] ATTRIBUTE_ALIGN(32);
} PCPlayerPreviewSlot;

static PCPlayerPreviewSlot s_slot[PC_PLAYER_PREVIEW_SLOTS] ATTRIBUTE_ALIGN(32);

_Static_assert(offsetof(PCPlayerPreviewSlot, face_tex) % 32 == 0 && offsetof(PCPlayerPreviewSlot, face_pallet) % 32 == 0 && offsetof(PCPlayerPreviewSlot, cloth_tex) % 32 == 0 &&
                   offsetof(PCPlayerPreviewSlot, cloth_pallet) % 32 == 0,
               "the preview's ARAM DMA destinations must be 32-byte aligned");

int pc_player_preview_set(int slot, int gender, int face, int cloth_item) {
    PCPlayerPreviewSlot* s;
    cKF_Animation_R_c* wait_anim;
    mPr_cloth_c scratch;
    if (slot < 0 || slot >= PC_PLAYER_PREVIEW_SLOTS) {
        return 0;
    }
    s = &s_slot[slot];
    if (face < 0 || face >= mPr_FACE_TYPE_NUM) {
        face = 0;
    }
    gender = (gender == mPr_SEX_MALE) ? mPr_SEX_MALE : mPr_SEX_FEMALE;
    if (s->ready && s->gender == gender && s->face == face && s->cloth == cloth_item) {
        return 1;
    }
    if (gamePT == NULL) {
        return 0; /* the ARAM resources need the running game (pc_remote_player_apply_appearance has the same gate); retried next frame */
    }
    memset(s, 0, sizeof(*s));
    s->gender = gender;
    s->face = face;
    s->cloth = cloth_item;
    mPlib_Load_FaceTexAndPallet(s->face_tex, s->face_pallet, gender, face, 0, FALSE, FALSE);
    mPlib_change_player_cloth_info(&scratch, (mActor_name_t)cloth_item);
    mPlib_Load_PlayerTexAndPallet(s->cloth_tex, s->cloth_pallet, scratch.idx);
    wait_anim = mPlib_Get_Pointer_Animation(mPlayer_ANIM_WAIT1);
    cKF_SkeletonInfo_R_ct(&s->kf, gender == mPr_SEX_MALE ? &cKF_bs_r_boy_1 : &cKF_bs_r_grl_1, NULL, s->joint_data, s->morph_data);
    cKF_SkeletonInfo_R_init_standard_repeat_setframeandspeedandmorph(&s->kf, wait_anim, NULL, 1.0f, 0.5f, 0.0f);
    s->ready = 1;
    printf("[PC][PREVIEW] slot %d prepared: gender=%d face=%d cloth=0x%04X (menu-only player model, no actor / resident / save access)\n", slot, gender, face, (unsigned)cloth_item);
    return 1;
}

int pc_player_preview_set_character(int slot, const struct PCCharacter* c) {
    PersonalID_c pid;
    int gender, face;
    if (c == NULL) {
        return 0;
    }
    memset(&pid, 0, sizeof(pid));
    memcpy(pid.player_name, c->name_bytes, 8);
    memcpy(pid.land_name, c->home_town_bytes, 8);
    pid.player_id = c->player_id;
    pid.land_id = c->land_id;
    gender = c->gender == mPr_SEX_MALE ? mPr_SEX_MALE : mPr_SEX_FEMALE;
    face = c->face;
    return pc_player_preview_set(slot, gender, face, (int)pc_guest_starter_shirt(&pid, gender));
}

void pc_player_preview_tick(void) {
    int i;
    for (i = 0; i < PC_PLAYER_PREVIEW_SLOTS; i++) {
        if (s_slot[i].ready) {
            cKF_SkeletonInfo_R_play(&s_slot[i].kf);
        }
    }
}

void pc_player_preview_reset(void) {
    memset(s_slot, 0, sizeof(s_slot));
}

void pc_player_preview_draw(struct game_s* game_s, int slot, float cx, float cy, float height_px) {
    GAME* game = (GAME*)game_s;
    GRAPH* graph;
    PCPlayerPreviewSlot* s;
    Gfx* poly_save;
    Gfx* gfx;
    Mtx* mtx;
    f32 scale;
    if (game == NULL || game->graph == NULL || slot < 0 || slot >= PC_PLAYER_PREVIEW_SLOTS || !s_slot[slot].ready) {
        return;
    }
    s = &s_slot[slot];
    graph = game->graph;
    mtx = (Mtx*)GRAPH_ALLOC_TYPE(graph, Mtx, s->kf.skeleton->num_shown_joints);
    if (mtx == NULL) {
        return;
    }
    /* the menu's ortho projection maps 1 screen pixel to 16 units (mFont_SCALE_F); the player model is ~PCPV_MODEL_H units tall */
    scale = (height_px * 16.0f) / PC_PLAYER_PREVIEW_MODEL_H;

    Matrix_push();
    Matrix_translate((cx - 160.0f) * 16.0f, (120.0f - cy) * 16.0f, 0.0f, MTX_LOAD);
    Matrix_scale(scale, scale, scale, MTX_MULT);

    /* cKF draws into the OPAQUE polygon list; the menu overlay lives in the FONT list (drawn last): swap them around the draw, like the title logo does */
    OPEN_DISP(graph);
    poly_save = NOW_POLY_OPA_DISP;
    SET_POLY_OPA_DISP(NOW_FONT_DISP);
    CLOSE_DISP(graph);

    _texture_z_light_fog_prim(graph); /* writes into NOW_POLY_OPA_DISP = the FONT list now */

    OPEN_DISP(graph);
    gfx = NOW_POLY_OPA_DISP;
    gSPMatrix(gfx++, _Matrix_to_Mtx_new(graph), G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
    gSPSegment(gfx++, ANIME_1_TXT_SEG, (u8*)s->face_tex + 0 * 0x100);
    gSPSegment(gfx++, ANIME_2_TXT_SEG, (u8*)s->face_tex + mPlayer_EYE_TEX_NUM * 0x100);
    gSPSegment(gfx++, ANIME_3_TXT_SEG, (u8*)s->cloth_tex);
    gSPSegment(gfx++, ANIME_4_TXT_SEG, (u16*)s->cloth_pallet);
    gSPSegment(gfx++, ANIME_5_TXT_SEG, (u16*)s->face_pallet);
    SET_POLY_OPA_DISP(gfx);
    CLOSE_DISP(graph);

    cKF_Si3_draw_R_SV(game, &s->kf, mtx, NULL, NULL, NULL);

    OPEN_DISP(graph);
    SET_FONT_DISP(NOW_POLY_OPA_DISP);
    SET_POLY_OPA_DISP(poly_save);
    /* the menu text (pc_text_draw) emits modelview-transformed vertices and expects the IDENTITY modelview: put it back after the model's own matrix */
    gfx = NOW_FONT_DISP;
    gSPMatrix(gfx++, &Mtx_clear, G_MTX_MODELVIEW | G_MTX_LOAD | G_MTX_NOPUSH);
    SET_FONT_DISP(gfx);
    CLOSE_DISP(graph);

    Matrix_pull();
}
