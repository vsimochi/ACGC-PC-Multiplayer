#include "m_submenu.h"
#include "m_submenu_ovl.h"

#include "m_common_data.h"
#include "m_name_table.h"
#include "m_player_lib.h"
#include "m_msg.h"
#include "m_quest.h"
#include "libultra/libultra.h"

#ifdef PC_ENHANCEMENTS
#include "m_museum_display.h"
#endif

#if defined(TARGET_PC) /* test hook: runtime-gated by pc_test_hook_getenv (AC_TEST_HOOKS=1 in a PC_TEST_HOOKS build), same as m_msg_main.c_inc */
#include "pc_test_hooks.h"
#include "pc_net_game.h"
#include <stdio.h>
#include <stdlib.h>
/* TEST-ONLY (AC_TEST_HOOKS=1 + AC_TEST_HOST_SUBMENU="open_after_ms,hold_ms"): on a HOST with a READY client, open the
 * inventory once open_after_ms after the first READY peer is seen, and close it through the overlay's own close path
 * hold_ms later; logs a pockets+wallet checksum at open and at close so a test can prove nothing changed. Zero effect
 * otherwise: pc_test_hook_getenv returns NULL unless PC_TEST_HOOKS build + AC_TEST_HOOKS=1, read once, then stage 3. */
static int s_pc_th_sm_stage = 0; /* 0 idle, 1 opened, 2 close requested, 3 done */
static unsigned s_pc_th_sm_t0 = 0, s_pc_th_sm_t_open = 0, s_pc_th_sm_hold = 0;

static unsigned mSM_pc_th_pocket_sum(void) {
    Private_c* priv = Common_Get(now_private);
    unsigned sum = 0;
    int i;

    if (priv == NULL) {
        return 0;
    }
    for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
        sum = sum * 31u + (unsigned)priv->inventory.pockets[i];
    }
    return sum * 31u + (unsigned)priv->inventory.wallet;
}

static int mSM_pc_th_want_open(void) {
    static int parsed = 0;
    static unsigned after_ms = 0;
    const char* e;
    unsigned now;

    if (s_pc_th_sm_stage != 0) {
        return FALSE;
    }
    if (!parsed) {
        unsigned a = 0, h = 0;
        parsed = 1;
        if ((e = pc_test_hook_getenv("AC_TEST_HOST_SUBMENU")) == NULL || sscanf(e, "%u,%u", &a, &h) != 2) {
            s_pc_th_sm_stage = 3;
            return FALSE;
        }
        after_ms = a;
        s_pc_th_sm_hold = h;
    }
    if (pc_net_game_host_ready_peer_count() <= 0) {
        return FALSE;
    }
    now = (unsigned)pc_net_game_now_ms();
    if (s_pc_th_sm_t0 == 0) {
        s_pc_th_sm_t0 = now | 1u;
    }
    return (unsigned)(now - s_pc_th_sm_t0) >= after_ms;
}

/* TEST-ONLY, any role (the autopilot command "submenu TYPE|close", pc_net_game.c, PC_NET_TEST_HOOKS builds with AC_TEST_AUTOPILOT;
 * nothing else ever writes it): > 0 = open submenu TYPE (mSM_OVL_*) from free play at the next chance, -1 = close the open one through
 * its overlay's own close path. Opens and closes log "[TEST][SUBMENU] <role> menu T OPEN|CLOSED pockets_sum=..." for the
 * "world keeps running behind menus" tests (test_menu_world_live_real.py). Always 0 in normal play. */
int g_pc_th_submenu_req = 0;
int g_pc_th_submenu_data0 = 0; /* TEST-ONLY (Nook bulk sell basket real test, pc_sell_ui.c): data0 of the next test open (0 = the default), consumed by it */
static int s_pc_th_ap_type = 0; /* the menu the autopilot opened (0 = none) */

static int mSM_pc_th_ap_close_dir(int type) {
    switch (type) {
        case mSM_OVL_INVENTORY:
            return mSM_MOVE_OUT_RIGHT;
        default:
            return mSM_MOVE_OUT_TOP; /* map, notice board, calendar */
    }
}

static const char* mSM_pc_th_role(void) {
    return pc_net_game_role() == PC_NETGAME_ROLE_HOST ? "host" : pc_net_game_role() == PC_NETGAME_ROLE_CLIENT ? "client" : "single";
}

