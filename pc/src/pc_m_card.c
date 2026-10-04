/* pc_m_card.c - memory card manager: GCI save/load, village generation, ARAM data blocks
 *
 * Card A (save/card_a/) = player's home town
 * Card B (save/card_b/) = second town for visiting (drop any AC GCI file there)
 */
#include "m_card.h"
#include "m_start_data_init.h"
#include "m_common_data.h"
#include "m_flashrom.h"
#include "m_field_make.h"
#include "m_msg.h"
#include "m_npc.h"
#include "m_quest.h"
#include "m_island.h"
#include "m_font.h"
#include "m_vibctl.h"
#include "m_bg_item.h"
#include "m_land.h"
#include "m_private.h"
#include "m_event.h"
#include "m_time.h"
#include "m_scene.h"
#include "m_name_table.h"
#include "sys_math3d.h"
#include "sys_math.h"
#include "zurumode.h"
#include "pc_save_bswap.h"
#include "pc_settings.h"
#include "m_cockroach.h"
#include "m_all_grow_ovl.h"
#include "m_home.h"
#include "m_house.h"
#include "m_play.h"
#include "lb_rtc.h"
#include "game.h"
#include "pc_net_game.h"
/* OBSERVER-BEGIN */
#include "pc_host_observer.h"
#include "pc_field_authority.h"
#include "m_player_lib.h"
#include "ac_birth_control.h"
/* OBSERVER-END */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>
#include <unistd.h>
#ifdef _WIN32
#include <direct.h>  /* _mkdir */
#endif
#include <dolphin/os.h>  /* OSReport */

/* --- Path constants --- */
#define PC_CARD_A_DIR     "save/card_a"
#define PC_CARD_B_DIR     "save/card_b"
#define PC_GCI_FILENAME   "DobutsunomoriP_MURA.gci"
#define PC_GCI_PATH       PC_CARD_A_DIR "/" PC_GCI_FILENAME
#define PC_GCI_TMP_PATH   PC_CARD_A_DIR "/" PC_GCI_FILENAME ".tmp"
#define PC_SAVE_DIR       "save"
#define PC_SAVE_MAX_BACKUPS 3

/* Legacy paths for migration from flat save/ layout */
#define PC_GCI_PATH_LEGACY     "save/DobutsunomoriP_MURA.gci"
#define PC_GCI_TMP_PATH_LEGACY "save/DobutsunomoriP_MURA.gci.tmp"

#define GCI_HEADER_SIZE      sizeof(CARDDir)        /* 64 bytes */
#define GCI_FILE_DATA_SIZE   mCD_LAND_SAVE_SIZE     /* 0x72000 */
#define GCI_OTHERS_OFFSET    0
#define GCI_SAVE_MAIN_OFFSET OTHERS_SIZE            /* 0x26000 */
#define GCI_SAVE_BACK_OFFSET (OTHERS_SIZE + sizeof(Save))  /* 0x4C000 */
#define GCI_SECTOR_SIZE      mCD_MEMCARD_SECTORSIZE /* 0x2000 */

int pc_save_loaded = 0;
static int pc_save_ready = 0;

/* --- Travel state --- */
static Save l_keepSave;                        /* Other town's save data (for Card B visit) */
static int l_keepSave_set = FALSE;
static mCD_keep_mail_c l_keepMail;             /* Other town's mail ARAM block */
static mCD_keep_original_c l_keepOriginal;     /* Other town's original designs ARAM block */
static mCD_keep_diary_c l_keepDiary;           /* Other town's diary ARAM block */

/* Passport: the traveling player's private data + departing animal */
static union {
    mCD_foreigner_c file;
    u8 sector_align[mCD_ALIGN_SECTORSIZE(sizeof(mCD_foreigner_c))];
} l_mcd_foreigner_file;

static int l_mcd_keep_startCond = 0;
static char l_card_b_gci_path[300] = {0};      /* Path to the Card B GCI file, if found */

/* OBSERVER-BEGIN */
/* --- --host-observer (the hidden SERVER OBSERVER, see pc_host_observer.h) -------------------------------------------------------------
 * s_pc_observer_private is a PC-owned STATIC record: it is NOT an element of Save_t.private_data[], NOT the passport (l_mcd_foreigner_file) and NOT
 * g_foreigner_private, so no save routine, no travel merge and no resident lookup can ever see or serialise it (the writer serialises Save_t only). It is
 * bound as Common.now_private with Common.player_no == mPr_FOREIGNER (exactly 4). Its PersonalID is RESERVED (name "SERVER", this town's land, a fixed
 * player_id chosen by pc_host_observer_pick_id() so that it equals no resident / house owner of the loaded save). */
static Private_c s_pc_observer_private;
static int s_pc_observer_latched = 0; /* one-way: the town field was loaded once with the observer in it (pcfa_save_ready() foreigner exemption) */

int pc_host_observer_active(void) {
    return g_pc_host_observer != 0 && pc_net_game_role() == PC_NETGAME_ROLE_HOST &&
           Common_Get(now_private) == &s_pc_observer_private && Common_Get(player_no) == mPr_FOREIGNER;
}

int pc_host_observer_ready(void) {
    return s_pc_observer_latched != 0 && pc_host_observer_active();
}

int pc_host_observer_id_matches(const void* personal_id) {
    return personal_id != NULL && pc_host_observer_active() &&
           memcmp(personal_id, &s_pc_observer_private.player_ID, sizeof(PersonalID_c)) == 0;
}
/* OBSERVER-END */
/* External: scan card_b/ for valid AC GCI file (defined in pc_card.c) */
extern int pc_card_scan_for_gci(int chan, char* out_path, int out_size);

/* External: functions from decomp used in mCD_toNextLand */
extern void mTM_rtcTime_limit_check(void);
extern void mEv_ClearEventInfo(void);
extern void mEv_toland_clear_common(void);
extern void mNpc_ClearInAnimal(void);
extern void mNpc_FirstClearGoodbyMail(void);
extern void mQst_ClearGrabItemInfo(void);
extern void mISL_ClearKeepIsland(void);
extern void mNpc_ClearCacheName(void);
extern void mTM_clear_renew_is(void);
extern void lbRTC_GetTime(lbRTC_time_c* time);

/* --- ARAM data blocks (mail/diary/original designs) — malloc'd instead of ARAM DMA --- */

static u32 l_aram_alloc_size_table[mCD_ARAM_DATA_NUM] = {
    ALIGN_NEXT(sizeof(mCD_keep_original_c), 32),
    ALIGN_NEXT(sizeof(mCD_keep_mail_c), 32),
    ALIGN_NEXT(sizeof(mCD_keep_diary_c), 32),
};

static void* l_aram_block_p_table[mCD_ARAM_DATA_NUM];

static void pc_init_diary_entries(void* block) {
    mCD_keep_diary_c* diary = (mCD_keep_diary_c*)block;
    int p, m;
    for (p = 0; p < mCD_KEEP_DIARY_COUNT; p++) {
        for (m = 0; m < mCD_KEEP_DIARY_ENTRY_COUNT; m++) {
            memset(diary->entries[p][m].text, CHAR_SPACE, mDI_ENTRY_SIZE);
        }
    }
}

/* Match GC's mCD_set_init_mail_data: clear mail slots with font=0xFF (unused) */
static void pc_init_mail_entries(void* block) {
    mCD_keep_mail_c* keep_mail = (mCD_keep_mail_c*)block;
    Mail_c* mail = (Mail_c*)keep_mail->mail;
    int i, j;
    for (i = 0; i < mCD_KEEP_MAIL_PAGE_COUNT; i++) {
        mem_clear(keep_mail->folder_names[i], sizeof(keep_mail->folder_names[i]), CHAR_SPACE);
        for (j = 0; j < mCD_KEEP_MAIL_COUNT; j++) {
            mMl_clear_mail(mail);
            mail++;
        }
    }
}

void mCD_save_data_aram_malloc(void) {
    int i;
    for (i = 0; i < mCD_ARAM_DATA_NUM; i++) {
        if (l_aram_block_p_table[i] == NULL) {
            l_aram_block_p_table[i] = malloc(l_aram_alloc_size_table[i]);
            if (l_aram_block_p_table[i]) {
                memset(l_aram_block_p_table[i], 0, l_aram_alloc_size_table[i]);
                if (i == mCD_ARAM_DATA_DIARY) {
                    pc_init_diary_entries(l_aram_block_p_table[i]);
                } else if (i == mCD_ARAM_DATA_MAIL) {
                    pc_init_mail_entries(l_aram_block_p_table[i]);
                }
            }
        }
    }
}

int mCD_save_data_aram_to_main(void* dst, u32 size, u32 idx) {
    void* block;
    if (idx >= mCD_ARAM_DATA_NUM) idx = 0;
    block = l_aram_block_p_table[idx];
    if (block != NULL) {
        u32 copy_size = size < l_aram_alloc_size_table[idx] ? size : l_aram_alloc_size_table[idx];
        memcpy(dst, block, copy_size);
        return TRUE;
    }
    return FALSE;
}

int mCD_save_data_main_to_aram(void* src, u32 size, u32 idx) {
    void* block;
    if (idx >= mCD_ARAM_DATA_NUM) idx = 0;
    block = l_aram_block_p_table[idx];
    if (block != NULL) {
        u32 copy_size = size < l_aram_alloc_size_table[idx] ? size : l_aram_alloc_size_table[idx];
        memcpy(block, src, copy_size);
        return TRUE;
    }
    return FALSE;
}

void mCD_set_aram_save_data(void) {
    int i;
    for (i = 0; i < mCD_ARAM_DATA_NUM; i++) {
        if (l_aram_block_p_table[i] != NULL) {
            memset(l_aram_block_p_table[i], 0, l_aram_alloc_size_table[i]);
            if (i == mCD_ARAM_DATA_DIARY) {
                pc_init_diary_entries(l_aram_block_p_table[i]);
            } else if (i == mCD_ARAM_DATA_MAIL) {
                pc_init_mail_entries(l_aram_block_p_table[i]);
            }
        }
    }
}

/* --- GCI read/write helpers --- */

static void put_be32(u8* dst, u32 val) {
    dst[0] = (u8)(val >> 24);
    dst[1] = (u8)(val >> 16);
    dst[2] = (u8)(val >> 8);
    dst[3] = (u8)(val);
}

static void put_be16(u8* dst, u16 val) {
    dst[0] = (u8)(val >> 8);
    dst[1] = (u8)(val);
}

/* rotate backups: .bak3→delete, .bak2→.bak3, .bak1→.bak2, current→.bak1 */
static void pc_save_rotate_backups(const char* base_path) {
    char older[300], newer[300];
    int i;
    struct stat st;

    snprintf(older, sizeof(older), "%s.bak%d", base_path, PC_SAVE_MAX_BACKUPS);
    remove(older);

    /* Windows rename() fails if dest exists, so remove first */
    for (i = PC_SAVE_MAX_BACKUPS; i > 1; i--) {
        snprintf(older, sizeof(older), "%s.bak%d", base_path, i - 1);
        snprintf(newer, sizeof(newer), "%s.bak%d", base_path, i);
        remove(newer);
        rename(older, newer);
    }

    if (stat(base_path, &st) == 0) {
        snprintf(newer, sizeof(newer), "%s.bak1", base_path);
        remove(newer);
        rename(base_path, newer);
    }
}

static void pc_ensure_save_dirs(void) {
#ifdef _WIN32
    _mkdir(PC_SAVE_DIR);
    _mkdir(PC_CARD_A_DIR);
    _mkdir(PC_CARD_B_DIR);
#else
    mkdir(PC_SAVE_DIR, 0755);
    mkdir(PC_CARD_A_DIR, 0755);
    mkdir(PC_CARD_B_DIR, 0755);
#endif
}

static int pc_save_write_gci_to(const char* gci_path, const char* tmp_path);

/* mCD_get_land_copyProtect */
static u16 pc_get_land_copy_protect(void) {
    u16 code = (u16)RANDOM(0xFFF0);
    return (u16)(code + 1);
}

/* mCD_CheckResetCode: TRUE if no reset code armed (birthday clears it) */
static int pc_check_reset_code(Private_c* priv) {
    if (priv->state_flags & mPr_FLAG_BIRTHDAY_ACTIVE) {
        priv->reset_code = 0;
    }
    return priv->reset_code == 0;
}

