/* pc_mp_guests.h - guest (extra player) storage for the multiplayer host and the client token file.
 *
 * Two self-contained storage formats (pure functions over buffers + an injectable path, no game state):
 *
 *  1. HOST: save/mp/guests.dat -- the guest table (max PC_MP_GUEST_SLOTS = 8, ALL host towns together): per guest its KEY
 *     (the HOST TOWN identity it visits + the guest's full HOME PersonalID, the 0x14-byte BE PersonalID prefix of a Private_c),
 *     the host-issued 16-byte random TOKEN (trust on first use; the PersonalID is public, so the token is the only
 *     anti-impersonation secret), the CONFIRMED flag and the mint AGE (M3), the record lineage (epoch, rev) and
 *     the guest's PERSONAL RECORD (Private_c, canonical BE image, 0x2440 B). A guest's record lives OUTSIDE Save_t: guests never
 *     occupy a private_data[]/homes[] slot. The location is a SIBLING of save/card_a and save/card_b ("save/mp/guests.dat"), it
 *     is never *.gci and has no extension beginning with "gci"/"GAF", so the Card-B GCI scan cannot see it.
 *     Same storage pattern as pc_mp_records.c (tmp -> flush+commit -> validated rotation .bak1/.bak2 -> atomic replace; a bad
 *     file is MOVED ASIDE to <file>.corrupt-<timestamp>, never deleted; UNTRUSTED when files existed but none is readable, or
 *     when only preserved *.corrupt-* files remain: the host must then NOT admit new guests (no silent re-issue of tokens).
 *
 *     Format v2 (little-endian, fixed size; v1 -- one table for one town, no confirmed / age -- is REFUSED as OLDER, never silently
 *     reinterpreted; the format was never released):
 *       header (32 B): char magic[8]="ACMPGST\0"; u32 version(2); u32 flags(0); u32 slot_count(8); u32 entry_size; u32 generation;
 *                      u32 reserved(0)
 *       8 x entry (9352 B): u8 present; u8 confirmed; u16 rsv(0); u8 pid[20]; u8 token[16]; u32 epoch; u32 rev; u32 age;
 *                      u32 record_crc32; u8 town_land_name[8]; u16 town_land_id; u16 rsv(0); u32 town_terrain_hash;
 *                      u8 record[0x2440]
 *       trailer (4 B): u32 crc32 over everything before it
 *     Validation: magic, version (older and newer REFUSED), flags/reserved zero, counts, exact length, file CRC32, flags 0/1,
 *     absent entries all-zero, present entries: non-zero pid, non-zero token, non-zero epoch, rev <= 0x7FFFFFFF, confirmed 0/1,
 *     a non-empty town land name, record CRC, record[0..19] == pid, record `exists` byte (0x1086) == 1, no duplicate
 *     (town, pid) pair and no duplicate token between entries. Entries of a town other than the host's current one are KEPT but INACTIVE.
 *
 *     SECURITY MODEL / KNOWN LIMITATIONS (M2/M3): the token is a BEARER token over an UNENCRYPTED, unauthenticated UDP transport: anyone
 *     who can observe the client's IDENTITY_EXT / the host's IDENTITY_TOKEN packets (same LAN segment, a relay, a captured pcap) can replay
 *     it and be that guest (it can only ever reach THAT guest's own record, never a resident's); real protection needs a v9 encrypted /
 *     challenge-response handshake. The first contact of a key is TRUST ON FIRST USE: until the client has presented its token again and
 *     started a record exchange (CONFIRMED), the entry holds no proof that the real guest ever received it, so an unconfirmed data-less idle
 *     entry may be re-minted for the same key (a lost TOKEN message must not lock the real guest out), at the price that someone who
 *     claims a key FIRST gets its entry (a squatter / a lock-out of a not-yet-confirmed guest: bounded by the per-address limit of 3
 *     issuances per 60 s, the 8-entry table and the eviction rule: only the OLDEST unconfirmed data-less idle entry is ever evicted). An
 *     unconfirmed entry that already holds accepted data, and every confirmed entry, is never re-minted or evicted by the host process.
 *
 *  2. CLIENT: save/mp/guest_token.dat -- a small CLIENT METADATA file (NOT a GCI, never authoritative for inventory/currency):
 *     per visited host TOWN (the host's town land name / land id / terrain hash: the identity the client has already matched against its
 *     own copy of the town before it connects, because the token must be presented BEFORE the host's IDENTITY_ACK names the host) the
 *     guest's home PersonalID and the token the host issued. Versioned, CRC-checked, written atomically (tmp -> commit -> rotate .bak1 ->
 *     replace), validated on load (an unreadable file is moved aside, never deleted; the log says how to reset).
 *       header (32 B): magic "ACMPGTK\0"; u32 version; u32 flags(0); u32 slot_count(4); u32 entry_size; u32 generation; u32 rsv
 *       4 x entry (64 B): u8 present; u8 rsv[3]; u8 host_land_name[8]; u16 host_land_id; u16 rsv; u32 host_terrain_hash; u8 rsv[8];
 *                      u8 home_pid[20]; u8 token[16]
 *       trailer: u32 crc32
 */