static void mSM_pc_th_play_tick(Submenu* submenu) {
    mSM_MenuInfo_c* mi;

    if (g_pc_th_submenu_req == -1 && s_pc_th_ap_type != 0 && submenu->overlay != NULL &&
        submenu->menu_type == s_pc_th_ap_type) {
        mi = &submenu->overlay->menu_info[s_pc_th_ap_type];
        if (mi->proc_status == mSM_OVL_PROC_PLAY) {
            submenu->overlay->move_chg_base_proc(mi, mSM_pc_th_ap_close_dir(s_pc_th_ap_type));
            g_pc_th_submenu_req = 0;
            printf("[TEST][SUBMENU] %s menu %d close requested\n", mSM_pc_th_role(), s_pc_th_ap_type);
        }
    }
    if (s_pc_th_sm_stage != 1 || submenu->menu_type != mSM_OVL_INVENTORY || submenu->overlay == NULL ||
        (unsigned)(pc_net_game_now_ms() - s_pc_th_sm_t_open) < s_pc_th_sm_hold) {
        return;
    }
    mi = &submenu->overlay->menu_info[mSM_OVL_INVENTORY];
    if (mi->proc_status == mSM_OVL_PROC_PLAY) {
        submenu->overlay->move_chg_base_proc(mi, mSM_MOVE_OUT_RIGHT);
        s_pc_th_sm_stage = 2;
        printf("[TEST][SUBMENU] host inventory close requested\n");
    }
}
#endif

static mSM_dlftbl_c SubmenuArea_dlftbl[mSM_DLF_NUM] = { { NULL, 0, 0, 0, 0, 0, "submenu_ovl" },
                                                        { NULL, 0, 0, 0, 0, 0, "player_actor" } };

static mSM_dlftbl_c* SubmenuArea_visit = NULL;
static void* SubmenuArea_allocp = NULL;

extern int mSM_COLLECT_INSECT_GET(int idx) {
    u32 ftr_idx = (0x2F2 << 2) + (idx << 2);
    return (Common_Get(now_private)->furniture_collected_bitfield[ftr_idx >> 2 >> 5] &
            (1 << ((ftr_idx >> 2) & 0x1F))) != 0;
}

extern void mSM_COLLECT_INSECT_SET(int idx) {
    u32 ftr_idx = (0x2F2 << 2) + (idx << 2);
    Common_Get(now_private)->furniture_collected_bitfield[ftr_idx >> 2 >> 5] |= (1 << ((ftr_idx >> 2) & 0x1F));
}

static int mSM_CHECK_ALL_INSECT_GET_SUB() {
    int i;
    int n = 0;

    for (i = 0; i < INSECT_ONLY_NUM; i++) {
        if (mSM_COLLECT_INSECT_GET(i) != FALSE) {
            n++;
        }
    }

    return n;
}

extern int mSM_CHECK_ALL_INSECT_GET() {
    if (mSM_CHECK_ALL_INSECT_GET_SUB() == INSECT_ONLY_NUM) {
        return TRUE;
    }

    return FALSE;
}

extern int mSM_CHECK_LAST_INSECT_GET(int idx) {
    int res = FALSE;

    if (mSM_CHECK_ALL_INSECT_GET_SUB() == (INSECT_ONLY_NUM - 1) && mSM_COLLECT_INSECT_GET(idx) == FALSE) {
        res = TRUE;
    }

    return res;
}

extern int mSM_COLLECT_FISH_GET(int idx) {
    u32 ftr_idx = (0x31A << 2) + (idx << 2);
    return (Common_Get(now_private)->furniture_collected_bitfield[ftr_idx >> 2 >> 5] &
            (1 << ((ftr_idx >> 2) & 0x1F))) != 0;
}

extern void mSM_COLLECT_FISH_SET(int idx) {
    u32 ftr_idx = (0x31A << 2) + (idx << 2);
    Common_Get(now_private)->furniture_collected_bitfield[ftr_idx >> 2 >> 5] |= (1 << ((ftr_idx >> 2) & 0x1F));
}

static int mSM_CHECK_ALL_FISH_GET_SUB() {
    int i;
    int n = 0;

    for (i = 0; i < FISH_NUM; i++) {
        if (mSM_COLLECT_FISH_GET(i) != FALSE) {
            n++;
        }
    }

    return n;
}

extern int mSM_CHECK_ALL_FISH_GET() {
    if (mSM_CHECK_ALL_FISH_GET_SUB() == FISH_NUM) {
        return TRUE;
    }

    return FALSE;
}

extern int mSM_CHECK_LAST_FISH_GET(int idx) {
    int res = FALSE;

    if (mSM_CHECK_ALL_FISH_GET_SUB() == (FISH_NUM - 1) && mSM_COLLECT_FISH_GET(idx) == FALSE) {
        res = TRUE;
    }

    return res;
}

extern int SubmenuArea_IsPlayer() {
    return SubmenuArea_visit == &SubmenuArea_dlftbl[mSM_DLF_PLAYER_ACTOR];
}

#if defined(TARGET_PC)
/* "World behind menus" (m_play.c live draw): drawing the live world while a submenu is open runs the player's draw through
 * Player_actor_draw_call (m_player_call.c), whose load_player() relinks the shared GameCube "submenu area" to the player code. That
 * would unlink the open submenu (mSM_submenu_draw then draws nothing, and every SubmenuArea_IsPlayer() check would flip). On PC the
 * area is pure bookkeeping (nothing is loaded), so the live world draw is bracketed by save / restore of exactly that bookkeeping. */
