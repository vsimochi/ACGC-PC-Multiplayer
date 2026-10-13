/* pc_mp_guest_store.h - the host's GUEST STORE as ONE FILE PER GUEST (capacity phase 3), replacing the fixed 8-entry save/mp/guests.dat of pc_mp_guests.h.
 * Pure C (libc + the file APIs), no game state, injectable directory, native tests in tools/net_spike/guest_store_selftest.c.
 *
 * WHY per-guest files: guests.dat held exactly PC_MP_GUEST_SLOTS (8) entries of 9.3 KB in one file that every durable event rewrote whole. A guest is now an independent
 * file <dir>/<32 hex id>.gst: the number of guests is limited by memory / disk only, and an event rewrites just the guests that changed (one 9.4 KB file, not the table).
 *
 * File v3 (little-endian, fixed size 9404 B):
 *   header (32 B): char magic[8]="ACMPGS1\0"; u32 version(3); u32 flags(0); u32 entry_size(9352); u32 generation (per guest, +1 per write); u32 rsv(0); u32 rsv(0)
 *   id  (16 B)   : the guest's store id (random, == the 32 hex digits of the file name; never reused for another guest)
 *   entry (9352 B): EXACTLY the guests.dat v2 entry (pc_mp_guests_entry_encode / decode: key pid, token, confirmed, epoch / rev, age, host town, canonical BE record + CRC)
 *   trailer (4 B): u32 crc32 over everything before it
 * The security model and every entry validation rule are those of guests.dat (see pc_mp_guests.h): bearer token, trust on first use, confirmed flag, oldest-first eviction.
 *
 * Durability / safety (same discipline as guests.dat): every write is tmp -> flush+commit -> validated rotation .bak1 / .bak2 -> atomic replace; an unreadable file is MOVED
 * ASIDE to <file>.corrupt-<timestamp>, never deleted; a guest removed by the operator / promotion is moved aside to <file>.removed-<timestamp> (with its .bak files), never deleted.
 * UNTRUSTED (the host must not admit new guests): any guest file whose primary AND backups are unreadable, a preserved *.corrupt-* file with no valid guest of that id, two guests
 * sharing a (town, key) or a token, or a directory that cannot be listed -- exactly the "tokens are unknown, a squatter could not be told from the real guest" rule of guests.dat.
 *
 * MIGRATION from guests.dat v2 (pc_mp_gs_migrate_legacy): every present entry is written as its own file (create-only: an existing guest of the same (town, key) is never overwritten),
 * read back and compared byte for byte; only when ALL succeeded the legacy file and its .bak1 / .bak2 are renamed to <name>.v2-migrated-<timestamp> (kept, never deleted, so they cannot
 * be re-imported over later changes). A crash half way just repeats safely. A legacy file that is itself unreadable / UNTRUSTED is left untouched and reported.
 *
 * File v4 (guest mailbox, "Guest mailbox via the post office"): the v3 layout followed by ONE EXTENSION BLOCK, all of it covered by the trailing CRC (fixed size 12396 B):
 *   header (32 B, version 4) | id (16 B) | entry (9352 B) | u32 ext_len (= 2988: the bytes of the block after this field) | u32 ext_version (1) | u32 mbox_mask (bits 0..9)
 *   | 10 x 298 B letters (the canonical BIG-ENDIAN Mail_c image of each used slot; an unused slot is all zero bytes) | u32 crc32
 * The guest's host-held MAILBOX (letters waiting for it at the post office) therefore lives in the SAME file as its carried letters (entry.record mail[]): a letter moved from the
 * mailbox into mail[] (MAIL_TAKE) is ONE atomic file write, so a crash can neither duplicate nor lose it.
 * The writer emits v4 ONLY while the mailbox holds a letter; an empty mailbox is written as plain v3 (byte-identical to the previous build). A v3 file reads as an empty mailbox.
 * Rules of the block: ext_version != 1 -> VERSION_FUTURE (never reinterpreted); ext_len != 2988 -> SHAPE; mask bits above 9 -> FIELD; an unused slot that is not all zero / a used slot that
 * is all zero -> FIELD. DOWNGRADE (known, accepted): an OLDER build refuses a v4 file as "a newer version" and moves it aside to *.corrupt-* (its store then becomes UNTRUSTED: no guests
 * admitted). This build heals that on its next start: a preserved *.corrupt-* file of a guest that has no readable generation, which parses cleanly now, is copied back as the guest's
 * primary file (the *.corrupt-* file is kept) -- see pc_mp_gs_load. */
