#include "ac_shop_indoor.h"

#include "m_common_data.h"
#include "m_malloc.h"
#include "m_rcp.h"
#include "sys_matrix.h"
#ifdef TARGET_PC
#include "m_scene.h"
#include "m_play.h"
#include "m_player_lib.h"
#include "m_demo.h"
#include "m_msg.h"
#include <stdio.h>
#endif

enum {
    aSI_SHOP_TYPE_ZAKKA,
    aSI_SHOP_TYPE_CONVENI,
    aSI_SHOP_TYPE_SUPER,
    aSI_SHOP_TYPE_DEPART_1F,
    aSI_SHOP_TYPE_DEPART_2F,

    aSI_SHOP_TYPE_NUM
};

enum {
    aSI_SE_MODE_NORMAL,
    aSI_SE_MODE_SILENT,

    aSI_SE_MODE_NUM
};

static int aSI_se_mode = aSI_SE_MODE_NORMAL;

typedef struct shop_indoor_disp_s {
    Gfx* wall_gfx;
    Gfx* wall_xlu_gfx;
    Gfx* floor_gfx;
    Gfx* floor_xlu_gfx;
    Gfx* fukubiki_gfx;
    Gfx* fukubiki_xlu_gfx;
} aSI_disp_data_c;

extern Gfx rom_shop1w_model[];
extern Gfx rom_shop1w_modelT[];
extern Gfx rom_shop1f_model[];
extern Gfx rom_shop1f_modelT[];
extern Gfx rom_shop1_fuku_model[];
extern Gfx rom_shop1_fuku_modelT[];

extern Gfx rom_shop2w_model[];
extern Gfx rom_shop2w_modelT[];
extern Gfx rom_shop2f_model[];
extern Gfx rom_shop2f_modelT[];
extern Gfx rom_shop2_fuku_model[];
extern Gfx rom_shop2_fuku_modelT[];

extern Gfx rom_shop3w_model[];
extern Gfx rom_shop3w_modelT[];
extern Gfx rom_shop3f_model[];
extern Gfx rom_shop3f_modelT[];
extern Gfx rom_shop3_fuku_model[];
extern Gfx rom_shop3_fuku_modelT[];

extern Gfx rom_shop4_fuku_model[];
extern Gfx rom_shop4_fuku_modelT[];

extern Gfx rom_shop4_2w_model[];
extern Gfx rom_shop4_2w_modelT[];
extern Gfx rom_shop4_2f_model[];
extern Gfx rom_shop4_2f_modelT[];

static aSI_disp_data_c aSI_disp_data_table[] = {
    {
        rom_shop1w_model,
        rom_shop1w_modelT,
        rom_shop1f_model,
        rom_shop1f_modelT,
        rom_shop1_fuku_model,
        rom_shop1_fuku_modelT,
    },
    {
        rom_shop2w_model,
        rom_shop2w_modelT,
        rom_shop2f_model,
        rom_shop2f_modelT,
        rom_shop2_fuku_model,
        rom_shop2_fuku_modelT,
    },
    {
        rom_shop3w_model,
        rom_shop3w_modelT,
        rom_shop3f_model,
        rom_shop3f_modelT,
        rom_shop3_fuku_model,
        rom_shop3_fuku_modelT,
    },
    {
        rom_shop1w_model,
        rom_shop1w_modelT,
        rom_shop1f_model,
        rom_shop1f_modelT,
        rom_shop4_fuku_model,
        rom_shop4_fuku_modelT,
    },
    {
        rom_shop4_2w_model,
        rom_shop4_2w_modelT,
        rom_shop4_2f_model,
        rom_shop4_2f_modelT,
        rom_shop1_fuku_model,
        rom_shop1_fuku_modelT,
    },
};

static s16 aSI_wall_default_table[] = {
    (u8)ITM_WALL67, (u8)ITM_WALL68, (u8)ITM_WALL69, (u8)ITM_WALL68, (u8)ITM_WALL70,
};
static s16 aSI_floor_default_table[] = {
    (u8)ITM_CARPET67, (u8)ITM_CARPET68, (u8)ITM_CARPET69, (u8)ITM_CARPET70, (u8)ITM_CARPET70,
};

