/* pc_dresser_store.h - GROUNDWORK ONLY (not wired into gameplay; nothing calls it yet). See docs/dresser-storage-design.md.
 *
 * Host-side sidecar table of EXTRA dresser storage per character identity (resident PersonalID / guest key, 16 bytes), persisted as
 * a pc_tabfile (dresser_store.dat: atomic tmp -> rename, magic, version, order-sensitive FNV check; a missing file = every pool empty,
 * a corrupt file = refused, the caller keeps the previous in-memory table and must NOT overwrite the bad file blindly).
 * The 3 vanilla keep slots of a furniture piece (aFTR_KEEP_ITEM_COUNT, stored in the house image) are NOT touched by this module.
 *
 * Every mutation is a transaction identified by (nonce, seq) + a hash of the request bytes, journalled IN the persisted record:
 *   - the same (nonce, seq, hash) again          -> the journalled outcome is replayed (REPLAYED), nothing executes twice, also after a restart;
 *   - the same (nonce, seq) with another hash    -> CONFLICT, nothing changes;
 *   - a seq of the current nonce <= max_seq that is no longer in the journal -> FENCED;
 *   - a new nonce supersedes the previous one (the old one is remembered as fenced).
 * Each journal entry also stores the pocket pre/post digests the host computed, so a host restart between "sidecar saved" and
 * "resident pocket mirror saved" can be rolled forward (see pc_dresser_store_last_applied()). Pure libc, no game headers. */
#ifndef PC_DRESSER_STORE_H
#define PC_DRESSER_STORE_H

#include <stddef.h>
#include <stdint.h>

#include "pc_keytab.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PC_DRESSER_KEY_LEN    16
#define PC_DRESSER_SLOTS      27  /* v3 (6r, user decision A1): ALL 27 cells of a dresser are host slots (v2 had 24 + the 3 vanilla keep slots of the house image) */
#define PC_DRESSER_V2_SLOTS   24  /* the persisted v2 record (read once, converted losslessly: its 24 items keep their slots, 24..26 start empty) */
#define PC_DRESSER_VANILLA    3   /* aFTR_KEEP_ITEM_COUNT: the vanilla keep slots (in the house image, not in this store) */
#define PC_DRESSER_TOTAL      PC_DRESSER_SLOTS /* 27 (6r: every cell is a host slot; the vanilla keep slots are no cells any more) */
#define PC_DRESSER_JOURNAL    16  /* journal entries per identity, same depth as PC_NETGAME_TXN_RING */
#define PC_DRESSER_FENCED_NUM 4
#define PC_DRESSER_MAGIC      0x52534344u /* "DCSR" */
#define PC_DRESSER_VERSION    3u  /* v3: one record per DRESSER identity, 27 slots (6r). v2 (24 slots) is converted on load (the v2 file is kept as .v2.bak);
                                     * v1 = per resident, 60 slots: backed up, then migrated lazily per owner */

enum { PC_DRESSER_OP_PUT = 1, PC_DRESSER_OP_TAKE = 2, PC_DRESSER_OP_MOVE = 3, PC_DRESSER_OP_SWAP = 4 };
/* 6q (S3): MOVE = slot -> dst (empty: move; occupied: swap); no pocket change. 6r: SWAP = pool `slot` (holding `item`) <-> pocket `pocket` (holding `item2`):
 * `item` goes to the pocket, `item2` into the slot, in one transaction (the journal keeps `item` = what entered the pocket, for the restart roll-forward) */
enum { PC_DRESSER_OUT_APPLIED = 0, PC_DRESSER_OUT_REJECTED = 1 };
/* Reason values deliberately equal the existing PC_NETGAME_TXN_REASON_* numbers (pc_net_game.c) so the host can forward them unchanged. */
enum {
    PC_DRESSER_R_NONE = 0,
    PC_DRESSER_R_FENCED = 6,
    PC_DRESSER_R_CONFLICT = 7,
    PC_DRESSER_R_PRECOND = 9,
    PC_DRESSER_R_BAD_SHAPE = 11,
    PC_DRESSER_R_FAULT = 13,
    PC_DRESSER_R_REPLAYED = 14,
    PC_DRESSER_R_NOT_AVAILABLE = 16, /* TAKE: the slot does not hold the expected item */
    PC_DRESSER_R_NO_ROOM = 22        /* PUT: the slot is occupied / the pool is full */
};