#ifndef PC_MP_GUESTS_H
#define PC_MP_GUESTS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_MP_GUESTS_PATH         "save/mp/guests.dat"
#define PC_MP_GUEST_TOKEN_PATH    "save/mp/guest_token.dat"
#define PC_MP_GUEST_SLOTS         8
#define PC_MP_GUEST_PRIVATE_SIZE  0x2440
#define PC_MP_GUEST_PID_SIZE      0x14
#define PC_MP_GUEST_TOKEN_SIZE    16
#define PC_MP_GUEST_EXISTS_OFF    0x1086
#define PC_MP_GUEST_VERSION       2u
#define PC_MP_GUEST_HEADER_SIZE   32
#define PC_MP_GUEST_TOWN_NAME_SIZE 8
#define PC_MP_GUEST_ENTRY_SIZE    (4 + PC_MP_GUEST_PID_SIZE + PC_MP_GUEST_TOKEN_SIZE + 16 + 16 + PC_MP_GUEST_PRIVATE_SIZE)
#define PC_MP_GUEST_FILE_SIZE     (PC_MP_GUEST_HEADER_SIZE + PC_MP_GUEST_SLOTS * PC_MP_GUEST_ENTRY_SIZE + 4)
#define PC_MP_GUEST_MAX_REV       0x7FFFFFFFu

enum {
    PC_MP_GST_OK = 0,
    PC_MP_GST_ERR_IO,
    PC_MP_GST_ERR_TRUNCATED,
    PC_MP_GST_ERR_MAGIC,
    PC_MP_GST_ERR_VERSION_OLD,
    PC_MP_GST_ERR_VERSION_FUTURE,
    PC_MP_GST_ERR_SHAPE,
    PC_MP_GST_ERR_CRC,
    PC_MP_GST_ERR_FIELD,
    PC_MP_GST_ERR_RECORD_CRC,
    PC_MP_GST_ERR_FAULT_CRASH   /* TEST ONLY */
};

typedef struct PCMpGuestEntry {
    uint8_t  present;
    uint8_t  confirmed;                        /* M3: the client presented the token and started a record exchange */
    uint8_t  pid[PC_MP_GUEST_PID_SIZE];
    uint8_t  token[PC_MP_GUEST_TOKEN_SIZE];
    uint32_t epoch;
    uint32_t rev;                              /* > 0 = the first-contact migration completed */
    uint32_t age;                              /* M3: mint order (larger = newer), for the oldest-first eviction */
    uint8_t  town_land_name[PC_MP_GUEST_TOWN_NAME_SIZE]; /* M1: the host town this entry belongs to (land name / id / terrain hash) */
    uint16_t town_land_id;
    uint32_t town_terrain_hash;
    uint8_t  record[PC_MP_GUEST_PRIVATE_SIZE]; /* canonical BE Private_c */
} PCMpGuestEntry;

typedef struct PCMpGuestFile {
    uint32_t       generation;
    PCMpGuestEntry e[PC_MP_GUEST_SLOTS];
} PCMpGuestFile;

typedef enum PCMpGuestLoadMode {
    PC_MP_GST_LOAD_MISSING = 0,
    PC_MP_GST_LOAD_OK = 1,
    PC_MP_GST_LOAD_OK_BACKUP = 2,
    PC_MP_GST_LOAD_UNTRUSTED = 3   /* files existed but none is readable: the caller must NOT admit new guests */
} PCMpGuestLoadMode;

typedef struct PCMpGuestLoadInfo {
    PCMpGuestLoadMode mode;
    int  gen_used;
    int  err[3];
    int  moved_aside;
    char first_corrupt_path[320];
} PCMpGuestLoadInfo;

/* TEST-ONLY fault injection (never set by game code). */
typedef struct PCMpGuestFault {
    long fail_write_after;
    long crash_partial_tmp;
    int  crash_after_tmp;
    int  crash_after_rotate;
    int  fail_rename;
} PCMpGuestFault;
void pc_mp_guests_set_fault(const PCMpGuestFault* f);

