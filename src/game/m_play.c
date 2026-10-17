#include "m_play.h"

#include "evw_anime.h"
#include "m_common_data.h"
#include "libultra/libultra.h"
#include "m_fbdemo_wipe1.h"
#include "m_fbdemo_triforce.h"
#include "m_fbdemo_fade.h"
#include "m_debug.h"
#include "boot.h"
#include "version.h"
#include "m_bgm.h"
#include "m_rcp.h"
#include "m_msg.h"
#include "m_banti.h"
#include "m_event.h"
#include "m_npc.h"
#include "m_huusui_room.h"
#include "m_field_make.h"
#include "m_card.h"
#include "m_eappli.h"
#include "famicom_emu.h"
#include "Famicom/famicomPriv.h"
#include "m_malloc.h"
#include "m_field_info.h"
#include "m_time.h"
#include "m_titledemo.h"
#include "sys_matrix.h"
#include "m_fuusen.h"
#include "m_watch_my_step.h"
#include "m_vibctl.h"
#include "m_train_control.h"
#include "gfxalloc.h"
#include "m_debug_display.h"
#include "jsyswrap.h"
#include "m_trademark.h"
#include "zurumode.h"
#include "m_scene_table.h"
#include "m_player_lib.h"
#include "m_skin_matrix.h"
#include "padmgr.h"
#ifdef TARGET_PC
#include "pc_diag.h"
#include "pc_platform.h"
#include "ac_npc.h"      /* NPC_ACTOR / aNPC_ACTION_TYPE_* for the host submenu world hold */
#include "pc_net_game.h" /* pc_net_game_notify_npc_move */
#endif

#define Game_play_HYRAL_SIZE 0x3E800 // 256,000 bytes
#define Game_play_IS_PAUSED(play) (ZURUMODE2_ENABLED() ? (!Pause_proc(&(play)->pause, &(play)->game.pads[PAD1])) : FALSE)

static fbdemo_c fbdemo;
static f32 S_back_title_timer = 0.0f;
static f32 S_se_endcheck_timeout = 0.0f;
static u8 gxbuf[0x140] ATTRIBUTE_ALIGN(32);
static u8 prbuf[(2*SCREEN_WIDTH) * (2*SCREEN_HEIGHT) * sizeof(u32)] ATTRIBUTE_ALIGN(32); // 0x12C000

static void Game_play_fbdemo_wipe_init(GAME_PLAY* play);
static void Gameplay_Scene_Read(GAME_PLAY* play, s16 scene_no);

static void Game_play_dec_dt_timer(f32* timer, GAME_PLAY* play) {
    if (*timer > 0.0f) {
        *timer -= (f32)play->game.graph->dt_num_60fps_frames;
        if (*timer < 0.0f) {
            *timer = 0.0f;
        }
    }
}

static void Game_play_Reset_destiny() {
    lbRTC_time_c* rtc_time = Common_GetPointer(time.rtc_time);
    mPr_destiny_c* destiny = &Common_Get(now_private)->destiny;

    if (destiny->type == mPr_DESTINY_NORMAL) {
        return;
    }

    if ((rtc_time->year != destiny->received_time.year) || (rtc_time->month != destiny->received_time.month) ||
        (rtc_time->day != destiny->received_time.day)) {
        Common_Set(now_private->destiny.type, mPr_DESTINY_NORMAL);
    }
}

static void event_title_flag_on() {
    if (Common_Get(event_title_flags) != 0) {
        Common_Set(event_notification_active, 1);
    }
}

static void event_title_flag_off() {
    if (Common_Get(event_title_flags) == 2) {
        Common_Set(event_title_flags, 0);
        Common_Set(event_notification_active, 0);
    }
}

static void Game_play_camera_proc(GAME_PLAY* play) {
    Camera2_ClearActorTalking_Cull(play);
    Camera2_process(play);
}

static void Game_play_fbdemo_wipe_destroy(GAME_PLAY* play) {
    play->fbdemo_wipe.wipe_procs.cleanup_proc(&play->fbdemo_wipe.wipe_data);
    play->fb_wipe_mode = WIPE_MODE_NONE;
    play->fbdemo_wipe.wipe_type = -1;
}

static void Game_play_fbdemo_wipe_create_sub(GAME_PLAY* play) {
    static void* wipe_normal_proc[] = {
        fbdemo_wipe1_init,
        none_proc1,
        fbdemo_wipe1_move,
        fbdemo_wipe1_draw,
        fbdemo_wipe1_startup,
        fbdemo_wipe1_settype,
        fbdemo_wipe1_setcolor_rgba8888,
        NULL,
        fbdemo_wipe1_is_finish,
    };
    static void* wipe_triforce_proc[] = {
        fbdemo_triforce_init,
        none_proc1,
        fbdemo_triforce_move,
        fbdemo_triforce_draw,
        fbdemo_triforce_startup,
        fbdemo_triforce_settype,
        fbdemo_triforce_setcolor_rgba8888,
        NULL,
        fbdemo_triforce_is_finish,
    };
    static void* wipe_fade_proc[] = {
        fbdemo_fade_init,
        none_proc1,
        fbdemo_fade_move,
        fbdemo_fade_draw,
        fbdemo_fade_startup,
        fbdemo_fade_settype,
        fbdemo_fade_setcolor_rgba8888,
        NULL,
        fbdemo_fade_is_finish,
    };

    static void* wipe_proc[] = {
        wipe_normal_proc,   wipe_triforce_proc, wipe_fade_proc, wipe_fade_proc,
        wipe_triforce_proc, wipe_triforce_proc, wipe_fade_proc,
    };

    int type = play->fb_wipe_type;
    fbdemo_wipe_proc_c* cur;

    bzero(&play->fbdemo_wipe, sizeof(fbdemo_wipe));

    if (type >= WIPE_TYPE_NUM) {
        type = WIPE_TYPE_TRIFORCE;
    }

    play->fbdemo_wipe.wipe_type = type;
    cur = wipe_proc[type];
    play->fbdemo_wipe.wipe_procs.init_proc = cur->init_proc;
    play->fbdemo_wipe.wipe_procs.cleanup_proc = cur->cleanup_proc;
    play->fbdemo_wipe.wipe_procs.move_proc = cur->move_proc;
    play->fbdemo_wipe.wipe_procs.draw_proc = cur->draw_proc;
    play->fbdemo_wipe.wipe_procs.startup_proc = cur->startup_proc;
    play->fbdemo_wipe.wipe_procs.settype_proc = cur->settype_proc;
    play->fbdemo_wipe.wipe_procs.setcolor_proc = cur->setcolor_proc;
    play->fbdemo_wipe.wipe_procs.isfinished_proc = cur->isfinished_proc;
}

static void Game_play_fbdemo_wipe_create(GAME_PLAY* play) {
    Game_play_fbdemo_wipe_create_sub(play);
    Game_play_fbdemo_wipe_init(play);
}

static void Game_play_fbdemo_wipe_init(GAME_PLAY* play) {
    fbdemo_wipe* wipe = &play->fbdemo_wipe;
    u8 type;

    play->fbdemo_wipe.wipe_procs.init_proc(&wipe->wipe_data);

    Common_Set(transition.wipe_rate, 28);
    Common_Set(transition.fade_rate, 30);

    wipe->wipe_procs.setcolor_proc(&wipe->wipe_data, 0);

    type = play->fb_fade_type;

    if (wipe->wipe_type == WIPE_TYPE_CIRCLE_LEFT) {
        type |= 0x80;
    } else if (wipe->wipe_type == WIPE_TYPE_CIRCLE_RIGHT) {
        type |= 0x40;
    }
    wipe->wipe_procs.settype_proc(&wipe->wipe_data, type);
    wipe->wipe_procs.startup_proc(&wipe->wipe_data);

    play->fb_wipe_mode = WIPE_MODE_MOVE;

    S_back_title_timer = 120.0f;
    S_se_endcheck_timeout = 120.0f;
}

static void Game_play_fbdemo_fade_in_move_end(GAME_PLAY* play) {
    Game_play_fbdemo_wipe_destroy(play);

    if (play->fb_mode == FBDEMO_MODE_MOVE) {
        fbdemo_cleanup(&fbdemo);
        play->fb_mode = FBDEMO_MODE_NONE;
        SetGameFrame(3);
    }
}

static void Game_play_fbdemo_fade_out_start_emu_move_end(GAME_PLAY* play) {
    GAME_GOTO_NEXT(&play->game, famicom_emu, FAMICOM_EMU);
}

static void Game_play_fbdemo_fade_out_game_end_move_end(GAME_PLAY* play) {
    GAME_GOTO_NEXT(&play->game, trademark, TRADEMARK);
}