#ifndef PC_MP_GUEST_STORE_H
#define PC_MP_GUEST_STORE_H

#include <stddef.h>
#include <stdint.h>

#include "pc_mp_guests.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PC_MP_GS_VERSION_V3  3u /* no extension block (empty guest mailbox) */
#define PC_MP_GS_VERSION     4u /* the newest version this build reads; written only while the guest mailbox holds a letter */
#define PC_MP_GS_HEADER_SIZE 32
#define PC_MP_GS_ID_SIZE     16
#define PC_MP_GS_FILE_SIZE   (PC_MP_GS_HEADER_SIZE + PC_MP_GS_ID_SIZE + PC_MP_GUEST_ENTRY_SIZE + 4) /* a v3 file: 9404 */
#define PC_MP_GS_MBOX_SLOTS  10   /* == HOME_MAILBOX_SIZE */
#define PC_MP_GS_MAIL_SIZE   298  /* == sizeof(Mail_c), the canonical BE letter image */
#define PC_MP_GS_MBOX_MASK   0x03FFu
#define PC_MP_GS_EXT_VERSION 1u
#define PC_MP_GS_EXT_LEN     (4 + 4 + PC_MP_GS_MBOX_SLOTS * PC_MP_GS_MAIL_SIZE) /* ext_version + mbox_mask + letters = 2988 */
#define PC_MP_GS_FILE_SIZE_V4 (PC_MP_GS_FILE_SIZE + 4 + PC_MP_GS_EXT_LEN)     /* 12396 */
#define PC_MP_GS_FILE_MAX    PC_MP_GS_FILE_SIZE_V4

typedef struct PCMpGsRecord {
    uint8_t        id[PC_MP_GS_ID_SIZE];
    uint32_t       generation;
    PCMpGuestEntry e; /* e.present == 1 */
    /* the guest mailbox (v4 extension): bit i of mbox_mask = slot i holds a letter, whose canonical BE image is mbox[i]; unused slots are all zero. mask 0 = written as v3. */
    uint16_t       mbox_mask;
    uint8_t        mbox[PC_MP_GS_MBOX_SLOTS][PC_MP_GS_MAIL_SIZE];
} PCMpGsRecord;

/* Pure buffer functions. serialize: `out` must hold pc_mp_gs_serialized_size(r) bytes (PC_MP_GS_FILE_SIZE for an empty mailbox, PC_MP_GS_FILE_SIZE_V4 otherwise).
 * Errors are PC_MP_GST_ERR_*. */
size_t pc_mp_gs_serialized_size(const PCMpGsRecord* r);
int pc_mp_gs_serialize(const PCMpGsRecord* r, uint8_t* out, size_t cap);
int pc_mp_gs_parse(const uint8_t* buf, size_t len, PCMpGsRecord* out);

/* A digest of everything durable about an entry EXCEPT its epoch (an epoch re-roll alone is not a reason to rewrite): equal digests = nothing to write. */
uint32_t pc_mp_gs_entry_digest(const PCMpGuestEntry* e);
/* Folds the mailbox of `r` into an entry digest: returns `entry_digest` unchanged for an empty mailbox (a v3 guest keeps its digest), else a CRC over it + the mask + the letters. */
uint32_t pc_mp_gs_ext_digest(const PCMpGsRecord* r, uint32_t entry_digest);