/* mCD_SetResetCode: arm a nonzero code; still set at next load = reset */
static void pc_set_reset_code(Private_c* priv) {
    priv->reset_code = (u32)RANDOM_F(USHT_MAX_S);
    priv->reset_code++;
}

/* Money rock / Wisp / Copy Protect. save_mode mirrors GC SaveHome _04:
 * 0 = full save (clears reset code), nonzero = door save (keeps it armed) */
static void pc_save_pre_write_side_effects(int save_mode) {
    Private_c* priv = Now_Private;
    u16 copy_protect;
    int i;

/* OBSERVER-BEGIN */
    if (pc_host_observer_active()) {
        return; /* --host-observer: nothing here applies to the static observer record (never reached: mCD_SaveHome_bg refuses first) */
    }
/* OBSERVER-END */
    mCkRh_SavePlayTime(Common_Get(player_no));

    if (priv != NULL) {
        if (save_mode == 0) {
            priv->reset_code = 0;

            for (i = 0; i < mPr_POCKETS_SLOT_COUNT; i++) {
                if (ITEM_IS_WISP(priv->inventory.pockets[i])) {
                    mPr_SetPossessionItem(priv, i, EMPTY_NO, mPr_ITEM_COND_NORMAL);
                }
            }
        } else if (pc_check_reset_code(priv)) {
            pc_set_reset_code(priv);
        }
    }

    if (save_mode == 0) {
        mAGrw_ClearMoneyStoneShineGround();
    }

    copy_protect = pc_get_land_copy_protect();
    Common_Set(copy_protect, copy_protect);
    Save_Set(copy_protect, copy_protect);
    Save_Set(travel_hard_time, lbRTC_HardTime());
}

static int pc_save_write_gci(void) {
    int ok = pc_save_write_gci_to(PC_GCI_PATH, PC_GCI_TMP_PATH);
    /* D3-4: the Card-A GCI is the only place a host-merged resident record becomes durable. Right after it was really written
     * (pc_save_write_gci_to() also returns TRUE without writing when the save is not ready), the host persists the resident
     * record lineage sidecar save/mp/records.dat (pc_net_game.c -> pc_mp_records.c; no-op unless this process is the HOST).
     * Same (main) thread, synchronously, so the sidecar describes exactly the records the GCI just serialized; the GCI layout,
     * checksum, backup rotation and atomic rename above are untouched. The sidecar never lives in or next to a card directory. */
    if (ok && pc_save_ready) {
        pc_net_game_record_after_gci_save(PC_GCI_PATH);
    }
    return ok;
}

static int pc_save_write_gci_to(const char* gci_path, const char* tmp_path) {
    FILE* fp;
    u8* file_data;
    CARDDir dir_hdr;
    Save_t* save_copy;
    u16 checksum;
    u8* others_ptr;

    if (!pc_save_ready) return TRUE;

    pc_ensure_save_dirs();

    Save_Get(save_exist) = TRUE;
    Save_Get(save_check).version = mFRm_VERSION;
    mFRm_SetSaveCheckData(Save_GetPointer(save_check));

    file_data = (u8*)calloc(1, GCI_FILE_DATA_SIZE);
    if (!file_data) return FALSE;

    /* Others block (offset 0) — comment, banner, ARAM blocks */
    others_ptr = file_data + GCI_OTHERS_OFFSET;
    {
        const char* title = "DobutsunomoriP (AC PC Port)";
        u8* comment = others_ptr;
        memset(comment, 0, CARD_COMMENT_SIZE);
        strncpy((char*)comment, title, 32);
        memcpy(comment + 32, Save_Get(land_info).name, 8);
    }

    /* ARAM blocks: mail, original, diary (GC/Dolphin order).
     * Set landid on mail block so load-time detection identifies the order. */
    {
        u32 offset = sizeof(MemcardHeader_c) + 32; /* 0x1460 */
        u16 land_id = Save_Get(land_info).id;

        offset = ALIGN_NEXT(offset, 32);
        if (l_aram_block_p_table[mCD_ARAM_DATA_MAIL]) {
            u8* blk = others_ptr + offset;
            u32 sz = l_aram_alloc_size_table[mCD_ARAM_DATA_MAIL];
            memcpy(blk, l_aram_block_p_table[mCD_ARAM_DATA_MAIL], sz);
            pc_save_bswap_keep_mail((mCD_keep_mail_c*)blk, PC_BSWAP_TO_BE);
            /* Set landid (BE u16 at offset 2) so load detects GC order */
            put_be16(blk + 2, land_id);
            /* Recompute BE checksum over the bswapped block so Dolphin's
             * mFRm_ReturnCheckSum validates. */
            blk[0] = 0;
            blk[1] = 0;
            put_be16(blk, pc_checksum_be(blk, sz, 0));
        }
        offset += l_aram_alloc_size_table[mCD_ARAM_DATA_MAIL];

        offset = ALIGN_NEXT(offset, 32);
        if (l_aram_block_p_table[mCD_ARAM_DATA_ORIGINAL]) {
            u8* blk = others_ptr + offset;
            u32 sz = l_aram_alloc_size_table[mCD_ARAM_DATA_ORIGINAL];
            memcpy(blk, l_aram_block_p_table[mCD_ARAM_DATA_ORIGINAL], sz);
            pc_save_bswap_keep_original((mCD_keep_original_c*)blk, PC_BSWAP_TO_BE);
            blk[0] = 0;
            blk[1] = 0;
            put_be16(blk, pc_checksum_be(blk, sz, 0));
        }
        offset += l_aram_alloc_size_table[mCD_ARAM_DATA_ORIGINAL];

        offset = ALIGN_NEXT(offset, 32);
        if (l_aram_block_p_table[mCD_ARAM_DATA_DIARY]) {
            u8* blk = others_ptr + offset;
            u32 sz = l_aram_alloc_size_table[mCD_ARAM_DATA_DIARY];
            memcpy(blk, l_aram_block_p_table[mCD_ARAM_DATA_DIARY], sz);
            pc_save_bswap_keep_diary((mCD_keep_diary_c*)blk, PC_BSWAP_TO_BE);
            blk[0] = 0;
            blk[1] = 0;
            put_be16(blk, pc_checksum_be(blk, sz, 0));
        }
    }

    /* Main Save_t (offset 0x26000) */
    save_copy = (Save_t*)(file_data + GCI_SAVE_MAIN_OFFSET);
    memcpy(save_copy, &common_data.save.save, sizeof(Save_t));

    pc_save_bswap(save_copy, PC_BSWAP_TO_BE);
    {
        u8* chk_ptr = (u8*)&save_copy->save_check.checksum;
        chk_ptr[0] = 0;
        chk_ptr[1] = 0;
    }
    checksum = pc_checksum_be((const u8*)save_copy, sizeof(Save_t), 0);
    put_be16((u8*)&save_copy->save_check.checksum, checksum);

    /* Backup = copy of main */
    memcpy(file_data + GCI_SAVE_BACK_OFFSET, file_data + GCI_SAVE_MAIN_OFFSET, sizeof(Save));

    /* CARDDir header */
    memset(&dir_hdr, 0, sizeof(dir_hdr));
    memcpy(dir_hdr.gameName, "GAFE", 4);
    memcpy(dir_hdr.company, "01", 2);
    dir_hdr.bannerFormat = 0;
    strncpy((char*)dir_hdr.fileName, "DobutsunomoriP_MURA", CARD_FILENAME_MAX);
    {
        time_t unix_now = time(NULL);
        u32 gc_secs = (u32)(unix_now - 946684800LL);
        put_be32((u8*)&dir_hdr.time, gc_secs);
    }
    put_be32((u8*)&dir_hdr.iconAddr, 0xFFFFFFFF);
    put_be16((u8*)&dir_hdr.iconFormat, 0);
    put_be16((u8*)&dir_hdr.iconSpeed, 0);
    dir_hdr.permission = 0x04;
    dir_hdr.copyTimes = 0;
    put_be16((u8*)&dir_hdr.startBlock, 5);
    put_be16((u8*)&dir_hdr.length, (u16)(GCI_FILE_DATA_SIZE / GCI_SECTOR_SIZE));
    put_be32((u8*)&dir_hdr.commentAddr, 0);

    /* write temp file → rotate backups → rename */
    fp = fopen(tmp_path, "wb");
    if (!fp) {
        OSReport("[PC] GCI save: failed to open temp file '%s'\n", tmp_path);
        free(file_data);
        return FALSE;
    }

    if (fwrite(&dir_hdr, GCI_HEADER_SIZE, 1, fp) != 1 ||
        fwrite(file_data, GCI_FILE_DATA_SIZE, 1, fp) != 1) {
        OSReport("[PC] GCI save: fwrite failed (disk full?)\n");
        fclose(fp);
        remove(tmp_path);
        free(file_data);
        return FALSE;
    }

    fflush(fp);
    fclose(fp);
    free(file_data);

    pc_save_rotate_backups(gci_path);
    if (rename(tmp_path, gci_path) != 0) {
        OSReport("[PC] GCI save: rename '%s' -> '%s' failed, recovering...\n",
                 tmp_path, gci_path);
        {
            char bak1[300];
            snprintf(bak1, sizeof(bak1), "%s.bak1", gci_path);
            rename(bak1, gci_path);
        }
        remove(tmp_path);
        return FALSE;
    }

    OSReport("[PC] GCI save: written successfully to %s (backups rotated)\n", gci_path);
    return TRUE;
}

/* Stage 0.5C: the smallest possible callable surface for the existing Card-A GCI writer, for
 * future server-persistence code that must not route through mCD_SaveHome_bg()/
 * mCD_InitGameStart_bg()/mCD_SaveStation_NextLand_bg() -- all three carry gameplay-specific side
 * effects (mCD_SaveHome_bg's pc_save_pre_write_side_effects() alone clears held Wisp items and
 * rewrites reset_code/copy_protect/travel_hard_time; the other two carry their own bind/travel
 * side effects) that are inappropriate for a headless persistence operation.
 *
 * This calls pc_save_write_gci() -- not pc_save_write_gci_to() -- because it is already the
 * existing zero-logic selector for the canonical Card-A path (PC_GCI_PATH/PC_GCI_TMP_PATH); using
 * it means this wrapper never constructs or duplicates a path itself. pc_save_write_gci_to() is
 * used elsewhere only for the Card-B travel case, which does not apply here: a bootstrapped
 * resident is always a Card-A resident (pcfa_save_ready() already excludes player_no >=
 * mPr_FOREIGNER, per the Stage 0.5 persistence audit).
 *
 * Introduces no new serialization, Save_t manipulation, inventory/field mutation, network
 * behavior, timing, or shutdown behavior -- it is exactly one call to the existing writer, whose
 * own pc_save_ready gate and tmp/backup/rename mechanics are entirely unchanged. Returns whatever
 * pc_save_write_gci() returns (TRUE/FALSE), unchanged.
 *
 * Stage M1-1: every INTERACTIVE save trigger reaches the writer only via Actor_info_save_actor()
 * (src/game/m_actor.c:899-915), which settles transient live-actor field placeholders
 * (DUMMY_* / RSV_NO / RSV_SIGNBOARD) via restore_fgdata_all(play) before serializing. This wrapper's
 * periodic/shutdown callers never went through that function (by design -- see above), so this
 * settling step was missing for M1's own triggers. Call the SAME existing decomp function
 * directly -- restore_fgdata_all(GAME_PLAY* play), declared include/m_actor.h:1208 -- not
 * Actor_info_save_actor() itself, since that also runs every live actor's one-shot sv_proc
 * callback and permanently clears it (m_actor.c:907-910), a broader, non-idempotent side effect
 * this checkpoint is not scoped to introduce.
 *
 * Guarded on gamePT != NULL because the two callers reach this function in genuinely different
 * states: pc_vi.c's periodic call runs mid-frame with a live GAME_PLAY (SCENE_FG loaded, actors
 * alive, exactly when a transient placeholder could exist) -- restore_fgdata_all runs and settles
 * it, as intended. src/main.c's shutdown call runs AFTER graph_proc() has already returned, and
 * graph_proc's own frame-loop exit already tore the scene down first: game_dt() (src/game.c:239-
 * 250) calls the current game's cleanup (play_cleanup, src/game/m_play.c:355) BEFORE returning,
 * which calls Actor_info_dt() (m_play.c:377) -> Actor_info_delete() per actor (m_actor.c:917-932),
 * itself already calling restore_fgdata_one() (m_actor.c:922) for every actor as it is destroyed
 * -- so by shutdown-save time every transient placeholder is already settled by ordinary teardown,
 * and game_dt() has already set gamePT = NULL (src/game.c:249). Calling restore_fgdata_all(NULL)
 * would dereference play->actor_info unconditionally (m_actor.c:883) and crash; the NULL guard
 * correctly and safely skips a call that has nothing left to do.
 *
 * M1 hardening: guarded on pc_net_game_role() != PC_NETGAME_ROLE_CLIENT as a defense-in-depth
 * invariant, independent of what any caller already checked. A network CLIENT never owns the
 * authoritative Save_t it is rendering (the host is the only correct writer of it -- see
 * pc_vi.c's periodic-save doc comment); today's two callers (pc_vi.c's periodic block and
 * src/main.c's shutdown block) already gate on this themselves before calling in, but this check
 * makes the invariant hold for ANY future caller too, without relying on every call site
 * remembering to re-derive it. Rejects by returning FALSE without touching gamePT,
 * restore_fgdata_all(), or pc_save_write_gci() at all -- no partial work, no serialization
 * duplication, no change to the GCI format/Save_t/checksum/backup-rotation/atomic-rename
 * behavior of the existing writer. Single-player (PC_NETGAME_ROLE_NONE) and the host
 * (PC_NETGAME_ROLE_HOST) are both unaffected and fall through exactly as before.
 * s_pc_save_authoritative_client_reject_count / pc_save_authoritative_client_reject_count()
 * exist purely as deterministic test instrumentation (see the accessor below) so this rejection
 * can be observed/asserted on without inferring it from log timestamps. */