static void Shop_Indoor_Actor_ct(ACTOR* actorx, GAME* game);
static void Shop_Indoor_Actor_dt(ACTOR* actorx, GAME* game);
static void Shop_Indoor_Actor_move(ACTOR* actorx, GAME* game);
static void Shop_Indoor_Actor_draw(ACTOR* actorx, GAME* game);
#ifdef TARGET_PC
static void aSI_pc_restock_eject_reset(void);
#endif

// clang-format off
ACTOR_PROFILE Shop_Indoor_Profile = {
    mAc_PROFILE_SHOP_INDOOR,
    ACTOR_PART_FG,
    ACTOR_STATE_NO_DRAW_WHILE_CULLED | ACTOR_STATE_NO_MOVE_WHILE_CULLED,
    EMPTY_NO,
    ACTOR_OBJ_BANK_KEEP,
    sizeof(SHOP_INDOOR_ACTOR),
    &Shop_Indoor_Actor_ct,
    &Shop_Indoor_Actor_dt,
    &Shop_Indoor_Actor_move,
    &Shop_Indoor_Actor_draw,
    NULL,
};
// clang-format on

static int aSI_GetShopIndoorBank(ACTOR* actorx, GAME* game) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
    u8** floor_data_pp = shop_indoor->floor_data_p;
    u8** wall_data_pp = shop_indoor->wall_data_p;
    int i;

    for (i = 0; i < 2; i++) {
        wall_data_pp[i] = zelda_malloc_align(aMI_WALL_BANK_SIZE, 32);
    }

    for (i = 0; i < 2; i++) {
        floor_data_pp[i] = zelda_malloc_align(aMI_FLOOR_BANK_SIZE, 32);
    }
}

static void aSI_CopyFloorTexture(ACTOR* actorx, s16 floor_idx, s16 bank_idx) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
    u8** floor_data_pp = shop_indoor->floor_data_p;

    if (bank_idx == 2) {
        int i;

        for (i = 0; i < 2; i++) {
            u8* floor_data_p = floor_data_pp[i];

            if (floor_data_p != NULL) {
                mRmTp_CopyFloorData(floor_data_p, floor_idx);
            }
        }
    } else {
        u8* floor_data_p = floor_data_pp[bank_idx];

        if (floor_data_p != NULL) {
            mRmTp_CopyFloorData(floor_data_p, floor_idx);
        }
    }
}

static void aSI_CopyWallTexture(ACTOR* actorx, s16 wall_idx, s16 bank_idx) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
    u8** wall_data_pp = shop_indoor->wall_data_p;

    if (bank_idx == 2) {
        int i;

        for (i = 0; i < 2; i++) {
            u8* wall_data_p = wall_data_pp[i];

            if (wall_data_p != NULL) {
                mRmTp_CopyWallData(wall_data_p, wall_idx);
            }
        }
    } else {
        u8* wall_data_p = wall_data_pp[bank_idx];

        if (wall_data_p != NULL) {
            mRmTp_CopyWallData(wall_data_p, wall_idx);
        }
    }
}

static void aSI_ShopIndoorCopy(ACTOR* actorx, GAME* game) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;

    aSI_CopyWallTexture(actorx, (s16)aSI_wall_default_table[shop_indoor->shop_idx], 2);
    aSI_CopyFloorTexture(actorx, (s16)aSI_floor_default_table[shop_indoor->shop_idx], 2);
}

static int aSI_ChangeWall(mActor_name_t item);
static int aSI_ChangeCarpet(mActor_name_t item);
static void aSI_IndoorDmaAgain(void);
static void aSI_Change2Default(void);

static void aSI_SetClipProc(ACTOR* actorx, int clear) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;

    if (clear == TRUE) {
        Common_Get(clip).shop_indoor_clip = NULL;
    } else {
        aSI_Clip_c* clip_p = &shop_indoor->clip;

        Common_Get(clip).shop_indoor_clip = clip_p;
        clip_p->shop_indoor_actor = actorx;
        clip_p->change_wall_proc = &aSI_ChangeWall;
        clip_p->change_carpet_proc = &aSI_ChangeCarpet;
        clip_p->indoor_dma_again_proc = &aSI_IndoorDmaAgain;
        clip_p->change2default_proc = &aSI_Change2Default;
    }
}