typedef struct PCDresserJrnl {
    uint32_t nonce, seq, msg_hash;
    uint32_t pre_pocket_dig, post_pocket_dig; /* host-computed digests of the resident pockets before / after this txn */
    uint32_t rev;                             /* the pool rev after this txn (APPLIED) or at the time (REJECTED) */
    uint8_t  op, outcome, reason, slot;
    uint16_t item;
    uint8_t  pocket, _r; /* pocket: the resident pocket slot of this txn (restart roll-forward of the pocket side); _r: MOVE's destination slot (0 otherwise) */
} PCDresserJrnl; /* 32 B (layout unchanged by 6q: MOVE uses op 3 and the reserved byte) */

typedef struct PCDresserRec {
    uint8_t       key[PC_DRESSER_KEY_LEN];
    uint32_t      rev;     /* +1 per APPLIED txn */
    uint32_t      nonce;   /* the client process nonce currently served */
    uint32_t      max_seq; /* highest seq answered for `nonce` */
    uint32_t      fenced[PC_DRESSER_FENCED_NUM];
    uint8_t       head, n;
    uint8_t       orphan; /* 1 = the dresser of this key vanished from the house in a way the host could not re-key unambiguously (kept, never deleted) */
    uint8_t       _r;
    uint16_t      items[PC_DRESSER_SLOTS]; /* 0 = empty */
    PCDresserJrnl jr[PC_DRESSER_JOURNAL];
} PCDresserRec;

typedef struct PCDresserRecV2 { /* the persisted v2 layout (608 B): read-only, for the v2 -> v3 conversion */
    uint8_t       key[PC_DRESSER_KEY_LEN];
    uint32_t      rev, nonce, max_seq;
    uint32_t      fenced[PC_DRESSER_FENCED_NUM];
    uint8_t       head, n, orphan, _r;
    uint16_t      items[PC_DRESSER_V2_SLOTS];
    PCDresserJrnl jr[PC_DRESSER_JOURNAL];
} PCDresserRecV2;

typedef struct PCDresserStore {
    PCKeyTab tab;
    int      ready;
    int      dirty; /* 1 after an APPLIED txn until the next successful save */
} PCDresserStore;

typedef struct PCDresserTxn {
    uint32_t nonce, seq, msg_hash;
    uint32_t pre_pocket_dig, post_pocket_dig;
    uint8_t  op, slot;
    uint16_t item; /* PUT: the item put in; TAKE / MOVE: the item the client expects in `slot` */
    uint8_t  pocket; /* the pocket slot (journalled for the restart roll-forward; not validated here) */
    uint8_t  dst;    /* MOVE: the destination slot (6q) */
    uint16_t item2;  /* SWAP: the pocket item that goes into the slot (6r) */
} PCDresserTxn;

typedef struct PCDresserResult {
    uint8_t  outcome, reason, slot;
    uint16_t item;
    uint32_t rev;
} PCDresserResult;

int  pc_dresser_store_init(PCDresserStore* s, int max_identities);
void pc_dresser_store_free(PCDresserStore* s);
/* 1 = loaded (v3), 2 = a v2 file was loaded and CONVERTED in memory (lossless; the caller backs the v2 file up with pc_dresser_store_backup_copy and then
 * saves v3), 0 = no file (empty store: a pre-feature save), -1 = corrupt / unknown version (the store is left EMPTY; the caller must keep the bad file aside). */
