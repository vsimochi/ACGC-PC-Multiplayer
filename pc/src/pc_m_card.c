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
#include "pc_residence.h" /* PC_RESIDENCE_SLOTS / PC_RESIDENCE_HOUSES (lifecycle hardening) */
#include "pc_session.h" /* M2: pc_session() = a STORE character (characters/<uuid>/character.ini) instead of a legacy guest profile file */
#include "pc_guest_profile.h" /* Guests G3.2: PCGuestProfile / pc_guest_profile_load_or_create (the title-menu "Join as Guest" item) */
#include "pc_mp_guests.h" /* Guests G1: pc_mp_guests_name_valid() (the guest NAME rule shared with the host) */
#include "m_string.h"   /* Guests G1: mString_Load_StringFromRom (default design names) */
#include "jsyswrap.h"   /* Guests G1: _JW_GetResourceAram (default design textures) */
/* OBSERVER-BEGIN */
#include "pc_host_observer.h"
#include "pc_log.h"
#include "pc_dedicated.h"
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
#include "pc_town_cache.h"  /* M-A: pc_card_a_dir() / pc_gci_path() / pc_gci_tmp_path() (runtime Card-A dir); M-B: PCTownId, CRC32 */
#include "pc_town_sanitize.h" /* M-G: sanitized town transfer image (pure module) + the templates built below */

/* --- Path constants --- */
#define PC_CARD_B_DIR     "save/card_b"
/* M-A: the Card-A directory / GCI paths are RUNTIME values (pc_town_cache.c: pc_card_a_dir() / pc_gci_path() / pc_gci_tmp_path()); default save/card_a. */
#define PC_GCI_FILENAME   "DobutsunomoriP_MURA.gci"
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
/* M-G: 1 when the Card-A GCI this process loaded is a SANITIZED town transfer image (marker "ACMPSAN1" at file offset 0x70, see pc_town_sanitize.h). Only a network
 * CLIENT may hold one (pc_save_read_gci refuses it in every other role, exit 3); the writer never writes while it is set. */
int g_pc_save_sanitized = 0;
extern void pc_main_refuse_sanitized_town(void); /* pc_main.c: message box + exit 3 */

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

/* PERSONAL DATA sync (diary only, docs/multiplayer-guest-roadmap.md "Personal data sync"): per-resident access to ONE slot of the live diary ARAM block
 * (entries[slot][0..11], 12 x 992 = 0x2E80 bytes at +2). The block is the same memory the normal host save writes to the GCI and the vanilla diary overlay copies
 * whole at open / writes back whole at close; the CALLER (pc_net_game.c) never patches it while a diary menu is open. checksum / landid are never touched here
 * (the writer recomputes them). get / put return 1 on success, 0 for a bad slot or a block that does not exist yet. */
_Static_assert(offsetof(mCD_keep_diary_c, entries) == 2 && sizeof(((mCD_keep_diary_c*)0)->entries[0]) == PC_M_CARD_DIARY_SLOT_SIZE &&
                   mCD_KEEP_DIARY_COUNT == 4 && PC_M_CARD_DIARY_SLOT_SIZE == 0x2E80,
               "diary block layout changed: the personal data sync slot accessor is wrong");

int pc_m_card_diary_slot_get(int slot, void* out) {
    const mCD_keep_diary_c* d = (const mCD_keep_diary_c*)l_aram_block_p_table[mCD_ARAM_DATA_DIARY];
    if (d == NULL || out == NULL || slot < 0 || slot >= mCD_KEEP_DIARY_COUNT) {
        return 0;
    }
    memcpy(out, d->entries[slot], PC_M_CARD_DIARY_SLOT_SIZE);
    return 1;
}