static void Shop_Indoor_Actor_ct(ACTOR* actorx, GAME* game) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
    int i;
    int scene_no = Save_Get(scene_no);
    static int scene_no_table[] = { SCENE_SHOP0, SCENE_CONVENI, SCENE_SUPER, SCENE_DEPART, SCENE_DEPART_2 };

    for (i = 0; i < aSI_SHOP_TYPE_NUM; i++) {
        if (scene_no == scene_no_table[i]) {
            actorx->actor_specific = i;
            shop_indoor->shop_idx = i;
        }
    }

    if (Common_Get(tanuki_shop_status) == mSP_TANUKI_SHOP_STATUS_FUKUBIKI &&
        actorx->actor_specific != aSI_SHOP_TYPE_DEPART_2F) {
        shop_indoor->fukubiki_active = TRUE;
    }

    shop_indoor->floor_idx = aSI_floor_default_table[shop_indoor->shop_idx];
    shop_indoor->wall_idx = aSI_wall_default_table[shop_indoor->shop_idx];
    shop_indoor->floor_data_idx = 0;
/* @BUG - floor_data_idx is set again instead of wall_data_idx */
#ifndef BUGFIXES
    shop_indoor->floor_data_idx = 0;
#else
    shop_indoor->wall_data_idx = 0;
#endif
    shop_indoor->_17C = 0;
    shop_indoor->floor_data_p[0] = NULL;
    shop_indoor->floor_data_p[1] = NULL;
    shop_indoor->wall_data_p[0] = NULL;
    shop_indoor->wall_data_p[1] = NULL;

    aSI_GetShopIndoorBank(actorx, game);
    aSI_ShopIndoorCopy(actorx, game);
    aSI_SetClipProc(actorx, FALSE);
#ifdef TARGET_PC
    aSI_pc_restock_eject_reset(); /* a fresh shop room: no exit latched from an earlier visit */
#endif
}

static void Shop_Indoor_Actor_dt(ACTOR* actorx, GAME* game) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
    int i;

    aSI_SetClipProc(actorx, TRUE);
    for (i = 0; i < 2; i++) {
        if (shop_indoor->floor_data_p[i] != NULL) {
            zelda_free(shop_indoor->floor_data_p[i]);
        }

        if (shop_indoor->wall_data_p[i] != NULL) {
            zelda_free(shop_indoor->wall_data_p[i]);
        }
    }
}