int  pc_dresser_store_load(PCDresserStore* s, const char* path);
/* Copies `path` to <path><suffix> (or <path><suffix>-<tag> when that already exists); never overwrites a backup, never deletes. 1 = copied, -1 = failed. */
int  pc_dresser_store_backup_copy(const char* path, const char* suffix, unsigned long tag);
int  pc_dresser_store_save(PCDresserStore* s, const char* path); /* 1 = written atomically */
/* Executes or replays one transaction. Returns 1 when `out` is filled (APPLIED or REJECTED), 0 on a bad argument / table full (FAULT). */
int  pc_dresser_store_apply(PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN], const PCDresserTxn* t, PCDresserResult* out);
/* Looks (nonce, seq) up in the persisted journal WITHOUT executing anything. 1 = journalled (`out` = the replayed / CONFLICT answer), 0 = unknown. */
int  pc_dresser_store_peek(const PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN], const PCDresserTxn* t, PCDresserResult* out);
/* Overwrites (or creates) the record of rec->key with a copy taken before an apply: the in-memory undo when the write-ahead save failed. 1 = done. */
int  pc_dresser_store_restore(PCDresserStore* s, const PCDresserRec* rec);
/* ---- v2 per-dresser identity (16 bytes): [0..7] owner digest (the house owner's PersonalID), [8] floor, [9] layer, [10] ut_x, [11] ut_z,
 * [12..13] base furniture id (rotation bits masked, little endian), [14..15] 0 ---- */
#define PC_DRESSER_KEY_PREFIX 10 /* owner + floor + layer: the scope of the orphan re-attach search */
void pc_dresser_key_make(uint8_t key[PC_DRESSER_KEY_LEN], const uint8_t owner8[8], int floor, int layer, int ut_x, int ut_z, uint16_t ftr_base);
/* A v1 (per-resident) file at `path` is renamed ONCE to <path>.v1.bak (or <path>.v1.bak-<tag> when that backup already exists; `tag` = a caller
 * timestamp); never deleted (the plain .v1.bak is the lazy migration source, see below; a timestamped one is never migrated). 1 = backed up, 0 = nothing to do (absent / already v2 / not ours), -1 = the rename failed (the caller disables the feature). */
int  pc_dresser_store_backup_v1(const char* path, unsigned long tag);
/* Moves the record of `from` to `to` (journal and rev kept, orphan flag cleared). Refused (0) when `from` does not exist or `to` already holds a
 * non-empty / orphaned record. 1 = moved. Nothing is ever merged or dropped. */
int  pc_dresser_store_rekey(PCDresserStore* s, const uint8_t from[PC_DRESSER_KEY_LEN], const uint8_t to[PC_DRESSER_KEY_LEN]);
int  pc_dresser_store_set_orphan(PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN], int orphan); /* 1 = set */
/* The orphaned record of the same owner / floor / layer (key prefix) and base furniture id with the LOWEST cell (ut_z, then ut_x): the deterministic
 * re-attach candidate. NULL = none. */
const PCDresserRec* pc_dresser_store_find_orphan(const PCDresserStore* s, const uint8_t prefix[PC_DRESSER_KEY_PREFIX], uint16_t ftr_base);
int  pc_dresser_store_used(const PCDresserRec* r); /* items held (0..PC_DRESSER_SLOTS) */
/* pc_dresser_store_peek over every record whose key starts with `prefix` (plen bytes): the first journalled answer. 1 = found. */
int  pc_dresser_store_peek_prefix(const PCDresserStore* s, const uint8_t* prefix, size_t plen, const PCDresserTxn* t, PCDresserResult* out);
/* ---- v1 -> v2 MIGRATION (hardening; docs/dresser-storage-redesign-plan.md section 6b) ----
 * The unreleased v1 file held ONE pool of 60 slots per resident (key: player_name[8], player_id LE, land_id LE, FNV-1a of land_name). The host renames it
 * to <path>.v1.bak at start (pc_dresser_store_backup_v1, atomic, never deleted) and migrates each resident LAZILY, when that resident's house is known:
 * the v1 items (slot order) fill the free extra slots of that owner's dressers in the order the caller gives (floor, layer, cell ascending), 24 each; what
 * does not fit goes to that owner's LEGACY OVERFLOW records (key [8] = 0xFD, [9] = page 0..2, 24 items per page). Page 0 always exists after a migration
 * and is the per-owner MARKER (_r = 1, nonce = the v1 file digest). Marker + items reach disk in ONE atomic v2 save, so a restart before it redoes the
 * migration from the unchanged .v1.bak and a restart after it skips it: every v1 item ends up exactly once. Never part of a dresser's identity scan
 * (floor 0xFD is no real floor). */