int pc_m_card_diary_slot_put(int slot, const void* in) {
    mCD_keep_diary_c* d = (mCD_keep_diary_c*)l_aram_block_p_table[mCD_ARAM_DATA_DIARY];
    if (d == NULL || in == NULL || slot < 0 || slot >= mCD_KEEP_DIARY_COUNT) {
        return 0;
    }
    memcpy(d->entries[slot], in, PC_M_CARD_DIARY_SLOT_SIZE);
    return 1;
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
    if (pc_card_town_dir_active()) {
        pc_town_mkdirs(pc_card_a_dir()); /* M-A: save/mp/towns/<key>/card_a (save/card_a is NOT created / touched when a town dir is set) */
    } else {
        _mkdir(pc_card_a_dir());
    }
    _mkdir(PC_CARD_B_DIR);
#else
    mkdir(PC_SAVE_DIR, 0755);
    if (pc_card_town_dir_active()) {
        pc_town_mkdirs(pc_card_a_dir());
    } else {
        mkdir(pc_card_a_dir(), 0755);
    }
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

static unsigned s_pc_town_gen = 0;
unsigned pc_save_town_gen(void) { return s_pc_town_gen; }

static int pc_save_write_gci(void) {
    int ok = pc_save_write_gci_to(pc_gci_path(), pc_gci_tmp_path());
    if (ok && pc_save_ready) {
        s_pc_town_gen++; /* M-B: the host's TOWN_INFO.town_gen = number of durable GCI writes since start */
    }
    /* D3-4: the Card-A GCI is the only place a host-merged resident record becomes durable. Right after it was really written
     * (pc_save_write_gci_to() also returns TRUE without writing when the save is not ready), the host persists the resident
     * record lineage sidecar save/mp/records.dat (pc_net_game.c -> pc_mp_records.c; no-op unless this process is the HOST).
     * Same (main) thread, synchronously, so the sidecar describes exactly the records the GCI just serialized; the GCI layout,
     * checksum, backup rotation and atomic rename above are untouched. The sidecar never lives in or next to a card directory. */
    if (ok && pc_save_ready) {
        pc_net_game_record_after_gci_save(pc_gci_path());
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

    if (g_pc_save_sanitized) {
        /* M-G: this process loaded a SANITIZED town transfer image (a client's cache): it must never be written back as a town (it holds blank records of the
         * other residents). Reported as success so no save UI reacts; nothing is written. */
        OSReport("[PC] GCI save: SKIPPED (the loaded town is a sanitized transfer image; it is never saved)\n");
        return TRUE;
    }

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
 * existing zero-logic selector for the canonical Card-A path (pc_gci_path()/pc_gci_tmp_path()); using
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

static int pc_save_write_authoritative_impl(void);

int pc_save_write_authoritative(void) {
    int ok = pc_save_write_authoritative_impl();
    PC_LOG(PCL_SAVE, "authoritative save %s\n", ok ? "OK" : "FAILED/REJECTED");
    if (g_pc_dedicated) pc_dedicated_notify_save_result(ok); /* --dedicated: every save result (periodic / early / shutdown / console) */
    return ok;
}

static int pc_save_write_authoritative_impl(void) {
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

/* Guest -> resident lifecycle hardening (B3a): pc_save_write_gci_to() returns TRUE WITHOUT WRITING when the save is not ready or is a sanitized transfer image, which a promotion
 * must not mistake for a durable save (it then removes the guest entry for a resident that is not on disk). pc_save_can_be_durable() = a real write can happen in this process;
 * pc_save_write_authoritative_durable() = FALSE unless the GCI was really written (the test fault --promote-fault fail_save / no_durable act here, test builds only). */
#ifdef PC_NET_TEST_HOOKS
extern int g_pc_promote_fault;
#endif
int pc_save_can_be_durable(void) {
    if (pc_net_game_role() == PC_NETGAME_ROLE_CLIENT || !pc_save_ready || g_pc_save_sanitized) {
        return FALSE;
    }
#ifdef PC_NET_TEST_HOOKS
    if (g_pc_promote_fault == 4) {
        printf("[PC][TEST-ONLY] --promote-fault no_durable: the town save is reported as NOT durable\n");
        return FALSE;
    }
#endif
    return TRUE;
}

int pc_save_write_authoritative_durable(void) {
    if (!pc_save_can_be_durable()) {
        OSReport("[PC] pc_save_write_authoritative_durable: REFUSED -- no real write can happen here (client / save not ready / sanitized image)\n");
        return FALSE;
    }
#ifdef PC_NET_TEST_HOOKS
    if (g_pc_promote_fault == 1) { /* one shot: the authoritative save FAILS (the promotion rolls back; later saves work) */
        g_pc_promote_fault = 0;
        printf("[PC][TEST-ONLY] --promote-fault fail_save: the authoritative save FAILS now (one shot)\n");
        return FALSE;
    }
#endif
    return pc_save_write_authoritative();
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

    /* M-G: a sanitized transfer image (marker in the Others comment area) is only valid in a network CLIENT: a host / single-player process would play (and save)
     * a town whose other residents are blank. */
    if (memcmp(file_data + GCI_OTHERS_OFFSET + (PC_TS_MARKER_OFF - PC_TS_OTHERS_OFF), PC_TS_MARKER_TEXT, PC_TS_MARKER_LEN) == 0) {
        if (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT) {
            OSReport("[PC] GCI: '%s' is a SANITIZED town transfer image: refused (only a network client may load it)\n", path);
            free(file_data);
            pc_main_refuse_sanitized_town(); /* does not return */
            return FALSE;
        }
        g_pc_save_sanitized = 1;
        OSReport("[PC] GCI: '%s' is a SANITIZED town transfer image (client cache: never saved, never uploaded as a record)\n", path);
    } else {
        g_pc_save_sanitized = 0;
    }

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
        stat(pc_gci_path(), &st_new) != 0) {
        int b;
        OSReport("[PC] Migrating save from '%s' to '%s'\n", PC_GCI_PATH_LEGACY, pc_gci_path());
        pc_ensure_save_dirs();

        /* Move main save */
        remove(pc_gci_path()); /* in case it somehow exists */
        rename(PC_GCI_PATH_LEGACY, pc_gci_path());

        /* Move backups */
        for (b = 1; b <= PC_SAVE_MAX_BACKUPS; b++) {
            char old_bak[300], new_bak[300];
            snprintf(old_bak, sizeof(old_bak), "%s.bak%d", PC_GCI_PATH_LEGACY, b);
            snprintf(new_bak, sizeof(new_bak), "%s.bak%d", pc_gci_path(), b);
            remove(new_bak);
            rename(old_bak, new_bak);
        }

        /* Move temp file if orphaned */
        {
            char old_tmp[300];
            snprintf(old_tmp, sizeof(old_tmp), "%s.tmp", PC_GCI_PATH_LEGACY);
            remove(pc_gci_tmp_path());
            rename(old_tmp, pc_gci_tmp_path());
        }

        OSReport("[PC] Migration complete\n");
    }
}

static int pc_save_scan_gci_dir(void) {
    /* Try common AC save filenames in card_a/ */
    char gci_name_a[320], gci_name_b[320];
    const char* gci_names[3];
    int i;
    struct stat st;

    snprintf(gci_name_a, sizeof(gci_name_a), "%s/DobutsunomoriP_MURA.gci", pc_card_a_dir());
    snprintf(gci_name_b, sizeof(gci_name_b), "%s/8P-GAFE-DobutsunomoriP_MURA.gci", pc_card_a_dir());
    gci_names[0] = gci_name_a;
    gci_names[1] = gci_name_b;
    gci_names[2] = NULL;

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
    if (stat(pc_gci_path(), &st) == 0) {
        return pc_save_read_gci(pc_gci_path());
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
    if (!pc_card_town_dir_active()) { /* M-A: a town dir must never pull save/DobutsunomoriP_MURA.gci into itself (the legacy file is left alone) */
        pc_save_migrate_legacy();
    }

    if (stat(pc_gci_path(), &st) == 0) {
        OSReport("[PC] Found GCI save: %s (%ld bytes)\n", pc_gci_path(), (long)st.st_size);
        if (pc_save_read_gci(pc_gci_path())) {
            OSReport("[PC] GCI save loaded successfully\n");
            return TRUE;
        }
        OSReport("[PC] GCI save load FAILED\n");
    } else {
        OSReport("[PC] No GCI save at %s\n", pc_gci_path());
    }

    OSReport("[PC] Scanning for other GCI files...\n");
    if (pc_save_scan_gci_dir()) {
        OSReport("[PC] GCI save loaded via scan\n");
        return TRUE;
    }

    /* recovery: try temp file, then backups */
    if (stat(pc_gci_tmp_path(), &st) == 0) {
        OSReport("[PC] Found orphaned temp save '%s', recovering...\n", pc_gci_tmp_path());
        if (rename(pc_gci_tmp_path(), pc_gci_path()) == 0 && pc_save_read_gci(pc_gci_path())) {
            OSReport("[PC] Recovered save from temp file\n");
            return TRUE;
        }
    }
    {
        char bak_path[300];
        int b;
        for (b = 1; b <= PC_SAVE_MAX_BACKUPS; b++) {
            snprintf(bak_path, sizeof(bak_path), "%s.bak%d", pc_gci_path(), b);
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
    PC_LOG(PCL_PLAYERS, "bootstrap resident %d bound\n", player_no);
}

/* M-C (Play Online): bind the local resident by PersonalID instead of by index. A character with a RESIDENT membership of the fetched town knows the resident's PID
 * (membership.ini town_pid), not its slot. This thin entry waits for exactly the same moment pc_bootstrap_resident_poll() waits for (a live play_main GAME_PLAY, the
 * entrance wipe settled), resolves the index with the vanilla comparator over private_data[] (an existing resident only), stores it in g_pc_bootstrap_resident and lets
 * the UNCHANGED pc_bootstrap_resident_poll() (called right after it from pc_vi.c) drive the bind. One shot per process; the CLI index flow (--bootstrap-resident N) never
 * arms it. A PID that matches no resident is logged once and nothing is bound. */
static void pc_title_notice(const char* head, const char* msg, int secs); /* below (guest title message) */

void pc_bootstrap_resident_pid_poll(void) {
    extern int g_pc_bootstrap_resident;                   /* pc_main.c */
    extern int g_pc_bootstrap_resident_pid_set;           /* pc_main.c: 1 = armed by Play Online (membership role = resident) */
    extern unsigned char g_pc_bootstrap_resident_pid[20]; /* pc_main.c: the 20 BE bytes of the resident PersonalID (name 8, land 8, player_id BE16, land_id BE16) */
    static int l_done = 0;
    PersonalID_c want;
    Private_c* priv;
    int i, found = -1, matches = 0;

    if (l_done || !g_pc_bootstrap_resident_pid_set) {
        return;
    }
    if (gamePT == NULL || gamePT->exec != play_main || ((GAME_PLAY*)gamePT)->fb_wipe_mode != WIPE_MODE_NONE) {
        return; /* the same wait as pc_bootstrap_resident_poll() */
    }
    l_done = 1;
    if (mFRm_CheckSaveData() == FALSE) {
        OSReport("[PC] --resident-by-pid: no valid town save is loaded\n");
        return;
    }
    memcpy(want.player_name, g_pc_bootstrap_resident_pid, 8);
    memcpy(want.land_name, g_pc_bootstrap_resident_pid + 8, 8);
    want.player_id = (u16)(((u16)g_pc_bootstrap_resident_pid[16] << 8) | g_pc_bootstrap_resident_pid[17]);
    want.land_id = (u16)(((u16)g_pc_bootstrap_resident_pid[18] << 8) | g_pc_bootstrap_resident_pid[19]);
    priv = Save_GetPointer(private_data[0]);
    for (i = 0; i < PLAYER_NUM; i++) {
        if (mPr_CheckPrivate(&priv[i]) == TRUE && mPr_NullCheckPersonalID(&priv[i].player_ID) == FALSE && mPr_CheckCmpPersonalID(&want, &priv[i].player_ID) == TRUE) {
            matches++;
            if (found < 0) {
                found = i;
            }
        }
    }
    if (matches != 1) {
        OSReport("[PC] --resident-by-pid: %s (%d match(es) in this town): nothing is bound\n", matches == 0 ? "the resident of this membership is not in the town save" : "ambiguous resident", matches);
        /* M-J: do not sit silent on the title screen */
        pc_title_notice("Could not play this character:", matches == 0 ? "this character is not a resident of this town save (removed or town reset). Ask the operator." : "this character matches more than one resident of this town. Ask the operator.", 600);
        return;
    }
    OSReport("[PC] --resident-by-pid: resident PersonalID matches slot %d: binding through the --bootstrap-resident path\n", found);
    g_pc_bootstrap_resident = found;
}

/* Guests G2: parses "NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]]" (NAME / LAND 1..8 chars, space padded like every vanilla name; ids decimal or 0x hex, 1..0xFFFE).
 * Guests G1: the OPTIONAL trailing GENDER (0 = male, 1 = female: mPr_SEX_MALE / mPr_SEX_FEMALE) and FACE (0..7: mPr_FACE_TYPE0..7) are returned through
 * *gender_out / *face_out, -1 = not given (an empty field also means "not given": the value is then derived from the guest identity).
 * Guests G1.1: on failure returns 0 and, when `why` is not NULL, points *why at a static string naming WHICH part of the spec is wrong. */
static int pc_guest_parse_spec(const char* spec, PersonalID_c* out, int* gender_out, int* face_out, const char** why) {
    char buf[96];
    char* tok[6];
    char* p;
    int n = 0;
    unsigned long pid, lid;
    char* end;
    size_t len;
    size_t k;
    const char* dummy_why;

    if (why == NULL) {
        why = &dummy_why;
    }
    *gender_out = -1;
    *face_out = -1;
    if (spec == NULL) {
        *why = "no spec given";
        return 0;
    }
    if (strlen(spec) >= sizeof(buf)) {
        *why = "the spec is too long (limit 95 characters)";
        return 0;
    }
    strcpy(buf, spec);
    p = buf;
    tok[n++] = p;
    while (*p != '\0') {
        if (*p == ',') {
            *p = '\0';
            if (n >= 6) {
                *why = "too many fields (at most 6: NAME,LAND,PLAYER_ID,LAND_ID,GENDER,FACE; extra fields are not allowed)";
                return 0;
            }
            tok[n++] = p + 1;
        }
        p++;
    }
    if (n < 4) {
        *why = "wrong field count (need 4..6 comma-separated fields: NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]])";
        return 0;
    }
    len = strlen(tok[0]);
    if (len < 1) {
        *why = "NAME is empty (1..8 characters required)";
        return 0;
    }
    if (len > PLAYER_NAME_LEN) {
        *why = "NAME is too long (1..8 characters allowed)";
        return 0;
    }
    memset(out->player_name, ' ', PLAYER_NAME_LEN);
    for (k = 0; k < len; k++) {
        out->player_name[k] = (u8)tok[0][k];
    }
    len = strlen(tok[1]);
    if (len < 1) {
        *why = "LAND (the home town name) is empty (1..8 characters required)";
        return 0;
    }
    if (len > LAND_NAME_SIZE) {
        *why = "LAND (the home town name) is too long (1..8 characters allowed)";
        return 0;
    }
    memset(out->land_name, ' ', LAND_NAME_SIZE);
    for (k = 0; k < len; k++) {
        out->land_name[k] = (u8)tok[1][k];
    }
    pid = strtoul(tok[2], &end, 0);
    if (*tok[2] == '\0' || *end != '\0' || pid == 0 || pid >= 0xFFFFul) {
        *why = "bad PLAYER_ID (a decimal or 0x-hex number in 1..0xFFFE)";
        return 0;
    }
    lid = strtoul(tok[3], &end, 0);
    if (*tok[3] == '\0' || *end != '\0' || lid == 0 || lid >= 0xFFFFul) {
        *why = "bad LAND_ID (a decimal or 0x-hex number in 1..0xFFFE)";
        return 0;
    }
    out->player_id = (u16)pid;
    out->land_id = (u16)lid;
    if (n >= 5 && *tok[4] != '\0') {
        unsigned long gv = strtoul(tok[4], &end, 10);
        if (*end != '\0' || gv > (unsigned long)mPr_SEX_FEMALE) {
            *why = "bad GENDER (must be 0 = male or 1 = female, or empty)";
            return 0;
        }
        *gender_out = (int)gv;
    }
    if (n >= 6 && *tok[5] != '\0') {
        unsigned long fv = strtoul(tok[5], &end, 10);
        if (*end != '\0' || fv >= (unsigned long)mPr_FACE_TYPE_NUM) {
            *why = "bad FACE (must be 0..7, or empty)";
            return 0;
        }
        *face_out = (int)fv;
    }
    return 1;
}

/* Guests G1.1: every spec-only rule a guest must satisfy, in one place (used by the early validator AND, as defence in depth, by pc_bootstrap_guest_poll).
 * Beyond the syntax (pc_guest_parse_spec): a real game name (pc_mp_guests_name_valid, the host's own rule), not the RESERVED observer name
 * (pc_mp_guests_name_reserved), and an identity the host would not reject as invalid (the same tests as pcnetgame_guest_key_valid: first byte of name and
 * land non-NUL, player_id / land_id != 0xFFFF). Returns 1 = fine, else 0 with *why naming the problem. The resident-name rule needs the loaded save and
 * stays in pc_bootstrap_guest_poll (and on the host). */
static int pc_guest_spec_check(const char* spec, PersonalID_c* home, int* gender_out, int* face_out, const char** why) {
    if (!pc_guest_parse_spec(spec, home, gender_out, face_out, why)) {
        return 0;
    }
    if (home->player_name[0] == 0 || home->land_name[0] == 0 || home->player_id == 0xFFFFu || home->land_id == 0xFFFFu) {
        *why = "the identity is not a valid PersonalID (empty name / land or an id of 0xFFFF)";
        return 0;
    }
    if (!pc_mp_guests_name_valid(home->player_name)) {
        *why = "NAME is not a valid game player name (blank, or a character the name entry cannot produce)";
        return 0;
    }
    if (pc_mp_guests_name_reserved(home->player_name)) {
        *why = "NAME 'SERVER' is reserved for the server observer (a guest cannot take it)";
        return 0;
    }
    return 1;
}

/* Guests G1.1: the EARLY --bootstrap-guest validator, called from pc_main.c right after option parsing (before any window / network / save work).
 * Returns 1 = the spec is acceptable, 0 = a diagnostic naming the wrong part was printed to stderr and the caller must exit with status 2. */
int pc_bootstrap_guest_validate(const char* spec) {
    PersonalID_c home;
    int g, f;
    const char* why = "unknown";
    if (pc_guest_spec_check(spec, &home, &g, &f, &why)) {
        return 1;
    }
    fprintf(stderr, "[PC] --bootstrap-guest: REFUSED: bad spec '%s': %s (expected NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]])\n", spec != NULL ? spec : "(null)", why);
    fflush(stderr);
    return 0;
}

/* Guests G1: the FRESH guest character. A new guest is its own independent character, never a copy of a resident.
 *  - identity hash: FNV-1a32 over the canonical 20-byte PersonalID image (name[8], land[8], player_id BE, land_id BE: independent of the host byte order, so the
 *    python test double mirrors it). Everything the guest does not choose is derived from it DETERMINISTICALLY (never from the client RNG, never from a resident):
 *    gender = bit 31, face = bits 16..18, starter shirt index = bits 8..10 (within the gender's 8-shirt table).
 *  - pc_guest_build_fresh_record(): mPr_ClearPrivateInfo (the vanilla "empty" markers of every field: quests, letters, birthday, maps, museum, remail, animal
 *    memory, starter shirt CLOTH001, inventory background CLOTH226, state_flags = 1; pockets / wallet / bank / catalog / calendar / lotto are zero = EMPTY_NO / 0)
 *    then mPr_InitPrivateInfo (exists = TRUE, loan = 100 (the vanilla pre-house value), my_org_no_table 0..7; it also stamps THIS town's land + a random 8-bit id
 *    + a random shirt + a random face, and draws the client RNG: ALL of those are overwritten below, nothing random survives), then the guest's HOME PersonalID,
 *    exists = TRUE, reset_code = 0, gender / face / starter shirt (the same explicit setter the vanilla code ends in: mPlib_change_player_cloth_info_lv2) and the
 *    default Able Sisters designs. NO starter bag (vanilla's 1000-bell pocket is written by mSDI_StartInitNew*, which a guest never runs): a guest starts with
 *    EMPTY pockets and an empty wallet. It reads Save_t only (mPr_InitPrivateInfo's face / id uniqueness scan) and writes ONLY `rec`. */
static u32 pc_guest_identity_hash(const PersonalID_c* id) {
    u32 h = 2166136261u;
    int k;
    for (k = 0; k < PLAYER_NAME_LEN; k++) {
        h = (h ^ (u32)id->player_name[k]) * 16777619u;
    }
    for (k = 0; k < LAND_NAME_SIZE; k++) {
        h = (h ^ (u32)id->land_name[k]) * 16777619u;
    }
    h = (h ^ (u32)((id->player_id >> 8) & 0xFFu)) * 16777619u;
    h = (h ^ (u32)(id->player_id & 0xFFu)) * 16777619u;
    h = (h ^ (u32)((id->land_id >> 8) & 0xFFu)) * 16777619u;
    h = (h ^ (u32)(id->land_id & 0xFFu)) * 16777619u;
    return h;
}

/* The default designs a vanilla new player gets (mNW_InitOneMyOriginal, which only knows Save_t's private_data[player_no] slots): the palette table, the
 * ROM names and the ARAM default textures of the first mNW_DEFAULT_ORIGINAL_TEX_NUM designs, 'blank' for the rest -- written into `rec` instead. */
static void pc_guest_init_designs(Private_c* rec) {
    static const u8 pal_table[mPr_ORIGINAL_DESIGN_COUNT] = { 0, 8, 7, 7, 0, 0, 0, 0 };
    int i;
    for (i = 0; i < mPr_ORIGINAL_DESIGN_COUNT; i++) {
        mNW_original_design_c* d = &rec->my_org[i];
        if (i < mNW_DEFAULT_ORIGINAL_TEX_NUM) {
            d->palette = pal_table[i];
            mString_Load_StringFromRom(d->name, mNW_ORIGINAL_DESIGN_NAME_LEN, 0x6DF + i);
            _JW_GetResourceAram(JW_GetAramAddress(27) + i * mNW_DESIGN_TEX_SIZE, d->design.data, mNW_DESIGN_TEX_SIZE);
        } else {
            mNW_InitOriginalData(d);
        }
    }
}

_Static_assert(mPr_SEX_MALE == 0 && mPr_SEX_FEMALE == 1 && mPr_FACE_TYPE_NUM == 8, "guest gender / face derivation assumes 2 genders and 8 faces");
_Static_assert(ITM_CLOTH008 == ITM_CLOTH000 + 8 && ITM_CLOTH015 == ITM_CLOTH000 + 15, "starter shirts: boys ITM_CLOTH000..007, girls ITM_CLOTH008..015");

/* The starter shirt a guest wears: derived from the identity hash (bits 8..10 pick one of the gender's 8 shirts), never random. Shared by the fresh record and
 * the first-run creation finish (the vanilla Rover scene hands out a RANDOM shirt there; the guest keeps this deterministic one). */
u16 pc_guest_starter_shirt(const PersonalID_c* id, int gender) {
    const u32 h = pc_guest_identity_hash(id);
    const int shirt_idx = (int)((h >> 8) & 7u);

    return (u16)(ITM_CLOTH000 + (gender == mPr_SEX_FEMALE ? 8 : 0) + shirt_idx);
}

static void pc_guest_build_fresh_record(Private_c* rec, const PersonalID_c* home, int gender, int face) {
    const u32 h = pc_guest_identity_hash(home);

    if (gender < 0) {
        gender = (int)((h >> 31) & 1u);
    }
    if (face < 0) {
        face = (int)((h >> 16) & 7u);
    }
    mPr_ClearPrivateInfo(rec);
    mPr_InitPrivateInfo(rec);
    mPr_CopyPersonalID(&rec->player_ID, (PersonalID_c*)home);
    rec->exists = TRUE;
    rec->reset_code = 0;
    rec->gender = (s8)gender;
    rec->face = (s8)face;
    mPlib_change_player_cloth_info_lv2(rec, (mActor_name_t)pc_guest_starter_shirt(home, gender));
    pc_guest_init_designs(rec);
}

/* Guests G1: the vanilla guide2 rule "a new player's name must not equal a resident's name" (aNG2_check_pname), as a BOUNDED loop over private_data[0..3]
 * (not aNG2_getP_other_pl_name, whose second loop runs out of bounds when no resident exists). Every slot holding a PersonalID counts, even an away resident.
 * Returns the resident index whose name equals `home`'s, or -1. */
static int pc_guest_resident_name_conflict(const PersonalID_c* home) {
    int i;
    for (i = 0; i < PLAYER_NUM; i++) {
        PersonalID_c* p = &Save_Get(private_data)[i].player_ID;
        if (mPr_NullCheckPersonalID(p) == FALSE && memcmp(p->player_name, home->player_name, PLAYER_NAME_LEN) == 0) {
            return i;
        }
    }
    return -1;
}

/* M-F (guest -> resident promotion, HOST only, game thread): turns a guest's stored record into a NEW resident in the FREE resident slot `slot` with the FREE house
 * `house` of THIS host save. The caller (pc_net_game.c pc_net_game_dedicated_promote) has already checked every precondition and owns the members.dat / records.dat /
 * guests.dat bookkeeping; this routine only builds the game state, in the order and with the vanilla routines the new-player flow uses:
 *   mPr_ClearPrivateInfo + mPr_InitPrivateInfo(&private_data[slot])   vanilla new-player defaults: THIS town's land + a unique 0xF000|id player id, exists = TRUE,
 *                                                                    random shirt (overwritten below), a face unused by the other residents, my_org_no_table
 *   copied from the guest record (BE -> native already done by the guest table): name, gender, the shirt it wears, the pockets / item conditions / wallet, the bank
 *   account, the Able Sisters designs (so no ARAM resource read is needed on a host); the face when no other resident wears it (else the vanilla unique face stays)
 *   inventory.loan = mPlayer_DEBT0       what vanilla's house selection (ac_intro_demo_move.c_inc aID_retire_rcn_guide_wait) writes right after mHS_set_use
 *   mHS_set_use(slot, house)             swaps house_arrangement and sets homes[house].ownerID through mHm_InitHomeInfo (the house must have a null owner: vanilla
 *                                        deletion mHm_ClearHomeInfo's it, so a null-owner house is a clean default house)
 *   mEv_ClearPersonalEventFlag(slot)     the veteran state of the slot: NO first-job / first-intro (the Nook intro is never started for a promoted resident)
 *   birthday (M-I)                       copied from the guest record when it is a plausible date
 *   catalog bits (M-I)                   the four mPr_SetItemCollectBit calls of the intro demo (worn shirt, FTR_SUM_CASSE01, the house's carpet and wallpaper)
 *   villager memories (M-I)              memory_player_id == the guest's home PID -> the new PID (+ host land / tune for town villagers; the islander's union is left alone)
 * NOT done (documented limits): the first-job quest and the Nook intro (host-owned Save state with no client -> host path), mCkRh roach data (the house is already a
 * default house). Everything keyed to the guest PID or carried by the guest record is migrated by pc_mp_promote_migrate (see below); fish records / host-held mail never existed for a guest. One snapshot (resident, house, event flags, animals[], island animal) is kept so the caller can roll the whole change back when the save cannot be written. Returns 1 or 0 with err. */
static struct {
    int      valid;
    int      slot;
    int      house;
    Private_c priv;
    mHm_hs_c home;
    u32      arrangement;
    u32      ev_save_flags;
    u32      ev_common_flags;
    Animal_c animals[ANIMAL_NUM_MAX]; /* M-I: villager memories re-keyed to the new PID (rolled back when the save fails) */
    Animal_c island_animal;
    mFR_record_c fish[mFR_RECORD_NUM]; /* lifecycle hardening: fish records re-keyed defensively (rolled back with the rest) */
} s_pc_promote_snap;

void pc_mp_promote_rollback(void);

/* Guest-first paid house purchase: the NEXT pc_mp_promote_create starts the new resident with loan 0 (the guest paid the whole house price up front; the caller already
 * debited the guest record's wallet, which is copied as-is). One-shot: consumed (and cleared) at the entry of pc_mp_promote_create, so a refused promote never leaks it. */
static int s_pc_promote_paid = 0;
void pc_mp_promote_set_paid(int paid) {
    s_pc_promote_paid = paid != 0;
}

/* ===== Guest -> resident lifecycle hardening: PID re-key + record migration (HOST, game thread, inside pc_mp_promote_create) ===== */
extern int pc_net_game_pocket_legal_item(unsigned item); /* pc_net_game.c: the D3 pocket-legal predicate (the one the record upload validators use) */

_Static_assert(offsetof(Save_t, fishRecord) == 0x23E68 && offsetof(Save_t, animals) == 0x17438 && sizeof(mFR_record_c) == 0x20, "the lifecycle tests seed the GCI at these Save_t offsets");

/* Copies the WHOLE PersonalID (name, land_name, player_id, land_id) of `npid` over `*p` when `*p` equals `gkey`. Returns 1 when it re-keyed. */
int pc_pid_rekey(PersonalID_c* p, PersonalID_c* gkey, PersonalID_c* npid) {
    if (p == NULL || gkey == NULL || npid == NULL || mPr_NullCheckPersonalID(p) != FALSE || mPr_CheckCmpPersonalID(p, gkey) != TRUE) {
        return 0;
    }
    mPr_CopyPersonalID(p, npid);
    return 1;
}

static void pc_promote_or_bits(u32* dst, const u32* src, int n) {
    int i;
    for (i = 0; i < n; i++) {
        dst[i] |= src[i];
    }
}

/* Returns the number of migrated items (all classes); the per-class counts are logged on one line. NOT carried (documented): state_flags, museum_record (host-owned),
 * reset_code, ecard_letter_data, calendar, soncho trophies, first-job quests. Fish records / host-held mail do NOT need carrying (fish records are written only by client
 * actors, mail is never held on the host for a guest); the defensive fish re-key is a no-op in practice. */
int pc_mp_promote_migrate(PersonalID_c* gkey, const Private_c* g, Private_c* priv) {
    int mem = 0, contest = 0, fish = 0, letters = 0, quests = 0, a, m, i, k;
    int equip = 0;
    PersonalID_c* npid = &priv->player_ID;
    if (gkey == NULL || g == NULL || priv == NULL) {
        return 0;
    }
    /* (a) memories + contest quests of the 15 villagers and the islander (the islander's memuni is a different union member: only its id is re-keyed) */
    if (mPr_NullCheckPersonalID(gkey) == FALSE) {
        for (a = 0; a < ANIMAL_NUM_MAX + 1; a++) {
            Animal_c* an = a < ANIMAL_NUM_MAX ? Save_GetPointer(animals[a]) : Save_GetPointer(island.animal);
            const int is_island = a >= ANIMAL_NUM_MAX;
            for (m = 0; m < ANIMAL_MEMORY_NUM; m++) {
                Anmmem_c* mem_p = &an->memories[m];
                if (pc_pid_rekey(&mem_p->memory_player_id, gkey, npid)) {
                    if (!is_island) {
                        mLd_CopyLandName(mem_p->memuni.land.name, Save_Get(land_info).name);
                        mem_p->memuni.land.id = Save_Get(land_info).id;
                        mem_p->saved_town_tune = Save_Get(melody);
                    }
                    mem++;
                }
            }
            contest += pc_pid_rekey(&an->contest_quest.player_id, gkey, npid);
        }
        for (i = 0; i < mFR_RECORD_NUM; i++) {
            fish += pc_pid_rekey(&Save_Get(fishRecord)[i].pid, gkey, npid);
        }
    }
    /* (b) the guest record's own state */
    for (i = 0, k = 0; i < mPr_INVENTORY_MAIL_COUNT; i++) {
        Mail_c* src = (Mail_c*)&g->mail[i];
        if (mMl_check_not_used_mail(src) == TRUE) {
            continue;
        }
        priv->mail[k] = *src;
        (void)pc_pid_rekey(&priv->mail[k].header.recipient.personalID, gkey, npid);
        (void)pc_pid_rekey(&priv->mail[k].header.sender.personalID, gkey, npid);
        if (priv->mail[k].present != (mActor_name_t)EMPTY_NO && priv->mail[k].present != (mActor_name_t)RSV_NO && !pc_net_game_pocket_legal_item(priv->mail[k].present)) {
            priv->mail[k].present = (mActor_name_t)EMPTY_NO;
        }
        k++;
        letters++;
    }
    if (letters > 0) {
        priv->saved_mail_header = g->saved_mail_header;
    }
    if (g->equipment != (mActor_name_t)EMPTY_NO && pc_net_game_pocket_legal_item(g->equipment)) {
        priv->equipment = g->equipment;
        equip = 1;
    }
    pc_promote_or_bits(priv->aircheck_collect_bitfield, g->aircheck_collect_bitfield, 2);
    pc_promote_or_bits(priv->furniture_collected_bitfield, g->furniture_collected_bitfield, 43);
    pc_promote_or_bits(priv->wall_collected_bitfield, g->wall_collected_bitfield, 3);
    pc_promote_or_bits(priv->carpet_collected_bitfield, g->carpet_collected_bitfield, 3);
    pc_promote_or_bits(priv->paper_collected_bitfield, g->paper_collected_bitfield, 2);
    pc_promote_or_bits(priv->music_collected_bitfield, g->music_collected_bitfield, 2);
    memcpy(priv->maps, g->maps, sizeof(priv->maps));
    if (g->backgound_texture == (mActor_name_t)EMPTY_NO || ITEM_IS_CLOTH(g->backgound_texture)) {
        priv->backgound_texture = g->backgound_texture;
    }
    priv->hint_count = g->hint_count;
    if ((int)g->destiny.type < mPr_DESTINY_NUM) {
        priv->destiny = g->destiny;
    }
    if (g->sunburn.rank >= mPr_SUNBURN_RANK_MIN && g->sunburn.rank <= mPr_SUNBURN_RANK_MAX && g->sunburn.rankdown_days >= 0) {
        priv->sunburn = g->sunburn;
    }
    priv->remail = g->remail;
    priv->animal_memory = g->animal_memory;
    for (i = 0; i < mPr_DELIVERY_QUEST_NUM; i++) {
        if (g->deliveries[i].base.quest_type == mQst_QUEST_TYPE_DELIVERY) {
            priv->deliveries[i] = g->deliveries[i];
            quests++;
        }
    }
    for (i = 0; i < mPr_ERRAND_QUEST_NUM; i++) {
        const mQst_errand_c* e = &g->errands[i];
        if (e->base.quest_type == mQst_QUEST_TYPE_ERRAND && e->errand_type != mQst_ERRAND_TYPE_FIRST_JOB && (int)e->base.quest_kind < mQst_ERRAND_FIRSTJOB_CHANGE_CLOTH) {
            priv->errands[i] = *e;
            quests++;
        }
    }
    printf("[PC] migrate: memories=%d contest_quests=%d fish_records=%d letters=%d equipment=%d quests=%d (+ collection bits OR-ed, maps, hint_count, destiny, sunburn, remail, animal_memory, backgound_texture)\n",
           mem, contest, fish, letters, equip, quests);
    return mem + contest + fish + letters + equip + quests;
}

int pc_mp_promote_create(const void* guest_rec, const void* guest_key, int slot, int house, char* err, size_t cap) {
    const Private_c* g = (const Private_c*)guest_rec;
    const int paid = s_pc_promote_paid;
    PersonalID_c* gkey = (PersonalID_c*)guest_key; /* the guest's home PersonalID = bound_pid = memory_player_id of its villager memories */
    int remapped = 0;
    Private_c* priv;
    mHm_hs_c* home;
    int i, face_ok = 1;

    s_pc_promote_paid = 0; /* consumed above into `paid` */
    if (err != NULL && cap > 0) {
        err[0] = '\0';
    }
#define PC_PROMOTE_FAIL(...) do { if (err != NULL && cap > 0) snprintf(err, cap, __VA_ARGS__); return 0; } while (0)
    if (g == NULL || slot < 0 || slot >= PC_RESIDENCE_SLOTS || house < 0 || house >= PC_RESIDENCE_HOUSES) {
        PC_PROMOTE_FAIL("bad slot / house / guest record");
    }
    if (!pc_save_loaded || pc_net_game_role() != PC_NETGAME_ROLE_HOST) {
        PC_PROMOTE_FAIL("this process is not a host with a loaded save");
    }
    priv = Save_GetPointer(private_data[slot]);
    home = Save_GetPointer(homes[house]);
    if (mPr_CheckPrivate(priv) == TRUE) {
        PC_PROMOTE_FAIL("resident slot %d is not free", slot);
    }
    if (mPr_NullCheckPersonalID(&home->ownerID) != TRUE) {
        PC_PROMOTE_FAIL("house %d already has an owner", house);
    }
    { /* the arrangement must be a permutation of 0..3 (else mHS_set_use's swap would corrupt it), the slot the house maps to must be this slot or free, every live resident owns its house */
        char cerr[200];
        if (!pc_residence_check(slot, house, cerr, sizeof(cerr))) {
            PC_PROMOTE_FAIL("%s (nothing was changed)", cerr);
        }
    }
    if (g->gender != mPr_SEX_MALE && g->gender != mPr_SEX_FEMALE) {
        PC_PROMOTE_FAIL("the guest record has an invalid gender (%d)", (int)g->gender);
    }
    if (g->face < 0 || g->face >= mPr_FACE_TYPE_NUM) {
        PC_PROMOTE_FAIL("the guest record has an invalid face (%d)", (int)g->face);
    }

    memset(&s_pc_promote_snap, 0, sizeof(s_pc_promote_snap));
    s_pc_promote_snap.valid = 1;
    s_pc_promote_snap.slot = slot;
    s_pc_promote_snap.house = house;
    s_pc_promote_snap.priv = *priv;
    s_pc_promote_snap.home = *home;
    s_pc_promote_snap.arrangement = (u32)Save_Get(house_arrangement);
    s_pc_promote_snap.ev_save_flags = (u32)Save_Get(event_save_data).flags;
    s_pc_promote_snap.ev_common_flags = (u32)Common_Get(event_flags[mEv_SAVED_EVENT]);
    memcpy(s_pc_promote_snap.animals, Save_Get(animals), sizeof(s_pc_promote_snap.animals));
    s_pc_promote_snap.island_animal = Save_Get(island).animal;
    memcpy(s_pc_promote_snap.fish, Save_Get(fishRecord), sizeof(s_pc_promote_snap.fish));

    mPr_ClearPrivateInfo(priv);
    mPr_InitPrivateInfo(priv);
    memcpy(priv->player_ID.player_name, g->player_ID.player_name, PLAYER_NAME_LEN);
    priv->exists = TRUE;
    priv->reset_code = 0;
    priv->gender = g->gender;
    for (i = 0; i < PLAYER_NUM; i++) {
        if (i != slot && mPr_NullCheckPersonalID(&Save_Get(private_data)[i].player_ID) == FALSE && Save_Get(private_data)[i].face == g->face) {
            face_ok = 0;
        }
    }
    if (face_ok) {
        priv->face = g->face;
    }
    if (ITEM_IS_CLOTH(g->cloth.item)) {
        mPlib_change_player_cloth_info_lv2(priv, (mActor_name_t)g->cloth.item);
    } else {
        mPlib_change_player_cloth_info_lv2(priv, (mActor_name_t)pc_guest_starter_shirt(&priv->player_ID, priv->gender));
    }
    memcpy(priv->inventory.pockets, g->inventory.pockets, sizeof(priv->inventory.pockets));
    priv->inventory.item_conditions = g->inventory.item_conditions;
    priv->inventory.wallet = g->inventory.wallet;
    priv->bank_account = g->bank_account;
    memcpy(priv->my_org, g->my_org, sizeof(priv->my_org));
    memcpy(priv->my_org_no_table, g->my_org_no_table, sizeof(priv->my_org_no_table));
    priv->inventory.loan = mPlayer_DEBT0;
    if (paid) {
        priv->inventory.loan = 0; /* the guest paid the 1,000 down payment + the 17,400 loan up front (wallet debited by the host before this call) */
    }

    if (!pc_residence_assign(slot, house) || mPr_CheckCmpPersonalID(&home->ownerID, &priv->player_ID) != TRUE) {
        pc_mp_promote_rollback();
        PC_PROMOTE_FAIL("mHS_set_use(%d, %d) did not give the house to the new resident (everything was rolled back)", slot, house);
    }
    /* pc_residence_assign() also ran mEv_ClearPersonalEventFlag(slot) (the veteran state of the slot) */

    /* M-I: the birthday (vanilla's intro asks it; a guest record carries it). Only a plausible date is copied, else the vanilla cleared value stays. */
    if (g->birthday.month >= 1 && g->birthday.month <= 12 && g->birthday.day >= 1 && g->birthday.day <= 31) {
        priv->birthday = g->birthday;
    }
    /* M-I: the catalog bits vanilla's intro demo sets right after mHS_set_use (ac_intro_demo_move.c_inc aID_retire_rcn_guide_wait): the worn shirt, the casette
     * furniture, the carpet and the wallpaper of the assigned house. mPr_SetItemCollectBit writes Common now_private, so it is pointed at the new resident for these
     * calls only (game thread, restored right after). The indices come from the house just assigned; out-of-range ones (the bitfields hold 96 carpets / walls) are skipped. */
    {
        Private_c* prev_now = Common_Get(now_private);
        const int fl = (int)home->floors[0].wall_floor.flooring_idx, wp = (int)home->floors[0].wall_floor.wallpaper_idx;
        Common_Set(now_private, priv);
        mPr_SetItemCollectBit(priv->cloth.item);
        mPr_SetItemCollectBit(FTR_START(FTR_SUM_CASSE01));
        if (fl >= 0 && fl < 96) {
            mPr_SetItemCollectBit(ITM_CARPET_START + fl);
        }
        if (wp >= 0 && wp < 96) {
            mPr_SetItemCollectBit(ITM_WALL_START + wp);
        }
        Common_Set(now_private, prev_now);
    }

    /* Lifecycle hardening: everything keyed to the guest PID (villager memories, contest quests, fish records) is re-keyed and the guest record's carried-over state is
     * migrated (pc_mp_promote_migrate; ATOMIC with the promotion: inside this call, before the durable save, covered by pc_mp_promote_rollback). */
    remapped = pc_mp_promote_migrate(gkey, g, priv);
    printf("[PC] M-F promote: resident slot %d created for '%.8s' (gender %d, face %d%s, player id 0x%04X), house %d assigned (arrangement 0x%02X), loan %u, pockets/wallet/bank carried over, %d PID-keyed / carried items migrated (see the [PC] migrate line)\n",
           slot, (const char*)priv->player_ID.player_name, (int)priv->gender, (int)priv->face, face_ok ? "" : " (the guest's face is worn by another resident: vanilla unique face kept)",
           (unsigned)priv->player_ID.player_id, house, (unsigned)Save_Get(house_arrangement), (unsigned)priv->inventory.loan, remapped);
#undef PC_PROMOTE_FAIL
    return 1;
}

/* Restores the snapshot taken by the last successful-or-failed pc_mp_promote_create (idempotent: the snapshot is consumed). */
void pc_mp_promote_rollback(void) {
    if (!s_pc_promote_snap.valid) {
        return;
    }
    *Save_GetPointer(private_data[s_pc_promote_snap.slot]) = s_pc_promote_snap.priv;
    *Save_GetPointer(homes[s_pc_promote_snap.house]) = s_pc_promote_snap.home;
    Save_Set(house_arrangement, s_pc_promote_snap.arrangement);
    Save_Get(event_save_data).flags = s_pc_promote_snap.ev_save_flags;
    Common_Set(event_flags[mEv_SAVED_EVENT], s_pc_promote_snap.ev_common_flags);
    memcpy(Save_Get(animals), s_pc_promote_snap.animals, sizeof(s_pc_promote_snap.animals));
    Save_Get(island).animal = s_pc_promote_snap.island_animal;
    memcpy(Save_Get(fishRecord), s_pc_promote_snap.fish, sizeof(s_pc_promote_snap.fish));
    memset(&s_pc_promote_snap, 0, sizeof(s_pc_promote_snap));
    printf("[PC] M-F promote: the in-memory change was ROLLED BACK\n");
}

/* The change is durable (or deliberately kept): drop the snapshot. */
void pc_mp_promote_commit(void) {
    memset(&s_pc_promote_snap, 0, sizeof(s_pc_promote_snap));
}

/* Guests G3.2 (client-only title menu "Join as Guest"): the failure message of the last attempt (shown by the title menu for a few seconds) and the cached
 * label. Plain statics, no allocation. */
static char s_pc_guest_title_msg[256];
static time_t s_pc_guest_title_msg_until = 0;
static const char* s_pc_guest_title_msg_head = "Could not join as a guest:"; /* M-J: heading of the message (pc_title_notice may set another) */
static char s_pc_guest_title_label[48] = "Join as Guest";
static int s_pc_guest_title_label_done = 0;

/* First-run guest creation (--guest-profile NAME / title item with a NEW named profile): the profile file does not exist, its identity (ids, placeholder name) is
 * held IN MEMORY (armed by pc_main.c / pc_guest_title_join), the guest arrives in the REAL vanilla Rover scene (SCENE_START_DEMO2: name entry, gender, face
 * questions) and pc_guest_creation_finish() writes the file ONCE, create-only, then sends the guest to the station like pc_guest_arrive. `armed` stays set from
 * the arming until the finish succeeded (it is also what pc_net_game.c's handshake gate and the Rover actor ask through pc_guest_creation_active()). */
static int s_pc_guest_create_armed = 0;
static PCGuestProfile s_pc_guest_create_profile;

void pc_guest_creation_arm(const PCGuestProfile* p) {
    s_pc_guest_create_profile = *p;
    s_pc_guest_create_armed = 1;
}

int pc_guest_creation_active(void) {
    return s_pc_guest_create_armed;
}

/* Play Online without relaunch (pc_main.c pc_main_play_online_poll): a cancelled / failed in-process connect drops a first-run creation that pc_main_prepare_store_character armed. */
void pc_guest_creation_disarm(void) {
    s_pc_guest_create_armed = 0;
}

/* The Rover scene's name check (ac_npc_guide2_move.c_inc aNG2_check_pname): 1 iff the typed name can be stored EXACTLY in the profile file. */
int pc_guest_creation_name_ok(const u8* game_name) {
    char nm[PC_GUEST_PROFILE_NAME_LEN + 1];
    return pc_guest_profile_name_from_game(game_name, nm);
}

/* The station arrival door: SCENE_FG at (1979, 760), exactly aNPS2_make_door_data's station entry; the caller adds RIDE_OFF_DEMO + the circle wipe. */
static void pc_guest_station_door(Door_data_c* door_data) {
    door_data->next_scene_id = SCENE_FG;
    door_data->exit_orientation = mSc_DIRECT_SOUTH;
    door_data->exit_type = 0;
    door_data->extra_data = 0;
    door_data->exit_position.x = 1979;
    door_data->exit_position.y = 0;
    door_data->exit_position.z = 760;
    door_data->door_actor_name = EMPTY_NO;
    door_data->wipe_type = WIPE_TYPE_FADE_BLACK;
}

/* Guests G3: the ONE guest arrival, shared by --bootstrap-guest / --guest (pc_bootstrap_guest_poll, exit(2) on failure) and the title-menu item
 * (pc_guest_title_join, back to the title with a message on failure). `tag` prefixes the log lines ("--bootstrap-guest" / "join-as-guest").
 * Steps, each failing BEFORE anything is bound unless noted (err = "REFUSED: ..." / "FAILED: ...", the function returns 0):
 *   1. CLIENT role, a ready title scene (play_main, no wipe, a player actor: goto_other_scene needs exactly those, so it can no longer fail after the init);
 *   2. the town save is RE-READ from disk with pc_save_reload() -- the very call the vanilla Start path makes (ac_animal_logo.c
 *      aAL_title_game_data_init_start_select: "the title demo mutated save data in RAM") -- and must be valid (mFRm_CheckSaveData);
 *   3. spec check + resident-name check (read-only over the freshly reloaded private_data[0..3]);
 *   4. the fresh record into the passport, bind now_private = passport / player_no = mPr_FOREIGNER / rtc_enabled (the previous values are kept and RESTORED if
 *      the init fails);
 *   5. mSDI_StartDataInitGuest() = the visitor (PAK) init WITHOUT mEv_SetGateway / return animal / goodbye mail (src/game/m_start_data_init.c);
 *   6. goto_other_scene to the station (SCENE_FG at (1979, 760)) with RIDE_OFF_DEMO and the circle wipe.
 * It NEVER arms pc_save_ready (a guest process can write no save at all), never writes or indexes Save_t's private_data[] / homes[] except the read-only name
 * check, runs no new-town / new-player init and allocates no house. */
static int pc_guest_arrive(const char* tag, const char* spec, char* err, size_t errcap) {
    PersonalID_c home;
    Private_c* pass;
    Private_c* prev_private;
    int prev_player_no;
    int prev_rtc;
    GAME_PLAY* play;
    Door_data_c door_data;
    int opt_gender = -1;
    int opt_face = -1;
    int clash;
    int scene_res;
    const int create = s_pc_guest_create_armed; /* first-run creation: the Rover scene (SCENE_START_DEMO2) first, the station after the finish */
    const char* bad_why = "unknown";

    if (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT) {
        snprintf(err, errcap, "REFUSED: a CLIENT-only test hook (the process is not a client)");
        return 0;
    }
    if (gamePT == NULL || gamePT->exec != play_main) {
        snprintf(err, errcap, "FAILED: the title scene is not running (no GAME_PLAY)");
        return 0;
    }
    play = (GAME_PLAY*)gamePT;
    if (play->fb_wipe_mode != WIPE_MODE_NONE || get_player_actor_withoutCheck(play) == NULL) {
        snprintf(err, errcap, "FAILED: the title scene is not ready (a scene change is already running or there is no player actor)");
        return 0;
    }
    if (pc_save_loaded) {
        if (!pc_save_reload()) {
            snprintf(err, errcap, "FAILED: the town save could not be re-read from disk (save/card_a)");
            return 0;
        }
        OSReport("[PC] %s: town save re-read from disk before the arrival (pc_save_reload(), as the vanilla Start path does: the title demo mutated it in RAM)\n", tag);
    }
    if (mFRm_CheckSaveData() == FALSE) {
        snprintf(err, errcap, "FAILED: no valid town save is loaded (a guest needs the town the client loaded)");
        return 0;
    }
    /* defence in depth: pc_main.c already ran pc_bootstrap_guest_validate() before anything started */
    if (!pc_guest_spec_check(spec, &home, &opt_gender, &opt_face, &bad_why)) {
        snprintf(err, errcap, "REFUSED: bad spec '%s': %s (expected NAME,LAND,PLAYER_ID,LAND_ID[,GENDER[,FACE]])", spec, bad_why);
        return 0;
    }
    /* creation: `home`'s name is only a placeholder (the player types the real one in the Rover scene, checked there and again in the finish) */
    clash = create ? -1 : pc_guest_resident_name_conflict(&home);
    if (clash >= 0 && pc_net_game_client_holds_guest_token(&home)) {
        /* M-I: this character already holds a GUEST token of this town, so the resident with its name is most likely the host's promotion of it (the town was just
         * re-fetched). Arrive anyway: the guest claim goes out and the host decides (RESIDENT_HANDOFF + REJECT 6 PROMOTED, or a refusal, never an admission of a
         * second "Roger"). */
        OSReport("[PC] %s: the guest name '%.8s' equals resident %d of this town, but this character holds a guest token of it: arriving so the host can confirm the promotion\n",
                 tag, (const char*)home.player_name, clash);
        clash = -1;
    }
    if (clash >= 0) {
        snprintf(err, errcap, "REFUSED: the guest name '%.8s' equals the name of resident %d of this town (a guest must have its own name)",
                 (const char*)home.player_name, clash);
        return 0;
    }
    /* Guests G6.3: a guest comes from ANOTHER town. The host refuses a home land equal to its own town's land (1 / 65534 chance for a random land id): say
     * it HERE, before anything is bound, instead of letting the host refuse it silently. The ids are permanent, so the fix is the user's (guest.ini). */
    if (home.land_id == (u16)Save_Get(land_info.id) && memcmp(home.land_name, Save_Get(land_info.name), sizeof(home.land_name)) == 0) {
        snprintf(err, errcap, "REFUSED: your guest home town '%.8s' (land id 0x%04X) is the same as this town, so the host would refuse you. Edit land_id in "
                              "save/mp/guest.ini (a different land_id or home_town makes you a NEW guest on every host).",
                 (const char*)home.land_name, (unsigned)home.land_id);
        return 0;
    }
    memset(&l_mcd_foreigner_file, 0, sizeof(l_mcd_foreigner_file));
    pass = &l_mcd_foreigner_file.file.priv;
    pc_guest_build_fresh_record(pass, &home, opt_gender, opt_face);
    l_mcd_foreigner_file.file.copy_protect = (u16)Common_Get(copy_protect);
    OSReport("[PC] %s: FRESH guest record (not a copy of any resident): gender=%d face=%d shirt=0x%04X (%s), empty pockets / wallet / letters\n", tag,
             (int)pass->gender, (int)pass->face, (unsigned)pass->cloth.item,
             (opt_gender >= 0 || opt_face >= 0) ? "given / derived from the identity" : "derived from the identity");

    prev_private = Common_Get(now_private);
    prev_player_no = Common_Get(player_no);
    prev_rtc = Common_Get(time.rtc_enabled);
    Common_Set(time.rtc_enabled, TRUE); /* see pc_bootstrap_resident_poll() */
    Common_Set(now_private, pass);
    Common_Set(player_no, mPr_FOREIGNER);
    if (mSDI_StartDataInitGuest(gamePT) != TRUE) {
        Common_Set(now_private, prev_private);
        Common_Set(player_no, prev_player_no);
        Common_Set(time.rtc_enabled, prev_rtc);
        snprintf(err, errcap, "FAILED: mSDI_StartDataInitGuest failed (the guest could not be bound)");
        return 0;
    }
    OSReport("[PC] %s: guest init ran WITHOUT the gateway (mSDI_StartDataInitGuest: no mEv_SetGateway / return animal / goodbye mail): gateway flag after init = %d (0 = not set), player_no = %d\n",
             tag, (int)mEv_CheckGateway(), (int)Common_Get(player_no));
    /* pc_save_ready is deliberately NOT armed: this process can never write a save. */

    if (create) {
        /* the vanilla Rover entry (aNPS2_make_door_data type DEMO2): SCENE_START_DEMO2, north, (120, 340), no RIDE_OFF_DEMO */
        door_data.next_scene_id = SCENE_START_DEMO2;
        door_data.exit_orientation = mSc_DIRECT_NORTH;
        door_data.exit_type = 0;
        door_data.extra_data = 0;
        door_data.exit_position.x = 120;
        door_data.exit_position.y = 0;
        door_data.exit_position.z = 340;
        door_data.door_actor_name = EMPTY_NO;
        door_data.wipe_type = WIPE_TYPE_FADE_BLACK;
        OSReport("[PC] %s: FIRST-RUN guest creation: profile file %s does not exist yet -> the real Rover scene (SCENE_START_DEMO2) creates it\n", tag,
                 pc_guest_profile_selected_path());
    } else {
        pc_guest_station_door(&door_data);
        Common_Set(demo_profiles[0], mAc_PROFILE_RIDE_OFF_DEMO);
        Common_Get(transition).wipe_type = WIPE_TYPE_CIRCLE_LEFT;
    }
    scene_res = goto_other_scene(play, &door_data, TRUE);
    if (scene_res != TRUE) {
        Common_Set(demo_profiles[0], mAc_PROFILE_NUM);
        Common_Set(now_private, prev_private);
        Common_Set(player_no, prev_player_no);
        Common_Set(time.rtc_enabled, prev_rtc);
        snprintf(err, errcap, "FAILED: goto_other_scene to %s failed (res=%d)", create ? "SCENE_START_DEMO2 (Rover)" : "SCENE_FG (station)", scene_res);
        return 0;
    }
    OSReport("[PC] %s: guest '%.8s' (home land id 0x%04X, player id 0x%04X) bound as a foreigner, arriving at the %s\n", tag,
             (const char*)home.player_name, (unsigned)home.land_id, (unsigned)home.player_id, create ? "Rover scene (SCENE_START_DEMO2)" : "station (SCENE_FG)");
    return 1;
}

/* First-run creation, END of the Rover scene (called by the guide2 actor's aNG2_scene_change_wait_init instead of the vanilla body, and by the test hook):
 * validates what the player chose, writes save/mp/guest_<name>.ini ONCE (create-only, atomic), then applies the identity-derived starter shirt and sends the
 * guest to the station exactly like pc_guest_arrive (RIDE_OFF_DEMO, circle wipe). The vanilla body's mEv_SetFirstJob / mEv_SetFirstIntro / random shirt /
 * weather decision / submenu lock are NOT run (a guest owns no house and no first-day events), and the record is NOT rebuilt: the Rover edited it in place.
 * Nothing is written before this point, so an interrupted creation simply replays on the next launch. Any failure exits 2 with a message (never a half state). */
static void pc_guest_creation_die(const char* msg) {
    fprintf(stderr, "[PC] guest creation: FAILED: %s\n", msg);
    fflush(stdout);
    fflush(stderr);
    exit(2);
}

void pc_guest_creation_finish(GAME_PLAY* play) {
    Private_c* rec = Common_Get(now_private);
    PCGuestProfile p;
    char nm[PC_GUEST_PROFILE_NAME_LEN + 1];
    char err[400];
    Door_data_c door_data;
    int cr, clash;

    if (!s_pc_guest_create_armed || rec == NULL || Common_Get(player_no) != mPr_FOREIGNER) {
        pc_guest_creation_die("internal error: the creation finish ran without an armed first-run creation / a bound guest");
    }
    if (!pc_guest_profile_name_from_game(rec->player_ID.player_name, nm)) {
        pc_guest_creation_die("the chosen name cannot be stored in the guest profile (allowed: 1..8 of A-Z a-z 0-9 blank . ' -, no leading blank, not SERVER)");
    }
    clash = pc_guest_resident_name_conflict(&rec->player_ID);
    if (clash >= 0) {
        pc_guest_creation_die("the chosen name equals the name of a resident of this town");
    }
    if (rec->gender != mPr_SEX_MALE && rec->gender != mPr_SEX_FEMALE) {
        pc_guest_creation_die("the chosen gender is invalid");
    }
    if (rec->face < 0 || rec->face >= mPr_FACE_TYPE_NUM) {
        pc_guest_creation_die("the chosen face is invalid");
    }
    p = s_pc_guest_create_profile;
    memcpy(p.name, nm, sizeof(nm));
    p.gender = (int)rec->gender;
    p.face = (int)rec->face;
    err[0] = '\0';
    if (pc_session()->storage == PC_CHARACTER_STORAGE_STORE) {
        /* M2: the session character is a STORE character: the creation result becomes characters/<uuid>/character.ini (create-only); no guest_<name>.ini */
        cr = pc_session_store_create_finish(p.name, p.gender, p.face, err, sizeof(err));
        if (cr == 1) {
            printf("[PC] guest creation: CHARACTER %s CREATED in the character store (%s)\n", pc_session()->character.uuid, pc_session()->character.path);
        }
    } else
    cr = pc_guest_profile_create_exclusive(pc_guest_profile_selected_path(), &p, err, sizeof(err));
    if (cr == 0) {
        pc_guest_creation_die("the profile file appeared while the Rover scene was running; it was NOT replaced (restart to use it)");
    } else if (cr < 0) {
        pc_guest_creation_die(err);
    }
    s_pc_guest_create_armed = 0;
    OSReport("[PC] guest creation: profile %s CREATED: name '%s', gender %d, face %d, home town '%s', player id 0x%04X, land id 0x%04X\n",
             pc_guest_profile_selected_path(), p.name, p.gender, p.face, p.home_town, (unsigned)p.player_id, (unsigned)p.land_id);
    printf("[PC] guest creation: profile %s CREATED: name '%s', gender %d, face %d\n", pc_guest_profile_selected_path(), p.name, p.gender, p.face);
    fflush(stdout);

    mPlib_change_player_cloth_info_lv2(rec, (mActor_name_t)pc_guest_starter_shirt(&rec->player_ID, p.gender));

    pc_guest_station_door(&door_data);
    Common_Set(demo_profiles[0], mAc_PROFILE_RIDE_OFF_DEMO);
    Common_Get(transition).wipe_type = WIPE_TYPE_CIRCLE_LEFT;
    if (goto_other_scene(play, &door_data, TRUE) != TRUE) {
        Common_Set(demo_profiles[0], mAc_PROFILE_NUM);
        pc_guest_creation_die("the profile was saved, but the scene change to the station failed; start the game again (the profile now exists)");
    }
    OSReport("[PC] guest creation: guest '%s' leaves the Rover scene for the station (SCENE_FG, RIDE_OFF_DEMO)\n", p.name);
}

/* TEST-ONLY hook (--guest-creation-test NAME,GENDER,FACE, needs --guest-profile): once the Rover scene has been running for a while it types NAME / GENDER /
 * FACE into the guest record WITHOUT any UI and runs the very same finish. Driven from pc_bootstrap_guest_poll(). */
extern const char* g_pc_guest_creation_test; /* pc_main.c; NULL = off */
static void pc_guest_creation_test_poll(void) {
    static int l_frames = 0;
    static int l_done = 0;
    GAME_PLAY* play;
    char spec[64];
    char* c1;
    char* c2;
    Private_c* rec;
    size_t n, k;

    if (l_done || g_pc_guest_creation_test == NULL || !s_pc_guest_create_armed) {
        return;
    }
    if (gamePT == NULL || gamePT->exec != play_main || Save_Get(scene_no) != SCENE_START_DEMO2) {
        return;
    }
    play = (GAME_PLAY*)gamePT;
    if (play->fb_wipe_mode != WIPE_MODE_NONE || get_player_actor_withoutCheck(play) == NULL || ++l_frames < 120) {
        return; /* let the Rover scene (guide2, train window, player) really run first */
    }
    l_done = 1;
    snprintf(spec, sizeof(spec), "%s", g_pc_guest_creation_test);
    c1 = strchr(spec, ',');
    c2 = c1 != NULL ? strchr(c1 + 1, ',') : NULL;
    if (c1 == NULL || c2 == NULL) {
        pc_guest_creation_die("--guest-creation-test needs NAME,GENDER,FACE");
    }
    *c1 = '\0';
    *c2 = '\0';
    rec = Common_Get(now_private);
    n = strlen(spec);
    if (rec == NULL || n < 1 || n > PLAYER_NAME_LEN) {
        pc_guest_creation_die("--guest-creation-test: bad NAME");
    }
    memset(rec->player_ID.player_name, ' ', PLAYER_NAME_LEN);
    for (k = 0; k < n; k++) {
        rec->player_ID.player_name[k] = (u8)spec[k];
    }
    rec->gender = (s8)atoi(c1 + 1);
    rec->face = (s8)atoi(c2 + 1);
    OSReport("[PC] guest creation: TEST HOOK typed name '%s', gender %d, face %d in the Rover scene (no UI)\n", spec, (int)rec->gender, (int)rec->face);
    pc_guest_creation_finish(play);
}

/* Guests G2: --bootstrap-guest NAME,LAND,PLAYER_ID,LAND_ID (see pc_main.c; --guest feeds it from guest.ini). TEST-ONLY / headless entry, default off, CLIENT role
 * only (refused otherwise): makes THIS process a GUEST -- a foreigner whose HOME PersonalID is the given one -- in the town it loaded, the state a vanilla train
 * arrival leaves (mCD_InitGameStart_bg, start_cond INCOMING_FOREIGNER: now_private = the passport, player_no = mPr_FOREIGNER) and spawns it at the station exactly
 * like the restart NPC's type 1 / 2 entry (aNPS2_make_door_data: SCENE_FG at (1979, 760), RIDE_OFF_DEMO, circle wipe). All of that is pc_guest_arrive() (shared with
 * the title-menu item): since Guests G3 it re-reads the town save first (pc_save_reload()) and uses mSDI_StartDataInitGuest() (no gateway) instead of the visitor PAK
 * init. The passport is a FRESH, independent character (pc_guest_build_fresh_record), NEVER a copy of a resident: that is what the host's first-contact MIGRATE
 * imports. EVERY failure after the readiness wait prints a diagnostic on stderr and exits 2 (never a silent return that would leave an unbound client running);
 * pc_bootstrap_guest_validate() fails the same bad specs even earlier, before any window / network / save work. Same one-shot / readiness preconditions as
 * pc_bootstrap_resident_poll(). Fires at most once per process. */
void pc_bootstrap_guest_poll(void) {
    extern const char* g_pc_bootstrap_guest; /* pc_main.c; NULL = disabled (default) */
    static int l_done = 0;
    char err[320];

    pc_guest_creation_test_poll();
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

    if (!pc_guest_arrive("--bootstrap-guest", g_pc_bootstrap_guest, err, sizeof(err))) {
        fprintf(stderr, "[PC] --bootstrap-guest: %s\n", err);
        fflush(stdout);
        exit(2);
    }
}

/* ===== Play Online WITHOUT relaunch: the game-side helpers of pc_main.c pc_main_play_online_poll() (the title scene, an in-process connect) =====
 * All of them are tiny and non-blocking. The connect itself (fetch, session, net start, save load) lives in pc_main.c; this block only touches the play scene and the save
 * flags, which are statics of this file. */

/* 1 iff a live play_main scene is idle: no entrance / exit wipe running (the same readiness the bootstrap polls wait for). */
int pc_play_online_scene_ready(void) {
    if (gamePT == NULL || gamePT->exec != play_main) {
        return 0;
    }
    return ((GAME_PLAY*)gamePT)->fb_wipe_mode == WIPE_MODE_NONE;
}

/* 1 once the play scene has been left (the fade-to-title ended in trademark_init). */
int pc_play_online_scene_left(void) {
    return gamePT == NULL || gamePT->exec != play_main;
}

/* The vanilla "back to the title" request (ac_npc_restart_schedule.c_inc aNRST_think_title: the fade + wipe; m_play.c then goes to trademark -> common_data_reinit ->
 * pc_save_reload). Only the two fade fields: the vanilla think function also invades the player actor, which does not exist on the title. */
void pc_play_online_begin_return_title(void) {
    GAME_PLAY* play = (GAME_PLAY*)gamePT;
    play->fb_wipe_type = WIPE_TYPE_FADE_BLACK;
    play->fb_fade_type = FADE_TYPE_OUT_RETURN_TITLE;
}

/* Re-reads the town save from the CURRENT town dir (pc_card_a_dir) with the boot-time call (second_game.c: pc_save_loaded = pc_save_check_and_load()). The caller has already
 * made the role CLIENT when that dir holds a sanitized copy (pc_save_read_gci exits 3 otherwise). pc_save_ready is cleared first: it can be left at 1 by a single-player
 * session, and the in-process connect must not let any save writer run before the new town is entered. Returns the new pc_save_loaded. */
int pc_play_online_save_load(void) {
    pc_save_ready = 0;
    pc_save_loaded = pc_save_check_and_load();
    return pc_save_loaded;
}

/* Rollback helpers: read / restore the two save flags. */
int pc_play_online_save_flags_get(int* ready) {
    *ready = pc_save_ready;
    return pc_save_loaded;
}

void pc_play_online_save_flags_set(int loaded, int ready) {
    pc_save_loaded = loaded;
    pc_save_ready = ready;
}

/* Guests G3.2: the title-menu "Join as Guest" item (src/actor/ac_animal_logo.c, PC_ENHANCEMENTS). Client-only: the item is visible for any --connect client. The label
 * carries the guest name once the SELECTED profile (save/mp/guest.ini, or save/mp/guest_<name>.ini with --guest-profile) could be read; a missing profile is only READ
 * (label "Join as Guest (new profile)", nothing is created by drawing the menu) and created when the player joins; a broken one is never touched. */
int pc_guest_title_item_visible(void) {
    return pc_net_game_role() == PC_NETGAME_ROLE_CLIENT;
}

const char* pc_guest_title_label(void) {
    if (!s_pc_guest_title_label_done) {
        PCGuestProfile gp;
        char perr[512];
        s_pc_guest_title_label_done = 1; /* read once; the click re-reads the file */
        /* READ ONLY: drawing the menu never creates the profile (that happens only when the player joins: pc_guest_title_join / --guest) */
        const int rres = pc_guest_profile_read_selected(&gp, perr, sizeof(perr));
        if (rres == PC_GUEST_PROFILE_LOADED) {
            snprintf(s_pc_guest_title_label, sizeof(s_pc_guest_title_label), "Join as Guest (%s)", gp.name);
        } else if (rres == PC_GUEST_PROFILE_ABSENT) {
            snprintf(s_pc_guest_title_label, sizeof(s_pc_guest_title_label), "Join as Guest (new profile)");
        } else {
            OSReport("[PC] join-as-guest: the guest profile is not usable yet: %s\n", perr);
        }
    }
    return s_pc_guest_title_label;
}

const char* pc_guest_title_message(void) {
    if (s_pc_guest_title_msg[0] == '\0' || time(NULL) >= s_pc_guest_title_msg_until) {
        return NULL;
    }
    return s_pc_guest_title_msg;
}

const char* pc_guest_title_message_head(void) {
    return s_pc_guest_title_msg_head;
}

/* M-J: a title-menu notice with its own heading and duration (the Play Online resident bind found nothing to bind). */
static void pc_title_notice(const char* head, const char* msg, int secs) {
    snprintf(s_pc_guest_title_msg, sizeof(s_pc_guest_title_msg), "%s", msg);
    s_pc_guest_title_msg_head = head;
    s_pc_guest_title_msg_until = time(NULL) + secs;
}

/* Guest-first purchase: the in-process REJOIN (pc_main.c) failed after the guest was promoted: the title shows why (head + message for `secs` seconds). `head` must outlive the call. */
void pc_title_notice_post(const char* head, const char* msg, int secs) {
    pc_title_notice(head, msg, secs);
}

static void pc_guest_title_fail(const char* what) {
    snprintf(s_pc_guest_title_msg, sizeof(s_pc_guest_title_msg), "%s", what);
    s_pc_guest_title_msg_head = "Could not join as a guest:";
    s_pc_guest_title_msg_until = time(NULL) + 8;
    OSReport("[PC] join-as-guest: FAILED: %s -- staying on the title screen\n", what);
}

/* Returns 1 = the arrival began (the station scene change was requested), 0 = it failed: a message is set for the title menu, NOTHING exits and the title
 * keeps running. (The save reload, if reached, is harmless: it is what Start Game does too.) */
int pc_guest_title_join(void) {
    PCGuestProfile gp;
    char perr[512];
    char spec[96];
    char err[320];
    int gres;
    int create = 0;

    s_pc_guest_title_msg[0] = '\0';
    if (pc_session()->storage == PC_CHARACTER_STORAGE_STORE) {
        pc_guest_title_fail("this session plays a stored character (--character / imported profile): it joins from the command line, not from this menu item");
        return 0;
    }
    if (pc_net_game_role() != PC_NETGAME_ROLE_CLIENT) {
        pc_guest_title_fail("only a network client (--connect) can join as a guest");
        return 0;
    }
    if (pc_guest_profile_selected() != NULL) {
        /* a NAMED profile is never auto-created: an existing one is loaded, a missing one is created by the Rover scene (first-run creation) */
        gres = pc_guest_profile_read_selected(&gp, perr, sizeof(perr));
        if (gres == PC_GUEST_PROFILE_ABSENT) {
            if (!pc_guest_profile_prepare_new_selected(&gp, perr, sizeof(perr))) {
                pc_guest_title_fail(perr);
                return 0;
            }
            pc_guest_creation_arm(&gp);
            create = 1;
        }
    } else {
        gres = pc_guest_profile_load_or_create_selected(&gp, perr, sizeof(perr)); /* the DEFAULT profile, created only here */
    }
    if (gres == PC_GUEST_PROFILE_ERR) {
        pc_guest_title_fail(perr);
        return 0;
    }
    s_pc_guest_title_label_done = 0; /* the label is re-read (it shows the name of a just created profile if this join fails and the title stays) */
    if (!pc_guest_profile_spec(&gp, spec, sizeof(spec))) {
        s_pc_guest_create_armed = 0;
        pc_guest_title_fail("internal error: the guest profile does not fit the spec buffer");
        return 0;
    }
    if (!pc_guest_arrive("join-as-guest", spec, err, sizeof(err))) {
        if (create) {
            s_pc_guest_create_armed = 0; /* nothing was written; the next click draws and arms again */
        }
        pc_guest_title_fail(err);
        return 0;
    }
    return 1;
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
            if (g_pc_dedicated) pc_dedicated_say("observer active at acre (%d,%d): the hidden server identity is parked in the town", (int)play->block_table.block_x, (int)play->block_table.block_z);
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

/* M-B: validate a whole GCI image and derive its town identity EXACTLY like pcnetgame_capture_town_identity() does for the live Save (land_name, land_id and
 * the FNV-1a hash of the town-acre combination table), so a transferred file can be compared with the host's TOWN_INFO. Requires the exact file size
 * (64 + 0x72000), the "GAF" game code and a successful Save_t read; the Save_t checksum is not verified here (the loader does not either; the transport
 * checks size + CRC32). Returns TRUE and fills *town on success. */
static uint32_t pc_town_fnv1a_u16(uint32_t h, uint16_t v) {
    h ^= (uint32_t)(v & 0xFFu);
    h *= 16777619u;
    h ^= (uint32_t)(v >> 8);
    h *= 16777619u;
    return h;
}

int pc_save_validate_gci_buffer(const unsigned char* buf, size_t len, PCTownId* town) {
    const CARDDir* hdr;
    Save_t* sv;
    uint32_t h = 2166136261u;
    int ax, az;

    if (buf == NULL || town == NULL || len != (size_t)GCI_HEADER_SIZE + (size_t)GCI_FILE_DATA_SIZE) {
        return FALSE;
    }
    hdr = (const CARDDir*)buf;
    if (memcmp(hdr->gameName, "GAF", 3) != 0) {
        return FALSE;
    }
    sv = (Save_t*)malloc(sizeof(Save_t));
    if (sv == NULL) {
        return FALSE;
    }
    memcpy(sv, buf + GCI_HEADER_SIZE + GCI_SAVE_MAIN_OFFSET, sizeof(Save_t));
    pc_save_bswap(sv, PC_BSWAP_FROM_BE);
    memset(town, 0, sizeof(*town));
    memcpy(town->land_name, sv->land_info.name, 8);
    town->land_id = (uint16_t)sv->land_info.id;
    for (az = 0; az < PCFA_ACRE_Z_NUM; az++) {
        for (ax = 0; ax < PCFA_ACRE_X_NUM; ax++) {
            const mFM_combination_c* c = &sv->combi_table[az + 1][ax + 1];
            uint16_t v = (uint16_t)(((unsigned)c->combination_type & 0x3FFFu) | (((unsigned)c->height & 3u) << 14));
            h = pc_town_fnv1a_u16(h, v);
        }
    }
    town->terrain_hash = h;
    free(sv);
    return TRUE;
}

int pc_save_validate_gci_file(const char* path, PCTownId* town) {
    FILE* fp = fopen(path, "rb");
    unsigned char* buf;
    size_t want = (size_t)GCI_HEADER_SIZE + (size_t)GCI_FILE_DATA_SIZE;
    int ok = FALSE;

    if (fp == NULL) {
        return FALSE;
    }
    buf = (unsigned char*)malloc(want + 1);
    if (buf != NULL) {
        size_t got = fread(buf, 1, want + 1, fp); /* one extra byte: an oversized file is rejected by the length check */
        ok = pc_save_validate_gci_buffer(buf, got, town);
        free(buf);
    }
    fclose(fp);
    return ok;
}

/* ===== M-G: SANITIZED TOWN TRANSFER: layout mirrors + replacement templates =====
 * pc_town_sanitize.c (pure) encodes the offsets of the table; every one of them is checked here against the real structs, so a layout change breaks the build. */
_Static_assert(PC_TS_GCI_SIZE == sizeof(CARDDir) + mCD_LAND_SAVE_SIZE && PC_TS_HDR_SIZE == sizeof(CARDDir), "M-G: GCI size / header drifted");
_Static_assert(PC_TS_OTHERS_OFF == sizeof(CARDDir) && PC_TS_OTHERS_SIZE == OTHERS_SIZE && PC_TS_MAIN_OFF == sizeof(CARDDir) + OTHERS_SIZE &&
                   PC_TS_SAVE_SIZE == sizeof(Save) && PC_TS_SAVE_T_SIZE == sizeof(Save_t) && PC_TS_BACK_OFF == sizeof(CARDDir) + OTHERS_SIZE + sizeof(Save) &&
                   PC_TS_BACK_OFF + PC_TS_SAVE_SIZE == PC_TS_GCI_SIZE,
               "M-G: Others / Save / backup offsets drifted");
_Static_assert(PC_TS_ARAM_START == ALIGN_NEXT(sizeof(MemcardHeader_c) + 32, 32) && PC_TS_ARAM_MAIL_SIZE == ALIGN_NEXT(sizeof(mCD_keep_mail_c), 32) &&
                   PC_TS_ARAM_ORIG_SIZE == ALIGN_NEXT(sizeof(mCD_keep_original_c), 32) && PC_TS_ARAM_DIARY_SIZE == ALIGN_NEXT(sizeof(mCD_keep_diary_c), 32) &&
                   PC_TS_ARAM_START + PC_TS_ARAM_MAIL_SIZE + PC_TS_ARAM_ORIG_SIZE + PC_TS_ARAM_DIARY_SIZE <= PC_TS_OTHERS_SIZE &&
                   PC_TS_MARKER_OFF >= PC_TS_OTHERS_OFF + 48 && PC_TS_MARKER_OFF + PC_TS_MARKER_LEN <= PC_TS_OTHERS_OFF + sizeof(MemcardHeader_c),
               "M-G: ARAM block offsets / marker position drifted");
_Static_assert(PC_TS_PRIV_SIZE == sizeof(Private_c) && PC_TS_MAIL_SIZE == sizeof(Mail_c) && offsetof(Save_t, private_data) == 0x20 && sizeof(Private_c) == 0x2440 &&
                   offsetof(Save_t, save_check) == 0 && offsetof(mFRm_chk_t, checksum) == 0x12 && sizeof(((mFRm_chk_t*)0)->checksum) == 2 &&
                   offsetof(Save_t, land_info) == 0x9120 && offsetof(mLd_land_info_c, id) == 0xA,
               "M-G: Save_t / Private_c / land id offsets drifted");
_Static_assert(offsetof(Private_c, reset_count) == 0x16 && offsetof(Private_c, museum_record) == 0x18 && offsetof(Private_c, exists) == 0x1086 &&
                   offsetof(Private_c, hint_count) == 0x1087 && offsetof(Private_c, cloth) == 0x1088 && sizeof(mPr_cloth_c) == 4 &&
                   offsetof(Private_c, stored_anm_id) == 0x108C && offsetof(Private_c, reset_code) == 0x10F4 && offsetof(Private_c, animal_memory) == 0x10F8 &&
                   offsetof(Private_c, state_flags) == 0x2348 && offsetof(Private_c, calendar) == 0x234C && offsetof(Private_c, mail) == 0x4E0,
               "M-G: Private_c keep ranges drifted");
_Static_assert(offsetof(Save_t, homes) == 0x9CE8 && sizeof(mHm_hs_c) == 0x26B0 && offsetof(mHm_hs_c, mailbox) == 0x1A30 && HOME_MAILBOX_SIZE == 10 &&
                   offsetof(mHm_hs_c, haniwa) + offsetof(Haniwa_c, bells) == 0x2674 && sizeof(((Haniwa_c*)0)->bells) == 4 && offsetof(mHm_hs_c, goki) == 0x2678,
               "M-G: house mailbox / gyroid bells offsets drifted");
_Static_assert(offsetof(Save_t, animals) == 0x17438 && sizeof(Animal_c) == 0x988 && ANIMAL_NUM_MAX == 15 && ANIMAL_MEMORY_NUM == 7 && offsetof(Animal_c, memories) == 0x10 &&
                   sizeof(Anmmem_c) == 0x138 && offsetof(Anmmem_c, letter) == 0x32 && offsetof(Anmplmail_c, present) == 2 && offsetof(Anmplmail_c, header_back_start) == 4 &&
                   offsetof(Anmplmail_c, header) == 5 && offsetof(Anmplmail_c, pad0) == 0xFD && offsetof(Anmplmail_c, date) == 0xFE &&
                   offsetof(Save_t, island) == 0x22540 && offsetof(Island_c, animal) == 0xF00,
               "M-G: villager memory / letter / islander offsets drifted");
_Static_assert(offsetof(Save_t, post_office) == 0x20694 && offsetof(PostOffice_c, mail) == 8 && mPO_MAIL_STORAGE_SIZE == 5 && offsetof(PostOffice_c, leaflet) == 0x5DA,
               "M-G: post office offsets drifted");

static u8 s_ts_priv[4][sizeof(Private_c)];
static u8 s_ts_mail[sizeof(Mail_c)];
static u8* s_ts_aram[mCD_ARAM_DATA_NUM]; /* mail / original / diary, aligned sizes (l_aram_alloc_size_table) */
static PCTownSanitizeTpl s_ts_tpl;
static int s_ts_built = 0;

/* Builds the replacement templates ONCE (lazily from the first transfer request; they do not depend on any world state): the cleared Private_c of every slot
 * (mPr_ClearPrivateInfo, my_org_no_table 0..7, canonical BE image), the cleared Mail_c (taken from that BE record: Private_c.mail[0] is mMl_clear_mail's output), and the
 * ARAM blocks a FRESH PC save holds (calloc + pc_init_mail_entries / pc_init_diary_entries; "original" stays zero), in BE. Returns 1 when ready. */
int pc_save_build_sanitize_templates(void) {
    int i, j;
    Private_c* p;
    if (s_ts_built) {
        return 1;
    }
    p = (Private_c*)malloc(sizeof(Private_c));
    if (p == NULL) {
        return 0;
    }
    for (i = 0; i < 4; i++) {
        mPr_ClearPrivateInfo(p);
        for (j = 0; j < mPr_ORIGINAL_DESIGN_COUNT; j++) {
            p->my_org_no_table[j] = (u8)j;
        }
        pc_save_bswap_private(p, PC_BSWAP_TO_BE);
        memcpy(s_ts_priv[i], p, sizeof(Private_c));
    }
    free(p);
    memcpy(s_ts_mail, s_ts_priv[0] + offsetof(Private_c, mail), sizeof(Mail_c));
    for (i = 0; i < mCD_ARAM_DATA_NUM; i++) {
        u8* blk = (u8*)calloc(1, l_aram_alloc_size_table[i]);
        if (blk == NULL) {
            return 0;
        }
        if (i == mCD_ARAM_DATA_MAIL) {
            pc_init_mail_entries(blk);
            pc_save_bswap_keep_mail((mCD_keep_mail_c*)blk, PC_BSWAP_TO_BE);
        } else if (i == mCD_ARAM_DATA_DIARY) {
            pc_init_diary_entries(blk);
            pc_save_bswap_keep_diary((mCD_keep_diary_c*)blk, PC_BSWAP_TO_BE);
        } else {
            pc_save_bswap_keep_original((mCD_keep_original_c*)blk, PC_BSWAP_TO_BE);
        }
        s_ts_aram[i] = blk;
    }
    for (i = 0; i < 4; i++) {
        s_ts_tpl.private_be[i] = s_ts_priv[i];
    }
    s_ts_tpl.mail_be = s_ts_mail;
    s_ts_tpl.aram_mail_be = s_ts_aram[mCD_ARAM_DATA_MAIL];
    s_ts_tpl.aram_orig_be = s_ts_aram[mCD_ARAM_DATA_ORIGINAL];
    s_ts_tpl.aram_diary_be = s_ts_aram[mCD_ARAM_DATA_DIARY];
    s_ts_built = 1;
    return 1;
}

const PCTownSanitizeTpl* pc_save_sanitize_templates(void) {
    return pc_save_build_sanitize_templates() ? &s_ts_tpl : NULL;
}

/* Scan the "other" card for a travel-eligible town.
 *  - Resident: scan Card B for a different town.
 *  - Foreigner: scan Card A for the home town. */
int mCD_CheckStation_bg(s32* chan) {
    int is_foreigner = mLd_PlayerManKindCheck();

    if (is_foreigner) {
        Save_t temp_save;
        if (chan) *chan = mCD_SLOT_B;
        if (pc_read_gci_land_info(pc_gci_path(), &temp_save)) {
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

        if (!pc_save_read_gci_to_keep(pc_gci_path())) {
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