static int s_pc_save_authoritative_client_reject_count = 0;

int pc_save_write_authoritative(void) {
    if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {
        s_pc_save_authoritative_client_reject_count++;
        OSReport("[PC] pc_save_write_authoritative: REJECTED -- this process is a network CLIENT "
                 "and must never write the authoritative town (the host is the only valid writer)\n");
        return FALSE;
    }
    if (gamePT != NULL) {
        restore_fgdata_all((GAME_PLAY*)gamePT);
    }
    return pc_save_write_gci();
}

/* Test/diagnostic accessor for s_pc_save_authoritative_client_reject_count above -- how many
 * times pc_save_write_authoritative() has rejected a call because this process was a network
 * CLIENT, since process start. Deliberately a real extern function (not static) so it resolves
 * by name for out-of-process inspection (e.g. via gdb -p PID -batch -ex "print (int)
 * pc_save_authoritative_client_reject_count()"), the same technique this project's net_spike
 * tooling already uses elsewhere. Never mutates anything. */
int pc_save_authoritative_client_reject_count(void) {
    return s_pc_save_authoritative_client_reject_count;
}

/* M1 hardening: a tiny role predicate exposed for non-PC-only translation units (namely
 * src/main.c) that cannot #include pc_net_game.h (it is a Nintendo-side file kept close to the
 * original decomp, which by this file's own established convention declares whatever externs it
 * needs locally rather than including PC-only headers). Wraps pc_net_game_role() ==
 * PC_NETGAME_ROLE_CLIENT -- the exact same semantics pc_vi.c's periodic-save block and
 * pc_save_write_authoritative() above both already check -- so src/main.c's shutdown-save block
 * can gate on "am I a network CLIENT?" without needing the real PCNetGameRole enum visible in
 * that translation unit, and without inventing any new role/authority concept of its own. */
int pc_net_game_role_is_client(void) {
    return pc_net_game_role() == PC_NETGAME_ROLE_CLIENT;
}

/* Read a GCI file into common_data (for home town / Card A) */
static int pc_save_read_gci(const char* path) {
    FILE* fp;
    CARDDir dir_hdr;
    u8* file_data;
    Save_t* save_src;
    u32 offset;
    long file_size;

    fp = fopen(path, "rb");
    if (!fp) {
        OSReport("[PC] GCI: fopen('%s') failed\n", path);
        return FALSE;
    }

    fseek(fp, 0, SEEK_END);
    file_size = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    OSReport("[PC] GCI: opened '%s', size = %ld (0x%lX), expected %ld (0x%lX)\n",
             path, file_size, (unsigned long)file_size,
             (long)(GCI_HEADER_SIZE + GCI_FILE_DATA_SIZE),
             (unsigned long)(GCI_HEADER_SIZE + GCI_FILE_DATA_SIZE));

    if (fread(&dir_hdr, GCI_HEADER_SIZE, 1, fp) != 1) {
        OSReport("[PC] GCI: failed to read %u-byte header\n", (unsigned)GCI_HEADER_SIZE);
        fclose(fp);
        return FALSE;
    }

    OSReport("[PC] GCI: gameName='%c%c%c%c' company='%c%c' fileName='%.32s'\n",
             dir_hdr.gameName[0], dir_hdr.gameName[1],
             dir_hdr.gameName[2], dir_hdr.gameName[3],
             dir_hdr.company[0], dir_hdr.company[1],
             dir_hdr.fileName);
    if (memcmp(dir_hdr.gameName, "GAF", 3) != 0) {
        OSReport("[PC] GCI: not an Animal Crossing save (expected GAFx, got '%.4s')\n",
                 dir_hdr.gameName);
        fclose(fp);
        return FALSE;
    }

    file_data = (u8*)malloc(GCI_FILE_DATA_SIZE);
    if (!file_data) {
        OSReport("[PC] GCI: malloc(%u) failed\n", (unsigned)GCI_FILE_DATA_SIZE);
        fclose(fp);
        return FALSE;
    }

    if (fread(file_data, GCI_FILE_DATA_SIZE, 1, fp) != 1) {
        OSReport("[PC] GCI: failed to read %u bytes of file data (file may be too small)\n",
                 (unsigned)GCI_FILE_DATA_SIZE);
        fclose(fp);
        free(file_data);
        return FALSE;
    }
    fclose(fp);

    save_src = (Save_t*)(file_data + GCI_SAVE_MAIN_OFFSET);
    pc_save_bswap_verify_roundtrip((const u8*)save_src, sizeof(Save_t));

    memcpy(&common_data.save.save, save_src, sizeof(Save_t));
    pc_save_bswap(&common_data.save.save, PC_BSWAP_FROM_BE);

    /* --- Load ARAM blocks from Others section ---
     * Current saves (PC + Dolphin/GC) use order: mail, original, diary.
     * Old PC saves (before landid fix) used: original, mail, diary.
     * Detect by checking the landid field at offset 2 of the first block:
     * if it matches save's land_info.id, first block is mail (GC order). */
    {
        u8* others_ptr = file_data + GCI_OTHERS_OFFSET;
        u32 block_start = ALIGN_NEXT(sizeof(MemcardHeader_c) + 32, 32);
        u16 first_landid = ((u16)others_ptr[block_start + 2] << 8) | others_ptr[block_start + 3];
        u16 save_land_id = common_data.save.save.land_info.id;
        int gc_order = (first_landid == save_land_id && save_land_id != 0);
        u32 mail_size = l_aram_alloc_size_table[mCD_ARAM_DATA_MAIL];
        u32 orig_size = l_aram_alloc_size_table[mCD_ARAM_DATA_ORIGINAL];
        u32 off_mail, off_orig, off_diary;

        if (gc_order) {
            /* Dolphin/GC save: mail, original, diary */
            off_mail = block_start;
            off_orig = ALIGN_NEXT(block_start + mail_size, 32);
            off_diary = ALIGN_NEXT(off_orig + orig_size, 32);
        } else {
            /* PC save: original, mail, diary */
            off_orig = block_start;
            off_mail = ALIGN_NEXT(block_start + orig_size, 32);
            off_diary = ALIGN_NEXT(off_mail + mail_size, 32);
        }

        if (l_aram_block_p_table[mCD_ARAM_DATA_MAIL]) {
            pc_save_bswap_verify_roundtrip_mail(others_ptr + off_mail, mail_size);
            memcpy(l_aram_block_p_table[mCD_ARAM_DATA_MAIL], others_ptr + off_mail, mail_size);
            pc_save_bswap_keep_mail((mCD_keep_mail_c*)l_aram_block_p_table[mCD_ARAM_DATA_MAIL],
                                    PC_BSWAP_FROM_BE);
        }
        if (l_aram_block_p_table[mCD_ARAM_DATA_ORIGINAL]) {
            pc_save_bswap_verify_roundtrip_original(others_ptr + off_orig, orig_size);
            memcpy(l_aram_block_p_table[mCD_ARAM_DATA_ORIGINAL], others_ptr + off_orig, orig_size);
            pc_save_bswap_keep_original((mCD_keep_original_c*)l_aram_block_p_table[mCD_ARAM_DATA_ORIGINAL],
                                        PC_BSWAP_FROM_BE);
        }
        if (l_aram_block_p_table[mCD_ARAM_DATA_DIARY]) {
            pc_save_bswap_verify_roundtrip_diary(others_ptr + off_diary,
                                                  l_aram_alloc_size_table[mCD_ARAM_DATA_DIARY]);
            memcpy(l_aram_block_p_table[mCD_ARAM_DATA_DIARY], others_ptr + off_diary,
                   l_aram_alloc_size_table[mCD_ARAM_DATA_DIARY]);
            pc_save_bswap_keep_diary((mCD_keep_diary_c*)l_aram_block_p_table[mCD_ARAM_DATA_DIARY],
                                     PC_BSWAP_FROM_BE);
        }
    }

    free(file_data);
    return TRUE;
}

/* Read a GCI file's Save_t into a provided buffer (for Card B — does NOT touch common_data).
 * Also loads ARAM blocks (mail/original/diary) into the l_keep* buffers.
 * Returns TRUE on success. */
static int pc_save_read_gci_to_keep(const char* path) {
    FILE* fp;
    CARDDir dir_hdr;
    u8* file_data;
    Save_t* save_src;
    u32 offset;

    fp = fopen(path, "rb");
    if (!fp) return FALSE;

    if (fread(&dir_hdr, GCI_HEADER_SIZE, 1, fp) != 1) { fclose(fp); return FALSE; }
    if (memcmp(dir_hdr.gameName, "GAF", 3) != 0) { fclose(fp); return FALSE; }

    file_data = (u8*)malloc(GCI_FILE_DATA_SIZE);
    if (!file_data) { fclose(fp); return FALSE; }

    if (fread(file_data, GCI_FILE_DATA_SIZE, 1, fp) != 1) {
        fclose(fp); free(file_data); return FALSE;
    }
    fclose(fp);

    /* Load Save_t into l_keepSave (try main, fall back to backup) */
    save_src = (Save_t*)(file_data + GCI_SAVE_MAIN_OFFSET);
    memcpy(&l_keepSave.save, save_src, sizeof(Save_t));
    pc_save_bswap(&l_keepSave.save, PC_BSWAP_FROM_BE);

    /* Validate — if main is corrupt, try backup */
    if (!mLd_CheckId(l_keepSave.save.land_info.id)) {
        OSReport("[PC] Card B: main save invalid, trying backup\n");
        save_src = (Save_t*)(file_data + GCI_SAVE_BACK_OFFSET);
        memcpy(&l_keepSave.save, save_src, sizeof(Save_t));
        pc_save_bswap(&l_keepSave.save, PC_BSWAP_FROM_BE);
        if (!mLd_CheckId(l_keepSave.save.land_info.id)) {
            OSReport("[PC] Card B: backup save also invalid\n");
            free(file_data);
            return FALSE;
        }
    }

    /* Load ARAM blocks — detect GC vs legacy PC order (same landid check as main load) */
    {
        u8* others_ptr = file_data + GCI_OTHERS_OFFSET;
        u32 block_start = ALIGN_NEXT(sizeof(MemcardHeader_c) + 32, 32);
        u16 first_landid = ((u16)others_ptr[block_start + 2] << 8) | others_ptr[block_start + 3];
        u16 save_land_id = l_keepSave.save.land_info.id;
        int gc_order = (first_landid == save_land_id && save_land_id != 0);
        u32 mail_size = l_aram_alloc_size_table[mCD_ARAM_DATA_MAIL];
        u32 orig_size = l_aram_alloc_size_table[mCD_ARAM_DATA_ORIGINAL];
        u32 off_mail, off_orig, off_diary;

        if (gc_order) {
            off_mail = block_start;
            off_orig = ALIGN_NEXT(block_start + mail_size, 32);
            off_diary = ALIGN_NEXT(off_orig + orig_size, 32);
        } else {
            off_orig = block_start;
            off_mail = ALIGN_NEXT(block_start + orig_size, 32);
            off_diary = ALIGN_NEXT(off_mail + mail_size, 32);
        }

        memcpy(&l_keepMail, others_ptr + off_mail, mail_size);
        pc_save_bswap_keep_mail(&l_keepMail, PC_BSWAP_FROM_BE);

        memcpy(&l_keepOriginal, others_ptr + off_orig, orig_size);
        pc_save_bswap_keep_original(&l_keepOriginal, PC_BSWAP_FROM_BE);

        memcpy(&l_keepDiary, others_ptr + off_diary, l_aram_alloc_size_table[mCD_ARAM_DATA_DIARY]);
        pc_save_bswap_keep_diary(&l_keepDiary, PC_BSWAP_FROM_BE);
    }

    free(file_data);
    l_keepSave_set = TRUE;
    OSReport("[PC] Card B: loaded town '%.*s' (id=0x%04X) from '%s'\n",
             8, l_keepSave.save.land_info.name,
             l_keepSave.save.land_info.id, path);
    return TRUE;
}