static void Game_play_change_scene_move_end(GAME_PLAY* play) {
    game_goto_next_game_play(&play->game);
    Common_Set(last_scene_no, Save_Get(scene_no));
    Save_Set(scene_no, play->next_scene_no);
}

static void Game_play_fbdemo_wipe_move(GAME_PLAY* play) {
    fbdemo_wipe* wipe = &play->fbdemo_wipe;
    s16 wipet = FALSE;
    int isDone = TRUE;

    if (wipe->wipe_procs.isfinished_proc(&wipe->wipe_data) != 0) {
        if ((play->fb_fade_type != FADE_TYPE_IN) && (play->fb_fade_type != FADE_TYPE_EVENT)) {
            Game_play_dec_dt_timer(&S_se_endcheck_timeout, play);

            if ((sAdo_SeFadeoutCheck() == 0) && (S_se_endcheck_timeout > 0.0f)) {
                isDone = FALSE;
            } else {
                sAdo_Set_ongenpos_refuse_fg(1);
            }
        }
        if (play->fb_fade_type == FADE_TYPE_EVENT) {
            sAdo_Set_ongenpos_refuse_fg(2);
        }

        if (isDone == TRUE) {
            switch (play->fb_fade_type) {
                case FADE_TYPE_IN:
                    Game_play_fbdemo_fade_in_move_end(play);
                    break;

                case FADE_TYPE_OUT_START_EMU:
                    Game_play_dec_dt_timer(&S_back_title_timer, play);

                    if ((sAdo_BgmFadeoutCheck() == 1) || (S_back_title_timer <= 0.0f)) {
                        Game_play_fbdemo_fade_out_start_emu_move_end(play);
                    } else {
                        wipet = TRUE;
                    }
                    break;

                case FADE_TYPE_OUT_RETURN_TITLE:
                case FADE_TYPE_OUT_GAME_END:
                    if (S_back_title_timer >= 120.0f) {
                        mBGMPsComp_make_ps_wipe(360);
                    }
                case FADE_TYPE_OUT_GAME_END_TRAIN:
                case FADE_TYPE_SELECT_END:
                    Game_play_dec_dt_timer(&S_back_title_timer, play);

                    if ((sAdo_BgmFadeoutCheck() == 1) || (S_back_title_timer <= 0.0f)) {
                        Game_play_fbdemo_fade_out_game_end_move_end(play);
                    } else {
                        wipet = TRUE;
                    }
                    break;

                case FADE_TYPE_SELECT:
                    Game_play_dec_dt_timer(&S_back_title_timer, play);

                    if ((sAdo_BgmFadeoutCheck() == 1) || (S_back_title_timer <= 0.0f)) {
                        if ((Common_Get(clip.animal_logo_clip) != NULL) &&
                            (Common_Get(clip.animal_logo_clip->data_init_proc) != NULL)) {
                            Common_Get(clip.animal_logo_clip->data_init_proc)(play);
                            Game_play_change_scene_move_end(play);
                        }
                    } else {
                        wipet = TRUE;
                    }
                    break;

                case FADE_TYPE_DEMO:
                    Game_play_dec_dt_timer(&S_back_title_timer, play);

                    if ((sAdo_BgmFadeoutCheck() == 1) || (S_back_title_timer <= 0.0f)) {
                        Game_play_change_scene_move_end(play);
                    } else {
                        wipet = TRUE;
                    }
                    break;

                case FADE_TYPE_EVENT:
                case FADE_TYPE_OTHER_ROOM:
                    Game_play_change_scene_move_end(play);
                    break;

                default:
                    Game_play_dec_dt_timer(&S_back_title_timer, play);

                    if ((sAdo_BgmFadeoutCheck() == 1) || (S_back_title_timer <= 0.0f)) {
                        Game_play_change_scene_move_end(play);
                    } else {
                        wipet = TRUE;
                    }
                    break;
            }
            if (!wipet) {
                play->fb_fade_type = FADE_TYPE_NONE;
            }
        }
    } else {
        wipe->wipe_procs.move_proc(&wipe->wipe_data, game_GameFrame);
    }
}

static void Game_play_fbdemo_wipe_proc(GAME_PLAY* play) {
    static PLAY_WIPE_PROC wipe_proc[] = { (PLAY_WIPE_PROC)none_proc1, Game_play_fbdemo_wipe_create,
                                          Game_play_fbdemo_wipe_init, Game_play_fbdemo_wipe_move };

    if ((play->fb_wipe_mode == WIPE_MODE_NONE) && (play->fb_fade_type != FADE_TYPE_NONE)) {
        play->fb_wipe_mode = WIPE_MODE_CREATE;
    }

    wipe_proc[play->fb_wipe_mode](play);
}

static Gfx* game_play_set_fog(GAME_PLAY* play, Gfx* gfx) {
    return gfx_set_fog_nosync(gfx, play->global_light.fogColor[0], play->global_light.fogColor[1],
                              play->global_light.fogColor[2], 0, play->global_light.fogNear, play->global_light.fogFar);
}

static void Game_play_fbdemo_proc(GAME_PLAY* play) {
    switch (play->fb_mode) {
        case FBDEMO_MODE_INIT:
            if (fbdemo_init(&fbdemo, 10, 7) == 0) {
                play->fb_mode = FBDEMO_MODE_NONE;
            } else {
                play->fb_mode = FBDEMO_MODE_MOVE;
                SetGameFrame(1);
            }
            break;

        case FBDEMO_MODE_MOVE:
            fbdemo_move(&fbdemo);
            break;
    }
}

extern void play_cleanup(GAME* game) {
    GAME_PLAY* play = (GAME_PLAY*)game;

    mMsg_dt(game);
    banti_dt();

    game->graph->taskEndCallback = NULL;
    game->graph->taskEndData = NULL;
    play->submenu.mode = mSM_MODE_IDLE;

    PreRender_cleanup(&play->prerender);
    CollisionCheck_dt(game, &play->collision_check);

    if (play->fb_mode == FBDEMO_MODE_MOVE) {
        fbdemo_cleanup(&fbdemo);
        play->fb_mode = FBDEMO_MODE_NONE;
    }

    if (play->fb_wipe_mode == WIPE_MODE_MOVE) {
        Game_play_fbdemo_wipe_destroy(play);
    }

    Actor_info_dt(&play->actor_info, play);
    mEv_finish(&play->event);
    mNpc_ClearEventNpc();
    mNpc_ClearMaskNpc();

    mSM_submenu_dt(&play->submenu);
    play->submenu_ground_idx = -1;
    mSM_submenu_ovlptr_cleanup(&play->submenu);

    mPlib_Object_Exchange_keep_Player_dt(play);

    mHsRm_GetHuusuiRoom(NULL, Common_Get(player_no));
    mFM_Field_dt();
    mCD_toNextLand();
    mEA_CleanCardDLProgram();

    if (my_malloc_current == &my_malloc_func) {
        my_malloc_current = NULL;
    }

    zelda_CleanupArena();
}

static void VR_Box_ct(GAME_PLAY* play) {
    Global_kankyo_ct((GAME*)play, &play->kankyo);
}

