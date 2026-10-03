/* pc_mp_records.h - D3-4: host-side multiplayer resident-record sidecar file (save/mp/records.dat).
 *
 * Self-contained storage module: pure functions over buffers and an injectable path (no game state, no globals except an
 * optional test-only fault hook). It persists, per resident slot of the HOST save: PersonalID (identity key), epoch, rev,
 * digest_at_save, and the most recent pre-migration BACKUP of the host record a MIGRATE import replaced.
 *
 * File location: a SIBLING of save/card_a and save/card_b ("save/mp/records.dat", relative to the working directory exactly like
 * the GCI paths in pc_m_card.c). It is never *.gci, has no extension beginning with "gci"/"GAF", and is never inside a card
 * directory, so the Card-B GCI scan (pc_card.c pc_card_scan_for_gci) cannot see it. The vanilla GCI layout is untouched.
 *
 * On-disk format (version 1, all integers little-endian, fixed size):
 *   header (32 B): char magic[8]="ACMPREC\0"; u32 version; u32 flags(0); u32 slot_count(4); u32 entry_size;
 *                  u32 generation (monotonic per save); u32 reserved(0)
 *   4 x entry (9320 B): u8 present; u8 backup_present; u16 reserved(0); u8 pid[20] (first 0x14 bytes of the BE Private_c =
 *                  the BE PersonalID); u32 epoch; u32 rev; u32 digest_at_save; u32 backup_crc32; u8 backup[0x2440]
 *                  (canonical BE Private_c image of the replaced host record, zero when absent)
 *   trailer (4 B): u32 crc32 over everything before it
 * Validation: magic, version, flags/reserved zero, slot_count, entry_size, exact length, file CRC32, present/backup flags in
 * {0,1}, absent entries all-zero, present entries have a non-zero pid and epoch and rev <= 0x7FFFFFFF, a present backup has a
 * matching crc32 and a non-zero embedded pid.
 * Missing vs untrusted: with no valid generation, a sibling "<file>*.corrupt-*" (a preserved unreadable file of an earlier run)
 * keeps the load UNTRUSTED. Only removing records.dat, its .bak files AND the .corrupt-* files together resets to "missing".
 * Versions: 1 is the only version that ever shipped. An OLDER version (0) is refused (PC_MP_REC_ERR_VERSION_OLD) and a NEWER
 * version is refused (PC_MP_REC_ERR_VERSION_FUTURE); a refused file is treated exactly like a corrupt one (try the .bak
 * generations, then the UNTRUSTED path) and is never overwritten or deleted.
 */
#ifndef PC_MP_RECORDS_H
#define PC_MP_RECORDS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_MP_RECORDS_PATH        "save/mp/records.dat"
#define PC_MP_REC_SLOTS           4
#define PC_MP_REC_PRIVATE_SIZE    0x2440
#define PC_MP_REC_PID_SIZE        0x14
#define PC_MP_REC_VERSION         1u
#define PC_MP_REC_HEADER_SIZE     32
#define PC_MP_REC_ENTRY_SIZE      (4 + PC_MP_REC_PID_SIZE + 16 + PC_MP_REC_PRIVATE_SIZE)
#define PC_MP_REC_FILE_SIZE       (PC_MP_REC_HEADER_SIZE + PC_MP_REC_SLOTS * PC_MP_REC_ENTRY_SIZE + 4)
#define PC_MP_REC_MAX_REV         0x7FFFFFFFu

enum {
    PC_MP_REC_OK = 0,
    PC_MP_REC_ERR_IO,             /* open/read/write/rename failed */
    PC_MP_REC_ERR_TRUNCATED,      /* shorter than the format requires */
    PC_MP_REC_ERR_MAGIC,
    PC_MP_REC_ERR_VERSION_OLD,
    PC_MP_REC_ERR_VERSION_FUTURE,
    PC_MP_REC_ERR_SHAPE,          /* slot_count / entry_size / length / flags / reserved */
    PC_MP_REC_ERR_CRC,            /* file checksum mismatch */
    PC_MP_REC_ERR_FIELD,          /* an entry field is out of range / inconsistent */
    PC_MP_REC_ERR_BACKUP_CRC,     /* a pre-migration backup blob fails its own checksum */
    PC_MP_REC_ERR_FAULT_CRASH     /* TEST ONLY: a fault hook simulated a crash (files left exactly as a crash would) */
};