/* Migrate legacy flat save/ layout to save/card_a/ */
static void pc_save_migrate_legacy(void) {
    struct stat st_legacy, st_new;

    if (stat(PC_GCI_PATH_LEGACY, &st_legacy) == 0 &&
        stat(PC_GCI_PATH, &st_new) != 0) {
        int b;
        OSReport("[PC] Migrating save from '%s' to '%s'\n", PC_GCI_PATH_LEGACY, PC_GCI_PATH);
        pc_ensure_save_dirs();

        /* Move main save */
        remove(PC_GCI_PATH); /* in case it somehow exists */
        rename(PC_GCI_PATH_LEGACY, PC_GCI_PATH);

        /* Move backups */
        for (b = 1; b <= PC_SAVE_MAX_BACKUPS; b++) {
            char old_bak[300], new_bak[300];
            snprintf(old_bak, sizeof(old_bak), "%s.bak%d", PC_GCI_PATH_LEGACY, b);
            snprintf(new_bak, sizeof(new_bak), "%s.bak%d", PC_GCI_PATH, b);
            remove(new_bak);
            rename(old_bak, new_bak);
        }

        /* Move temp file if orphaned */
        {
            char old_tmp[300];
            snprintf(old_tmp, sizeof(old_tmp), "%s.tmp", PC_GCI_PATH_LEGACY);
            remove(PC_GCI_TMP_PATH);
            rename(old_tmp, PC_GCI_TMP_PATH);
        }

        OSReport("[PC] Migration complete\n");
    }
}

static int pc_save_scan_gci_dir(void) {
    /* Try common AC save filenames in card_a/ */
    static const char* gci_names[] = {
        PC_CARD_A_DIR "/DobutsunomoriP_MURA.gci",
        PC_CARD_A_DIR "/8P-GAFE-DobutsunomoriP_MURA.gci",
        NULL
    };
    int i;
    struct stat st;

    for (i = 0; gci_names[i] != NULL; i++) {
        if (stat(gci_names[i], &st) == 0) {
            OSReport("[PC] GCI scan: found '%s'\n", gci_names[i]);
            if (pc_save_read_gci(gci_names[i])) {
                return TRUE;
            }
        }
    }

    /* Also try dynamic scan of card_a/ for any GCI */
    {
        char found_path[300];
        if (pc_card_scan_for_gci(0, found_path, sizeof(found_path))) {
            OSReport("[PC] GCI scan: found '%s' via directory scan\n", found_path);
            if (pc_save_read_gci(found_path)) {
                return TRUE;
            }
        }
    }

    return FALSE;
}

/* Reload save from GCI file on disk. PC equivalent of GC re-reading the
 * memory card. */
int pc_save_reload(void) {
    struct stat st;
    if (!pc_save_loaded) return 0;
    if (stat(PC_GCI_PATH, &st) == 0) {
        return pc_save_read_gci(PC_GCI_PATH);
    }
    return pc_save_scan_gci_dir();
}

int pc_save_check_and_load(void) {
    struct stat st;
    {
        char cwd[512];
        if (getcwd(cwd, sizeof(cwd))) {
            OSReport("[PC] Save: current working directory = '%s'\n", cwd);
        }
    }

    pc_ensure_save_dirs();
    pc_save_migrate_legacy();

    if (stat(PC_GCI_PATH, &st) == 0) {
        OSReport("[PC] Found GCI save: %s (%ld bytes)\n", PC_GCI_PATH, (long)st.st_size);
        if (pc_save_read_gci(PC_GCI_PATH)) {
            OSReport("[PC] GCI save loaded successfully\n");
            return TRUE;
        }
        OSReport("[PC] GCI save load FAILED\n");
    } else {
        OSReport("[PC] No GCI save at %s\n", PC_GCI_PATH);
    }

    OSReport("[PC] Scanning for other GCI files...\n");
    if (pc_save_scan_gci_dir()) {
        OSReport("[PC] GCI save loaded via scan\n");
        return TRUE;
    }

    /* recovery: try temp file, then backups */
    if (stat(PC_GCI_TMP_PATH, &st) == 0) {
        OSReport("[PC] Found orphaned temp save '%s', recovering...\n", PC_GCI_TMP_PATH);
        if (rename(PC_GCI_TMP_PATH, PC_GCI_PATH) == 0 && pc_save_read_gci(PC_GCI_PATH)) {
            OSReport("[PC] Recovered save from temp file\n");
            return TRUE;
        }
    }
    {
        char bak_path[300];
        int b;
        for (b = 1; b <= PC_SAVE_MAX_BACKUPS; b++) {
            snprintf(bak_path, sizeof(bak_path), "%s.bak%d", PC_GCI_PATH, b);
            if (stat(bak_path, &st) == 0) {
                OSReport("[PC] Found backup save '%s', recovering...\n", bak_path);
                if (pc_save_read_gci(bak_path)) {
                    OSReport("[PC] Recovered save from backup %d\n", b);
                    return TRUE;
                }
            }
        }
    }

    OSReport("[PC] No save file found\n");
    return FALSE;
}

/* --- Card B scanning --- */

/* Check if Card B directory has a valid AC town GCI */
static int pc_card_b_find_town(void) {
    if (pc_card_scan_for_gci(1, l_card_b_gci_path, sizeof(l_card_b_gci_path))) {
        OSReport("[PC] Card B: found GCI at '%s'\n", l_card_b_gci_path);
        return TRUE;
    }
    l_card_b_gci_path[0] = '\0';
    return FALSE;
}

/* --- Memory card API implementation --- */

void mCD_init_card(void) {
    CARDInit();
}

void mCD_InitAll(void) {
    /* ARAM blocks must persist — don't null them */
    memset(&l_mcd_foreigner_file, 0, sizeof(l_mcd_foreigner_file));
    l_keepSave_set = FALSE;
    l_mcd_keep_startCond = 0;
    l_card_b_gci_path[0] = '\0';
}

int mCD_InitGameStart_bg(int player_no, int card_private_idx, int start_cond, s32* mounted_chan) {
    static int init_done = 0;

/* OBSERVER-BEGIN */
    if (pc_host_observer_active()) {
        /* --host-observer: the observer never goes through the player-select / train flow. mSDI_StartDataInit(PAK) from here would call
         * mEv_SetGateway() and the OUTGOING branch would merge the passport into a resident record. Refused (defence in depth: unreachable). */
        OSReport("[NET][OBSERVER] host: mCD_InitGameStart_bg refused (the observer never enters the player-select / travel flow)\n");
        if (mounted_chan) *mounted_chan = mCD_SLOT_A;
        return mCD_TRANS_ERR_NONE;
    }
/* OBSERVER-END */
    /* On GC, save is re-read from the memory card each game start.
     * On PC, the save was already reloaded from disk in common_data_reinit
     * and aAL_title_game_data_init_start_select. We just need to allow
     * mSDI_StartDataInit to re-process it. */
    if (init_done && pc_save_loaded) {
        init_done = 0;
    }

    if (!init_done) {
        init_done = 1; /* before call — prevents re-entry via crash recovery */

        if (pc_save_loaded) {
            static int init_mode_table[] = { mSDI_INIT_MODE_NEW, mSDI_INIT_MODE_FROM,
                                             mSDI_INIT_MODE_NEW_PLAYER, mSDI_INIT_MODE_PAK,
                                             mSDI_INIT_MODE_PAK };
            int mode = mSDI_INIT_MODE_FROM;
            if (start_cond >= 0 && start_cond < 5) {
                mode = init_mode_table[start_cond];
            }
            mSDI_StartDataInit(gamePT, player_no, mode);
            l_mcd_keep_startCond = start_cond;
            pc_save_ready = 1;

            /* Reset detection (Resetti): check if previous session ended without saving */
            if (Now_Private != NULL && start_cond != mCD_START_COND_INCOMING_FOREIGNER
                && start_cond != mCD_START_COND_OUTGOING_FOREIGNER) {
                Common_Set(reset_flag, FALSE);
                if (Now_Private->reset_code != 0) {
                    /* Birthday clears reset penalty (matches original) */
                    if (Now_Private->state_flags & mPr_FLAG_BIRTHDAY_ACTIVE) {
                        Now_Private->reset_code = 0;
                    } else if (!ZURUMODE2_ENABLED() && !g_pc_settings.disable_resetti) {
                        Common_Set(reset_flag, TRUE);
                        Now_Private->reset_count++;
                        OSReport("[PC] Reset detected! reset_count=%d — Resetti will appear\n",
                                 Now_Private->reset_count);
                    }
                }
                /* Arm reset code: if player quits without saving, next load detects it.
                 * Batch G1: a network CLIENT never persists (F1: save dialog, periodic and shutdown
                 * saves are all skipped), so an armed code written here could never be cleared by a
                 * full save and Resetti would nag on every later launch. The client role is set at
                 * boot (pc_main.c) before any save loads, so skip the arming AND the persist below
                 * for a CLIENT. Host and single-player are unchanged. */
                if (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT) {
                pc_set_reset_code(Now_Private);

                /* GC writes the save (armed code included) back to the card
                 * here (bg_write_main/bg_write_bk). Persist to disk or the
                 * armed code never survives a quit and Resetti can't trigger.
                 * Cond 1 only - matches GC (new players aren't saved yet). */
                if (start_cond == mCD_START_COND_1) {
                    u16 copy_protect = pc_get_land_copy_protect();
                    Common_Set(copy_protect, copy_protect);
                    Save_Set(copy_protect, copy_protect);
                    Save_Set(travel_hard_time, lbRTC_HardTime());
                    if (!pc_save_write_gci()) {
                        OSReport("[PC] InitGameStart: reset-code persist failed\n");
                    }
                }
                } /* Batch G1: role != CLIENT */
            }

            /* Handle foreigner start conditions */
            if (start_cond == mCD_START_COND_INCOMING_FOREIGNER) {
                /* Arriving at another town as foreigner — point now_private to passport */
                Common_Set(now_private, &l_mcd_foreigner_file.file.priv);
                Common_Set(player_no, mPr_FOREIGNER);
                OSReport("[PC] InitGameStart: INCOMING_FOREIGNER — player is visiting\n");
            } else if (start_cond == mCD_START_COND_OUTGOING_FOREIGNER) {
                /* Returning home. Matches m_card.c:4780 (GC case 4): merge
                 * the session passport back into the matching home save slot
                 * — this restores player_no/now_private and carries visit
                 * inventory changes into the home save. */
                Private_c* foreigner = mPr_GetForeignerP();
                mPr_CopyPrivateInfo(foreigner, &l_mcd_foreigner_file.file.priv);
                mPr_LoadPak_and_SetPrivateInfo2(foreigner, (u8)player_no);
                mHm_SetNowHome();

                /* Cond 4 fires on both train rides; write card A only after
                 * a real home landing (merge left player_no in a home slot). */
                if (Common_Get(player_no) != mPr_FOREIGNER && Now_Private != NULL) {
                    u16 copy_protect = pc_get_land_copy_protect();
                    pc_set_reset_code(Now_Private);
                    Common_Set(copy_protect, copy_protect);
                    Save_Set(copy_protect, copy_protect);
                    Save_Set(travel_hard_time, lbRTC_HardTime());
                    if (!pc_save_write_gci()) {
                        OSReport("[PC] InitGameStart: return-home persist failed\n");
                        if (mounted_chan) *mounted_chan = mCD_SLOT_A;
                        return mCD_TRANS_ERR_IOERROR;
                    }
                }
                OSReport("[PC] InitGameStart: OUTGOING_FOREIGNER — landed player_no=%d\n",
                         Common_Get(player_no));
            }
        } else if (start_cond == mCD_START_COND_0 || start_cond == mCD_START_COND_2) {
            mSDI_StartDataInit(gamePT, player_no, mSDI_INIT_MODE_NEW);
            pc_save_ready = 1;
        }
    }

    if (mounted_chan) *mounted_chan = mCD_SLOT_A;
    return mCD_TRANS_ERR_NONE;
}