static struct {
    int valid;
    mSM_dlftbl_c dlftbl[mSM_DLF_NUM];
    mSM_dlftbl_c* visit;
    char* overlay_address;
    char* next_overlay_address;
} s_pc_area_save;

extern void mSM_pc_area_save(Submenu* submenu) {
    bcopy(SubmenuArea_dlftbl, s_pc_area_save.dlftbl, sizeof(SubmenuArea_dlftbl));
    s_pc_area_save.visit = SubmenuArea_visit;
    s_pc_area_save.overlay_address = (char*)submenu->overlay_address;
    s_pc_area_save.next_overlay_address = (char*)submenu->next_overlay_address;
    s_pc_area_save.valid = 1;
}

extern void mSM_pc_area_restore(Submenu* submenu) {
    if (!s_pc_area_save.valid) {
        return;
    }
    bcopy(s_pc_area_save.dlftbl, SubmenuArea_dlftbl, sizeof(SubmenuArea_dlftbl));
    SubmenuArea_visit = s_pc_area_save.visit;
    submenu->overlay_address = s_pc_area_save.overlay_address;
    submenu->next_overlay_address = s_pc_area_save.next_overlay_address;
    s_pc_area_save.valid = 0;
}
#endif

static void mSM_load_player_anime(GAME_PLAY* play) {
    // stubbed
}

static void SubmenuArea_DoLink(mSM_dlftbl_c* dlftbl, Submenu* submenu, int dlf_idx) {
    dlftbl->_00 = SubmenuArea_allocp;
    dlftbl->_14 = 0;
    SubmenuArea_visit = dlftbl;
    submenu->overlay_address = dlftbl->_00;
    submenu->next_overlay_address = dlftbl->_00;
}

static void SubmenuArea_DoUnlink(mSM_dlftbl_c* dlftbl, Submenu* submenu) {
    if (dlftbl->_00 != NULL) {
        submenu->overlay_address = dlftbl->_00;
        submenu->next_overlay_address = dlftbl->_00;
        dlftbl->_14 = 0;
        dlftbl->_00 = NULL;
        SubmenuArea_visit = NULL;
    }
}

static int mSM_ovlptr_dllcnv_sub(void* proc, mSM_dlftbl_c* dlftbl, Submenu* submenu, int dlf_idx) {
    if (dlf_idx == mSM_DLF_SUBMENU_OVL || dlf_idx == mSM_DLF_PLAYER_ACTOR) {
        SubmenuArea_DoLink(dlftbl, submenu, dlf_idx);
        return TRUE;
    }

    return FALSE;
}

extern void* mSM_ovlptr_dllcnv(void* proc, Submenu* submenu, int dlf_idx) {
    if (SubmenuArea_visit != NULL) {
        return proc; /* overlay already loaded, pointer is valid */
    } else if (mSM_ovlptr_dllcnv_sub(proc, SubmenuArea_dlftbl, submenu, dlf_idx) == FALSE) {
        return NULL;
    }

    return proc;
}

extern void mSM_submenu_ovlptr_init(GAME_PLAY* play) {
    SubmenuArea_allocp = (void*)1;
    SubmenuArea_visit = NULL;
}

extern void mSM_submenu_ovlptr_cleanup(Submenu* submenu) {
    if (SubmenuArea_visit != NULL) {
        SubmenuArea_DoUnlink(SubmenuArea_visit, submenu);
        SubmenuArea_visit = NULL;
    }

    SubmenuArea_allocp = NULL;
}

extern void load_player(Submenu* submenu) {
    mSM_dlftbl_c* dlftbl = &SubmenuArea_dlftbl[mSM_DLF_PLAYER_ACTOR];

    if (SubmenuArea_visit != dlftbl) {
        if (SubmenuArea_visit != NULL) {
            SubmenuArea_DoUnlink(SubmenuArea_visit, submenu);
        }

        SubmenuArea_DoLink(dlftbl, submenu, mSM_DLF_PLAYER_ACTOR);
    }
}

extern void mSM_submenu_ct(Submenu* submenu) {
    bzero(submenu, sizeof(Submenu));
    submenu->process_status = mSM_PROCESS_WAIT;
    submenu->wait_timer = 0;

    if (Common_Get(submenu_disabled) == TRUE) {
        submenu->start_refuse = TRUE;
        Common_Set(submenu_disabled, FALSE);
    }

    submenu->move_proc = (SUBMENU_PROC)&none_proc1;
    submenu->draw_proc = (SUBMENU_GAME_PROC)&none_proc1;
}

extern void mSM_submenu_dt(Submenu* submenu) {
    // stubbed
}

extern void mSM_open_submenu(Submenu* submenu, int type, int arg0, int arg1) {
    mSM_open_submenu_new2(submenu, type, arg0, arg1, NULL, 0);
}