typedef struct PCMpRecEntry {
    uint8_t  present;                               /* 0/1: this slot has lineage and/or a backup */
    uint8_t  backup_present;                        /* 0/1 */
    uint8_t  pid[PC_MP_REC_PID_SIZE];
    uint32_t epoch;
    uint32_t rev;                                   /* >0 = migration complete (the GCI containing the import was saved) */
    uint32_t digest_at_save;                        /* FNV-1a-32 of the resident's BE Private_c image in the saved GCI */
    uint8_t  backup[PC_MP_REC_PRIVATE_SIZE];        /* BE image of the replaced host record */
} PCMpRecEntry;

typedef struct PCMpRecFile {
    uint32_t     generation;
    PCMpRecEntry e[PC_MP_REC_SLOTS];
} PCMpRecFile;

typedef enum PCMpRecLoadMode {
    PC_MP_REC_LOAD_MISSING = 0,    /* no records.dat and no .bak generation exists: first run, all slots rev 0 */
    PC_MP_REC_LOAD_OK = 1,         /* records.dat itself is valid */
    PC_MP_REC_LOAD_OK_BACKUP = 2,  /* records.dat unreadable/absent, a .bak generation was used (gen_used 1 or 2) */
    PC_MP_REC_LOAD_UNTRUSTED = 3   /* files existed but NONE is readable: caller must NOT allow migration */
} PCMpRecLoadMode;

typedef struct PCMpRecLoadInfo {
    PCMpRecLoadMode mode;
    int  gen_used;                 /* 0 = records.dat, 1 = .bak1, 2 = .bak2 (OK / OK_BACKUP) */
    int  err[3];                   /* per generation: -1 not present, else PC_MP_REC_OK / error code */
    int  moved_aside;              /* number of unreadable files moved to *.corrupt-<timestamp> */
    char first_corrupt_path[320];  /* where the first unreadable file was preserved (empty if none) */
} PCMpRecLoadInfo;

/* TEST-ONLY fault injection (never set by game code). All fields default to "off" (-1 / 0). */
typedef struct PCMpRecFault {
    long fail_write_after;    /* >=0: the tmp write fails (disk-full style) after this many bytes; normal cleanup runs */
    long crash_partial_tmp;   /* >=0: only this many bytes reach the tmp file, then a simulated crash (tmp left behind) */
    int  crash_after_tmp;     /* tmp complete and committed, simulated crash before the rotation */
    int  crash_after_rotate;  /* simulated crash after the .bak rotation, before the tmp->main replace */
    int  fail_rename;         /* the tmp->main replace fails (exercises the restore-.bak path) */
} PCMpRecFault;
void pc_mp_records_set_fault(const PCMpRecFault* f);   /* NULL clears */

const char* pc_mp_records_strerror(int err);

/* Pure buffer functions. serialize: `out` must hold PC_MP_REC_FILE_SIZE bytes. parse: validates everything (see above). */
int  pc_mp_records_serialize(const PCMpRecFile* f, uint8_t* out, size_t cap);
int  pc_mp_records_parse(const uint8_t* buf, size_t len, PCMpRecFile* out);
uint32_t pc_mp_records_crc32(const void* data, size_t n);

/* Loads `path` (and, if needed, path.bak1 / path.bak2). Never deletes anything; unreadable files are moved aside to
 * <file>.corrupt-<timestamp>. Fills `out` (zeroed on MISSING/UNTRUSTED). Every decision is logged ("[NET][REC][STORE]"). */
PCMpRecLoadMode pc_mp_records_load(const char* path, PCMpRecFile* out, PCMpRecLoadInfo* info);

/* Atomically writes the file: tmp write -> flush + commit -> (validate current) rotate .bak1/.bak2 -> atomic replace (restores
 * .bak1 on failure). Creates the parent directories. Returns PC_MP_REC_OK or an error; on any error the previously valid
 * generation is still readable by pc_mp_records_load. f->generation is NOT modified (the caller's copy); the written file
 * carries `generation` as given. */
int  pc_mp_records_save(const char* path, const PCMpRecFile* f);

/* Durably flushes an existing file (used on the GCI just written, before the sidecar may claim anything about it). 0 = ok. */
int  pc_mp_records_sync_file(const char* path);

#ifdef __cplusplus
}
#endif
#endif