extern void play_init(GAME* game) {
    GAME_PLAY* play = (GAME_PLAY*)game;
    GRAPH* graph = game->graph;
    int type;
    fbdemo_fade* fade;
    int freebytes;
    u32 alloc;
    u32 aligned;
    u32 size;

    game_resize_hyral(game, -Game_play_HYRAL_SIZE); // reserve bytes from gamealloc
    Common_Set(rhythym_updated, 0);

    mFI_ChangeClimate_ForEventNotice();
    mTM_time_init();
    sAdo_Set_ongenpos_refuse_fg(0);
    event_title_flag_on();
    mTD_rtc_set();
    mTM_set_season();
    mPlib_Clear_controller_data_for_title_demo();

    mSM_submenu_ovlptr_init(play);
    mDemo_Init(play);
    mEv_init(&play->event);

    initView(&play->view, graph);
    Init_Camera2(play);
    CollisionCheck_ct(game, &play->collision_check);

    mCoBG_InitMoveBgData();
    mCoBG_InitBlockBgCheckMode();
    mCoBG_InitDecalCircle();

    play->submenu_ground_idx = -1;

    Gameplay_Scene_Read(play, Save_Get(scene_no));

    mSM_submenu_ct(&play->submenu);
    play->submenu.mode = mSM_MODE_IDLE;

    PreRender_init(&play->prerender);
    PreRender_setup_savebuf(&play->prerender, SCREEN_WIDTH * 2, SCREEN_HEIGHT * 2, NULL, NULL, NULL);
    PreRender_setup_renderbuf(&play->prerender, SCREEN_WIDTH * 2, SCREEN_HEIGHT * 2, NULL, NULL);

    play->fb_mode = FBDEMO_MODE_NONE;
    type = WIPE_TYPE_TRIFORCE;

    play->fb_wipe_mode = WIPE_MODE_NONE;
    play->fb_fade_type = FADE_TYPE_IN;

    if (Common_Get(transition.wipe_type) != 0xFF) {
        type = Common_Get(transition.wipe_type);
        Common_Set(transition.wipe_type, 0xFF);
    }

    play->fb_wipe_type = type;

    Pause_ct(&play->pause);
    new_Matrix(game);

    fade = &play->color_fade;

    game->exec = play_main;
    game->cleanup = play_cleanup;
    fbdemo_fade_init(fade);
    fbdemo_fade_settype(fade, 7);
    fbdemo_fade_setcolor_rgba8888(fade, 0xA0A0A0FF);
    fbdemo_fade_startup(fade);

    play->fade_color_value.rgba8888 = 0;

    freebytes = game_getFreeBytes(game);
    alloc = (u32)THA_alloc16(&game->tha, freebytes);
    aligned = ALIGN_NEXT(alloc, 16);
    size = aligned - alloc;

    zelda_InitArena((void*)aligned, freebytes - size);

    if (my_malloc_current == NULL) {
        my_malloc_current = &my_malloc_func;
    }

#ifdef TARGET_PC
    {
        /* multiplayer HOST day rollover (pc_day_roll.h): when this load is the roll's own same-position town reload, the new day's boot init runs here
         * (old actors gone, arena up, before the field build and any actor). A no-op for every other load, single player and clients. */
        extern void pc_day_roll_play_init(void* play);
        pc_day_roll_play_init(play);
    }
#endif
    mFM_FieldInit(play);
    VR_Box_ct(play);
    mMmd_MakeMuseumDisplayData();
    Actor_info_ct(game, &play->actor_info, play->player_data);
    play->draw_chk_proc = none_proc1;
    mMsg_ct(game);
    mEv_2nd_init(&play->event);
    mTD_player_keydata_init(play);
    Balloon_init(play);
    mNtc_set_auto_nwrite_data();
    banti_ct();
    watch_my_step_ct();
    event_title_flag_off();
    mEA_GetCardDLProgram();
}

#ifdef TARGET_PC
/* Esc-pause talk guard (multiplayer HOST only). Game_play_move() keeps the world running while the host sits in the Esc menu
 * with at least one READY client, but the host's own input is blocked, so no demo may START for the local player then: a
 * villager's forced SPEAK, an event REPORT/SPEECH/EVENTMSG, or a door/scene demo would open a message window or a transition
 * that nobody can answer. mDemo_Main() is the only place a queued mDemo_Request() is accepted (choice_demo()), and
 * mDemo_stock_clear() drops the unaccepted queue every frame, so skipping it is exactly the vanilla submenu rule ("demos only
 * run while process_status == WAIT"): requesters re-request every frame and simply wait; an already running demo is held,
 * as the vanilla full freeze held it. Returns 0 in single player, on a client, and on a host with no READY client (that host
 * never gets here while paused: Game_play_move() returns early). */
static int Game_play_pc_host_paused_live(void) {
    extern int g_pc_paused;
    extern int pc_net_game_host_ready_peer_count(void);
    extern int pc_net_game_menu_world_live(void);

    /* "world behind menus": a READY CLIENT's Esc menu keeps its world running too (pc_net_game_menu_world_live), with the same guard */
    return g_pc_paused && (pc_net_game_host_ready_peer_count() > 0 || pc_net_game_menu_world_live());
}

/* Submenu world hold (multiplayer HOST with >= 1 READY client, process_status != WAIT). Vanilla freezes every actor while a
 * submenu is open and that stays so: running villager/wildlife AI here is NOT safe (villager action/think code births tool
 * actors and registers banks in the shared object_exchange that mSM_move_End repacks with mSc_dmacopy_all_exchange_bank,
 * the NPC control actor spawns/deletes actors, a submenu opened from a conversation would run the talking NPC against stale
 * message state, and the OC collider register/clear pair would drop the frozen player). What this does instead is purely
 * presentational and read-only: it keeps the NPC_MOVE stream alive with each visible villager's TRUE (held) pose, and maps a
 * walk/run action to the matching wait action, so the clients show the villagers standing still instead of walking in place
 * at the last sampled spot. No actor/bank/save/collision state is touched; the next real sample after the menu closes is the
 * same position, so there is no snap. Single player, a client and a host with no READY client never get here.
 * F2 (supersedes the "NOT safe" above for a subset): an idle/walking villager that is not talking and has no pending tool or
 * hand-over-item birth runs its REAL move proc under g_pc_submenu_world_tick (m_actor.c / m_scene.c refuse every actor
 * creation and exchange bank allocation while it is set, Actor_delete is mark-only = deferred to the first frame after the
 * menu). The NPC control actor (spawns, cloth bank registration) and every other actor stay frozen; anything else keeps the
 * held pose above. Submenu overlays on PC do not write into object_exchange RAM (no *_ovl.c touches it), so the bank data
 * the move proc reads is intact while the menu is open. */
static int s_pc_submenu_hold_frames = 0;
static int s_pc_submenu_hold_actors0 = 0;
static int s_pc_submenu_tick_count = 0; /* F2: villager move procs run while the submenu was open */
static int s_pc_submenu_bank_idx0 = 0;
extern unsigned g_pc_submenu_tick_refused_make;    /* m_actor.c */
extern unsigned g_pc_submenu_tick_deferred_delete; /* m_actor.c */

static int Game_play_pc_actor_total(GAME_PLAY* play) {
    int part, n = 0;

    for (part = 0; part < ACTOR_PART_NUM; part++) {
        n += play->actor_info.list[part].num_actors;
    }
    return n;
}

/* "World keeps running behind menus" (pc_net_game_menu_world_live(): a HOST with >= 1 READY peer or a READY CLIENT, setting
 * menu_world_live / env AC_MP_MENU_WORLD, default ON; single player and an unconnected process never get here = vanilla freeze).
 * Besides the villagers above (host F2; on a client the same real move proc, which there only CONSUMES the host's NPC_MOVE /
 * NPC_STATE: no schedule / action AI, no RNG), a WHITELIST of network-driven or purely presentational actors runs its own move
 * proc while a free-play submenu is open, with the same g_pc_submenu_world_tick protection (no actor creation, no exchange bank,
 * deletes deferred, OC registrations dropped):
 *   - remote-player puppets (pc_remote_player.c: interpolation / animation / cosmetics; the collider is held while the flag is set)
 *   - the effect control (eEC: visual effects only), the weather (particles, env SE; a client never rolls the weather), the insect
 *     control (bugs; the mosquito sting request is suppressed while the flag is set, ac_ins_ka.c) and the fish control (gyoei).
 * Everything else stays frozen exactly as vanilla: the LOCAL player (its menu state machine owns it), the NPC control actor and
 * shop / event NPCs, BG items (only their FILTERED cosmetic pass runs, see Game_play_pc_submenu_cosmetic_tick), bees, the ball,
 * structures, the set manager (no new spawns), the camera. See the roadmap section. */
static int s_pc_submenu_puppet_ticks = 0;
static int s_pc_submenu_ctl_ticks = 0;

static void Game_play_pc_submenu_tick_actor(GAME_PLAY* play, ACTOR* a) {
    extern int g_pc_submenu_world_tick;
    PLAYER_ACTOR* pl = GET_PLAYER_ACTOR(play);
    int oc_num = play->collision_check.collider_num;

    /* exactly the per-actor prologue / epilogue of Actor_info_call_actor (m_actor.c) */
    xyz_t_move(&a->last_world_position, &a->world.position);
    if (pl != NULL) {
        a->player_distance_xz = search_position_distanceXZ(&a->world.position, &pl->actor_class.world.position);
        a->player_distance_y = pl->actor_class.world.position.y - a->world.position.y;
        a->player_distance = a->player_distance_xz * a->player_distance_xz + a->player_distance_y * a->player_distance_y;
        a->player_angle_y = search_position_angleY(&a->world.position, &pl->actor_class.world.position);
    }
    a->state_bitfield &= ~ACTOR_STATE_24;
    g_pc_submenu_world_tick = 1;
    (*a->mv_proc)(a, (GAME*)play);
    g_pc_submenu_world_tick = 0;
    if (play->collision_check.collider_num > oc_num) {
        play->collision_check.collider_num = oc_num;
    }
    CollisionCheck_Status_Clear(&a->status_data);
}