const char* pc_mp_guests_strerror(int err);

/* Pure buffer functions. serialize: `out` must hold PC_MP_GUEST_FILE_SIZE bytes. */
int pc_mp_guests_serialize(const PCMpGuestFile* f, uint8_t* out, size_t cap);
int pc_mp_guests_parse(const uint8_t* buf, size_t len, PCMpGuestFile* out);

PCMpGuestLoadMode pc_mp_guests_load(const char* path, PCMpGuestFile* out, PCMpGuestLoadInfo* info);
int pc_mp_guests_save(const char* path, const PCMpGuestFile* f);
/* Guests G6.2: copy `path` byte for byte to "<path>.bak-<timestamp>" (out_path receives the name) BEFORE an operator command changes the table.
 * 1 = backup exists; 0 = no source file / I/O error (nothing is left behind). The caller must refuse its change when this returns 0. */
int pc_mp_guests_backup_file(const char* path, char* out_path, size_t cap);

/* Cryptographically secure random bytes from the OS (RtlGenRandom / SystemFunction036 on Windows, /dev/urandom elsewhere).
 * Returns 1 on success, 0 on failure. There is NO weak fallback: a caller that gets 0 must refuse to mint a token. */
int pc_mp_guests_random_bytes(uint8_t* out, size_t n);

/* ---------------- client token file ---------------- */
#define PC_MP_GTK_SLOTS         4
#define PC_MP_GTK_VERSION       1u
#define PC_MP_GTK_HEADER_SIZE   32
#define PC_MP_GTK_ENTRY_SIZE    64
#define PC_MP_GTK_FILE_SIZE     (PC_MP_GTK_HEADER_SIZE + PC_MP_GTK_SLOTS * PC_MP_GTK_ENTRY_SIZE + 4)

typedef struct PCMpGtkEntry {
    uint8_t  present;
    uint8_t  host_land_name[8];
    uint16_t host_land_id;
    uint32_t host_terrain_hash;
    uint8_t  home_pid[PC_MP_GUEST_PID_SIZE];
    uint8_t  token[PC_MP_GUEST_TOKEN_SIZE];
} PCMpGtkEntry;

typedef struct PCMpGtkFile {
    uint32_t     generation;
    PCMpGtkEntry e[PC_MP_GTK_SLOTS];
} PCMpGtkFile;

int  pc_mp_gtoken_serialize(const PCMpGtkFile* f, uint8_t* out, size_t cap);
int  pc_mp_gtoken_parse(const uint8_t* buf, size_t len, PCMpGtkFile* out);
/* Loads path (or path.bak1). MISSING (no file at all) and OK return 1 with `out` filled / zeroed; an unreadable file is moved
 * aside (never deleted) and the result is 0 with *unreadable set: the caller must treat tokens as unknown and tell the user how to
 * reset (the log of this function names the preserved file). */
int  pc_mp_gtoken_load(const char* path, PCMpGtkFile* out, int* unreadable);
int  pc_mp_gtoken_save(const char* path, const PCMpGtkFile* f);
/* Index of the entry for (host town identity, home pid), or -1. */
int  pc_mp_gtoken_find(const PCMpGtkFile* f, const uint8_t* host_land_name, uint16_t host_land_id, uint32_t host_terrain_hash,
                       const uint8_t* home_pid);
/* Inserts or replaces the entry for that key (a full table recycles the oldest-inserted slot: slot 0, shifting down). Returns the
 * index used. */
int  pc_mp_gtoken_put(PCMpGtkFile* f, const PCMpGtkEntry* e);

/* Guests G1: the guest NAME rule shared by the client (early check) and the host (authority). `name` = the 8-byte space padded player name in the
 * game's font encoding (PersonalID_c.player_name). Valid = at least one character that is not a blank (CHAR_SPACE 32, CHAR_SPACE_2 210,
 * CHAR_SPACE_3 211: the vanilla name entry refuses an all-space name, mED_all_space_check) and every byte is a printable font character:
 * not NUL, not CHAR_CONTROL_CODE (127), CHAR_MESSAGE_TAG (128), CHAR_NEW_LINE (205) and not one of the unused codes above 222 (the name keyboard
 * can produce none of them). Length is the fixed 8 bytes of the field. Returns 1 = valid, 0 = invalid. */
#define PC_MP_GUEST_NAME_LEN 8
int  pc_mp_guests_name_valid(const uint8_t* name);