extern void mSM_open_submenu_new(Submenu* submenu, int type, int arg0, int arg1, void* arg2) {
    mSM_open_submenu_new2(submenu, type, arg0, arg1, arg2, 0);
}

extern void mSM_open_submenu_new2(Submenu* submenu, int type, int arg0, int arg1, void* arg2, int arg3) {
    submenu->menu_type = type;
    submenu->param0 = arg0;
    submenu->param1 = arg1;
    submenu->param2 = arg2;
    submenu->param3 = arg3;
}

static void mSM_Reset_player_btn_type1(GAME_PLAY* play) {
    PLAYER_ACTOR* player = GET_PLAYER_ACTOR(play);

    if (player != NULL) {
        player->a_btn_pressed = FALSE;
        player->a_btn_triggers_submenu = TRUE;
    }
}

static void mSM_Reset_player_btn_type2(GAME_PLAY* play) {
    PLAYER_ACTOR* player = GET_PLAYER_ACTOR(play);

    if (player != NULL) {
        player->a_btn_triggers_submenu = TRUE;
    }
}

static int mSM_check_open_map_new(GAME_PLAY* play) {
    PLAYER_ACTOR* player = GET_PLAYER_ACTOR(play);
    int open_map = FALSE;
    int bx;
    int bz;

    if (mSM_CHECK_OPEN_MAP()) {
        open_map = TRUE;
    }

    if (open_map == FALSE) {
        return FALSE;
    }

    if (player == NULL) {
        return FALSE;
    }

    if (Save_Get(scene_no) == SCENE_COTTAGE_MY || Save_Get(scene_no) == SCENE_COTTAGE_NPC) {
        return FALSE;
    }

    mFI_Wpos2BlockNum(&bx, &bz, player->actor_class.world.position);
    return mFI_CheckBlockKind_OR(bx, bz, mRF_BLOCKKIND_OCEAN) == FALSE;
}

extern void mSM_submenu_ctrl(GAME_PLAY* play) {
    Submenu* submenu = &play->submenu;
    int open_inventory;

    if (submenu->process_status != mSM_PROCESS_WAIT) {
        return;
    }

    if (play->fb_fade_type != FADE_TYPE_NONE) {
        return;
    }

    if (play->fb_wipe_mode != WIPE_MODE_NONE) {
        return;
    }

    open_inventory = FALSE;
    if (chkTrigger(mSM_INV_BUTTON_0) || chkTrigger(mSM_INV_BUTTON_1)) {
        open_inventory = TRUE;
    }
#if defined(TARGET_PC) /* test hook: runtime-gated by pc_test_hook_getenv (AC_TEST_HOOKS=1 in a PC_TEST_HOOKS build), same as m_msg_main.c_inc */
    if (!open_inventory && mSM_pc_th_want_open() && mPlib_able_submenu_type1((GAME*)play)) {
        open_inventory = TRUE;
        s_pc_th_sm_stage = 1;
        s_pc_th_sm_t_open = (unsigned)pc_net_game_now_ms();
        printf("[TEST][SUBMENU] host inventory OPEN pockets_sum=%08X ready_peers=%d\n", mSM_pc_th_pocket_sum(),
               pc_net_game_host_ready_peer_count());
    }
    if (!open_inventory && g_pc_th_submenu_req > 0 && Common_Get(reset_flag) == FALSE && submenu->start_refuse == FALSE &&
        submenu->start_refuse_timer == 0 && mPlib_able_submenu_type1((GAME*)play)) {
        int t = g_pc_th_submenu_req;

        g_pc_th_submenu_req = 0;
        s_pc_th_ap_type = t;
        if (t == mSM_OVL_MAP) {
            mSM_open_submenu(submenu, mSM_OVL_MAP, 1, 0); /* as the map button (mSM_check_open_map_new) */
        } else if (t == mSM_OVL_CALENDAR) {
            mSM_open_submenu(submenu, mSM_OVL_CALENDAR, 0, Common_Get(player_no)); /* as the room calendar */
        } else {
            mSM_open_submenu(submenu, t, g_pc_th_submenu_data0, 0); /* inventory, notice board (data0 0; the sell basket test: HANIWA_ENTRUST) */
            g_pc_th_submenu_data0 = 0;
        }
        mSM_Reset_player_btn_type2(play);
        printf("[TEST][SUBMENU] %s menu %d OPEN pockets_sum=%08X live=%d\n", mSM_pc_th_role(), t, mSM_pc_th_pocket_sum(),
               pc_net_game_menu_world_live());
    }
#endif

    if (((open_inventory && Common_Get(reset_flag) == FALSE) ||
         (mSM_check_open_map_new(play) == TRUE && Common_Get(reset_flag) == FALSE)) &&
        submenu->start_refuse == FALSE && submenu->start_refuse_timer == 0 && mPlib_able_submenu_type1((GAME*)play) &&
        mEv_CheckFirstIntro() == FALSE) {
        if (open_inventory) {
            mSM_open_submenu(submenu, mSM_OVL_INVENTORY, 0, 0);
        } else {
            mSM_open_submenu(submenu, mSM_OVL_MAP, 1, 0);
        }

        mSM_Reset_player_btn_type2(play);
    } else {
        PLAYER_ACTOR* player = GET_PLAYER_ACTOR(play);

        if (player != NULL && player->a_btn_pressed == TRUE && Common_Get(reset_flag) == FALSE &&
            submenu->start_refuse == FALSE && submenu->start_refuse_timer == 0 &&
            mPlib_able_submenu_type1((GAME*)play)) {
            s16 y_dir = player->actor_class.shape_info.rotation.y;
            int dir;

            y_dir -= -0x8000;
            dir = ABS(y_dir);

            if (dir < DEG2SHORT_ANGLE(45.0f)) {
                switch (player->item_in_front) {
                    case MESSAGE_BOARD1:
                    case MESSAGE_BOARD0:
                        mSM_open_submenu(submenu, mSM_OVL_NOTICE, 0, 0);
                        mSM_Reset_player_btn_type1(play);
                        break;
                    case MAP_BOARD1:
                    case MAP_BOARD0:
                        mSM_open_submenu(submenu, mSM_OVL_MAP, 0, 0);
                        mSM_Reset_player_btn_type1(play);
                        break;
                }
            }
        }
    }

    if (submenu->menu_type != mSM_OVL_NONE) {
        submenu->process_status = mSM_PROCESS_PREWAIT;
        submenu->mode = mSM_MODE_PRERENDER_INIT;
        SetGameFrame(1);
    }
}