/* "Cosmetic world reactions behind menus" (same live session / free-play gate as the whitelist above): what another player does to
 * the town while THIS player's submenu is open plays live instead of after the menu closes:
 *   - the EffectBG actor (ac_effectbg.c, ACTOR_PART_EFFECT): the tree-shake sway a puppet starts (pc_remote_player.c ->
 *     make_effect_bg_proc) and the chop / fall animations: a 3-slot keyframe pool, leaves through the effect control, sounds; it only
 *     READS the grid (EfbgBgitemTreeCheck) and the ground height, creates no actor.
 *   - the field-item actor (bg_item / cherry / winter / xmas variants, ACTOR_PART_ITEM) through its FILTERED pass
 *     (bg_item_common.c_inc bg_item_common_pc_menu_cosmetic_move, selected by g_pc_submenu_world_tick): presentation drops (TREE_DROP
 *     falling fruit / bells / furniture, flag 0x4 = no tile write), the shine glow, item fade-outs and the draw-table refresh of tiles
 *     the host / tile sync changed (stump, dropped / picked-up items, holes, pulled weeds). Holes being dug / filled, pitfalls, the
 *     money-rock coin window, every drop that writes a tile and the outdoor-furniture collision stay frozen.
 * Proof hooks: a hash of the town map (Save fg + deposit) is taken around this pass; any change is counted (field_changed_cos in the
 * hold END line, expected 0) and logged. Bees, the ball, structures and the local player stay frozen (they chase / push / talk to
 * the local player or write the map). */
unsigned g_pc_menu_cos_drop_ticks = 0;   /* bg_item_common.c_inc: presentation drops moved during a menu */
unsigned g_pc_menu_cos_drop_held = 0;    /* ... other drops held (vanilla freeze) */
unsigned g_pc_menu_cos_redraws = 0;      /* ... draw-table refreshes of changed tiles */
unsigned g_pc_menu_cos_redraw_held = 0;  /* ... refreshes postponed (born item / wade / cut-tree reset pending) */
static int s_pc_menu_cos_efbg_ticks = 0;
static int s_pc_menu_cos_efbg_active = 0; /* frames on which >= 1 EffectBG slot (a tree sway / chop) was animating */
static int s_pc_menu_cos_bgitem_ticks = 0;
static int s_pc_menu_cos_field_changed = 0;
static int s_pc_menu_cos_logged = 0; /* bit 0 tree reaction, bit 1 drop, bit 2 refresh, bit 3 field change */

static u32 Game_play_pc_town_map_hash(void) {
    const u8* p = (const u8*)Save_GetPointer(fg);
    const u8* d = (const u8*)Save_GetPointer(deposit);
    u32 h = 2166136261u;
    u32 i;

    for (i = 0; i < (u32)sizeof(Save_Get(fg)); i++) {
        h = (h ^ p[i]) * 16777619u;
    }
    for (i = 0; i < (u32)sizeof(Save_Get(deposit)); i++) {
        h = (h ^ d[i]) * 16777619u;
    }
    return h;
}

static void Game_play_pc_submenu_cosmetic_tick(GAME_PLAY* play) {
    extern int Effectbg_pc_count_active(void); /* ac_effectbg.c (read-only pool view) */
    ACTOR* a;
    u32 h0 = Game_play_pc_town_map_hash();
    unsigned drops0 = g_pc_menu_cos_drop_ticks;
    unsigned redraw0 = g_pc_menu_cos_redraws;

    for (a = play->actor_info.list[ACTOR_PART_EFFECT].actor; a != NULL; a = a->next_actor) {
        if (a->id == mAc_PROFILE_EFFECTBG && a->ct_proc == NULL && a->mv_proc != NULL &&
            (a->state_bitfield & (ACTOR_STATE_NO_MOVE_WHILE_CULLED | ACTOR_STATE_NO_CULL)) != 0) {
            int act = Effectbg_pc_count_active();

            if (act > 0) {
                s_pc_menu_cos_efbg_active++;
                if (!(s_pc_menu_cos_logged & 1)) {
                    s_pc_menu_cos_logged |= 1;
                    printf("[NET][MENU] tree reaction during menu: EffectBG animating slots=%d menu_type=%d\n", act,
                           (int)play->submenu.menu_type);
                }
            }
            Game_play_pc_submenu_tick_actor(play, a);
            s_pc_menu_cos_efbg_ticks++;
        }
    }
    for (a = play->actor_info.list[ACTOR_PART_ITEM].actor; a != NULL; a = a->next_actor) {
        if ((a->id == mAc_PROFILE_BGITEM || a->id == mAc_PROFILE_BGCHERRYITEM || a->id == mAc_PROFILE_BGWINTERITEM ||
             a->id == mAc_PROFILE_BGXMASITEM) &&
            a->ct_proc == NULL && a->mv_proc != NULL &&
            (a->state_bitfield & (ACTOR_STATE_NO_MOVE_WHILE_CULLED | ACTOR_STATE_NO_CULL)) != 0) {
            Game_play_pc_submenu_tick_actor(play, a); /* -> bg_item_common_pc_menu_cosmetic_move (the flag is set) */
            s_pc_menu_cos_bgitem_ticks++;
        }
    }
    if (g_pc_menu_cos_drop_ticks != drops0 && !(s_pc_menu_cos_logged & 2)) {
        s_pc_menu_cos_logged |= 2;
        printf("[NET][MENU] tree reaction during menu: item drop presentation animating (drop_ticks=%u)\n", g_pc_menu_cos_drop_ticks);
    }
    if (g_pc_menu_cos_redraws != redraw0 && !(s_pc_menu_cos_logged & 4)) {
        s_pc_menu_cos_logged |= 4;
        printf("[NET][MENU] tree reaction during menu: ground items redrawn from the updated field (redraws=%u)\n",
               g_pc_menu_cos_redraws);
    }
    if (Game_play_pc_town_map_hash() != h0) {
        s_pc_menu_cos_field_changed++;
        if (!(s_pc_menu_cos_logged & 8)) {
            s_pc_menu_cos_logged |= 8;
            printf("[NET][MENU] WARNING: the town map changed inside the cosmetic menu tick\n");
        }
    }
}

static void Game_play_pc_submenu_live_tick(GAME_PLAY* play) {
    extern int pc_remote_player_is_puppet_actor(const void* actor);
    ACTOR* a;

    for (a = play->actor_info.list[ACTOR_PART_UNUSED].actor; a != NULL; a = a->next_actor) {
        if (a->ct_proc == NULL && a->mv_proc != NULL && pc_remote_player_is_puppet_actor(a)) {
            Game_play_pc_submenu_tick_actor(play, a);
            s_pc_submenu_puppet_ticks++;
        }
    }
    for (a = play->actor_info.list[ACTOR_PART_CONTROL].actor; a != NULL; a = a->next_actor) {
        if (a->ct_proc != NULL || a->mv_proc == NULL ||
            (a->state_bitfield & (ACTOR_STATE_NO_MOVE_WHILE_CULLED | ACTOR_STATE_NO_CULL)) == 0) {
            continue; /* not constructed / being deleted / would not move this frame in Actor_info_call_actor either */
        }
        if (a->id == mAc_PROFILE_EFFECT_CONTROL || a->id == mAc_PROFILE_WEATHER || a->id == mAc_PROFILE_INSECT ||
            a->id == mAc_PROFILE_GYOEI) {
            Game_play_pc_submenu_tick_actor(play, a);
            s_pc_submenu_ctl_ticks++;
        }
    }
    Game_play_pc_submenu_cosmetic_tick(play);
}

/* the live draw / live tick condition of a submenu that is open from FREE PLAY: the menu is fully up, no scene transition, no
 * conversation or scripted demo owns the world (a shop / Nook / talk-opened menu keeps the vanilla freeze and the snapshot). */
static int Game_play_pc_submenu_free_play(GAME_PLAY* play) {
    return play->fb_fade_type == FADE_TYPE_NONE && play->fb_wipe_mode == WIPE_MODE_NONE && mDemo_Get_talk_actor() == NULL &&
           mMsg_Check_main_hide(mMsg_Get_base_window_p()) == TRUE;
}