/* Stage 0: --bootstrap-resident N (see pc_main.c). Non-interactive town bootstrap: binds an
 * EXISTING resident from a config-selected slot and transitions into SCENE_FG, without going
 * through the interactive Rover/player-select flow. Fires at most once per process, from
 * pc_vi.c's per-frame poll, the first time gamePT is a live "play" GAME_PLAY -- i.e. after the
 * normal first_game -> second_game -> trademark boot chain has already run, so RNG seeding
 * (init_rnd(), only called from second_game_init()) and the ARAM mail/pattern/diary buffer
 * allocation (mCD_save_data_aram_malloc(), only called from first_game_init()) are unaffected;
 * this bootstrap never bypasses that chain (see the Stage 0 bootstrap audit's risk register).
 *
 * Deliberately narrow: this reaches gameplay init only. It does NOT set pc_save_ready (saving
 * under --bootstrap-resident is a no-op for now -- pc_save_ready is armed only inside
 * mCD_InitGameStart_bg above) and does NOT run mCD_InitGameStart_bg's Resetti reset-code
 * bookkeeping. Both are out of scope for this stage. */
void pc_bootstrap_resident_poll(void) {
    extern int g_pc_bootstrap_resident; /* pc_main.c; -1 = disabled (default) */
    static int l_done = 0;
    int player_no = g_pc_bootstrap_resident;
    Private_c* priv;
    GAME_PLAY* play;
    Door_data_c door_data;
    int arrange_idx;
    static const s16 homeX[] = { 2128, 2352, 2128, 2352 };
    static const s16 homeZ[] = { 1488, 1488, 1768, 1768 };
    static const u8 drt[] = { mSc_DIRECT_SOUTH_EAST, mSc_DIRECT_SOUTH_WEST, mSc_DIRECT_SOUTH_EAST,
                              mSc_DIRECT_SOUTH_WEST };

    if (l_done || player_no < 0) {
        return; /* disabled (default), or already attempted -- fires at most once per process */
    }
    if (gamePT == NULL || gamePT->exec != play_main) {
        return; /* wait for a live "play" GAME_PLAY (any scene -- matches the production NPC's own
                  * precondition: mSDI_StartDataInit only needs game->event to be valid) */
    }
    if (((GAME_PLAY*)gamePT)->fb_wipe_mode != WIPE_MODE_NONE) {
        return; /* wait for the scene's own entrance wipe/fade to settle -- goto_other_scene()
                  * below refuses (returns "already changing scenes") while this is anything but
                  * WIPE_MODE_NONE; play_init sets it to NONE, but the scene's first play_main
                  * frame(s) can still be mid-transition when this poll first observes exec ==
                  * play_main, so wait for it explicitly rather than guessing a frame count */
    }
    l_done = 1; /* never retried, whether what follows succeeds or fails */

    if (player_no >= PLAYER_NUM) {
        OSReport("[PC] --bootstrap-resident %d: slot out of range (valid: 0..%d)\n", player_no,
                 PLAYER_NUM - 1);
        return;
    }
    if (mFRm_CheckSaveData() == FALSE) {
        OSReport("[PC] --bootstrap-resident %d: no valid town save is loaded\n", player_no);
        return;
    }
    priv = Save_GetPointer(private_data[player_no]);
    if (mPr_CheckPrivate(priv) != TRUE) {
        OSReport("[PC] --bootstrap-resident %d: slot has no resident\n", player_no);
        return;
    }
    /* Stage 0.5A safety checkpoint: mSDI_StartInitFrom's own exists==FALSE branch
     * (src/game/m_start_data_init.c:426-450) is vanilla decomp logic for "this resident was away
     * travelling when the save was last written" -- confirmed by its own comment ("Player loaded
     * their player data while 'out travelling'") and by every priv->exists=FALSE assignment site
     * (src/game/m_card.c, src/save_menu.c) all being outgoing-travel paths. That branch does not
     * refuse the bind; it silently PUNISHES the resident instead -- bzero'ing pockets, zeroing the
     * wallet/lottery-ticket fields, and clearing deliveries/errands -- before still binding them.
     * A human hitting this in the interactive flow is a real, intended penalty for quitting mid-
     * travel. A non-interactively *chosen* resident hitting it is not a player being punished for
     * their own choice; it is silent data destruction picked by whoever configured
     * --bootstrap-resident. Refuse before mSDI_StartDataInit ever runs, so Now_Private/player_no
     * are never bound and none of mSDI_StartInitFrom's side effects (including this one) execute at
     * all -- Save_t and the resident are left completely untouched, matching this function's
     * existing fail-closed style for mFRm_CheckSaveData()/mPr_CheckPrivate() above. */
    if (priv->exists != TRUE) {
        OSReport("[PC] --bootstrap-resident %d: resident is marked away/travelling "
                 "(Private_c.exists == FALSE) -- refusing to bind to avoid the vanilla "
                 "\"loaded while out travelling\" pocket/wallet/quest wipe in "
                 "mSDI_StartInitFrom\n", player_no);
        return;
    }

    /* Diagnosed while testing host-authoritative weather/Stalk Market sync (2026-09-27): normal
     * interactive play sets Common_Get(time.rtc_enabled) = TRUE from the title-demo scene
     * (src/game/m_titledemo.c:265) well before a player ever reaches gameplay; this bootstrap
     * deliberately skips the entire trademark/title-demo chain, so rtc_enabled is still FALSE
     * (src/game/m_trademark.c:97's boot-time default) the first time mSDI_StartDataInit ->
     * mSDI_StartInitAfter -> Kabu_manager()/mEnv_DecideWeather_NormalGameStart() run. With
     * rtc_enabled FALSE, mTM_time_init()'s TARGET_PC clock path (src/game/m_time.c:414) is never
     * taken -- Common_Get(time.rtc_time) never re-reads the OS/--date/--time clock at all, so it
     * stays at Common_t's zeroed boot default indefinitely (observed as a frozen ~2001-04-06
     * reading even minutes into a run, and even under --date), and any date-driven renewal (Stalk
     * Market week rollover, daily weather renewal) can never fire. Matches this same file's own
     * existing temporary-toggle precedent (mCD_ReCheckLoadLand above: "Common_Set(time.rtc_enabled,
     * TRUE)"), made permanent here (not save/restored) because this bootstrap path never returns
     * to a scene that would toggle it back off. Test-only: g_pc_bootstrap_resident is -1 (disabled)
     * unless --bootstrap-resident is explicitly passed, so this never touches normal play. */
    Common_Set(time.rtc_enabled, TRUE);

    /* The exact call site the plan specifies: reuse mSDI_StartDataInit verbatim, the same
     * function/mode mCD_InitGameStart_bg already uses for "continue an existing resident"
     * (start_cond == mCD_START_COND_1 above). Its own guards (mFRm_CheckSaveData/mPr_CheckPrivate)
     * are checked again above so a failure here is unexpected, but its return value is still
     * checked -- the bootstrap must not continue as though the bind succeeded if it did not. */
    if (mSDI_StartDataInit(gamePT, player_no, mSDI_INIT_MODE_FROM) != TRUE) {
        OSReport("[PC] --bootstrap-resident %d: mSDI_StartDataInit failed\n", player_no);
        return;
    }

    /* Stage 0.5B: arm the same file-static disk-write gate mCD_InitGameStart_bg arms on its own
     * equivalent branch (pc_save_ready = 1;, above, inside the pc_save_loaded/mCD_START_COND_1
     * path) -- same variable, same value, same semantics; pc_save_write_gci_to()'s only gate
     * ("if (!pc_save_ready) return TRUE;") does not distinguish how it was armed. Unlike that
     * branch, which arms unconditionally right after calling mSDI_StartDataInit without checking
     * its return value, this arms ONLY after the bind above has already been confirmed to return
     * TRUE -- the bootstrap must not mark the save writer live for a resident it never actually
     * bound. Does not call mCD_InitGameStart_bg/mCD_SaveHome_bg and does not touch the writer
     * itself; still no new call site invokes it (that remains Stage 0.5C's scope). */
    pc_save_ready = 1;

    /* mSDI_StartDataInit binds Now_Private/player_no but never sets Save_t.scene_no (confirmed by
     * the Stage 0 audit). Reach SCENE_FG exactly the way the production continue-town NPC does --
     * this mirrors ac_npc_restart_schedule.c_inc's aNRST_think_door field-for-field (an ordinary
     * in-game door transition to this resident's own home, via the same goto_other_scene() every
     * other door in the game uses), skipping only that NPC's own cosmetic BGM/wipe flourishes
     * (mBGMPsComp_make_ps_wipe, FADE_TYPE_DEMO), which are not needed to reach a valid play_main. */
    play = (GAME_PLAY*)gamePT;
    arrange_idx = mHS_get_arrange_idx(player_no);

    door_data.next_scene_id = SCENE_FG;
    door_data.exit_type = 1;
    door_data.extra_data = 1;
    door_data.exit_position.x = homeX[arrange_idx];
    door_data.exit_position.y = 0;
    door_data.exit_position.z = homeZ[arrange_idx];
    door_data.exit_orientation = drt[arrange_idx];
    door_data.door_actor_name = HOUSE0 + arrange_idx;
    door_data.wipe_type = WIPE_TYPE_FADE_BLACK;

    {
        int scene_res = goto_other_scene(play, &door_data, TRUE);
        if (scene_res != TRUE) {
            OSReport("[PC] --bootstrap-resident %d: goto_other_scene to SCENE_FG failed (res=%d)\n",
                     player_no, scene_res);
            return;
        }
    }
    Common_Get(transition).wipe_type = WIPE_TYPE_TRIFORCE;

    OSReport("[PC] --bootstrap-resident %d: resident bound, transitioning to town (SCENE_FG)\n",
             player_no);
}

/* Guests G2: parses "NAME,LAND,PLAYER_ID,LAND_ID" (NAME / LAND 1..8 chars, space padded like every vanilla name; ids decimal or 0x hex, 1..0xFFFE). */
static int pc_guest_parse_spec(const char* spec, PersonalID_c* out) {
    char buf[96];
    char* tok[4];
    char* p;
    int n = 0;
    unsigned long pid, lid;
    char* end;
    size_t len;
    size_t k;

    if (spec == NULL || strlen(spec) >= sizeof(buf)) {
        return 0;
    }
    strcpy(buf, spec);
    p = buf;
    tok[n++] = p;
    while (*p != '\0') {
        if (*p == ',') {
            *p = '\0';
            if (n >= 4) {
                return 0;
            }
            tok[n++] = p + 1;
        }
        p++;
    }
    if (n != 4) {
        return 0;
    }
    len = strlen(tok[0]);
    if (len < 1 || len > PLAYER_NAME_LEN) {
        return 0;
    }
    memset(out->player_name, ' ', PLAYER_NAME_LEN);
    for (k = 0; k < len; k++) {
        out->player_name[k] = (u8)tok[0][k];
    }
    len = strlen(tok[1]);
    if (len < 1 || len > LAND_NAME_SIZE) {
        return 0;
    }
    memset(out->land_name, ' ', LAND_NAME_SIZE);
    for (k = 0; k < len; k++) {
        out->land_name[k] = (u8)tok[1][k];
    }
    pid = strtoul(tok[2], &end, 0);
    if (*tok[2] == '\0' || *end != '\0' || pid == 0 || pid >= 0xFFFFul) {
        return 0;
    }
    lid = strtoul(tok[3], &end, 0);
    if (*tok[3] == '\0' || *end != '\0' || lid == 0 || lid >= 0xFFFFul) {
        return 0;
    }
    out->player_id = (u16)pid;
    out->land_id = (u16)lid;
    return 1;
}