static void mSM_move_Wait(Submenu* submenu) {
    static float wait_accum = 0.0f;
    int ticks = graph_dt_60hz_ticks(gamePT, &wait_accum);

    if (submenu->wait_timer != 0) {
        submenu->wait_timer -= ticks;
        if (submenu->wait_timer < 0) {
            submenu->wait_timer = 0;
        }
    }

    if (submenu->start_refuse_timer != 0) {
        submenu->start_refuse_timer = ticks >= submenu->start_refuse_timer ? 0 : submenu->start_refuse_timer - ticks;
    }
}

static void mSM_move_PREWait(Submenu* submenu) {
    if (submenu->mode >= mSM_MODE_PRERENDER_DONE) {
        submenu->process_status = mSM_PROCESS_LINKWAIT;
    }
}

static void mSM_move_LINKWait(Submenu* submenu) {
    Submenu_Item_c* item;
    mSM_dlftbl_c* dlftbl = &SubmenuArea_dlftbl[mSM_DLF_SUBMENU_OVL];
    int i;

    if (SubmenuArea_visit != dlftbl) {
        if (SubmenuArea_visit != NULL) {
            SubmenuArea_DoUnlink(SubmenuArea_visit, submenu);
        }

        SubmenuArea_DoLink(dlftbl, submenu, mSM_DLF_SUBMENU_OVL);
        submenu->move_proc = (SUBMENU_PROC)mSM_ovlptr_dllcnv(&mSM_menu_ovl_init, submenu, mSM_DLF_SUBMENU_OVL);
        submenu->draw_proc = (SUBMENU_GAME_PROC)&none_proc1;
        submenu->process_status = mSM_PROCESS_PLAY;
        submenu->open_flag = TRUE;
        submenu->after_mode = 7;
        submenu->unk_164 = 0;
        mMl_clear_mail(&submenu->mail);
        submenu->item_p = &submenu->items[0];
        submenu->item_num = mPr_POCKETS_SLOT_COUNT;

        item = submenu->item_p;
        for (i = 0; i < submenu->item_num; i++) {
            item->item = EMPTY_NO;
            item->slot_no = mPr_POCKETS_SLOT_COUNT;
            item++;
        }

        if (submenu->mode != mSM_MODE_OTHER) {
            if ((submenu->menu_type == mSM_OVL_LEDIT && submenu->param0 == 0) ||
                Common_Get(now_private)->gender == mPr_SEX_MALE) {
                sAdo_SpecChange(5);
            } else {
                sAdo_SpecChange(6);
            }

            sAdo_SetVoiceMode(0);
        }
    }
}

static void mSM_move_Play(Submenu* submenu) {
#if defined(TARGET_PC) /* test hook: runtime-gated by pc_test_hook_getenv (AC_TEST_HOOKS=1 in a PC_TEST_HOOKS build), same as m_msg_main.c_inc */
    mSM_pc_th_play_tick(submenu);
#endif
    (*submenu->move_proc)(submenu);
}