static void Game_play_pc_submenu_world_hold(GAME_PLAY* play) {
    Actor_list* list;
    ACTOR* a;
    int k, slot;
    int tick_ok;
    int host = pc_net_game_host_ready_peer_count() > 0;
    int live = pc_net_game_menu_world_live();

    if (!host && !live) {
        return;
    }
    if (s_pc_submenu_hold_frames++ == 0) {
        s_pc_submenu_hold_actors0 = Game_play_pc_actor_total(play);
        s_pc_submenu_tick_count = 0;
        s_pc_submenu_puppet_ticks = 0;
        s_pc_submenu_ctl_ticks = 0;
        g_pc_menu_cos_drop_ticks = g_pc_menu_cos_drop_held = g_pc_menu_cos_redraws = g_pc_menu_cos_redraw_held = 0;
        s_pc_menu_cos_efbg_ticks = s_pc_menu_cos_efbg_active = s_pc_menu_cos_bgitem_ticks = 0;
        s_pc_menu_cos_field_changed = s_pc_menu_cos_logged = 0;
        s_pc_submenu_bank_idx0 = play->object_exchange.bank_idx;
        g_pc_submenu_tick_refused_make = 0;
        g_pc_submenu_tick_deferred_delete = 0;
        printf("[NET][SUBMENU] %s submenu world hold BEGIN menu_type=%d actors=%d live=%d\n", host ? "host" : "client",
               (int)play->submenu.menu_type, s_pc_submenu_hold_actors0, live);
    }
    /* F2 gate: the real villager tick runs only while the submenu itself is fully open (PLAY: not while it is being
     * linked/prerendered/torn down), outside any conversation (no talk actor, message window hidden: a submenu opened
     * from a conversation keeps the held pose), with no fade/wipe (scene change), and in the town field. */
    tick_ok = play->submenu.process_status == mSM_PROCESS_PLAY && play->fb_fade_type == FADE_TYPE_NONE &&
              play->fb_wipe_mode == WIPE_MODE_NONE && mDemo_Get_talk_actor() == NULL &&
              mMsg_Check_main_hide(mMsg_Get_base_window_p()) == TRUE && mFI_GET_TYPE(mFI_GetFieldId()) == mFI_FIELD_FG;
    list = &play->actor_info.list[ACTOR_PART_NPC];
    a = list->actor;
    for (k = list->num_actors; k > 0 && a != NULL; k--, a = a->next_actor) {
        NPC_ACTOR* n;
        u8 act;

        if (a->mv_proc == NULL) {
            continue; /* already being deleted */
        }
        for (slot = 0; slot < ANIMAL_NUM_MAX; slot++) {
            if ((u16)a->npc_id == (u16)Save_Get(animals[slot]).id.npc_id) {
                break;
            }
        }
        if (slot >= ANIMAL_NUM_MAX) {
            continue; /* not a resident villager (shopkeepers, event NPCs, ...) */
        }
        n = (NPC_ACTOR*)a;
        if (n->npc_info.animal != Save_GetPointer(animals[slot]) || (host && n->condition_info.hide_flg)) {
            continue; /* identity guard (a villager actor always points at its own Animal_c) / hidden: not sampled by N2 either
                       * (a CLIENT's hidden villager still ticks: its move only applies the host's NPC_STATE, e.g. leaving the house) */
        }
        if (tick_ok && a->ct_proc == NULL && !mDemo_IS_ACTOR_TALKING(a) &&
            n->condition_info.talk_condition == aNPC_TALK_TYPE_NONE &&
            n->right_hand.requested_item_type == aNPC_ITEM_TYPE_NONE &&
            n->left_hand.requested_item_type == aNPC_ITEM_TYPE_NONE) {
            /* F2: the villager's own move proc, exactly as Actor_info_call_actor runs it (m_actor.c, ACTOR_PART_NPC branch),
             * under g_pc_submenu_world_tick (no actor create, no exchange bank, deletes only marked). The move proc also
             * reports its own NPC_MOVE sample (aNPC_pc_host_sample_move). OC colliders it registers are dropped again
             * (collider_num restored): NPC<->NPC/player pushes are skipped for the stage, and the frozen frame's entries
             * (the player's included) stay intact for the first OC pass after the menu closes. */
            extern int g_pc_submenu_world_tick;
            PLAYER_ACTOR* pl = GET_PLAYER_ACTOR(play);
            int oc_num = play->collision_check.collider_num;

            xyz_t_move(&a->last_world_position, &a->world.position);
            if (pl != NULL) {
                a->player_distance_xz = search_position_distanceXZ(&a->world.position, &pl->actor_class.world.position);
                a->player_distance_y = pl->actor_class.world.position.y - a->world.position.y;
                a->player_distance =
                    a->player_distance_xz * a->player_distance_xz + a->player_distance_y * a->player_distance_y;
                a->player_angle_y = search_position_angleY(&a->world.position, &pl->actor_class.world.position);
            }
            a->state_bitfield &= ~ACTOR_STATE_24;
            g_pc_submenu_world_tick = 1;
            (*a->mv_proc)(a, (GAME*)play);
            g_pc_submenu_world_tick = 0;
            if (play->collision_check.collider_num > oc_num) {
                play->collision_check.collider_num = oc_num;
            }
            CollisionCheck_Status_Clear(&a->status_data);
            s_pc_submenu_tick_count++;
            continue;
        }
        if (!host) {
            continue; /* a client has no NPC_MOVE stream to keep alive: an untickable villager simply holds its pose */
        }
        act = n->condition_info.action;
        if (act >= aNPC_ACTION_TYPE_WALK && act <= aNPC_ACTION_TYPE_WALK_AI) {
            act = (u8)(aNPC_ACTION_TYPE_WAIT + (act - aNPC_ACTION_TYPE_WALK));
        } else if (act >= aNPC_ACTION_TYPE_RUN && act <= aNPC_ACTION_TYPE_RUN_AI) {
            act = (u8)(aNPC_ACTION_TYPE_WAIT + (act - aNPC_ACTION_TYPE_RUN));
        }
        pc_net_game_notify_npc_move(slot, (uint16_t)n->npc_info.animal->id.npc_id, a->world.position.x,
                                    a->world.position.y, a->world.position.z, a->world.angle.y, act);
    }
    if (live && play->submenu.process_status == mSM_PROCESS_PLAY && Game_play_pc_submenu_free_play(play)) {
        Game_play_pc_submenu_live_tick(play); /* puppets + the whitelisted control actors (any scene, not only the town field) */
    }
}

static void Game_play_pc_submenu_world_hold_end(GAME_PLAY* play) {
    if (s_pc_submenu_hold_frames != 0) {
        printf("[NET][SUBMENU] %s submenu world hold END frames=%d actors_open=%d actors_close=%d ticks=%d "
               "refused_make=%u deferred_delete=%u bank_idx_open=%d bank_idx_close=%d puppet_ticks=%d ctl_ticks=%d\n",
               pc_net_game_role() == PC_NETGAME_ROLE_HOST ? "host" : "client", s_pc_submenu_hold_frames,
               s_pc_submenu_hold_actors0, Game_play_pc_actor_total(play), s_pc_submenu_tick_count,
               g_pc_submenu_tick_refused_make, g_pc_submenu_tick_deferred_delete, s_pc_submenu_bank_idx0,
               play->object_exchange.bank_idx, s_pc_submenu_puppet_ticks, s_pc_submenu_ctl_ticks);
        printf("[NET][MENU] cosmetic reactions END efbg_ticks=%d efbg_active_frames=%d bgitem_ticks=%d drop_ticks=%u drop_held=%u "
               "redraws=%u redraw_held=%u field_changed_cos=%d\n",
               s_pc_menu_cos_efbg_ticks, s_pc_menu_cos_efbg_active, s_pc_menu_cos_bgitem_ticks, g_pc_menu_cos_drop_ticks,
               g_pc_menu_cos_drop_held, g_pc_menu_cos_redraws, g_pc_menu_cos_redraw_held, s_pc_menu_cos_field_changed);
        s_pc_submenu_hold_frames = 0;
    }
}

/* "World behind menus", the DRAW half. Vanilla draws a submenu over ONE prerendered picture of the scene (prbuf, copied from the
 * EFB on the menu's PRERENDER_INIT frame; on the GameCube the menu overlay was loaded over the player actor's code and the object
 * banks, so the world could not even be drawn). On PC nothing is overlaid (statically linked, no *_ovl.c writes object_exchange),
 * so in a live multiplayer session a submenu opened from free play draws the LIVE world every frame instead of prbuf
 * (makeBumpTexture), and Game_play_draw moves the menu's own display lists (POLY_OPA + POLY_XLU) behind the world's (they are
 * called from LIGHT_DISP, after the world's POLY_XLU; FONT / OVERLAY still come after, as in vanilla) and clears the depth buffer
 * first (PC_NOOP_CLEAR_DEPTH) so the menu sees the empty depth buffer it saw over prbuf. Decided ONCE per menu, on its first
 * PRERENDER_DONE frame (latched until the menu is gone), so a peer leaving mid-menu never flips back to a stale snapshot. A menu
 * opened from a conversation / demo, single player and an unconnected process keep the vanilla snapshot byte-identically. */
