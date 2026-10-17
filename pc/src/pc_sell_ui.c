/* pc_sell_ui.c - Nook bulk sell basket: the screen state and the Nook seam helpers (see pc_sell_ui.h). The rules are pc_sell_logic.c (pure, shared with the host). */
#include "pc_sell_ui.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pc_sell_logic.h"
#include "pc_dresser_ui.h"
#include "pc_net_game.h"
#include "pc_settings.h"
#include "pc_test_hooks.h"

#include "m_common_data.h"
#include "m_private.h"
#include "m_name_table.h"
#include "m_shop.h"
#include "m_kabu_manager.h"
#include "m_submenu.h"
#include "m_play.h"
#include "lb_rtc.h"
#include "m_actor.h"      /* TEST-ONLY Nook mode: the shop's NPC list */
#include "m_player_lib.h" /* TEST-ONLY Nook mode: get_player_actor_withoutCheck */
#include "m_demo.h"
#include "sys_math.h"
#include <math.h>

_Static_assert(PC_SELL_COND_NORMAL == mPr_ITEM_COND_NORMAL && PC_SELL_COND_PRESENT == mPr_ITEM_COND_PRESENT && PC_SELL_COND_QUEST == mPr_ITEM_COND_QUEST &&
                   PC_SELL_BASKET_CELLS == mPr_POCKETS_SLOT_COUNT && PC_SELL_BASKET_COLS * PC_SELL_BASKET_ROWS == PC_SELL_BASKET_CELLS &&
                   PC_SELL_BASKET_CELLS <= PC_DRESSER_UI_SLOTS,
               "pc_sell_logic.h constants drifted from the game's");

#define PC_SELL_NOTICE_MS 3000u

static PCSellBasket s_basket;
static int s_open = 0;         /* the Nook seam opened the basket screen and has not collected it yet */
static int s_confirmed = 0;    /* the player pressed the Sell button (or Start) on a non-empty basket: the close hands it to Nook; any other close = CANCEL */
static int s_destroyed = 0;    /* the gyroid overlay was destroyed since the open (a later construct with s_open still set = a stale basket) */
static int s_from_basket = 0;  /* one-shot for the Nook dialogue: the selection came from the basket */
static int s_last_count = 0;
static unsigned s_last_total = 0;
static const char* s_notice = NULL;
static uint32_t s_notice_ms = 0;

static int pc_sell_ui_sunday(void) {
    return Common_Get(time).rtc_time.weekday == lbRTC_SUNDAY;
}

static void pc_sell_ui_pockets(unsigned short* pockets, int* conds) {
    int i;
    for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
        pockets[i] = Now_Private != NULL ? (unsigned short)Now_Private->inventory.pockets[i] : (unsigned short)EMPTY_NO;
        conds[i] = Now_Private != NULL ? (int)mPr_GET_ITEM_COND(Now_Private->inventory.item_conditions, i) : 0;
    }
}

static int pc_sell_ui_refusal_of(int slot) {
    if (Now_Private == NULL || slot < 0 || slot >= mPr_POCKETS_SLOT_COUNT) {
        return PC_SELL_REF_EMPTY;
    }
    return pc_sell_refusal((unsigned)Now_Private->inventory.pockets[slot], (int)mPr_GET_ITEM_COND(Now_Private->inventory.item_conditions, slot), pc_sell_ui_sunday());
}

static unsigned pc_sell_ui_unit(unsigned item) {
    return pc_sell_unit_value(item, (unsigned)mSP_ItemNo2ItemPrice((mActor_name_t)item), (unsigned)Kabu_get_price());
}

static unsigned pc_sell_ui_total(void) {
    unsigned t = 0;
    int i;
    for (i = 0; i < PC_SELL_BASKET_CELLS; i++) {
        if (s_basket.slot[i] >= 0) {
            t += pc_sell_ui_unit(s_basket.item[i]);
        }
    }
    return t;
}