/* Guests G1.1: the RESERVED observer display name. 1 iff the 8 bytes equal ASCII "SERVER" space padded to 8 (PC_MP_GUEST_RESERVED_NAME), the exact bytes
 * the hidden server observer's PersonalID carries (pc_m_card.c pc_host_observer_poll). The comparison is EXACT (memcmp over 8 bytes, NOT case-folded, no
 * trimming): the game's own name comparison is exact, so "Server  " is a different name. NULL -> 0. A NEW guest (new key / re-mint) may not carry it; a
 * guest already stored in guests.dat is never locked out by it. */
#define PC_MP_GUEST_RESERVED_NAME "SERVER  "
int  pc_mp_guests_name_reserved(const uint8_t* name);

/* Guests G2.1: the FRESH-CHARACTER rule the HOST enforces on a NEW guest's first (MIGRATE) upload. A guest has nothing to import (it is created in the
 * session), so the client wins ONLY for identity / appearance / designs; every economy and progress field must still be the vanilla EMPTY default that
 * pc_m_card.c pc_guest_build_fresh_record() produces (mPr_ClearPrivateInfo + mPr_InitPrivateInfo with the home PersonalID):
 *   pockets all EMPTY_NO, item_conditions 0, wallet 0, bank 0, loan == 100 (the vanilla pre-house value), lotto 0/0, equipment EMPTY_NO, catalog orders empty,
 *   NO letter carries a gift (present EMPTY_NO / RSV_NO; a gift-less letter is accepted), no quest, hint_count / destiny / complete flags / aircheck / unk
 *   spans / museum-adjacent progress zero, state_flags == 1, my_org_no_table is checked elsewhere, maps empty (spaces / id 0), tortimer / golden / e-Card
 *   progress zero, and at most PC_MP_FRESH_CATALOG_MAX_BITS catalog bits set (the arrival init sets exactly 3 item-collect bits).
 * ACCEPTED as the guest's own choice (range-checked only): gender 0|1, face 0..7, starter shirt in ITM_CLOTH000..015 with idx == item - 0x2400, the Able
 * Sisters designs, the calendar / day-counter tail 0x234C..0x23B4 and 0x23B8..0x23D8 (date dependent), birthday, remail, animal memory (all vanilla-empty markers
 * that a client can legitimately differ in); reset_count must be 0. `be_rec` is the canonical 0x2440-byte BIG-ENDIAN Private_c image (the record wire / guests.dat
 * form), so the predicate is pure and natively testable. Returns 0 = a legal fresh character, else a PC_MP_FRESH_BAD_* reason; *detail (optional) is set to the
 * offset of the first bad field. Applies ONLY to a guest's first MIGRATE (never to later uploads, never to residents): see pc_net_game.c. */
#define PC_MP_FRESH_CATALOG_MAX_BITS 3
#define PC_MP_FRESH_LOAN 100
enum {
    PC_MP_FRESH_OK = 0,
    PC_MP_FRESH_BAD_SHAPE,        /* len != 0x2440 / NULL */
    PC_MP_FRESH_BAD_APPEARANCE,   /* gender / face / shirt out of range or reset_count != 0 */
    PC_MP_FRESH_BAD_POCKET,       /* a pocket slot is not empty, or an item condition is set */
    PC_MP_FRESH_BAD_WALLET,
    PC_MP_FRESH_BAD_BANK,
    PC_MP_FRESH_BAD_LOAN,
    PC_MP_FRESH_BAD_LOTTO,
    PC_MP_FRESH_BAD_EQUIPMENT,
    PC_MP_FRESH_BAD_MAIL_GIFT,    /* a letter carries a gift (present != EMPTY_NO / RSV_NO) */
    PC_MP_FRESH_BAD_CATALOG_ORDER,
    PC_MP_FRESH_BAD_CATALOG,      /* more than PC_MP_FRESH_CATALOG_MAX_BITS catalog bits */
    PC_MP_FRESH_BAD_QUEST,        /* a delivery / errand quest is active */
    PC_MP_FRESH_BAD_PROGRESS,     /* hint_count / destiny / complete flags / aircheck / unk spans / maps / tortimer / golden / e-Card / state_flags */
    PC_MP_FRESH_REASON_COUNT
};
int         pc_mp_guest_record_fresh_check(const uint8_t* be_rec, size_t len, unsigned* detail);
const char* pc_mp_guest_fresh_reason_str(int reason);

#ifdef __cplusplus
}
#endif
#endif