static void mSM_move_End(Submenu* submenu) {
    mSM_dlftbl_c* dlftbl = &SubmenuArea_dlftbl[mSM_DLF_SUBMENU_OVL];
    GAME_PLAY* play = (GAME_PLAY*)gamePT;

    (*submenu->move_proc)(submenu);
    submenu->process_status = mSM_PROCESS_WAIT;
    submenu->menu_type = mSM_OVL_NONE;
    submenu->wait_timer = 2;
    submenu->open_flag = FALSE;
    SetGameFrame(1);
#if defined(TARGET_PC) /* test hook: runtime-gated by pc_test_hook_getenv (AC_TEST_HOOKS=1 in a PC_TEST_HOOKS build), same as m_msg_main.c_inc */
    if (s_pc_th_sm_stage == 1 || s_pc_th_sm_stage == 2) {
        s_pc_th_sm_stage = 3;
        printf("[TEST][SUBMENU] host inventory CLOSED pockets_sum=%08X\n", mSM_pc_th_pocket_sum());
    }
    if (s_pc_th_ap_type != 0) {
        printf("[TEST][SUBMENU] %s menu %d CLOSED pockets_sum=%08X\n", mSM_pc_th_role(), s_pc_th_ap_type, mSM_pc_th_pocket_sum());
        s_pc_th_ap_type = 0;
    }
#endif

    if (submenu->mode != mSM_MODE_OTHER) {
        mMsg_Window_c* msg_win = mMsg_Get_base_window_p();
        submenu->mode = mSM_MODE_IDLE;
        mSc_dmacopy_all_exchange_bank(&play->object_exchange);
        SubmenuArea_DoUnlink(dlftbl, submenu);
        load_player(submenu);
        mSM_load_player_anime(play);
        submenu->start_refuse_timer = 1;

        if (submenu->after_mode == 12) {
            mPlib_request_main_demo_wait_from_submenu((ACTOR*)submenu->overlay->menu_info[mSM_OVL_MSCORE].data2);
        }

        if (mMsg_Check_main_hide(msg_win) == FALSE && mMsg_Check_not_series_main_wait(msg_win)) {
            mMsg_sound_spec_change_voice(msg_win);
        }
    }
}

extern void mSM_submenu_move(Submenu* submenu) {
    static SUBMENU_PROC move_proc[mSM_PROCESS_NUM] = { &mSM_move_Wait, &mSM_move_PREWait, &mSM_move_LINKWait,
                                                       &mSM_move_Play, &mSM_move_End };

    (*move_proc[submenu->process_status])(submenu);
}

extern void mSM_submenu_draw(Submenu* submenu, GAME* game) {
    if (submenu->mode >= mSM_MODE_PRERENDER_DONE && submenu->process_status == mSM_PROCESS_PLAY &&
        SubmenuArea_visit == &SubmenuArea_dlftbl[mSM_DLF_SUBMENU_OVL]) {
        (*submenu->draw_proc)(submenu, game);
    }
}

static int mSM_check_item_for_furniture(int slot_no, int param_2) {
    Private_c* priv = Common_Get(now_private);
    mActor_name_t item = priv->inventory.pockets[slot_no];
    int cat = ITEM_NAME_GET_CAT(item);
    int res = FALSE;

    if (item != EMPTY_NO && mPr_GET_ITEM_COND(priv->inventory.item_conditions, slot_no) == mPr_ITEM_COND_NORMAL &&
        ITEM_NAME_GET_TYPE(item) == NAME_TYPE_ITEM1 && cat != ITEM1_CAT_FISH && cat != ITEM1_CAT_KABU &&
        cat != ITEM1_CAT_INSECT && item != ITM_KNIFE_AND_FORK &&
        !(item >= ITM_EXCERCISE_CARD00 && item <= ITM_EXCERCISE_CARD12)) {
        res = TRUE;
    }

    return res;
}

static int mSM_check_item_for_quest(int slot_no, int param_2) {
    Private_c* priv = Common_Get(now_private);
    mActor_name_t item = priv->inventory.pockets[slot_no];
    int res = FALSE;

    if (item != EMPTY_NO && !(item >= ITM_EXCERCISE_CARD00 && item <= ITM_EXCERCISE_CARD12) &&
        item != ITM_KNIFE_AND_FORK && !(item >= ITM_SPIRIT0 && item <= ITM_SPIRIT4)) {
        res = TRUE;
    }

    return res;
}

static int mSM_check_item_for_sell(int slot_no, int param_2) {
    Private_c* priv = Common_Get(now_private);
    mActor_name_t item = priv->inventory.pockets[slot_no];
    int res = FALSE;

    if (item != EMPTY_NO && mPr_GET_ITEM_COND(priv->inventory.item_conditions, slot_no) == mPr_ITEM_COND_NORMAL &&
        (ITEM_NAME_GET_TYPE(item) != NAME_TYPE_ITEM1 || ITEM_NAME_GET_CAT(item) != ITEM1_CAT_MONEY) &&
        !(item >= ITM_EXCERCISE_CARD00 && item <= ITM_EXCERCISE_CARD12) && item != ITM_KNIFE_AND_FORK &&
        !(item >= ITM_SPIRIT0 && item <= ITM_SPIRIT4)) {
        res = TRUE;
    }

    return res;
}