/* Ids and names. id_hex: 33 bytes out. A file is <dir>/<hex>.gst (+ .bak1 / .bak2). */
void pc_mp_gs_id_hex(const uint8_t id[PC_MP_GS_ID_SIZE], char out[33]);
int  pc_mp_gs_id_parse(const char* hex32, uint8_t id[PC_MP_GS_ID_SIZE]); /* exactly 32 lower-case hex digits */
int  pc_mp_gs_new_id(uint8_t id[PC_MP_GS_ID_SIZE]);                       /* OS CSPRNG, never all zero; 0 = no randomness (the caller must refuse) */
void pc_mp_gs_path(const char* dir, const uint8_t id[PC_MP_GS_ID_SIZE], int gen, char* out, size_t cap);
/* The store directory that belongs to a guests.dat path: "<x>/guests.dat" -> "<x>/guests". */
void pc_mp_gs_dir_for_legacy(const char* legacy_path, char* out, size_t cap);

/* Atomic write of one guest (creates the directory). PC_MP_GST_OK or an error; nothing is changed on failure. */
int pc_mp_gs_save(const char* dir, const PCMpGsRecord* r);
/* The operator / promotion removal: moves the guest's file and its .bak files aside (<file>.removed-<timestamp>); 1 = done (or nothing to move), 0 = a move failed. */
int pc_mp_gs_retire(const char* dir, const uint8_t id[PC_MP_GS_ID_SIZE], char* moved_primary, size_t cap);
/* Copies the guest's current file byte for byte to "<file>.bak-<timestamp>" BEFORE an operator command changes it; 1 = done, 0 = no source / I/O error (nothing is left behind). */
int pc_mp_gs_backup(const char* dir, const uint8_t id[PC_MP_GS_ID_SIZE], char* out_path, size_t cap);

typedef struct PCMpGsLoadInfo {
    int  dir_missing;     /* the directory does not exist: first run, no guests */
    int  files;           /* distinct guest ids found (primary or backup files) */
    int  loaded;          /* guests handed to the visitor */
    int  from_backup;     /* of those, recovered from .bak1 / .bak2 (the primary was missing or unreadable) */
    int  unreadable;      /* ids with NO readable generation (their files were preserved as *.corrupt-*) */
    int  moved_aside;     /* unreadable files moved aside to *.corrupt-* during this load */
    int  stray_corrupt;   /* preserved *.corrupt-* files whose id has no valid guest */
    int  duplicates;      /* guests sharing a (town, key) or a token with another guest */
    int  restored;        /* guests whose only readable copy was a preserved *.corrupt-* file (an older build set a newer-version file aside): copied back as the primary */
    int  untrusted;       /* the caller must NOT admit new guests (see the header comment) */
    char first_problem[320];
} PCMpGsLoadInfo;

/* Visits every readable guest once, OLDEST FIRST (mint order, ties by id: deterministic, so slot numbers follow arrival order and survive a restart). `from_backup` = 0 / 1 / 2 = the generation used. Return 0 from the visitor to stop (the load then returns 0).
 * Returns 1 when the scan completed, 0 on allocation failure / visitor abort (info->untrusted is then set: nothing is known for sure). */
typedef int (*PCMpGsVisit)(void* ctx, const PCMpGsRecord* rec, int from_backup);
int pc_mp_gs_load(const char* dir, PCMpGsVisit visit, void* ctx, PCMpGsLoadInfo* info);

enum { PC_MP_GS_MIG_NONE = 0, PC_MP_GS_MIG_DONE = 1, PC_MP_GS_MIG_LEGACY_UNTRUSTED = 2, PC_MP_GS_MIG_FAILED = 3 };
typedef struct PCMpGsMigInfo {
    int  legacy_entries;
    int  written;
    int  skipped_existing; /* already present in the store (same town + key): left exactly as it is */
    char retired[460];     /* where the legacy guests.dat went */
    char error[240];
} PCMpGsMigInfo;
int pc_mp_gs_migrate_legacy(const char* legacy_path, const char* dir, PCMpGsMigInfo* info);

/* TEST SEAMS (never set by game code). */
void pc_mp_gs_set_fault(const PCMpGuestFault* f);
typedef int (*PCMpGsIdSource)(uint8_t out[PC_MP_GS_ID_SIZE]);
void pc_mp_gs_set_id_source(PCMpGsIdSource fn);

#ifdef __cplusplus
}
#endif
#endif