static void pc_sell_ui_set_notice(const char* text) {
    s_notice = text;
    s_notice_ms = pc_net_game_now_ms() | 1u;
}

int pc_sell_ui_enabled(void) {
    static int env = -1;
    const PCNetGameRole role = pc_net_game_role();
    if (env < 0) {
        const char* e = getenv("AC_MP_BULK_SELL");
        env = (e != NULL && e[0] == '0') ? 0 : 1;
        if (!env) {
            printf("[NET][SELL] AC_MP_BULK_SELL=0: Nook's sell menu stays the vanilla picker in this process\n");
        }
    }
    return env && g_pc_settings.bulk_sell != 0 && (role == PC_NETGAME_ROLE_HOST || role == PC_NETGAME_ROLE_CLIENT);
}

void pc_sell_ui_open(void) {
    int i, n_ok = 0;
    pc_sell_basket_clear(&s_basket); /* never remembered: every open starts EMPTY (user feedback: the basket must not refill itself) */
    for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
        n_ok += pc_sell_ui_refusal_of(i) == PC_SELL_OK;
    }
    s_open = 1;
    s_confirmed = 0;
    s_destroyed = 0;
    s_from_basket = 0;
    s_last_count = 0;
    s_last_total = 0u;
    pc_dresser_ui_begin_basket();
    if (n_ok == 0) {
        pc_sell_ui_set_notice("Nook won't buy anything you're carrying");
    }
    printf("[NET][SELL] basket screen OPEN (role %s): EMPTY basket, %d sellable pocket(s)\n", pc_net_game_role() == PC_NETGAME_ROLE_HOST ? "host" : "client", n_ok);
}

int pc_sell_ui_active(void) {
    return s_open && pc_dresser_ui_basket();
}

int pc_sell_ui_confirm(void) {
    const int n = pc_sell_basket_count(&s_basket);
    if (!s_open || !pc_dresser_ui_basket()) {
        return 0;
    }
    if (n == 0) {
        pc_sell_ui_set_notice("Put something in the basket first");
        printf("[NET][SELL] basket: Sell pressed on an EMPTY basket: blocked (nothing happens)\n");
        return 0;
    }
    s_confirmed = 1;
    printf("[NET][SELL] basket: Sell CONFIRMED: %d item(s) mask=0x%04X advisory %u Bells -> the screen closes, Nook quotes\n", n, pc_sell_basket_mask(&s_basket), pc_sell_ui_total());
    return 1;
}

int pc_sell_ui_can_confirm(void) {
    return s_open && pc_sell_basket_count(&s_basket) > 0;
}

int pc_sell_ui_table_cell(int idx) {
    return pc_sell_table_cell(idx);
}

int pc_sell_ui_table_is_button(int idx) {
    return pc_sell_table_is_button(idx);
}

void pc_sell_ui_reset(const char* why) {
    const int had = pc_sell_basket_count(&s_basket) > 0 || s_confirmed || s_from_basket || s_last_count != 0;
    pc_sell_basket_clear(&s_basket);
    s_confirmed = 0;
    s_from_basket = 0;
    s_last_count = 0;
    s_last_total = 0u;
    if (had) {
        printf("[NET][SELL] basket state CLEARED (%s): nothing staged, nothing remembered\n", why != NULL ? why : "?");
    }
}

void pc_sell_ui_tick(void) {
    static int last_scene = -1;
    const int scene = (int)Save_Get(scene_no);
    if (last_scene >= 0 && scene != last_scene) {
        pc_sell_ui_reset("scene change");
    }
    last_scene = scene;
    if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT && pc_net_game_client_link_state() != PC_NETGAME_LINK_READY) {
        pc_sell_ui_reset("host link not ready / lost");
    }
}