/* Guests G2: --bootstrap-guest NAME,LAND,PLAYER_ID,LAND_ID (see pc_main.c). TEST-ONLY, default off, CLIENT role only (refused otherwise): makes THIS process a
 * GUEST -- a foreigner whose HOME PersonalID is the given one -- in the town it loaded, the state a vanilla train arrival leaves (mCD_InitGameStart_bg,
 * start_cond INCOMING_FOREIGNER: now_private = the passport, player_no = mPr_FOREIGNER, mSDI_StartDataInit(.., MODE_PAK)), and spawns it at the station
 * exactly like the restart NPC's type 1 / 2 entry (aNPS2_make_door_data: SCENE_FG at (1979, 760), RIDE_OFF_DEMO, circle wipe). The passport is a COPY of
 * the first existing resident record of the loaded town, re-keyed to the guest's HOME PersonalID (a synthetic visitor: its pockets / wallet are that
 * resident's, which is what the host's first-contact MIGRATE then imports). Same one-shot / readiness preconditions as pc_bootstrap_resident_poll().
 * It NEVER arms pc_save_ready (a guest process can write no save at all), never touches Save_t's private_data[], and fires at most once per process. */
void pc_bootstrap_guest_poll(void) {
    extern const char* g_pc_bootstrap_guest; /* pc_main.c; NULL = disabled (default) */
    static int l_done = 0;
    PersonalID_c home;
    Private_c* tmpl = NULL;
    Private_c* pass;
    GAME_PLAY* play;
    Door_data_c door_data;
    int i;

    if (l_done || g_pc_bootstrap_guest == NULL) {
        return;
    }
    if (gamePT == NULL || gamePT->exec != play_main) {
        return;
    }
    if (((GAME_PLAY*)gamePT)->fb_wipe_mode != WIPE_MODE_NONE) {
        return; /* same wipe wait as the resident bootstrap (goto_other_scene refuses while a wipe runs) */
    }
    l_done = 1;

    if (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT) {
        OSReport("[PC] --bootstrap-guest: refused (a CLIENT-only test hook)\n");
        return;
    }
    if (mFRm_CheckSaveData() == FALSE) {
        OSReport("[PC] --bootstrap-guest: no valid town save is loaded\n");
        return;
    }
    if (!pc_guest_parse_spec(g_pc_bootstrap_guest, &home)) {
        OSReport("[PC] --bootstrap-guest: bad spec '%s' (expected NAME,LAND,PLAYER_ID,LAND_ID)\n", g_pc_bootstrap_guest);
        return;
    }
    for (i = 0; i < PLAYER_NUM; i++) {
        Private_c* p = Save_GetPointer(private_data[i]);
        if (mPr_CheckPrivate(p) == TRUE && p->exists == TRUE) {
            tmpl = p;
            break;
        }
    }
    if (tmpl == NULL) {
        OSReport("[PC] --bootstrap-guest: the loaded town has no resident record to use as the passport template\n");
        return;
    }
    memset(&l_mcd_foreigner_file, 0, sizeof(l_mcd_foreigner_file));
    pass = &l_mcd_foreigner_file.file.priv;
    mPr_CopyPrivateInfo(pass, tmpl);
    mPr_CopyPersonalID(&pass->player_ID, &home);
    pass->exists = TRUE;
    pass->reset_code = 0;
    l_mcd_foreigner_file.file.copy_protect = (u16)Common_Get(copy_protect);

    Common_Set(time.rtc_enabled, TRUE); /* see pc_bootstrap_resident_poll() */
    Common_Set(now_private, pass);
    Common_Set(player_no, mPr_FOREIGNER);
    if (mSDI_StartDataInit(gamePT, mPr_FOREIGNER, mSDI_INIT_MODE_PAK) != TRUE) {
        OSReport("[PC] --bootstrap-guest: mSDI_StartDataInit failed\n");
        return;
    }
    /* pc_save_ready is deliberately NOT armed: this process can never write a save. */

    play = (GAME_PLAY*)gamePT;
    door_data.next_scene_id = SCENE_FG;
    door_data.exit_orientation = mSc_DIRECT_SOUTH;
    door_data.exit_type = 0;
    door_data.extra_data = 0;
    door_data.exit_position.x = 1979;
    door_data.exit_position.y = 0;
    door_data.exit_position.z = 760;
    door_data.door_actor_name = EMPTY_NO;
    door_data.wipe_type = WIPE_TYPE_FADE_BLACK;
    Common_Set(demo_profiles[0], mAc_PROFILE_RIDE_OFF_DEMO);
    Common_Get(transition).wipe_type = WIPE_TYPE_CIRCLE_LEFT;
    {
        int scene_res = goto_other_scene(play, &door_data, TRUE);
        if (scene_res != TRUE) {
            OSReport("[PC] --bootstrap-guest: goto_other_scene to SCENE_FG (station) failed (res=%d)\n", scene_res);
            return;
        }
    }
    OSReport("[PC] --bootstrap-guest: guest '%.8s' (home land id 0x%04X, player id 0x%04X) bound as a foreigner, arriving at the station (SCENE_FG)\n",
             (const char*)home.player_name, (unsigned)home.land_id, (unsigned)home.player_id);
}

/* OBSERVER-BEGIN */
/* --host-observer: picks the observer's reserved player_id. The identity must equal NO resident of the loaded save (every private_data[] PersonalID,
 * even a resident whose `exists` is FALSE = away) and NO house owner (homes[].ownerID): checked on the full PersonalID AND, more conservatively, on the
 * 16-bit player_id alone (so not even an id-only comparison anywhere can confuse them). Deterministic candidate sequence 0xF0FE, 0xF0FD, ... (the
 * range mPr_InitPrivateInfo never hands out is 0xF0FD.. up, 0xF000..0xF0FC is its range). Returns the id or 0 if every candidate collides. The guests
 * table needs no check here: a guest key has a home land that differs from this town's land (pcnetgame_host_guest_check), the observer's land IS this
 * town's land, and pcnetgame_guest_key_conflict() additionally refuses the observer id explicitly. */
#define PC_OBSERVER_ID_CANDIDATES 16
static u16 pc_host_observer_pick_id(const PersonalID_c* base) {
    int c;
    for (c = 0; c < PC_OBSERVER_ID_CANDIDATES; c++) {
        PersonalID_c cand = *base;
        int i, clash = 0;
        cand.player_id = (u16)(0xF0FEu - (u16)c);
        for (i = 0; i < PLAYER_NUM && !clash; i++) {
            const PersonalID_c* p = &Save_Get(private_data)[i].player_ID;
            if (memcmp(p, &cand, sizeof(cand)) == 0 || p->player_id == cand.player_id) {
                clash = 1;
            }
        }
        for (i = 0; i < mHS_HOUSE_NUM && !clash; i++) {
            const PersonalID_c* p = &Save_Get(homes[i]).ownerID;
            if (memcmp(p, &cand, sizeof(cand)) == 0 || p->player_id == cand.player_id) {
                clash = 1;
            }
        }
        if (!clash) {
            return cand.player_id;
        }
    }
    return 0;
}

/* --host-observer (see pc_main.c / pc_host_observer.h): the hidden SERVER OBSERVER bootstrap + readiness latch, called every frame from pc_vi.c next
 * to the other bootstrap polls. HOST role + flag only. Modelled on pc_bootstrap_guest_poll() / pc_bootstrap_resident_poll():
 *   1. once, on the first settled play frame (play_main, no wipe): checks a valid town save is loaded, builds the static record
 *      (mPr_InitPrivateInfo, then the reserved PersonalID), sets rtc_enabled, binds Now_Private + player_no == mPr_FOREIGNER BEFORE the init (so the
 *      init's mEv_UnSetGateway() clears a stale visitor flag), runs mSDI_StartDataInitObserver() (no mEv_SetGateway / return-animal / goodbye mail, no
 *      passport), arms pc_save_ready ONLY on success, forces borderless acres (live 3x3 acres), and goes to SCENE_FG at the station point (1979, 760)
 *      with a plain fade (no train / station cutscene, no demo profile);
 *   2. then, once, on the first frame where the town field is loaded with the observer in it (SCENE_FG, no wipe, pcfa_scene_is_town()): sets the
 *      one-way readiness latch and logs "[NET][OBSERVER] host: observer active at acre (bx,bz)".
 * A saved resident whose PersonalID cannot be told from every candidate observer id FAILS the startup (loud message, exit code 3): a hosted town
 * must never continue with an ambiguous identity. Any other failure restores the previous binding, disarms the writer and leaves the host unbound
 * (not world-ready), exactly like a host whose save never loaded. */
void pc_host_observer_poll(void) {
    static int l_started = 0;
    static int l_init_ok = 0;
    GAME_PLAY* play;

    if (!g_pc_host_observer || pc_net_game_role() != PC_NETGAME_ROLE_HOST) {
        return;
    }
    if (gamePT == NULL || gamePT->exec != play_main) {
        return;
    }
    play = (GAME_PLAY*)gamePT;
    if (play->fb_wipe_mode != WIPE_MODE_NONE) {
        return; /* goto_other_scene() refuses while a wipe runs (same wait as the bootstraps) */
    }

    if (l_started) {
        if (l_init_ok && !s_pc_observer_latched && pc_host_observer_active() && play->scene_id == SCENE_FG && Save_Get(scene_no) == SCENE_FG &&
            Common_Get(player_actor_exists) && pcfa_scene_is_town()) {
            s_pc_observer_latched = 1;
            OSReport("[NET][OBSERVER] host: observer active at acre (%d,%d)\n", (int)play->block_table.block_x, (int)play->block_table.block_z);
            OSReport("[NET][OBSERVER] host: avatar main_index=%d (hidden, no collider, no input)\n", mPlib_get_player_actor_main_index(gamePT));
        }
        return;
    }
    l_started = 1; /* never retried, whether what follows succeeds or fails */

    {
        Private_c* prev_private = Common_Get(now_private);
        int prev_player_no = Common_Get(player_no);
        Private_c* rec = &s_pc_observer_private;
        Door_data_c door_data;
        PersonalID_c base;
        u16 pid;
        int scene_res;

        if (mFRm_CheckSaveData() == FALSE) {
            OSReport("[NET][OBSERVER] host: observer init FAILED: no valid town save is loaded (the host stays unbound and not world-ready)\n");
            return;
        }

        /* The static record: valid default appearance + exists = TRUE from the vanilla initialiser (it also sets land = THIS town), then the reserved
         * identity. memset first so no byte of an earlier life remains (this runs once per process). */
        memset(rec, 0, sizeof(*rec));
        mPr_InitPrivateInfo(rec);
        base = rec->player_ID;
        memset(base.player_name, CHAR_SPACE, PLAYER_NAME_LEN);
        memcpy(base.player_name, "SERVER", 6); /* the game's font encoding is ASCII-compatible for letters (CHAR_A == 65) */
        pid = pc_host_observer_pick_id(&base);
        if (pid == 0) {
            OSReport("[NET][OBSERVER] host: observer init FAILED: no reserved PersonalID is free of the saved residents / house owners (%d candidates tried) -- "
                     "refusing to host with an ambiguous identity\n", PC_OBSERVER_ID_CANDIDATES);
            fprintf(stderr, "[NET][OBSERVER] FATAL: no reserved observer PersonalID is free of this save's residents / house owners; start the host "
                            "without --host-observer or rename the conflicting resident\n");
            fflush(stdout);
            exit(3);
        }
        base.player_id = pid;
        mPr_CopyPersonalID(&rec->player_ID, &base);
        rec->exists = TRUE;
        rec->reset_code = 0;
        rec->destiny.type = mPr_DESTINY_NORMAL;

        Common_Set(time.rtc_enabled, TRUE); /* see pc_bootstrap_resident_poll() */
        /* Bind BEFORE the init: mSDI_StartDataInitObserver() -> mEv_UnSetGateway() needs player_no == 4 to clear a stale GATEWAY_FRGN flag. */
        Common_Set(now_private, rec);
        Common_Set(player_no, mPr_FOREIGNER);
        if (mSDI_StartDataInitObserver(gamePT) != TRUE) {
            Common_Set(now_private, prev_private);
            Common_Set(player_no, prev_player_no);
            OSReport("[NET][OBSERVER] host: observer init FAILED: mSDI_StartDataInitObserver failed (binding restored, the host stays unbound)\n");
            return;
        }

        /* Armed ONLY after a successful init (mirrors pc_bootstrap_resident_poll). */
        pc_save_ready = 1;

        /* Live 3x3 acres around the parked avatar (villagers / actors stay alive in the neighbouring acres): a runtime switch. */
        if (!g_mPlib_wade_disabled) {
            aBC_RequestNearbyRefresh();
        }
        g_mPlib_wade_disabled = TRUE;

        /* Outdoor SCENE_FG at the station point like pc_bootstrap_guest_poll, but a plain fade: no RIDE_OFF_DEMO / train cutscene, no demo profile
         * (both demo_profiles are cleared so no demo actor can spawn with the player). */
        Common_Set(demo_profiles[0], mAc_PROFILE_NUM);
        Common_Set(demo_profiles[1], mAc_PROFILE_NUM);
        door_data.next_scene_id = SCENE_FG;
        door_data.exit_orientation = mSc_DIRECT_SOUTH;
        door_data.exit_type = 0;
        door_data.extra_data = 0;
        door_data.exit_position.x = 1979;
        door_data.exit_position.y = 0;
        door_data.exit_position.z = 760;
        door_data.door_actor_name = EMPTY_NO;
        door_data.wipe_type = WIPE_TYPE_FADE_BLACK;
        Common_Get(transition).wipe_type = WIPE_TYPE_FADE_BLACK;
        scene_res = goto_other_scene(play, &door_data, TRUE);
        if (scene_res != TRUE) {
            pc_save_ready = 0;
            Common_Set(now_private, prev_private);
            Common_Set(player_no, prev_player_no);
            OSReport("[NET][OBSERVER] host: observer init FAILED: goto_other_scene to SCENE_FG (station) failed (res=%d) (binding restored, writer disarmed)\n",
                     scene_res);
            return;
        }
        l_init_ok = 1;
        OSReport("[PC] --host-observer: observer bound (not a network participant: player_no=%d, PersonalID '%.8s' id 0x%04X), transitioning to town (SCENE_FG)\n",
                 (int)Common_Get(player_no), (const char*)rec->player_ID.player_name, (unsigned)rec->player_ID.player_id);
    }
}
/* OBSERVER-END */
void mCD_LoadLand(void) {
    (void)pc_save_loaded;
}