static int mSM_check_item_for_give(int slot_no, int param_2) {
    Private_c* priv = Common_Get(now_private);
    mActor_name_t item = priv->inventory.pockets[slot_no];
    int res = FALSE;

    if (item != EMPTY_NO && mPr_GET_ITEM_COND(priv->inventory.item_conditions, slot_no) == mPr_ITEM_COND_NORMAL &&
        !(item >= ITM_EXCERCISE_CARD00 && item <= ITM_EXCERCISE_CARD12) && item != ITM_KNIFE_AND_FORK &&
        !(item >= ITM_SPIRIT0 && item <= ITM_SPIRIT4)) {
        res = TRUE;
    }

    return res;
}

static int mSM_check_item_for_take(int slot_no, int param_2) {
    Private_c* priv = Common_Get(now_private);
    mActor_name_t item = priv->inventory.pockets[slot_no];
    int cat = ITEM_NAME_GET_CAT(item);
    int res = FALSE;

    if (item != EMPTY_NO && mPr_GET_ITEM_COND(priv->inventory.item_conditions, slot_no) == mPr_ITEM_COND_NORMAL &&
        !(item >= ITM_EXCERCISE_CARD00 && item <= ITM_EXCERCISE_CARD12) && item != ITM_KNIFE_AND_FORK &&
        !(item >= ITM_SPIRIT0 && item <= ITM_SPIRIT4) &&
        (param_2 == 0 || (ITEM_NAME_GET_TYPE(item) == NAME_TYPE_ITEM1 &&
                          ((cat == ITEM1_CAT_FISH && param_2 == 1) || (cat == ITEM1_CAT_INSECT && param_2 == 2))))) {
        res = TRUE;
    }

    return res;
}

static int mSM_check_item_for_minidisk(int slot_no, int param_2) {
    Private_c* priv = Common_Get(now_private);
    mActor_name_t item = priv->inventory.pockets[slot_no];

    if (mPr_GET_ITEM_COND(priv->inventory.item_conditions, slot_no) == mPr_ITEM_COND_NORMAL &&
        ITEM_NAME_GET_TYPE(item) == NAME_TYPE_ITEM1 && ITEM_NAME_GET_CAT(item) == ITEM1_CAT_MINIDISK) {
        return TRUE;
    }

    return FALSE;
}

static int mSM_check_item_for_shrine(int slot_no, int param_2) {
    Private_c* priv = Common_Get(now_private);
    if (mPr_GET_ITEM_COND(priv->inventory.item_conditions, slot_no) == mPr_ITEM_COND_QUEST &&
        mQst_CheckLimitbyPossessionIdx(slot_no)) {
        return TRUE;
    }

    return FALSE;
}

static int mSM_check_item_for_entrust(int slot_no, int param_2) {
    Private_c* priv = Common_Get(now_private);
    mActor_name_t item = priv->inventory.pockets[slot_no];
    int res = FALSE;

    if (item == EMPTY_NO ||
        mPr_GET_ITEM_COND(priv->inventory.item_conditions, slot_no) == mPr_ITEM_COND_NORMAL &&
            (ITEM_NAME_GET_TYPE(item) != NAME_TYPE_ITEM1 || ITEM_NAME_GET_CAT(item) != ITEM1_CAT_MONEY) &&
            !(item >= ITM_EXCERCISE_CARD00 && item <= ITM_EXCERCISE_CARD12) && item != ITM_KNIFE_AND_FORK &&
            !(item >= ITM_SPIRIT0 && item <= ITM_SPIRIT4)) {
        res = TRUE;
    }

    return res;
}

static int mSM_check_item_for_exchange(int slot_no, int exchange_id) {
    int res = FALSE;
    Private_c* priv = Common_Get(now_private);
    mActor_name_t item = priv->inventory.pockets[slot_no];

    if (item != EMPTY_NO && mPr_GET_ITEM_COND(priv->inventory.item_conditions, slot_no) == mPr_ITEM_COND_NORMAL &&
        item != ITM_SIGNBOARD && item != ITM_KNIFE_AND_FORK &&
        !(item >= ITM_EXCERCISE_CARD00 && item <= ITM_EXCERCISE_CARD12)) {
        if ((ITEM_NAME_GET_TYPE(item) == NAME_TYPE_ITEM1 && ITEM_NAME_GET_CAT(item) == ITEM1_CAT_FISH) &&
            (ITEM_NAME_GET_TYPE(exchange_id) != NAME_TYPE_ITEM1 || ITEM_NAME_GET_CAT(exchange_id) != ITEM1_CAT_FISH)) {
            PLAYER_ACTOR* player = GET_PLAYER_ACTOR_NOW();
            xyz_t water_pos;

            if (mCoBG_SearchWaterLimitDistN(&water_pos, player->actor_class.world.position,
                                            player->actor_class.shape_info.rotation.y, 120.0f, 12) == FALSE) {
                return FALSE;
            }

            xyz_t_move(&((GAME_PLAY*)gamePT)->submenu.water_pos, &water_pos);
        }

        res = TRUE;
    }

    return res;
}