static void aSI_DrawShopFloor(ACTOR* actorx, GAME* game) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
    int data_idx = shop_indoor->floor_data_idx;
    int shop_level = actorx->actor_specific;
    u16* pal;
    u8* tex0;
    u8* tex1;
    u8* tex2;
    u8* tex3;

    pal = (u16*)shop_indoor->floor_data_p[data_idx];
    tex0 = (u8*)(shop_indoor->floor_data_p[data_idx] + aMI_PAL_SIZE);
    tex1 = (u8*)(shop_indoor->floor_data_p[data_idx] + aMI_PAL_SIZE + aMI_TEX_SIZE);
    tex2 = (u8*)(shop_indoor->floor_data_p[data_idx] + aMI_PAL_SIZE + aMI_TEX_SIZE * 2);
    tex3 = (u8*)(shop_indoor->floor_data_p[data_idx] + aMI_PAL_SIZE + aMI_TEX_SIZE * 3);

    _texture_z_light_fog_prim_xlu(game->graph);
    _texture_z_light_fog_prim(game->graph);
    Global_kankyo_set_room_prim(game);

    if (shop_indoor->floor_data_p[data_idx] != NULL) {
        if (aSI_disp_data_table[shop_level].floor_gfx != NULL) {
            OPEN_DISP(game->graph);

            /* Load floor palette & textures */
            gSPSegment(NEXT_POLY_OPA_DISP, ANIME_1_TXT_SEG, tex0);
            gSPSegment(NEXT_POLY_OPA_DISP, ANIME_2_TXT_SEG, tex1);
            gSPSegment(NEXT_POLY_OPA_DISP, ANIME_3_TXT_SEG, tex2);
            gSPSegment(NEXT_POLY_OPA_DISP, ANIME_4_TXT_SEG, tex3);
            gSPSegment(NEXT_POLY_OPA_DISP, ANIME_5_TXT_SEG, pal);

            Matrix_translate(0.0f, 0.0f, 0.0f, MTX_LOAD);
            Matrix_scale(0.0625f, 0.0625f, 0.0625f, MTX_MULT);

            gSPMatrix(NEXT_POLY_OPA_DISP, _Matrix_to_Mtx_new(game->graph), G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
            gSPDisplayList(NEXT_POLY_OPA_DISP, aSI_disp_data_table[shop_level].floor_gfx);

            CLOSE_DISP(game->graph);
        }

        if (aSI_disp_data_table[shop_level].floor_xlu_gfx != NULL) {
            OPEN_DISP(game->graph);

            /* Load floor palette & textures */
            gSPSegment(NEXT_POLY_XLU_DISP, ANIME_1_TXT_SEG, tex0);
            gSPSegment(NEXT_POLY_XLU_DISP, ANIME_2_TXT_SEG, tex1);
            gSPSegment(NEXT_POLY_XLU_DISP, ANIME_3_TXT_SEG, tex2);
            gSPSegment(NEXT_POLY_XLU_DISP, ANIME_4_TXT_SEG, tex3);
            gSPSegment(NEXT_POLY_XLU_DISP, ANIME_5_TXT_SEG, pal);

            Matrix_translate(0.0f, 0.0f, 0.0f, MTX_LOAD);
            Matrix_scale(0.0625f, 0.0625f, 0.0625f, MTX_MULT);

            gSPMatrix(NEXT_POLY_XLU_DISP, _Matrix_to_Mtx_new(game->graph), G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
            gSPDisplayList(NEXT_POLY_XLU_DISP, aSI_disp_data_table[shop_level].floor_xlu_gfx);

            CLOSE_DISP(game->graph);
        }
    }
}

static void aSI_DrawShopFukubiki(ACTOR* actorx, GAME* game) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
    int shop_level = shop_indoor->shop_idx;
    GAME_PLAY* play = (GAME_PLAY*)game;

    if (aSI_disp_data_table[shop_level].fukubiki_gfx != NULL) {
        _texture_z_light_fog_prim(game->graph);

        if (shop_level == aSI_SHOP_TYPE_CONVENI) {
            xyz_t mirror_pos = { 10.0f, 10.0f, 10.0f };

            Setpos_HiliteReflect_init(&mirror_pos, play);
        }

        OPEN_DISP(game->graph);

        Matrix_translate(0.0f, 0.0f, 0.0f, MTX_LOAD);
        Matrix_scale(0.0625f, 0.0625f, 0.0625f, MTX_MULT);

        gSPMatrix(NEXT_POLY_OPA_DISP, _Matrix_to_Mtx_new(game->graph), G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
        Global_kankyo_set_room_prim(game);
        gSPDisplayList(NEXT_POLY_OPA_DISP, aSI_disp_data_table[shop_level].fukubiki_gfx);

        CLOSE_DISP(game->graph);
    }

    if (aSI_disp_data_table[shop_level].fukubiki_xlu_gfx != NULL) {
        _texture_z_light_fog_prim_xlu(game->graph);
        OPEN_DISP(game->graph);

        Matrix_translate(0.0f, 0.0f, 0.0f, MTX_LOAD);
        Matrix_scale(0.0625f, 0.0625f, 0.0625f, MTX_MULT);

        gSPMatrix(NEXT_POLY_XLU_DISP, _Matrix_to_Mtx_new(game->graph), G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
        gSPDisplayList(NEXT_POLY_XLU_DISP, aSI_disp_data_table[shop_level].fukubiki_xlu_gfx);

        CLOSE_DISP(game->graph);
    }
}

static void aSI_DrawShopWall(ACTOR* actorx, GAME* game) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
    int data_idx = shop_indoor->wall_data_idx;
    u16* pal = (u16*)shop_indoor->wall_data_p[data_idx];
    u8* tex0 = (u8*)(shop_indoor->wall_data_p[data_idx] + aMI_PAL_SIZE);
    u8* tex1 = (u8*)(shop_indoor->wall_data_p[data_idx] + aMI_PAL_SIZE + aMI_TEX_SIZE);
    int shop_level = actorx->actor_specific;

    if (aSI_disp_data_table[shop_level].wall_gfx != NULL) {
        GAME_PLAY* play = (GAME_PLAY*)game;

        OPEN_DISP(game->graph);

        _texture_z_light_fog_prim(game->graph);

        if (shop_level == aSI_SHOP_TYPE_CONVENI) {
            xyz_t mirror_pos = { 10.0f, 10.0f, 10.0f };

            Setpos_HiliteReflect_init(&mirror_pos, play);
        }

        /* Load floor palette & textures */
        gSPSegment(NEXT_POLY_OPA_DISP, ANIME_1_TXT_SEG, tex0);
        gSPSegment(NEXT_POLY_OPA_DISP, ANIME_2_TXT_SEG, tex1);
        gSPSegment(NEXT_POLY_OPA_DISP, ANIME_3_TXT_SEG, pal);

        Matrix_translate(0.0f, 0.0f, 0.0f, MTX_LOAD);
        Matrix_scale(0.0625f, 0.0625f, 0.0625f, MTX_MULT);

        gSPMatrix(NEXT_POLY_OPA_DISP, _Matrix_to_Mtx_new(game->graph), G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
        Global_kankyo_set_room_prim(game);
        gSPDisplayList(NEXT_POLY_OPA_DISP, aSI_disp_data_table[shop_level].wall_gfx);

        CLOSE_DISP(game->graph);
    }

    if (aSI_disp_data_table[shop_level].wall_xlu_gfx != NULL) {
        GAME_PLAY* play = (GAME_PLAY*)game;

        OPEN_DISP(game->graph);

        _texture_z_light_fog_prim_xlu(game->graph);

        /* Load floor palette & textures */
        gSPSegment(NEXT_POLY_XLU_DISP, ANIME_1_TXT_SEG, tex0);
        gSPSegment(NEXT_POLY_XLU_DISP, ANIME_2_TXT_SEG, tex1);
        gSPSegment(NEXT_POLY_XLU_DISP, ANIME_3_TXT_SEG, pal);

        Matrix_translate(0.0f, 0.0f, 0.0f, MTX_LOAD);
        Matrix_scale(0.0625f, 0.0625f, 0.0625f, MTX_MULT);

        gSPMatrix(NEXT_POLY_XLU_DISP, _Matrix_to_Mtx_new(game->graph), G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW);
        Global_kankyo_set_room_prim(game);
        gSPDisplayList(NEXT_POLY_XLU_DISP, aSI_disp_data_table[shop_level].wall_xlu_gfx);

        CLOSE_DISP(game->graph);
    }
}

static void aSI_DrawShopIndoor(ACTOR* actorx, GAME* game) {
    SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;

    if (shop_indoor->fukubiki_active == TRUE) {
        aSI_DrawShopFukubiki(actorx, game);
    } else {
        aSI_DrawShopWall(actorx, game);
        aSI_DrawShopFloor(actorx, game);
    }
}

static void Shop_Indoor_Actor_draw(ACTOR* actorx, GAME* game) {
    aSI_DrawShopIndoor(actorx, game);
}

#ifdef TARGET_PC
/* Nook's manual restock: while the host says the shop is RESTOCKING every player inside is walked out through the shop's own exit data (the one the door wrote when this
 * player came in: Common_Get(structure_exit_door_data), the same scene change a walk onto the exit tile makes). "Elegantly": half a second of grace, then as soon as the player
 * is idle (not talking, no inventory / menu open).
 * A restock seen while inside LATCHES the exit (aSI_pc_eject_pending): a player still talking to Nook when the 60 s restock ends is shown out once the talk is over (the floor
 * display shows the old catalog), never in the middle of it. The forced exit of a busy player (a stuck state) applies only while the shop is STILL closed and only after
 * aSI_PC_EJECT_HARD_CAP frames of waiting (far beyond the 60 s restock); a busy player is never forced out once the restock is over. Reset when a shop room is built. */
extern int pc_net_game_shop_restocking(void);
/* Multiplayer shop upgrade (pc_shop_upgrade.h): the announced 5-minute closure shows everyone out the same way (the announcing player once Nook's talk is over). */
extern int pc_shop_upgrade_closed(void);
#define aSI_PC_EJECT_HARD_CAP 18000
static int aSI_pc_eject_wait = 0;
static int aSI_pc_eject_pending = 0;
static void aSI_pc_restock_eject_reset(void) {
    aSI_pc_eject_wait = 0;
    aSI_pc_eject_pending = 0;
}
static void aSI_pc_restock_eject(GAME* game) {
    GAME_PLAY* play = (GAME_PLAY*)game;
    const int restocking = pc_net_game_shop_restocking() || pc_shop_upgrade_closed(); /* closed for a restock OR for the upgrade */
    int busy;

    if (restocking) {
        aSI_pc_eject_pending = 1;
    }
    if (!aSI_pc_eject_pending) {
        aSI_pc_eject_wait = 0;
        return;
    }
    if (play->fb_wipe_mode != WIPE_MODE_NONE) {
        return;
    }
    aSI_pc_eject_wait++;
    if (aSI_pc_eject_wait < 30) {
        return;
    }
    /* never cut a Nook conversation: a visible message window (any talk / choice / purchase dialogue), a demo or a non-idle player holds the exit */
    busy = mPlib_able_submenu_type1(game) == FALSE || mDemo_CheckDemo() != FALSE || mMsg_Check_MainHide(mMsg_Get_base_window_p()) == FALSE;
    if (busy && (!restocking || aSI_pc_eject_wait < aSI_PC_EJECT_HARD_CAP)) {
        /* log only: the first held frame of a latch, then every 300 frames with --verbose (g_pc_verbose precedent) */
        extern int g_pc_verbose;
        if (aSI_pc_eject_wait == 30 || (g_pc_verbose && (aSI_pc_eject_wait % 300) == 0)) {
            printf("[NET][RESTOCK] exit held: demo=%d type=%d able=%d msg_hidden=%d restocking=%d waited=%d\n", (int)mDemo_CheckDemo(), (int)mDemo_CheckDemoType(),
                   (int)mPlib_able_submenu_type1(game), (int)mMsg_Check_MainHide(mMsg_Get_base_window_p()), restocking, aSI_pc_eject_wait);
        }
        return;
    }
    if (Common_Get(structure_exit_door_data).next_scene_id == 0 || Common_Get(structure_exit_door_data).next_scene_id == play->scene_id) {
        return; /* no valid way out was recorded (a door always writes it): never warp into scene 0; keep waiting */
    }
    if (goto_other_scene(play, Common_GetPointer(structure_exit_door_data), FALSE)) {
        printf("[NET][RESTOCK] shop %s: the player was shown out (waited %d frames%s)\n",
               !restocking ? "restocked while talking" : pc_shop_upgrade_closed() ? "closed for the upgrade" : "restocking", aSI_pc_eject_wait,
               busy ? ", FORCED after the hard cap" : "");
        aSI_pc_eject_pending = 0;
    }
    aSI_pc_eject_wait = 0;
}
#endif

static void Shop_Indoor_Actor_move(ACTOR* actorx, GAME* game) {
#ifdef TARGET_PC
    aSI_pc_restock_eject(game);
#endif
}

#ifdef TARGET_PC
/* Nook preview sync (presentation only): a LOCAL preview is reported to the other players in this shop (pc_net_game.c, inert in single player); a REMOTE one (or the nested calls
 * of aSI_Change2Default) runs with aSI_pc_no_net set so it is never re-sent and plays no sound. */
extern void pc_net_game_shop_preview_local(int kind, u16 item);
static int aSI_pc_no_net = 0;
#endif

static int aSI_ChangeCarpet(mActor_name_t item) {
    if (Common_Get(clip).shop_indoor_clip != NULL) {
        ACTOR* actorx = Common_Get(clip).shop_indoor_clip->shop_indoor_actor;

        if (actorx != NULL && ITEM_IS_CARPET(item)) {
            SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
            int floor_data_idx = shop_indoor->floor_data_idx;

            shop_indoor->floor_idx = (mActor_name_t)item - ITM_CARPET_START;
            shop_indoor->floor_data_idx = (floor_data_idx ^ 1) & 1;
            aSI_CopyFloorTexture(actorx, shop_indoor->floor_idx, shop_indoor->floor_data_idx);

            if (aSI_se_mode == aSI_SE_MODE_NORMAL) {
#ifdef TARGET_PC
                if (!aSI_pc_no_net)
#endif
                sAdo_SysTrgStart(NA_SE_11B);
            }

#ifdef TARGET_PC
            if (!aSI_pc_no_net) {
                pc_net_game_shop_preview_local(2, (u16)item);
            }
#endif
            return TRUE;
        }
    }

    return FALSE;
}

static int aSI_ChangeWall(mActor_name_t item) {
    if (Common_Get(clip).shop_indoor_clip != NULL) {
        ACTOR* actorx = Common_Get(clip).shop_indoor_clip->shop_indoor_actor;

        if (actorx != NULL && ITEM_IS_WALL(item)) {
            SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
            int wall_data_idx = shop_indoor->wall_data_idx;

            shop_indoor->wall_idx = (mActor_name_t)item - ITM_WALL_START;
            shop_indoor->wall_data_idx = (wall_data_idx ^ 1) & 1;
            aSI_CopyWallTexture(actorx, shop_indoor->wall_idx, shop_indoor->wall_data_idx);
#ifdef TARGET_PC
            if (!aSI_pc_no_net) {
                pc_net_game_shop_preview_local(1, (u16)item);
                sAdo_SysTrgStart(NA_SE_11B);
            }
#else
            sAdo_SysTrgStart(NA_SE_11B);
#endif
            return TRUE;
        }
    }

    return FALSE;
}

static void aSI_Change2Default(void) {
    ACTOR* actorx = Common_Get(clip).shop_indoor_clip->shop_indoor_actor;

    if (actorx != NULL) {
        SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;
        s16 shop_level = shop_indoor->shop_idx;

#ifdef TARGET_PC
        int pc_prev_no_net = aSI_pc_no_net;
        aSI_pc_no_net = 1; /* the two nested changes are one 'default' preview, reported once below */
#endif
        aSI_se_mode = aSI_SE_MODE_SILENT;
        aSI_ChangeWall(ITM_WALL_START + (mActor_name_t)aSI_wall_default_table[shop_level]);
        aSI_ChangeCarpet(ITM_CARPET_START + (mActor_name_t)aSI_floor_default_table[shop_level]);
        aSI_se_mode = aSI_SE_MODE_NORMAL;
#ifdef TARGET_PC
        aSI_pc_no_net = pc_prev_no_net;
        if (!aSI_pc_no_net) {
            pc_net_game_shop_preview_local(3, 0);
        }
#endif
    }
}

#ifdef TARGET_PC
/* pc_net_game.c: another player's preview in the shop this process is inside (kind 1 wall, 2 carpet, 3 default). Returns 0 when no shop room is live. Never re-sent, no sound. */
int aSI_pc_apply_remote_preview(int kind, u16 item) {
    int ret = 0;

    if (Common_Get(clip).shop_indoor_clip == NULL || Common_Get(clip).shop_indoor_clip->shop_indoor_actor == NULL) {
        return 0;
    }
    aSI_pc_no_net = 1;
    if (kind == 1) {
        ret = aSI_ChangeWall((mActor_name_t)item);
    } else if (kind == 2) {
        ret = aSI_ChangeCarpet((mActor_name_t)item);
    } else if (kind == 3) {
        aSI_Change2Default();
        ret = 1;
    }
    aSI_pc_no_net = 0;
    printf("[NET][SHOP] preview apply kind=%d item=0x%04X ret=%d wall_idx=%d floor_idx=%d\n", kind, (unsigned)item, ret,
           (int)((SHOP_INDOOR_ACTOR*)Common_Get(clip).shop_indoor_clip->shop_indoor_actor)->wall_idx,
           (int)((SHOP_INDOOR_ACTOR*)Common_Get(clip).shop_indoor_clip->shop_indoor_actor)->floor_idx);
    return ret;
}
#endif

static void aSI_IndoorDmaAgain(void) {
    if (Common_Get(clip).shop_indoor_clip != NULL) {
        ACTOR* actorx = Common_Get(clip).shop_indoor_clip->shop_indoor_actor;

        if (actorx != NULL) {
            SHOP_INDOOR_ACTOR* shop_indoor = (SHOP_INDOOR_ACTOR*)actorx;

            aSI_CopyWallTexture(actorx, shop_indoor->wall_idx, 2);
            aSI_CopyFloorTexture(actorx, shop_indoor->floor_idx, 2);
        }
    }
}
