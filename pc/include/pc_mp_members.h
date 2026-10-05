/* pc_mp_members.h - resident credentials (M-E): the host file save/mp/members.dat.
 *
 * Pure storage module (functions over buffers + an injectable path, no game state), the same storage pattern as pc_mp_guests.c: CRC32, a tmp file that is
 * committed, rotation .bak1/.bak2, atomic replace, a bad file is MOVED ASIDE to <file>.corrupt-<timestamp> (never deleted) and the host then runs UNTRUSTED
 * (existing files none of which is readable, or only preserved *.corrupt-* files left): no credential is trusted, none is minted, nothing is written.
 *
 * WHAT IT HOLDS. Per host TOWN (land name / land id / terrain hash) and RESIDENT PersonalID: the 16-byte random TOKEN the host minted for that resident
 * (trust on first use) plus the resident slot it had, a CONFIRMED flag (the client presented the token once) and a mint AGE. A second entry kind,
 * PROMOTION_HANDOFF (M-F), carries the guest's home PID in aux_pid. The file is NOT tied to the GCI save: it is loaded at host start next to guests.dat
 * and written synchronously at mint, BEFORE the token is sent.
 *
 * Format v1 (little-endian, fixed size):
 *   header (32 B): char magic[8]="ACMPMBR\0"; u32 version(1); u32 flags(0); u32 slot_count(16); u32 entry_size(80); u32 generation; u32 reserved(0)
 *   16 x entry (80 B): u8 present; u8 confirmed; u8 res_slot (0..3); u8 kind (1 RESIDENT_TOKEN, 2 PROMOTION_HANDOFF);
 *                      u8 land_name[8]; u16 land_id; u16 rsv(0); u32 terrain_hash;   (the town key: 16 B)
 *                      u8 pid[20] (BE resident PersonalID); u8 token[16]; u32 age; u8 aux_pid[20] (handoff only, else zero)
 *   trailer (4 B): u32 crc32 over everything before it
 * (The design text said 72 B per entry; its own field list adds up to 80, so 80 is what is stored: entry_size is in the header and checked.)
 * Validation: magic, version (older and newer REFUSED), flags/reserved zero, counts, exact length, file CRC32, absent entries all-zero, present entries:
 * kind 1|2, res_slot < 4, confirmed 0/1, non-empty land name, non-zero pid and token, aux_pid zero for kind 1 and non-zero for kind 2, no duplicate
 * (town, pid, kind) and no duplicate token between entries.
 *
 * SECURITY MODEL: a BEARER token over the same unencrypted UDP transport as the guest tokens (anyone who can observe the packets can replay it); not
 * internet-grade. TOFU: whoever claims a resident first while it has no credential gets it. See docs/multiplayer-guest-roadmap.md. */
#ifndef PC_MP_MEMBERS_H
#define PC_MP_MEMBERS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_MP_MEMBERS_PATH          "save/mp/members.dat"
#define PC_MP_MEMBERS_RESIDENT_TOKEN_PATH "save/mp/resident_token.dat" /* CLIENT: legacy / CLI resident token file (PCMpGtk format, keyed by town + resident PID) */
#define PC_MP_MEMBERS_SLOTS         16
#define PC_MP_MEMBERS_VERSION       1u
#define PC_MP_MEMBERS_HEADER_SIZE   32
#define PC_MP_MEMBERS_ENTRY_SIZE    80
#define PC_MP_MEMBERS_FILE_SIZE     (PC_MP_MEMBERS_HEADER_SIZE + PC_MP_MEMBERS_SLOTS * PC_MP_MEMBERS_ENTRY_SIZE + 4)
#define PC_MP_MEMBERS_PID_SIZE      20
#define PC_MP_MEMBERS_TOKEN_SIZE    16
#define PC_MP_MEMBERS_RES_SLOTS     4

enum { PC_MP_MEMBER_KIND_RESIDENT_TOKEN = 1, PC_MP_MEMBER_KIND_PROMOTION_HANDOFF = 2 };

enum {
    PC_MP_MBR_OK = 0,
    PC_MP_MBR_ERR_IO,
    PC_MP_MBR_ERR_TRUNCATED,
    PC_MP_MBR_ERR_MAGIC,
    PC_MP_MBR_ERR_VERSION_OLD,
    PC_MP_MBR_ERR_VERSION_FUTURE,
    PC_MP_MBR_ERR_SHAPE,
    PC_MP_MBR_ERR_CRC,
    PC_MP_MBR_ERR_FIELD
};

typedef struct PCMpMemberEntry {
    uint8_t  present;
    uint8_t  confirmed;
    uint8_t  res_slot;
    uint8_t  kind;
    uint8_t  land_name[8];
    uint16_t land_id;
    uint32_t terrain_hash;
    uint8_t  pid[PC_MP_MEMBERS_PID_SIZE];
    uint8_t  token[PC_MP_MEMBERS_TOKEN_SIZE];
    uint32_t age;
    uint8_t  aux_pid[PC_MP_MEMBERS_PID_SIZE];
} PCMpMemberEntry;

typedef struct PCMpMemberFile {
    uint32_t        generation;
    PCMpMemberEntry e[PC_MP_MEMBERS_SLOTS];
} PCMpMemberFile;

typedef enum PCMpMemberLoadMode {
    PC_MP_MBR_LOAD_MISSING = 0,
    PC_MP_MBR_LOAD_OK = 1,
    PC_MP_MBR_LOAD_OK_BACKUP = 2,
    PC_MP_MBR_LOAD_UNTRUSTED = 3 /* files existed but none is readable: no credential is trusted, none minted, nothing written */
} PCMpMemberLoadMode;

typedef struct PCMpMemberLoadInfo {
    PCMpMemberLoadMode mode;
    int  gen_used;
    int  err[3];
    int  moved_aside;
    char first_corrupt_path[320];
} PCMpMemberLoadInfo;

const char* pc_mp_members_strerror(int err);

/* Pure buffer functions. serialize: `out` must hold PC_MP_MEMBERS_FILE_SIZE bytes. */
int pc_mp_members_serialize(const PCMpMemberFile* f, uint8_t* out, size_t cap);
int pc_mp_members_parse(const uint8_t* buf, size_t len, PCMpMemberFile* out);

PCMpMemberLoadMode pc_mp_members_load(const char* path, PCMpMemberFile* out, PCMpMemberLoadInfo* info);
int pc_mp_members_save(const char* path, const PCMpMemberFile* f);

/* Index of the present entry of `kind` for (town key, pid), or -1. */
int pc_mp_members_find(const PCMpMemberFile* f, int kind, const uint8_t land_name[8], uint16_t land_id, uint32_t terrain_hash, const uint8_t pid[PC_MP_MEMBERS_PID_SIZE]);
/* First free slot index, or -1 when the table is full. */
int pc_mp_members_free_slot(const PCMpMemberFile* f);
/* Largest age in the table (0 when empty): a new mint takes max + 1. */
uint32_t pc_mp_members_max_age(const PCMpMemberFile* f);

#ifdef __cplusplus
}
#endif
#endif