static int mSM_check_item_for_curator(int slot_no, int param_2) {
    Private_c* priv = Common_Get(now_private);
    mActor_name_t item = priv->inventory.pockets[slot_no];
    int res = FALSE;

    if (item != EMPTY_NO && mPr_GET_ITEM_COND(priv->inventory.item_conditions, slot_no) == mPr_ITEM_COND_NORMAL &&
        item != ITM_KNIFE_AND_FORK && !(item >= ITM_EXCERCISE_CARD00 && item <= ITM_EXCERCISE_CARD12)) {
#ifdef PC_ENHANCEMENTS
        /* only show items the curator can accept */
        if (item == ITM_FOSSIL) {
            res = TRUE; /* fossils are always selectable */
        } else if (mMmd_GetDisplayInfo(item) == mMmd_DISPLAY_CAN_DONATE) {
            res = TRUE;
        }
#else
        res = TRUE;
#endif
    }

    return res;
}

typedef int (*mSM_INVENTORY_CHECK_PROC)(int, int);

extern int mSM_check_open_inventory_itemlist(int type, int param_2) {
    static mSM_INVENTORY_CHECK_PROC check_process[mSM_IV_OPEN_NUM] = { NULL,
                                                                       NULL,
                                                                       &mSM_check_item_for_entrust,
                                                                       NULL,
                                                                       &mSM_check_item_for_quest,
                                                                       &mSM_check_item_for_sell,
                                                                       &mSM_check_item_for_give,
                                                                       NULL,
                                                                       &mSM_check_item_for_take,
                                                                       &mSM_check_item_for_furniture,
                                                                       &mSM_check_item_for_minidisk,
                                                                       &mSM_check_item_for_shrine,
                                                                       NULL,
                                                                       &mSM_check_item_for_exchange,
                                                                       NULL,
                                                                       &mSM_check_item_for_curator,
                                                                       NULL };

    mSM_INVENTORY_CHECK_PROC check_proc = check_process[type];
    int i;
    int res = 0;

    if (check_proc == NULL) {
        return 0xFFFF; /* all slots are valid */
    }

    for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
        if ((*check_proc)(i, param_2) != FALSE) {
            res |= (1 << i); // slot is valid
        }
    }

    return res;
}

static char* mSM_Object_Exchange_keep_new(GAME_PLAY* play, s16 bank_id, size_t size) {
    Object_Exchange_c* exchange = &play->object_exchange;
    Object_Bank_c* bank = &exchange->banks[exchange->bank_idx];

    bank->bank_id = bank_id;
    bank->dma_start = exchange->next_bank_ram_address;
    bank->ram_start = exchange->next_bank_ram_address;
    bank->size = size;

    if (exchange->bank_idx < mSc_OBJECT_BANK_NUM - 1) {
        exchange->next_bank_ram_address = (char*)ALIGN_NEXT((u32)(exchange->next_bank_ram_address + size), 16);
        exchange->bank_idx++;
    }

    return bank->dma_start;
}

static void mSM_Object_Exchange_keep_new_MenuTexAndPallet(GAME_PLAY* play, int idx) {
    char* tex_p = mSM_Object_Exchange_keep_new(play, ACTOR_OBJ_BANK_14, (32 * 32) / 2);
    char* pal_p = mSM_Object_Exchange_keep_new(play, ACTOR_OBJ_BANK_15, 16 * sizeof(u16));

    play->submenu_ground_tex[idx] = tex_p;
    play->submenu_ground_pallet[idx] = pal_p;
    mPlib_Load_PlayerTexAndPallet(tex_p, pal_p, Common_Get(now_private)->backgound_texture - ITM_CLOTH_START);
}

extern void mSM_Object_Exchange_keep_new_Menu(GAME_PLAY* play) {
    play->submenu_ground_idx = 0;
    mSM_Object_Exchange_keep_new_MenuTexAndPallet(play, 0);
    mSM_Object_Exchange_keep_new_MenuTexAndPallet(play, 1);
}

extern u8* mSM_Get_ground_tex_p(GAME_PLAY* play) {
    int idx = play->submenu_ground_idx;

    if (idx < 0) {
        return NULL;
    }

    return (u8*)play->submenu_ground_tex[idx];
}

extern u16* mSM_Get_ground_pallet_p(GAME_PLAY* play) {
    int idx = play->submenu_ground_idx;

    if (idx < 0) {
        return NULL;
    }

    return (u16*)play->submenu_ground_pallet[idx];
}