int mCD_SaveHome_bg(int param_1, int* chan) {
    int slot = mCD_GetThisLandSlotNo();
    int result;

    /* M9-D F1: a network CLIENT never persists. Its Save_t is the mirrored HOST town plus its own
     * pockets; writing it would overwrite the client's own offline town with the host's and make
     * session-local inventory races permanent. Same role test as pc_save_write_authoritative() and
     * the shutdown save (src/main.c). The vanilla save dialog (aNRST_save, ac_npc_restart_talk.c_inc)
     * still has to complete normally, so report success: it only advances the talk state on
     * mCD_TRANS_ERR_NONE and keeps no other 'saved' bookkeeping. Everything below (the pre-write side
     * effects too: Wisp removal, money-rock shine clear, reset-code arming, copy-protect stamp) is
     * skipped so the client's world/private state is untouched by the skipped save. Host and
     * single-player (role != CLIENT) fall through exactly as before. */
    if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {
        static int s_client_save_skip_logged = 0;
        if (s_client_save_skip_logged < 3) {
            s_client_save_skip_logged++;
            OSReport("[NET][SAVE] client save skipped (network CLIENT never persists; the host owns the "
                     "town) -- reporting success to the save dialog\n");
        }
        if (chan) *chan = mCD_SLOT_A;
        return mCD_TRANS_ERR_NONE;
    }

/* OBSERVER-BEGIN */
    if (pc_host_observer_active()) {
        /* --host-observer: player_no 4 maps to Card B (mCD_GetThisLandSlotNo) and the pre-write side effects are resident logic. The observer never
         * opens the save dialog (no input); the authoritative periodic / early / shutdown saves of the host use their own entry point instead.
         * Refuse without touching anything. */
        OSReport("[NET][OBSERVER] host: mCD_SaveHome_bg refused (the observer never saves through the player save dialog)\n");
        if (chan) *chan = mCD_SLOT_A;
        return mCD_TRANS_ERR_NONE;
    }
/* OBSERVER-END */
    pc_save_pre_write_side_effects(param_1);

    if (slot == mCD_SLOT_B && l_card_b_gci_path[0] != '\0') {
        /* Visiting Card B's town — save to Card B GCI */
        char tmp_path[300];
        snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", l_card_b_gci_path);
        result = pc_save_write_gci_to(l_card_b_gci_path, tmp_path);
        if (chan) *chan = mCD_SLOT_B;
    } else {
        result = pc_save_write_gci();
        if (chan) *chan = mCD_SLOT_A;
    }

    if (!result) {
        OSReport("[PC] mCD_SaveHome_bg: save failed!\n");
        return mCD_TRANS_ERR_IOERROR;
    }

    return mCD_TRANS_ERR_NONE;
}

/* --- Travel / Station functions --- */

/* Read a GCI's Save_t into `out` (byte-swapped). Returns TRUE on success. */
static int pc_read_gci_land_info(const char* path, Save_t* out) {
    FILE* fp;
    CARDDir hdr;
    u8* file_data;
    int ok = FALSE;

    fp = fopen(path, "rb");
    if (!fp) return FALSE;

    if (fread(&hdr, GCI_HEADER_SIZE, 1, fp) == 1 &&
        memcmp(hdr.gameName, "GAF", 3) == 0) {
        file_data = (u8*)malloc(GCI_FILE_DATA_SIZE);
        if (file_data) {
            if (fread(file_data, GCI_FILE_DATA_SIZE, 1, fp) == 1) {
                Save_t* save_src = (Save_t*)(file_data + GCI_SAVE_MAIN_OFFSET);
                memcpy(out, save_src, sizeof(Save_t));
                pc_save_bswap(out, PC_BSWAP_FROM_BE);
                ok = TRUE;
            }
            free(file_data);
        }
    }
    fclose(fp);
    return ok;
}

/* Scan the "other" card for a travel-eligible town.
 *  - Resident: scan Card B for a different town.
 *  - Foreigner: scan Card A for the home town. */
int mCD_CheckStation_bg(s32* chan) {
    int is_foreigner = mLd_PlayerManKindCheck();

    if (is_foreigner) {
        Save_t temp_save;
        if (chan) *chan = mCD_SLOT_B;
        if (pc_read_gci_land_info(PC_GCI_PATH, &temp_save)) {
            if (mLd_CheckId(temp_save.land_info.id) &&
                !mLd_CheckThisLand(temp_save.land_info.name, temp_save.land_info.id)) {
                OSReport("[PC] CheckStation: Card A has home town '%.*s' (id=0x%04X) — return available\n",
                         8, temp_save.land_info.name, temp_save.land_info.id);
                if (chan) *chan = mCD_SLOT_A;
                return mCD_TRANS_ERR_NONE_NEXTLAND;
            }
            OSReport("[PC] CheckStation: Card A has same town as current (unexpected)\n");
        } else {
            OSReport("[PC] CheckStation: could not read Card A save\n");
        }
        return mCD_TRANS_ERR_NONE;
    }

    if (chan) *chan = mCD_SLOT_A;
    if (pc_card_b_find_town()) {
        Save_t temp_save;
        if (pc_read_gci_land_info(l_card_b_gci_path, &temp_save)) {
            if (mLd_CheckId(temp_save.land_info.id)) {
                if (!mLd_CheckThisLand(temp_save.land_info.name, temp_save.land_info.id)) {
                    OSReport("[PC] CheckStation: Card B has town '%.*s' (id=0x%04X) — travel available\n",
                             8, temp_save.land_info.name, temp_save.land_info.id);
                    if (chan) *chan = mCD_SLOT_B;
                    return mCD_TRANS_ERR_NONE_NEXTLAND;
                }
                OSReport("[PC] CheckStation: Card B has same town as Card A\n");
            } else {
                OSReport("[PC] CheckStation: Card B has invalid land_info\n");
            }
        }
    }

    return mCD_TRANS_ERR_NONE;
}

/* Persist current town and load the "other" town into l_keepSave.
 *  - Resident: save home (Card A, marked away) + load Card B → l_keepSave.
 *  - Foreigner: save visited town (Card B) + load Card A → l_keepSave. */
int mCD_SaveStation_NextLand_bg(s32* chan) {
    int is_foreigner = mLd_PlayerManKindCheck();

    /* M9-D G4-1: a network CLIENT never writes a save file here. Both branches below persist the
     * current Save_t (the mirrored HOST town for a client) to Card A / Card B and then reload keep data,
     * so a client that owns a second town GCI would have its own offline save overwritten with the
     * host's town. Report an error instead of success: success would let aSTM_save_talk start the
     * train trip (mCD_toNextLand) without l_keepSave having been loaded. mCD_TRANS_ERR_NO_TOWN_DATA is
     * an existing vanilla result of this function: aSTM_save_talk (ac_station_clip.c_inc) answers it
     * with the normal 'no town data' message (0x0946), sets next_think_idx to 8/9 and force-advances
     * the message exactly as for any other save error, so the talk ends and the player stays in town.
     * chan = Card A like the other early NO_TOWN_DATA return below. No state is touched before this
     * guard. Host and single-player (role != CLIENT) fall through unchanged. mCD_SaveStation_Passport_bg
     * writes nothing (in-memory passport only) and needs no guard. */
    if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT) {
        static int s_client_station_skip_logged = 0;
        if (s_client_station_skip_logged < 3) {
            s_client_station_skip_logged++;
            OSReport("[NET][SAVE] client station travel save skipped (network CLIENT never persists) -- "
                     "reporting NO_TOWN_DATA to the station talk\n");
        }
        if (chan) *chan = mCD_SLOT_A;
        return mCD_TRANS_ERR_NO_TOWN_DATA;
    }

/* OBSERVER-BEGIN */
    if (pc_host_observer_active()) {
        /* --host-observer: the foreigner branch below would write Card B and reload the home town into l_keepSave (train travel). The observer must
         * never travel: refused exactly like a client (the station talk answers it with the normal 'no town data' message). */
        OSReport("[NET][OBSERVER] host: station travel save refused (the observer never travels) -- reporting NO_TOWN_DATA\n");
        if (chan) *chan = mCD_SLOT_A;
        return mCD_TRANS_ERR_NO_TOWN_DATA;
    }