int pc_sell_ui_close(void* submenu_p) {
    Submenu* submenu = (Submenu*)submenu_p;
    unsigned short pockets[mPr_POCKETS_SLOT_COUNT], items[PC_SELL_BASKET_CELLS];
    unsigned char slots[PC_SELL_BASKET_CELLS];
    int conds[mPr_POCKETS_SLOT_COUNT];
    int n, k, dropped;
    if (!s_open) {
        return 0;
    }
    if (!s_confirmed) {
        /* B (Shift) / Y / any close without the Sell button = CANCEL: the basket is emptied (the staged items were only shown in it: the pockets never changed),
         * and Nook reads an EMPTY selection = the vanilla "nothing selected" path of the sell picker (aNSC_MSG_SELL_CANCEL, then his "anything else?" choice) */
        const int had = pc_sell_basket_count(&s_basket);
        if (submenu != NULL && submenu->item_p != NULL) {
            Submenu_Item_c* it = submenu->item_p;
            for (k = 0; k < mPr_POCKETS_SLOT_COUNT; k++) {
                it[k].item = (mActor_name_t)EMPTY_NO;
                it[k].slot_no = mPr_POCKETS_SLOT_COUNT;
            }
            submenu->selected_item_num = 0;
        }
        pc_sell_basket_clear(&s_basket);
        s_from_basket = 0;
        s_last_count = 0;
        s_last_total = 0u;
        s_open = 0;
        pc_dresser_ui_end();
        printf("[NET][SELL] basket screen CANCELLED (B): %d staged item(s) dropped, basket cleared, nothing sold -> Nook's vanilla sell-cancel row\n", had);
        return 0;
    }
    s_confirmed = 0;
    pc_sell_ui_pockets(pockets, conds);
    dropped = pc_sell_basket_prune(&s_basket, pockets, conds, pc_sell_ui_sunday()); /* nothing can change while the screen is open; a defensive re-check */
    n = pc_sell_basket_selection(&s_basket, items, slots, PC_SELL_BASKET_CELLS);
    s_last_count = n;
    s_last_total = pc_sell_ui_total();
    if (submenu != NULL && submenu->item_p != NULL) {
        Submenu_Item_c* it = submenu->item_p;
        for (k = 0; k < mPr_POCKETS_SLOT_COUNT; k++) {
            it[k].item = (mActor_name_t)EMPTY_NO;
            it[k].slot_no = mPr_POCKETS_SLOT_COUNT;
        }
        for (k = 0; k < n; k++) {
            it[k].item = (mActor_name_t)items[k];
            it[k].slot_no = slots[k];
        }
        submenu->selected_item_num = n;
    }
    s_from_basket = n > 0;
    s_open = 0;
    pc_dresser_ui_end();
    printf("[NET][SELL] basket screen CLOSED: %d item(s) mask=0x%04X advisory total %u Bells (%d dropped)%s\n", n, pc_sell_basket_mask(&s_basket), s_last_total, dropped,
           n > 0 ? " -> Nook's quote" : " -> nothing to sell");
    pc_sell_basket_clear(&s_basket); /* the selection now lives in the vanilla Submenu list only: the basket itself is never remembered (the next open is EMPTY) */
    return n;
}

int pc_sell_ui_take_from_basket(void) {
    const int r = s_from_basket;
    s_from_basket = 0;
    return r;
}

int pc_sell_ui_last_count(void) {
    return s_last_count;
}

unsigned pc_sell_ui_last_total(void) {
    return s_last_total;
}

int pc_sell_ui_index_item(int idx) {
    return pc_sell_ui_cell_item(pc_sell_table_cell(idx));
}

int pc_sell_ui_cell_item(int cell) {
    if (cell < 0 || cell >= PC_SELL_BASKET_CELLS || s_basket.slot[cell] < 0) {
        return 0;
    }
    return (int)s_basket.item[cell];
}

int pc_sell_ui_pocket_hidden(int slot) {
    return s_open && pc_sell_basket_cell_of(&s_basket, slot) >= 0;
}

int pc_sell_ui_pocket_offer(int slot) {
    return s_open && !pc_sell_ui_pocket_hidden(slot) && pc_sell_ui_refusal_of(slot) == PC_SELL_OK;
}