#define PC_DRESSER_V1_SLOTS     60
#define PC_DRESSER_LEGACY_FLOOR 0xFDu
#define PC_DRESSER_LEGACY_PAGES 3
typedef struct PCDresserV1Rec {
    uint8_t       key[PC_DRESSER_KEY_LEN];
    uint32_t      rev, nonce, max_seq;
    uint32_t      fenced[PC_DRESSER_FENCED_NUM];
    uint8_t       head, n;
    uint16_t      items[PC_DRESSER_V1_SLOTS];
    PCDresserJrnl jr[PC_DRESSER_JOURNAL];
} PCDresserV1Rec; /* 680 B: the persisted v1 layout */
/* Loads a v1 file (read-only) into `t` (initialised here with PCDresserV1Rec elements; free with pc_keytab_free). *digest = FNV-1a of the whole file.
 * 1 = loaded, 0 = no such file, -1 = not a valid v1 file (`t` left empty). */
int  pc_dresser_v1_load(const char* path, PCKeyTab* t, int max_identities, uint32_t* digest);
void pc_dresser_key_legacy(uint8_t key[PC_DRESSER_KEY_LEN], const uint8_t owner8[8], int page);
/* 1 = owner8 was already migrated from the v1 file with that digest (the marker exists). */
int  pc_dresser_store_migrated(const PCDresserStore* s, const uint8_t owner8[8], uint32_t v1_digest);
/* Migrates ONE owner (see above). keys[n] = that owner's dressers in the deterministic order. Returns the number of v1 items placed (dressers + overflow),
 * or -1 (already migrated / bad argument / table full: NOTHING changed). *to_overflow (optional) = how many went to the legacy overflow. In memory only:
 * the caller saves (and on a failed save reloads the file, which undoes it). */
int  pc_dresser_store_migrate_v1(PCDresserStore* s, const uint8_t owner8[8], const uint16_t items[PC_DRESSER_V1_SLOTS], uint32_t v1_digest,
                                 const uint8_t (*keys)[PC_DRESSER_KEY_LEN], int n, int* to_overflow);
/* Claim path of the legacy overflow: moves overflow items of the key's owner (page order, slot order) into the FREE slots of dresser `key` (created if
 * needed). Returns how many moved (0 = nothing to claim / no room). In memory only (the caller saves). */
int  pc_dresser_store_claim_overflow(PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN]);
int  pc_dresser_store_overflow_count(const PCDresserStore* s, const uint8_t owner8[8]);

/* Read-only view of a pool (NULL = no pool yet = every slot empty). */
const PCDresserRec* pc_dresser_store_find(const PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN]);
/* The newest APPLIED journal entry of `key` (restart roll-forward check), NULL if none. */
const PCDresserJrnl* pc_dresser_store_last_applied(const PCDresserStore* s, const uint8_t key[PC_DRESSER_KEY_LEN]);

/* 6n: the HOST feature switch, pure (unit-tested by pc/tools/net_spike/dresser_switch_selftest.c). env = getenv("AC_MP_DRESSER_STORAGE"):
 *   exactly "1"            -> 1 (ON);
 *   exactly "0"            -> 0 (OFF);
 *   unset / empty / other  -> build_default (1 only in a build configured with -DPC_DRESSER_STORAGE_DEFAULT_ON=ON: the local dev build pc/build64; every other
 *                             configuration, release included, has 0). */
static inline int pc_dresser_feature_enabled_from_env(const char* env, int build_default) {
    if (env != NULL && env[0] == '1' && env[1] == '\0') {
        return 1;
    }
    if (env != NULL && env[0] == '0' && env[1] == '\0') {
        return 0;
    }
    return build_default ? 1 : 0;
}

#ifdef __cplusplus
}
#endif
#endif