static int s_pc_menu_live_draw = -1; /* -1 undecided (no menu up), 0 vanilla prbuf, 1 live world */
extern void mSM_pc_area_save(Submenu* submenu);    /* m_submenu.c: the player's draw (load_player) must not unlink the open menu */
extern void mSM_pc_area_restore(Submenu* submenu);

static void Game_play_pc_menu_live_draw_update(GAME_PLAY* play) {
    if (play->submenu.mode != mSM_MODE_PRERENDER_DONE) {
        s_pc_menu_live_draw = -1;
        return;
    }
    if (s_pc_menu_live_draw < 0) {
        s_pc_menu_live_draw = (pc_net_game_menu_world_live() && Game_play_pc_submenu_free_play(play)) ? 1 : 0;
        printf("[NET][SUBMENU] menu_type=%d draw=%s\n", (int)play->submenu.menu_type,
               s_pc_menu_live_draw ? "live-world" : "vanilla-snapshot");
    }
}

static int Game_play_pc_menu_live_draw(GAME_PLAY* play) {
    return s_pc_menu_live_draw == 1 && play->submenu.mode == mSM_MODE_PRERENDER_DONE;
}
#endif

static void Game_play_move_fbdemo_not_move(GAME* game) {
    GAME_PLAY* play = (GAME_PLAY*)game;

    game->doing_point = 0;
    game->doing_point_specific = 0x8F;
    game->doing_point = 1;

    mSM_submenu_ctrl(play);

    if (play->submenu.process_status == mSM_PROCESS_WAIT) {
        game->doing_point = 2;
#ifdef TARGET_PC
        if (!Game_play_pc_host_paused_live())
#endif
        {
            mDemo_Main(play);
        }
        game->doing_point = 3;
        mEv_run(&play->event);
    }

    game->doing_point = 4;
    mDemo_stock_clear();
    game->doing_point = 5;
    mSc_dmacopy_data_bank(&play->object_exchange);
    game->doing_point = 6;
    mSM_submenu_move(&play->submenu);

    if (play->submenu.process_status == mSM_PROCESS_WAIT) {
#ifdef TARGET_PC
        Game_play_pc_submenu_world_hold_end(play); /* before Actor_info_call_actor: counts the menu period only */
#endif
        play->game_frame++;
        mVibctl_clr_force_stop(2);
        game->doing_point = 7;
        CollisionCheck_OC(game, &play->collision_check);
        game->doing_point = 8;
        CollisionCheck_clear(game, &play->collision_check);
        game->doing_point = 9;
        game->doing_point = 0;
        game->doing_point_specific = 0x90;
        Actor_info_call_actor(play, &play->actor_info);
        game->doing_point = 0;
        game->doing_point_specific = 0x91;
        game->doing_point = 1;
        mCoBG_CalcTimerDecalCircle();
        game->doing_point = 2;
        mMsg_Main(game);
    } else {
        mVibctl_set_force_stop(2);
#ifdef TARGET_PC
        Game_play_pc_submenu_world_hold(play);
#endif
    }

    fbdemo_fade_move(&play->color_fade, game_GameFrame);
}

static void Game_play_move(GAME* game) {
    GAME_PLAY* play = (GAME_PLAY*)game;
    int pause;

#ifdef TARGET_PC
    {
        extern int g_pc_paused;
        extern int pc_net_game_host_ready_peer_count(void);
        /* Multiplayer HOST with at least one READY client: the Esc menu must not freeze everyone else's world (host-authoritative villagers, wildlife, puppets, time). The menu
         * still blocks the host's own input (padmgr zeroes the pads and pc_main swallows the keys while g_pc_paused), so the local player simply stands idle. Single player and
         * a host with nobody connected (pc_net_game_host_ready_peer_count() == 0) keep the vanilla full freeze. */
        /* "world behind menus": a READY CLIENT (pc_net_game_menu_world_live, default on) keeps its world running behind the Esc menu the same way;
         * its pads are zeroed too, so its own player stands idle while puppets, the host-driven villagers, bugs, fish and weather go on. */
        if (g_pc_paused && pc_net_game_host_ready_peer_count() == 0 && !pc_net_game_menu_world_live()) return;
    }
#endif

    game->doing_point = 0;
    game->doing_point_specific = 0x8D;
    game->doing_point = 1;
    Game_play_Reset_destiny();
    game->doing_point = 2;

    if (ZURUMODE2_ENABLED()) {
        pause = !Pause_proc(&play->pause, &game->pads[PAD1]);
    } else {
        pause = FALSE;
    }

    game->doing_point = 0;
    game->doing_point_specific = 0x8E;

    if (!pause) {
        game->doing_point = 1;
        Game_play_fbdemo_wipe_proc(play);
        game->doing_point = 2;
        Game_play_fbdemo_proc(play);
        game->doing_point = 3;

        if (play->fb_mode != FBDEMO_MODE_MOVE) {
            Game_play_move_fbdemo_not_move(game);
        }
    }

    if (play->submenu.process_status == mSM_PROCESS_WAIT) {
        evw_anime_add_scroll_phase(&play->game);
        game->doing_point = 0;
        game->doing_point_specific = 0x92;
        game->doing_point = 1;
        Game_play_camera_proc(play);
        game->doing_point = 2;
        mPO_business_proc(play);
        game->doing_point = 3;
        mTRC_move(game);
        game->doing_point = 4;
        Balloon_move(play);
        mEnv_ManagePointLight(play, &play->kankyo, &play->global_light);
    }
#ifdef TARGET_PC
    else if (pc_net_game_menu_world_live()) {
        evw_anime_add_scroll_phase(&play->game); /* "world behind menus": water / texture scrolling keeps flowing behind the live-drawn menu (a phase counter only) */
    }
#endif

    game->doing_point = 0;
    game->doing_point_specific = 0x93;
    game->doing_point = 1;
    Global_kankyo_set(play, &play->kankyo, &play->global_light);
    game->doing_point = 2;
    mEnv_WindMove();
    game->doing_point = 3;
    watch_my_step_move(play);
    game->doing_point = 4;
    banti_move(play);
    game->doing_point = 5;
    title_demo_move(play);
    game->doing_point = 0;
    game->doing_point_specific = 0x95;
}

// @BUG - this function works properly in N64, but not in GC.
// The devs moved blocks (acres) to new display list buffers in GC,
// but forgot to apply fog to those new display list buffers.
static void setupFog(GAME_PLAY* play, GRAPH* graph) {
    OPEN_DISP(graph);

    SET_POLY_OPA_DISP(game_play_set_fog(play, NOW_POLY_OPA_DISP));
    SET_POLY_XLU_DISP(game_play_set_fog(play, NOW_POLY_XLU_DISP));

    #if defined(BUGFIXES) || defined(TARGET_PC)
    // Apply fog to blocks
    SET_BG_OPA_DISP(game_play_set_fog(play, NOW_BG_OPA_DISP));
    SET_BG_XLU_DISP(game_play_set_fog(play, NOW_BG_XLU_DISP));
    #endif

    CLOSE_DISP(graph);
}

static void setupViewer(GAME_PLAY* play) {
    showView(&play->view, VIEW_UPDATE_ALL);
}

static void setupViewMatrix(GAME_PLAY* play, GRAPH* graph1, GRAPH* graph2) {
    OPEN_DISP(graph1);

    Matrix_MtxtoMtxF(&play->view.mtx_viewing, &play->billboard_matrix);
    Matrix_MtxtoMtxF(&play->view.mtx_projection, &play->projection_matrix);
    Skin_Matrix_MulMatrix(&play->projection_matrix, &play->billboard_matrix, &play->projection_matrix);

    play->billboard_matrix.mf[0][3] = 0.0f;
    play->billboard_matrix.mf[1][3] = 0.0f;
    play->billboard_matrix.mf[2][3] = 0.0f;
    play->billboard_matrix.mf[3][0] = 0.0f;
    play->billboard_matrix.mf[3][1] = 0.0f;
    play->billboard_matrix.mf[3][2] = 0.0f;

    Matrix_reverse(&play->billboard_matrix);
    play->billboard_mtx_p = _MtxF_to_Mtx(&play->billboard_matrix, GRAPH_ALLOC_TYPE(graph2, MtxF, 1));
    gSPSegment(NEXT_BG_OPA_DISP, 7, play->billboard_mtx_p);
    gSPSegment(NEXT_POLY_OPA_DISP, 7, play->billboard_mtx_p);

    CLOSE_DISP(graph1);
}