int pc_sell_ui_put(int slot) {
    int ref, cell;
    if (!s_open || pc_sell_ui_pocket_hidden(slot)) {
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    ref = pc_sell_ui_refusal_of(slot);
    if (ref != PC_SELL_OK) {
        if (ref != PC_SELL_REF_EMPTY) {
            pc_sell_ui_set_notice(pc_sell_refusal_text(ref));
        }
        printf("[NET][SELL] basket: pocket %d refused (%d: %s)\n", slot, ref, pc_sell_refusal_text(ref));
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    cell = pc_sell_basket_add(&s_basket, slot, (unsigned)Now_Private->inventory.pockets[slot]);
    if (cell < 0) {
        pc_sell_ui_set_notice("The basket is full");
        return PC_DRESSER_UI_ACT_FULL;
    }
    printf("[NET][SELL] basket: pocket %d item 0x%04X -> cell %d (%d item(s), advisory %u Bells; staged only, the pocket is unchanged)\n", slot,
           (unsigned)s_basket.item[cell], cell, pc_sell_basket_count(&s_basket), pc_sell_ui_total());
    return PC_DRESSER_UI_ACT_DONE;
}

int pc_sell_ui_take(int cell) {
    const int item = pc_sell_ui_cell_item(cell);
    const int slot = s_open ? pc_sell_basket_remove(&s_basket, cell) : -1;
    if (slot < 0) {
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    printf("[NET][SELL] basket: cell %d item 0x%04X taken back to pocket %d (%d item(s) left)\n", cell, (unsigned)item, slot, pc_sell_basket_count(&s_basket));
    return PC_DRESSER_UI_ACT_DONE;
}

void pc_sell_ui_refuse_pocket(int slot) {
    int ref;
    if (!s_open || pc_sell_ui_pocket_hidden(slot)) {
        return;
    }
    ref = pc_sell_ui_refusal_of(slot);
    if (ref != PC_SELL_OK && ref != PC_SELL_REF_EMPTY) {
        pc_sell_ui_set_notice(pc_sell_refusal_text(ref));
        printf("[NET][SELL] basket: pocket %d item 0x%04X is not sellable (%d: %s)\n", slot, Now_Private != NULL ? (unsigned)Now_Private->inventory.pockets[slot] : 0u, ref,
               pc_sell_refusal_text(ref));
    }
}

unsigned pc_sell_ui_selectable_mask(void) {
    unsigned m = 0u;
    int i;
    for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
        if (pc_sell_ui_refusal_of(i) == PC_SELL_OK) {
            m |= 1u << i;
        }
    }
    return m;
}

int pc_sell_ui_header(char* l1, int c1, char* l2, int c2, char* l3, int c3) {
    const int n = pc_sell_basket_count(&s_basket);
    char num[24];
    if (l1 != NULL && c1 > 0) {
        snprintf(l1, (size_t)c1, n == 0 ? "Basket" : "%d item%s", n, n == 1 ? "" : "s");
    }
    if (l2 != NULL && c2 > 0) {
        snprintf(l2, (size_t)c2, "%s", n == 0 ? "empty" : pc_sell_format_bells(num, sizeof(num), pc_sell_ui_total()));
    }
    if (l3 != NULL && c3 > 0) {
        snprintf(l3, (size_t)c3, "%s", n == 0 ? "" : "Bells");
    }
    return n;
}

const char* pc_sell_ui_notice(void) {
    if (s_notice == NULL || s_notice_ms == 0 || (int32_t)(pc_net_game_now_ms() - s_notice_ms) >= (int32_t)PC_SELL_NOTICE_MS) {
        return NULL;
    }
    return s_notice;
}

const char* pc_sell_ui_hint(void) {
    return pc_sell_ui_active() ? "A: Add or Take Back    Sell: A on Sell or Start    B: Cancel" : NULL;
}

void pc_sell_ui_overlay_constructed(void) {
    if (s_open && s_destroyed) {
        printf("[NET][SELL] a basket that was never collected (its conversation ended) is dropped: nothing was sold, nothing changed\n");
        s_open = 0;
        pc_sell_ui_reset("stale basket at the overlay construct");
        if (pc_dresser_ui_basket()) {
            pc_dresser_ui_end();
        }
    }
}

void pc_sell_ui_overlay_destroyed(void) {
    if (s_open) {
        s_destroyed = 1;
    }
}

/* ---------------- TEST-ONLY: the real-game basket test (test_sell_basket_real.py) ----------------
 * PC_NET_TEST_HOOKS builds, AC_TEST_HOOKS=1 + AC_TEST_SELL_BASKET=1, CLIENT role, never in normal play; every line "[NET][SELL][TEST-ONLY]". (0) once READY + synced:
 * the ONE local test write puts a fish, an insect and a peach into pockets 0..2 and a 1,000 Bells bag into pocket 3 (items already there move to free pockets),
 * the D3 upload carries it (5 s); (1) the REAL basket screen opens (m_submenu.c's test open, inventory in HANIWA_ENTRUST mode, basket mode) and the scripted pad of
 * the dresser screen (AC_TEST_DRESSER_GYROID + _STEPS, pc_pad.c) plays the real cursor / A / B: pockets 0..2 "Add to Basket", A on the bag (refused + notice), A on
 * the Sell button confirms (B would CANCEL: nothing to sell); (2) the selection the Nook seam would read is built by pc_sell_ui_close() and sold exactly like aNSC_pc_sell_prepare / aNSC_pc_sell_step (ONE SHOP_SELL with
 * the slot mask); the pockets / wallet must not change before APPLIED; (3) the final pockets / wallet are reported. */
#ifdef PC_NET_TEST_HOOKS
extern int g_pc_th_submenu_req;    /* m_submenu.c (TEST-ONLY) */
extern int g_pc_th_submenu_data0;  /* m_submenu.c (TEST-ONLY): data0 of the next test open */
#endif

void pc_sell_ui_test_tick(void) {
#ifdef PC_NET_TEST_HOOKS
    static int enabled = -1, stage = 0;
    static uint32_t t0 = 0;
    static unsigned short snap[mPr_POCKETS_SLOT_COUNT];
    static u32 wallet0 = 0;
    static unsigned mask = 0;
    static int primary = -1;
    GAME_PLAY* play = (GAME_PLAY*)gamePT;
    uint32_t now;
    int i, r;
    if (enabled < 0) {
        const char* e = pc_test_hook_getenv("AC_TEST_SELL_BASKET");
        enabled = e == NULL ? 0 : (e[0] == '2' ? 2 : 1); /* 2 = the NOOK mode (test_sell_basket_real.py R4): the basket is opened by Nook's real dialogue */
    }
    if (!enabled || stage >= 90 || pc_net_game_role() != PC_NETGAME_ROLE_CLIENT || pc_net_game_client_link_state() != PC_NETGAME_LINK_READY || Now_Private == NULL ||
        play == NULL) {
        return;
    }
    now = pc_net_game_now_ms();
    if (stage == 0) {
        if (!pc_net_game_client_world_synced()) {
            return;
        }
        if (t0 == 0) {
            t0 = now | 1u;
            return;
        }
        if ((uint32_t)(now - t0) < 8000u) {
            return;
        }
        {
            mActor_name_t want[4];
            want[0] = want[1] = want[2] = (mActor_name_t)EMPTY_NO;
            want[3] = (mActor_name_t)ITM_MONEY_1000; /* refused by the predicate (money) */
            for (i = 0; i < 40 && want[0] == (mActor_name_t)EMPTY_NO; i++) {
                if (mSP_ItemNo2ItemPrice((mActor_name_t)(ITM_FISH_START + i)) / 4u > 0u) {
                    want[0] = (mActor_name_t)(ITM_FISH_START + i);
                }
            }
            for (i = 0; i < 40 && want[1] == (mActor_name_t)EMPTY_NO; i++) {
                if (mSP_ItemNo2ItemPrice((mActor_name_t)(ITM_INSECT_START + i)) / 4u > 0u) {
                    want[1] = (mActor_name_t)(ITM_INSECT_START + i);
                }
            }
            want[2] = (mActor_name_t)ITM_FOOD_PEACH;
            for (i = 0; i < 4; i++) { /* move whatever sits in pockets 0..3 to free pockets (a local rearrangement: nothing is lost) */
                if (Now_Private->inventory.pockets[i] != (mActor_name_t)EMPTY_NO) {
                    int f, moved = 0;
                    for (f = 4; f < mPr_POCKETS_SLOT_COUNT && !moved; f++) {
                        if (Now_Private->inventory.pockets[f] == (mActor_name_t)EMPTY_NO) {
                            mPr_SetPossessionItem(Now_Private, f, Now_Private->inventory.pockets[i], mPr_GET_ITEM_COND(Now_Private->inventory.item_conditions, i));
                            mPr_SetPossessionItem(Now_Private, i, (mActor_name_t)EMPTY_NO, mPr_ITEM_COND_NORMAL);
                            moved = 1;
                        }
                    }
                    if (!moved) {
                        printf("[NET][SELL][TEST-ONLY] basket test: not enough free pockets to set up -- hook gives up\n");
                        stage = 90;
                        return;
                    }
                }
            }
            for (i = 0; i < 4; i++) {
                mPr_SetPossessionItem(Now_Private, i, want[i], mPr_ITEM_COND_NORMAL); /* the hook's ONE local test write */
            }
            printf("[NET][SELL][TEST-ONLY] basket test: placed 0x%04X 0x%04X 0x%04X (sellable) and 0x%04X (refused) into pockets 0..3 (local test setup, NOT active in normal play)\n",
                   (unsigned)want[0], (unsigned)want[1], (unsigned)want[2], (unsigned)want[3]);
        }
        t0 = now;
        stage = 1;
        return;
    }
    if (stage == 1) {
        if ((uint32_t)(now - t0) < 5000u || pc_net_game_client_pocket_locked()) {
            return; /* the D3 upload carries the test setup first */
        }
        for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
            snap[i] = (unsigned short)Now_Private->inventory.pockets[i];
        }
        wallet0 = Now_Private->inventory.wallet;
        if (enabled == 2) {
            t0 = now;
            stage = 20; /* the NOOK mode: Nook's own dialogue opens the basket */
            return;
        }
        pc_sell_ui_open();
        g_pc_th_submenu_data0 = mSM_IV_OPEN_HANIWA_ENTRUST;
        g_pc_th_submenu_req = mSM_OVL_INVENTORY;
        printf("[NET][SELL][TEST-ONLY] basket test: opening the REAL basket screen (wallet=%u)\n", (unsigned)wallet0);
        t0 = now;
        stage = 2;
        return;
    }
    if (stage >= 20 && stage <= 24) { /* ---- the NOOK mode ---- */
        if (stage == 20) {
            /* (a) the client stands in Nook's shop (--nook-test warp,resident): it is placed 55 units in front of Nook, facing him (a positioning aid, like the
             * autopilot's "tp"); the autopilot's "mashf" then plays A: Nook's first page choice 0 = "I want to sell" -> the basket screen */
            Actor_list* list = &play->actor_info.list[ACTOR_PART_NPC];
            ACTOR* a = list->actor;
            ACTOR* nook = NULL;
            PLAYER_ACTOR* pa = get_player_actor_withoutCheck(play);
            int k;
            for (k = list->num_actors; k > 0 && a != NULL; k--, a = a->next_actor) {
                const u16 id = (u16)a->npc_id;
                if (id == SP_NPC_SHOP_MASTER || id == SP_NPC_SHOP_MASTERSP || id == SP_NPC_SHOP_MASTERSP_2 || id == SP_NPC_SHOP_MASTERSP_3 || id == SP_NPC_SHOP_MASTERSP_4 ||
                    id == SP_NPC_MAMEDANUKI0 || id == SP_NPC_MAMEDANUKI1) {
                    nook = a;
                    break;
                }
            }
            if (nook == NULL || pa == NULL || mDemo_CheckDemo()) {
                if ((uint32_t)(now - t0) > 3000u) {
                    printf("[NET][SELL][TEST-ONLY] basket test (nook): waiting for Nook in a shop (found %d, demo %d)\n", nook != NULL, mDemo_CheckDemo());
                    t0 = now;
                }
                return;
            }
            {
                f32 dx = pa->actor_class.world.position.x - nook->world.position.x;
                f32 dz = pa->actor_class.world.position.z - nook->world.position.z;
                f32 d = sqrtf(dx * dx + dz * dz);
                if (d < 1.0f) {
                    dx = 0.0f;
                    dz = 1.0f;
                    d = 1.0f;
                }
                pa->actor_class.world.position.x = nook->world.position.x + dx / d * 55.0f;
                pa->actor_class.world.position.z = nook->world.position.z + dz / d * 55.0f;
                pa->actor_class.shape_info.rotation.y = atans_table(-dz, -dx);
                pa->actor_class.world.angle.y = pa->actor_class.shape_info.rotation.y;
                printf("[NET][SELL][TEST-ONLY] basket test (nook): Nook 0x%04X at (%.1f,%.1f); the player placed at (%.1f,%.1f) facing him\n", (unsigned)(u16)nook->npc_id,
                       (double)nook->world.position.x, (double)nook->world.position.z, (double)pa->actor_class.world.position.x, (double)pa->actor_class.world.position.z);
            }
            t0 = now;
            stage = 21;
            return;
        }
        if (stage == 21) {
            if (!pc_sell_ui_active()) {
                if ((uint32_t)(now - t0) > 180000u) {
                    printf("[NET][SELL][TEST-ONLY] basket test (nook): Nook never opened the basket -- hook ends\n");
                    stage = 90;
                }
                return;
            }
            for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
                snap[i] = (unsigned short)Now_Private->inventory.pockets[i];
            }
            wallet0 = Now_Private->inventory.wallet;
            printf("[NET][SELL][TEST-ONLY] basket test (nook): Nook's sell menu opened the basket screen (wallet=%u)\n", (unsigned)wallet0);
            stage = 22;
            return;
        }
        if (stage == 22) {
            for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
                if (snap[i] != (unsigned short)Now_Private->inventory.pockets[i]) {
                    printf("[NET][SELL][TEST-ONLY] *** POCKET %d CHANGED WHILE THE BASKET WAS OPEN ***\n", i);
                    snap[i] = (unsigned short)Now_Private->inventory.pockets[i];
                }
            }
            if (!pc_sell_ui_active()) {
                printf("[NET][SELL][TEST-ONLY] basket test (nook): the basket closed; the pockets now wait for the host's answer to Nook's sale\n");
                t0 = now;
                stage = 23;
            }
            return;
        }
        if (stage == 23) {
            int changed = Now_Private->inventory.wallet != wallet0;
            for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
                changed |= snap[i] != (unsigned short)Now_Private->inventory.pockets[i];
            }
            if (changed || (uint32_t)(now - t0) > 240000u) { /* the cancel / reopen / No / reopen / Yes flow of the real test: 240 s */
                printf("[NET][SELL][TEST-ONLY] basket test (nook): pockets / wallet %s\n", changed ? "changed (the host's post-image)" : "UNCHANGED after 240 s");
                t0 = now;
                stage = 24;
            }
            return;
        }
        if ((uint32_t)(now - t0) >= 2500u) {
            stage = 5; /* the final report below */
            t0 = now - 2500u;
        } else {
            return;
        }
    }
    if (stage == 2 || stage == 3) {
        for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
            if (snap[i] != (unsigned short)Now_Private->inventory.pockets[i]) {
                printf("[NET][SELL][TEST-ONLY] *** POCKET %d CHANGED WHILE THE BASKET WAS OPEN ***\n", i);
                snap[i] = (unsigned short)Now_Private->inventory.pockets[i];
            }
        }
        if (stage == 2) {
            if (play->submenu.open_flag) {
                printf("[NET][SELL][TEST-ONLY] basket test: the basket screen is open\n");
                stage = 3;
            } else if ((uint32_t)(now - t0) > 30000u) {
                printf("[NET][SELL][TEST-ONLY] basket test: the screen never opened -- hook gives up\n");
                s_open = 0;
                pc_dresser_ui_end();
                stage = 90;
            }
            return;
        }
        if (play->submenu.open_flag) {
            return;
        }
        {
            const int n = pc_sell_ui_close(&play->submenu);
            const Submenu_Item_c* si = play->submenu.item_p;
            mask = 0;
            primary = -1;
            for (i = 0; i < n; i++) { /* = ac_npc_shop_common.c aNSC_pc_sell_prepare (several selected items = their slots) */
                if (si[i].slot_no < mPr_POCKETS_SLOT_COUNT) {
                    mask |= 1u << si[i].slot_no;
                }
            }
            for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
                if (mask & (1u << i)) {
                    primary = i;
                    break;
                }
            }
            printf("[NET][SELL][TEST-ONLY] basket test: selection n=%d mask=0x%04X advisory=%u wallet=%u\n", n, mask, pc_sell_ui_last_total(), (unsigned)Now_Private->inventory.wallet);
            if (n <= 0 || primary < 0) {
                printf("[NET][SELL][TEST-ONLY] basket test: nothing selected -- hook ends\n");
                stage = 90;
                return;
            }
            stage = 7; /* begin (retried while busy) */
        }
        return;
    }
    if (stage == 7) {
        r = pc_net_game_ts_begin_shop_sell((int)mask, primary, (int)Now_Private->inventory.pockets[primary]);
        if (r < 0) {
            return; /* busy: retried next frame */
        }
        if (r == 0) {
            printf("[NET][SELL][TEST-ONLY] basket test: the sale was refused locally -- hook ends\n");
            stage = 90;
            return;
        }
        printf("[NET][SELL][TEST-ONLY] basket test: ONE SHOP_SELL begun for mask=0x%04X (primary pocket %d)\n", mask, primary);
        stage = 4;
        return;
    }
    if (stage == 4) {
        r = pc_net_game_ts_poll();
        if (r == PC_NETGAME_TS_OP_PENDING) {
            for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
                if (snap[i] != (unsigned short)Now_Private->inventory.pockets[i]) {
                    printf("[NET][SELL][TEST-ONLY] *** POCKET %d CHANGED BEFORE APPLIED ***\n", i);
                    snap[i] = (unsigned short)Now_Private->inventory.pockets[i];
                }
            }
            if (Now_Private->inventory.wallet != wallet0) {
                printf("[NET][SELL][TEST-ONLY] *** WALLET CHANGED BEFORE APPLIED ***\n");
                wallet0 = Now_Private->inventory.wallet;
            }
            return;
        }
        printf("[NET][SELL][TEST-ONLY] basket test: result %s mask=0x%04X reason %d\n", r == PC_NETGAME_TS_OP_APPLIED ? "APPLIED" : "REJECTED", mask, pc_net_game_ts_last_reject_reason());
        t0 = now;
        stage = 5;
        return;
    }
    if (stage == 5 && (uint32_t)(now - t0) >= 2500u) {
        char pb[15 * 5 + 1];
        for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
            snprintf(pb + i * 5, sizeof(pb) - (size_t)i * 5, "%04X,", (unsigned)Now_Private->inventory.pockets[i]);
        }
        pb[sizeof(pb) - 1] = '\0';
        printf("[NET][SELL][TEST-ONLY] basket test: final pockets=%s wallet=%u\n", pb, (unsigned)Now_Private->inventory.wallet);
        stage = 90;
    }
#endif
}