/* OBSERVER-END */
    if (is_foreigner) {
        /* Record departure info (visited town) for Rover. */
        {
            mCD_persistent_data_c* persistant = Common_GetPointer(travel_persistent_data);
            int i;
            memcpy(&persistant->land, Save_GetPointer(land_info), sizeof(mLd_land_info_c));
            for (i = 0; i < PLAYER_NUM; i++) {
                mPr_CopyPersonalID(&persistant->pid[i], &Save_Get(private_data[i]).player_ID);
            }
        }

        /* Refresh passport from Now_Private so visit-time changes carry home. */
        {
            Private_c* current_priv = Now_Private;
            if (current_priv) {
                mPr_CopyPrivateInfo(&l_mcd_foreigner_file.file.priv, current_priv);
                l_mcd_foreigner_file.file.checksum = 0;
                l_mcd_foreigner_file.file.checksum =
                    mFRm_GetFlatCheckSum((u16*)&l_mcd_foreigner_file.file,
                                         sizeof(mCD_foreigner_c),
                                         l_mcd_foreigner_file.file.checksum);
                OSReport("[PC] SaveStation_NextLand(return): refreshed passport for '%.*s'\n",
                         PLAYER_NAME_LEN, l_mcd_foreigner_file.file.priv.player_ID.player_name);
            }
        }

        /* Persist visited-town state to its Card B GCI. */
        if (l_card_b_gci_path[0] != '\0') {
            char tmp_path[320];
            snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", l_card_b_gci_path);
            if (!pc_save_write_gci_to(l_card_b_gci_path, tmp_path)) {
                OSReport("[PC] SaveStation_NextLand(return): failed to save visited town\n");
                if (chan) *chan = mCD_SLOT_B;
                return mCD_TRANS_ERR_IOERROR;
            }
        } else {
            OSReport("[PC] SaveStation_NextLand(return): no Card B path cached\n");
        }

        if (!pc_save_read_gci_to_keep(PC_GCI_PATH)) {
            OSReport("[PC] SaveStation_NextLand(return): failed to load home town\n");
            if (chan) *chan = mCD_SLOT_A;
            return mCD_TRANS_ERR_CORRUPT;
        }

        l_mcd_keep_startCond = mCD_START_COND_OUTGOING_FOREIGNER;

        if (chan) *chan = mCD_SLOT_A;
        return mCD_TRANS_ERR_NONE;
    }

    if (chan) *chan = mCD_SLOT_A;

    /* Must have a Card B town path from prior CheckStation */
    if (l_card_b_gci_path[0] == '\0') {
        OSReport("[PC] SaveStation_NextLand: no Card B path\n");
        return mCD_TRANS_ERR_NO_TOWN_DATA;
    }

    /* 0. Record home town info for Rover's dialogue (departure town name) */
    {
        mCD_persistent_data_c* persistant = Common_GetPointer(travel_persistent_data);
        int i;
        memcpy(&persistant->land, Save_GetPointer(land_info), sizeof(mLd_land_info_c));
        for (i = 0; i < PLAYER_NUM; i++) {
            mPr_CopyPersonalID(&persistant->pid[i], &Save_Get(private_data[i]).player_ID);
        }
    }

    /* 1. Build passport BEFORE marking player as away (need full player data) */
    {
        Private_c* current_priv = Now_Private;
        memset(&l_mcd_foreigner_file, 0, sizeof(l_mcd_foreigner_file));
        if (current_priv) {
            mPr_CopyPrivateInfo(&l_mcd_foreigner_file.file.priv, current_priv);
        }
        memset(&l_mcd_foreigner_file.file.remove_animal, 0, sizeof(Animal_c));
        l_mcd_foreigner_file.file.copy_protect = (u16)Common_Get(copy_protect);
        l_mcd_foreigner_file.file.checksum = 0;
        l_mcd_foreigner_file.file.checksum =
            mFRm_GetFlatCheckSum((u16*)&l_mcd_foreigner_file.file,
                                 sizeof(mCD_foreigner_c),
                                 l_mcd_foreigner_file.file.checksum);
        OSReport("[PC] SaveStation_NextLand: built passport for '%.*s'\n",
                 PLAYER_NAME_LEN, l_mcd_foreigner_file.file.priv.player_ID.player_name);
    }

    /* 2. Mark player as "away" and clear reset code before saving Card A.
     * If the player quits during the visit, next load sees exists==FALSE
     * → gyroid face + inventory cleared as punishment (m_start_data_init.c:426) */
    if (Now_Private != NULL && mLd_PlayerManKindCheckNo(Common_Get(player_no)) == FALSE) {
        Now_Private->exists = FALSE;
        Now_Private->reset_code = 0;
        OSReport("[PC] SaveStation_NextLand: marked player as away (exists=FALSE)\n");
    }

    /* 3. Save home town to Card A (with player marked as away) */
    if (!pc_save_write_gci()) {
        /* Restore player state on failure */
        if (Now_Private != NULL) Now_Private->exists = TRUE;
        OSReport("[PC] SaveStation_NextLand: failed to save home town\n");
        return mCD_TRANS_ERR_IOERROR;
    }

    /* 3. Load other town from Card B into l_keepSave + l_keep* ARAM blocks */
    if (!pc_save_read_gci_to_keep(l_card_b_gci_path)) {
        OSReport("[PC] SaveStation_NextLand: failed to load Card B town\n");
        return mCD_TRANS_ERR_CORRUPT;
    }

    l_mcd_keep_startCond = mCD_START_COND_INCOMING_FOREIGNER;

    if (chan) *chan = mCD_SLOT_B;
    return mCD_TRANS_ERR_NONE;
}

/* Save passport file on Card B (simplified for PC).
 * On GC this creates a separate GCI file; on PC the passport is just in memory. */
int mCD_SaveStation_Passport_bg(s32* chan) {
    if (chan) *chan = mCD_SLOT_B;

    /* The passport is already built in l_mcd_foreigner_file from SaveStation_NextLand_bg.
     * On PC we don't need to write a separate passport GCI file — the foreigner data
     * persists in memory through the town transition (mCD_toNextLand). */
    OSReport("[PC] SaveStation_Passport: passport ready in memory\n");
    return mCD_TRANS_ERR_NONE;
}

/* Transition to the other town. Called from scene cleanup when switching to the visited town.
 * Faithfully reproduces the original mCD_toNextLand from m_card.c:7188. */
void mCD_toNextLand(void) {
    Save_t* save = &l_keepSave.save;
    int scene_no;
    mCD_persistent_data_c persis;
    mLd_land_info_c* land_info;
    int last_scene_no;
    mActor_name_t last_field_id;
    s16 demo_profile[2];
    Time_c time;
    Transition_c transition;
    int rtc_enabled;

/* OBSERVER-BEGIN */
    if (pc_host_observer_active()) {
        /* --host-observer: never leave the served town (this memset-s common_data and rebinds the passport as player 4). The vanilla scene cleanup
         * calls this on EVERY scene change (a no-op while l_keepSave_set != TRUE), so only a real travel attempt is logged; refused either way. */
        if (l_keepSave_set == TRUE) {
            OSReport("[NET][OBSERVER] host: toNextLand refused (the observer never travels)\n");
        }
        return;
    }
/* OBSERVER-END */
    if (l_keepSave_set != TRUE) {
        OSReport("[PC] toNextLand: l_keepSave not set, aborting\n");
        return;
    }

    land_info = &save->land_info;
    if (!mLd_CheckId(land_info->id)) {
        OSReport("[PC] toNextLand: invalid land_info in l_keepSave\n");
        return;
    }

    scene_no = Save_Get(scene_no);

    /* Save persistent data that must survive the common_data wipe */
    memcpy(&persis, Common_GetPointer(travel_persistent_data), sizeof(persis));
    last_scene_no = Common_Get(last_scene_no);
    last_field_id = Common_Get(last_field_id);
    memcpy(demo_profile, Common_Get(demo_profiles), sizeof(demo_profile));
    memcpy(&time, Common_GetPointer(time), sizeof(time));
    memcpy(&transition, Common_GetPointer(transition), sizeof(transition));

    /* Wipe common_data */
    memset(&common_data, 0, sizeof(common_data_t));

    /* Restore persistent fields */
    memcpy(Common_GetPointer(travel_persistent_data), &persis, sizeof(persis));
    Common_Set(last_field_id, last_field_id);
    Common_Set(last_scene_no, last_scene_no);
    memcpy(Common_Get(demo_profiles), demo_profile, sizeof(demo_profile));
    memcpy(Common_GetPointer(time), &time, sizeof(time));
    memcpy(Common_GetPointer(transition), &transition, sizeof(transition));

    /* Load the other town's save data */
    memcpy(Common_GetPointer(save), save, sizeof(Save));

    Common_Set(copy_protect, Save_Get(copy_protect));
    Save_Set(scene_no, scene_no);

    /* RTC check */
    rtc_enabled = Common_Get(time.rtc_enabled);
    Common_Set(time.rtc_enabled, TRUE);
    mTM_rtcTime_limit_check();
    Common_Set(time.rtc_enabled, rtc_enabled);
    lbRTC_GetTime(Common_GetPointer(time.rtc_time));

    /* Set current player as foreigner */
    Common_Set(now_private, &l_mcd_foreigner_file.file.priv);
    Common_Set(player_no, mPr_FOREIGNER);
    Common_Set(auto_nwrite_set, FALSE);
    memset(Common_GetPointer(auto_nwrite_time), 0, sizeof(Common_Get(auto_nwrite_time)));
    Common_Set(ball_pos, ZeroVec);

    /* Clear town-specific state */
    mTM_clear_renew_is();
    mEv_ClearEventInfo();
    mEv_toland_clear_common();
    mNpc_ClearInAnimal();
    mNpc_FirstClearGoodbyMail();
    mQst_ClearGrabItemInfo();
    memset(Common_Get(npc_schedule), 0, sizeof(Common_Get(npc_schedule)));
    mISL_ClearKeepIsland();
    memset(Common_GetPointer(unused_mail_26522), 0, sizeof(Mail_c));
    Common_Set(_2664E, 0);
    Common_Set(_26650, 0);
    mNpc_ClearCacheName();

    Common_Set(submenu_disabled, TRUE);

    /* Clear keepSave now that it's been applied */
    memset(&l_keepSave, 0, sizeof(Save));
    l_keepSave_set = FALSE;

    /* Load ARAM blocks from the other town */
    mCD_save_data_main_to_aram(&l_keepMail, l_aram_alloc_size_table[mCD_ARAM_DATA_MAIL], mCD_ARAM_DATA_MAIL);
    mCD_save_data_main_to_aram(&l_keepOriginal, l_aram_alloc_size_table[mCD_ARAM_DATA_ORIGINAL], mCD_ARAM_DATA_ORIGINAL);
    mCD_save_data_main_to_aram(&l_keepDiary, l_aram_alloc_size_table[mCD_ARAM_DATA_DIARY], mCD_ARAM_DATA_DIARY);

    OSReport("[PC] toNextLand: transitioned to visited town, player_no=%d\n",
             Common_Get(player_no));
}

/* Re-check and reload home town after returning from visit */
void mCD_ReCheckLoadLand(GAME_PLAY* play) {
    int scene = Save_Get(scene_no);

    /* Reload home town from Card A */
    pc_save_check_and_load();
    Save_Set(scene_no, scene);

    if (mFRm_CheckSaveData()) {
        int rtc_on = Common_Get(time.rtc_enabled);
        Common_Set(time.rtc_enabled, TRUE);
        mTM_rtcTime_limit_check();
        play->next_scene_no = SCENE_PLAYERSELECT_2;
        Common_Set(time.rtc_enabled, rtc_on);
        mEv_ClearEventInfo();
    } else {
        play->next_scene_no = SCENE_PLAYERSELECT;
        Common_Set(house_owner_name, RSV_NO);
        Common_Set(last_field_id, RSV_NO);
    }

    OSReport("[PC] ReCheckLoadLand: next_scene=%d\n", play->next_scene_no);
}

/* --- Remaining card management functions --- */

int mCD_GetThisLandSlotNo(void) {
    /* If visiting another town, current land is on Card B */
    if (Common_Get(player_no) == mPr_FOREIGNER) return mCD_SLOT_B;
    return mCD_SLOT_A;
}

int mCD_GetThisLandSlotNo_code(int* player_no, s32* slot_card_results) {
    int slot = mCD_GetThisLandSlotNo();

    if (player_no) *player_no = Common_Get(player_no);
    if (slot_card_results) {
        slot_card_results[mCD_SLOT_A] = CARD_RESULT_READY;
        slot_card_results[mCD_SLOT_B] = (slot == mCD_SLOT_B) ? CARD_RESULT_READY : CARD_RESULT_NOCARD;
    }
    return slot;
}

int mCD_GetSaveHomeSlotNo(void) {
    return mCD_SLOT_A;
}

int mCD_GetPlayerNum(void) {
    return 1;
}

int mCD_GetCardPrivateNameCopy(u8* name, int idx) {
    (void)name;
    (void)idx;
    return 0;
}

int mCD_CheckCardPlayerNative(int idx) {
    /* If foreigner mode is active, player 4 (mPr_FOREIGNER) is not native */
    if (Common_Get(player_no) == mPr_FOREIGNER && idx == mPr_FOREIGNER) {
        return FALSE;
    }
    return TRUE;
}

int mCD_CheckPassportFile(void) {
    /* Check if there's a passport in memory (from a previous travel) */
    if (l_mcd_foreigner_file.file.checksum != 0) {
        return 0; /* passport exists on slot 0 */
    }
    return -1; /* no passport */
}

int mCD_CheckBrokenPassportFile(int slot) {
    (void)slot;
    return 0;
}

int mCD_EraseBrokenLand_bg(int* slot) {
    if (slot) *slot = mCD_SLOT_A;
    return mCD_TRANS_ERR_NONE;
}

int mCD_EraseLand_bg(int* slot) {
    if (slot) *slot = mCD_SLOT_A;
    return mCD_TRANS_ERR_NONE;
}

int mCD_ErasePassportFile_bg(int slot) {
    (void)slot;
    /* Clear the in-memory passport */
    memset(&l_mcd_foreigner_file, 0, sizeof(l_mcd_foreigner_file));
    return mCD_TRANS_ERR_NONE;
}

int mCD_SaveErasePlayer_bg(int* slot) {
    if (slot) *slot = mCD_SLOT_A;
    return mCD_TRANS_ERR_NONE;
}

int mCD_card_format_bg(s32 chan) {
    (void)chan;
    return mCD_TRANS_ERR_NONE;
}

void mCD_PrintErrInfo(gfxprint_t* gfxprint) {
    (void)gfxprint;
}