static void copy_efb_to_texture(Gfx** graph, void* buf) {
    u32 dl;

    GXBeginDisplayList(gxbuf, sizeof(gxbuf));
    GXSetTexCopySrc(0, 0, SCREEN_WIDTH * 2, SCREEN_HEIGHT * 2);
    GXSetTexCopyDst(SCREEN_WIDTH * 2, SCREEN_HEIGHT * 2, GX_TF_RGB565, 0);
    GXSetCopyFilter(0, 0, 0, 0);
    GXCopyTex(buf, 0);
    dl = GXEndDisplayList();
    gGXCallDisplayList((*graph)++, gxbuf, dl);
}

static int makeBumpTexture(GAME_PLAY* play, GRAPH* graph1, GRAPH* graph2) {
    Gfx* newdisp;
    Gfx* polydisp;
    Gfx* rect_disp;
    Gfx* fbdemo_disp;
    Gfx* fb_disp;
    Gfx* old_fb;
    Gfx* poly;

    PC_DIAG(3, "makeBumpTexture: enter fb_mode=%d fb_wipe_mode=%d submenu.mode=%d\n",
            play->fb_mode, play->fb_wipe_mode, play->submenu.mode);
    OPEN_DISP(graph1);

    if ((GETREG(HREG, 80) != 10) || (GETREG(HREG, 92) != 0)) {
        newdisp = NOW_POLY_OPA_DISP;
        polydisp = gfxopen(newdisp);

        gSPDisplayList(NEXT_OVERLAY_DISP, polydisp);

#ifdef TARGET_PC
        /* Tell GXSetProjection to skip widescreen aspect correction for
         * transitions (wipes, fades) so they fill the entire screen.
         * See pc_platform.h for how the marker/flag mechanism works. */
        gDPNoOpTag(polydisp++, PC_NOOP_WIDESCREEN_STRETCH);
#endif

        if (play->fb_wipe_mode == WIPE_MODE_MOVE) {
            View view;
            rect rect;

            initView(&view, graph2);
            view.flag = VIEW_UPDATE_ORTHOGRAPHIC | VIEW_UPDATE_SCISSOR;
            rect.top = 0;
            rect.bottom = SCREEN_HEIGHT;
            rect.l = 0;
            rect.r = SCREEN_WIDTH;
            setScissorView(&view, &rect);
            showView1(&view, VIEW_UPDATE_ALL, &polydisp);
            play->fbdemo_wipe.wipe_procs.draw_proc(&play->fbdemo_wipe.wipe_data, &polydisp);
        }

        fbdemo_fade_draw(&play->color_fade, &polydisp);
        fade_rgba8888_draw(&polydisp, play->fade_color_value.rgba8888);

        gSPEndDisplayList(polydisp++);

        gfxclose(newdisp, polydisp);

        SET_POLY_OPA_DISP(polydisp);
    }

    if (play->fb_mode == FBDEMO_MODE_MOVE) {
        fbdemo_disp = NOW_POLY_OPA_DISP;
        fbdemo_draw(&fbdemo, &fbdemo_disp);
        SET_POLY_OPA_DISP(fbdemo_disp);
        return 1;
    }

    if (play->submenu.mode == mSM_MODE_PRERENDER_WAIT) {
        play->submenu.mode = mSM_MODE_PRERENDER_DONE;
        SETREG(SREG, 3, -(GETREG(SREG, 3)));
    } else if (play->submenu.mode >= mSM_MODE_NUM) {
        play->submenu.mode = mSM_MODE_IDLE;
    }
#ifdef TARGET_PC
    Game_play_pc_menu_live_draw_update(play);
#endif

    if (play->submenu.mode == mSM_MODE_PRERENDER_DONE
#ifdef TARGET_PC
        && !Game_play_pc_menu_live_draw(play) /* live multiplayer world: fall through to the normal world draw below */
#endif
    ) {
        poly = NOW_POLY_OPA_DISP;

#ifdef TARGET_PC
        /* The prerendered background is a fullscreen textured rect that goes
         * through ortho projection — enable stretch so it fills widescreen. */
        gDPNoOpTag(poly++, PC_NOOP_WIDESCREEN_STRETCH);
#endif
        gDPPipeSync(poly++);
        rect_disp = gfx_rect_moji(poly++);
        gDPSetOtherMode(rect_disp++,
                        G_AD_PATTERN | G_CD_MAGICSQ | G_CK_NONE | G_TC_CONV | G_TF_POINT | G_TT_NONE | G_TL_TILE |
                            G_TD_CLAMP | G_TP_NONE | G_CYC_COPY | G_PM_NPRIMITIVE,
                        G_AC_NONE | G_ZS_PIXEL | G_RM_NOOP | G_RM_NOOP2);
        gDPSetTextureImage_Dolphin(rect_disp++, G_IM_FMT_I, G_IM_SIZ_16b, (2*SCREEN_HEIGHT), (2*SCREEN_WIDTH), prbuf);
        gDPSetTile_Dolphin(rect_disp++, G_DOLPHIN_TLUT_DEFAULT_MODE, 0, 0, 0, 0, 0, 0);
        gSPTextureRectangle(rect_disp++, 0, 0, ((2*SCREEN_WIDTH) - 1) << 2, ((2*SCREEN_HEIGHT) - 1) << 2, G_TX_RENDERTILE, 0, 0, (256) << G_TEXTURE_IMAGE_FRAC,
                            (256) << G_TEXTURE_IMAGE_FRAC);
#ifdef TARGET_PC
        /* Disable stretch so the inventory overlay UI gets 4:3 correction. */
        gDPNoOpTag(rect_disp++, PC_NOOP_WIDESCREEN_STRETCH_OFF);
#endif

        SET_POLY_OPA_DISP(rect_disp);
        return 1;
    }

    if ((GETREG(HREG, 80) != 10) || (GETREG(HREG, 85) != 0)) {
        PC_DIAG(3, "makeBumpTexture: before Actor_info_draw_actor\n");
#ifdef TARGET_PC
        if (Game_play_pc_menu_live_draw(play)) {
            /* the player's draw relinks the GameCube "submenu area" to the player code (load_player): keep the open menu linked */
            mSM_pc_area_save(&play->submenu);
            Actor_info_draw_actor(play, &play->actor_info);
            mSM_pc_area_restore(&play->submenu);
        } else
#endif
        {
            Actor_info_draw_actor(play, &play->actor_info);
        }
        PC_DIAG(3, "makeBumpTexture: Actor draw done, Camera2...\n");
        Camera2_draw(play);
        mMsg_Draw((GAME*)play);
    }

    if ((GETREG(HREG, 80) != 10) || (GETREG(HREG, 93) != 0)) {
        Debug_Display_output(play);
    }

    if ((play->submenu.mode == mSM_MODE_PRERENDER_INIT) || (play->fb_mode == FBDEMO_MODE_CREATE)) {
        old_fb = NOW_POLY_OPA_DISP;
        fb_disp = gfxopen(old_fb);

        gSPDisplayList(NEXT_LIGHT_DISP, fb_disp);
        copy_efb_to_texture(&fb_disp, &prbuf);
        gSPEndDisplayList(fb_disp++);

        gfxclose(old_fb, fb_disp);

        SET_POLY_OPA_DISP(fb_disp);

        if (play->submenu.mode == mSM_MODE_PRERENDER_INIT) {
            play->submenu.mode = mSM_MODE_PRERENDER_WAIT;
            SETREG(SREG, 3, -(GETREG(SREG, 3)));
        } else {
            play->fb_mode = FBDEMO_MODE_INIT;
        }
        return 1;
    }

    CLOSE_DISP(graph1);

    return 1;
}

static void draw_version(GRAPH* graph) {
    JW_JUTReport(0x82, 0x172, 1, "[CopyDate:%.19s]", boot_copyDate);
    JW_JUTReport(0x82, 0x180, 1, "[Date:%s]", &__DateTime__);
    JW_JUTReport(0x82, 0x18E, 1, "[Creator:%s]", &__Creator__);
}

static void Game_play_draw(GAME_PLAY* play) {
    GRAPH* graph = play->game.graph;
    u8 fill_r = 0;
    u8 fill_g = 0;
    u8 fill_b = 0;

    // @BUG - when removing display list segment function present in N64, the devs went slightly too far
    // and removed setting the background fill color while in the main field area.
#if defined(BUGFIXES) || defined(TARGET_PC)
    if (Common_Get(field_type) == mFI_FIELDTYPE2_FG && mEv_CheckTitleDemo() != mEv_TITLEDEMO_STAFFROLL) {
        fill_r = play->kankyo.base_light.background_color[0];
        fill_g = play->kankyo.base_light.background_color[1];
        fill_b = play->kankyo.base_light.background_color[2];
    }
#endif

    DisplayList_initialize(graph, fill_r, fill_g, fill_b, &play->game);
    PC_DIAG(3, "Game_play_draw: DL_init done\n");

    if ((GETREG(HREG, 80) != 10) || (GETREG(HREG, 82) != 0)) {
        setupFog(play, graph);
        PC_DIAG(3, "Game_play_draw: fog done\n");
        setupViewer(play);
        PC_DIAG(3, "Game_play_draw: viewer done\n");
        setupViewMatrix(play, graph, graph);
        PC_DIAG(3, "Game_play_draw: viewMtx done\n");

        if ((makeBumpTexture(play, graph, graph) == 1) && ((GETREG(HREG, 80) != 10) || (GETREG(HREG, 89) != 0))) {
            PC_DIAG(3, "Game_play_draw: bump done, drawing\n");
            watch_my_step_draw(play);
            banti_draw(play);
#ifdef TARGET_PC
            if (Game_play_pc_menu_live_draw(play)) {
                /* live world behind the menu: the menu's POLY_OPA / POLY_XLU commands are written into sub-lists inside those buffers
                 * (skipped there by a branch, the gfxopen / gfxclose idiom makeBumpTexture uses for the fades) and CALLED from
                 * LIGHT_DISP, i.e. after the whole world incl. its translucent pass; the sub-list starts with the depth clear. */
                Gfx* opa0;
                Gfx* xlu0;
                Gfx* g;

                OPEN_DISP(graph);
                opa0 = NOW_POLY_OPA_DISP;
                xlu0 = NOW_POLY_XLU_DISP;
                g = gfxopen(opa0);
                gSPDisplayList(NOW_LIGHT_DISP++, g);
                gDPPipeSync(g++);
                gDPNoOpTag(g++, PC_NOOP_CLEAR_DEPTH);
                gDPNoOpTag(g++, PC_NOOP_WIDESCREEN_STRETCH_OFF); /* the menu UI gets its 4:3 correction, as after the prbuf rect */
                SET_POLY_OPA_DISP(g);
                g = gfxopen(xlu0);
                gSPDisplayList(NOW_LIGHT_DISP++, g);
                SET_POLY_XLU_DISP(g);
                CLOSE_DISP(graph);

                mSM_submenu_draw(&play->submenu, (GAME*)play);

                OPEN_DISP(graph);
                g = NOW_POLY_OPA_DISP;
                gSPEndDisplayList(g++);
                gfxclose(opa0, g);
                SET_POLY_OPA_DISP(g);
                g = NOW_POLY_XLU_DISP;
                gSPEndDisplayList(g++);
                gfxclose(xlu0, g);
                SET_POLY_XLU_DISP(g);
                CLOSE_DISP(graph);
            } else
#endif
            {
                mSM_submenu_draw(&play->submenu, (GAME*)play);
            }
        }
    }
    if (zurumode_flag != 0) {
        switch (mEv_CheckTitleDemo()) {
            case mEv_TITLEDEMO_STAFFROLL:
            case mEv_TITLEDEMO_LOGO:
            default:
                draw_version(graph);
                break;

            case mEv_TITLEDEMO_NONE:
                break;
        }
    }
}

extern void play_main(GAME* game) {
    GAME_PLAY* play = (GAME_PLAY*)game;

    PC_DIAG(5, "play_main: enter scene=%d frame=%d\n", play->scene_id, play->game_frame);
    game->doing_point = 0;
    game->doing_point_specific = 0x6E;
    fqrand();
    game->doing_point = 0;
    game->doing_point_specific = 0x78;
    mCon_main(game);
    game->doing_point = 0;
    game->doing_point_specific = 0x82;
    Debug_Display_init();

    if ((GETREG(HREG, 80) == 10) && (GETREG(HREG, 94) != 10)) {
        SETREG(HREG, 81, 1);
        SETREG(HREG, 82, 1);
        SETREG(HREG, 83, 1);
        SETREG(HREG, 84, 3);
        SETREG(HREG, 85, 1);
        SETREG(HREG, 86, 1);
        SETREG(HREG, 87, 1);
        SETREG(HREG, 88, 1);
        SETREG(HREG, 89, 1);
        SETREG(HREG, 90, 15);
        SETREG(HREG, 91, 1);
        SETREG(HREG, 92, 1);
        SETREG(HREG, 93, 1);
        SETREG(HREG, 94, 10);
    }

    game->doing_point = 0;
    game->doing_point_specific = 0x8C;

    if ((GETREG(HREG, 80) != 10) || (GETREG(HREG, 81) != 0)) {
        Game_play_move(game);
    }
    game->doing_point = 0;
    game->doing_point_specific = 0xAA;
    Game_play_draw(play);

    game->doing_point = 0;
    game->doing_point_specific = 0xB4;

    {
        GRAPH* graph = game->graph;
        game_debug_draw_last(game, graph);
        game_draw_last(graph);
    }

    game->doing_point = 0;
    game->doing_point_specific = 0XBe;
}

static void Gameplay_Scene_Init(GAME_PLAY* play) {
    play->player_data = NULL;
    play->actor_data_num = 0;
    play->ctrl_actor_data_num = 0;
    play->obj_bank_data_num = 0;
    play->_20A8 = 0;

    mSc_data_bank_ct(play, &play->object_exchange);
    Global_light_ct(&play->global_light);
    Door_info_ct(&play->door_info);
    common_data_clear();
    Scene_ct(play, play->current_scene_data);
    mSc_decide_exchange_bank(&play->object_exchange);
}

static u8 mPl_SceneNo2SoundRoomType(int scene) {
    switch (scene) {
        case SCENE_MY_ROOM_S:
            return 1;

        case SCENE_NPC_HOUSE:
        case SCENE_SHOP0:
        case SCENE_BROKER_SHOP:
        case SCENE_POST_OFFICE:
        case SCENE_BUGGY:
        case SCENE_MY_ROOM_M:
        case SCENE_KAMAKURA:
        case SCENE_MY_ROOM_LL2:
        case SCENE_TENT:
            return 2;

        case SCENE_MY_ROOM_L:
        case SCENE_CONVENI:
        case SCENE_SUPER:
        case SCENE_DEPART:
        case SCENE_DEPART_2:
        case SCENE_MY_ROOM_LL1:
        case SCENE_COTTAGE_MY:
        case SCENE_POLICE_BOX:
            return 3;

        default:
            return 0;
    }
}

static void Gameplay_Scene_Read(GAME_PLAY* play, s16 idx) {
    static Scene_Word_u* scene_word_data[SCENE_NUM] = {
        test01_info,
        test02_info,
        test03_info,
        water_test_info,
        test_step01_info,
        test04_info,
        npc_room01_info,
        test_fd_npc_land_info,
        field_tool_field_info,
        shop01_info,
        BG_TEST01_info,
        BG_TEST01_XLU_info,
        broker_shop_info,
        fg_tool_in_info,
        post_office_info,
        start_demo1_info,
        start_demo2_info,
        police_box_info,
        buggy_info,
        player_select_info,
        player_room_s_info,
        player_room_m_info,
        player_room_l_info,
        shop02_info,
        shop03_info,
        shop04_1f_info,
        test05_info,
        PLAYER_SELECT2_info,
        PLAYER_SELECT3_info,
        shop04_2f_info,
        event_notification_info,
        kamakura_info,
        field_tool_field_info,
        title_demo_info,
        PLAYER_SELECT4_info,
        museum_entrance_info,
        museum_picture_info,
        museum_fossil_info,
        museum_insect_info,
        museum_fish_info,
        player_room_ll1_info,
        player_room_ll2_info,
        p_room_bm_s_info,
        p_room_bm_m_info,
        p_room_bm_l_info,
        p_room_bm_ll1_info,
        NEEDLEWORK_info,
        player_room_island_info,
        npc_room_island_info,
        start_demo3_info,
        lighthouse_info,
        tent_info,
    };

    Scene_data_status_c* current = &scene_data_status[idx];

    current->unk13 = 0;
    play->scene_data_status = current;
    play->scene_id = idx;
    play->current_scene_data = scene_word_data[idx];
    current->unk13 = 0;
    Gameplay_Scene_Init(play);
    sAdo_RoomType(mPl_SceneNo2SoundRoomType(Save_Get(scene_no)));
}
