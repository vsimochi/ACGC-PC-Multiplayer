/* pc_town_cache.h - per-server TOWN CACHE (multiplayer phase 2, milestones M-A and M-B). PURE module: plain C, no game header, no network, no SDL,
 * so it is natively unit-testable (pc/tools/net_spike/town_cache_selftest.c + test_town_cache_unit.py).
 *
 * M-A  runtime Card-A directory. The compile-time macros of pc_m_card.c (PC_CARD_A_DIR / PC_GCI_PATH / PC_GCI_TMP_PATH) and the card_dir[0] of pc_card.c
 *      became the accessors pc_card_a_dir() / pc_gci_path() / pc_gci_tmp_path(). They return "save/card_a" (byte-identical to the old macros) until
 *      pc_card_set_town_dir(DIR) is called ONCE before boot_main; then they return DIR/card_a, DIR/card_a/DobutsunomoriP_MURA.gci and ...gci.tmp.
 * M-B  the cache layout save/mp/towns/<townkey>/{card_a/DobutsunomoriP_MURA.gci, incoming/town.part, origin.ini}, the townkey formatting (identical to
 *      pc_character_town_key_format: land_name 16 hex _ land_id 4 hex _ terrain_hash 8 hex), origin.ini (server name, address, port, last fetch time),
 *      the .part writer (strictly in-order), CRC32, and the ATOMIC INSTALL (MoveFileExA REPLACE_EXISTING|WRITE_THROUGH on Windows, rename elsewhere).
 *
 * Safety rules enforced here (and pinned by the unit test):
 *   - A download is staged ONLY in <town>/incoming/town.part, NEVER inside card_a (the loader renames an orphaned *.tmp and scans GAF*.gci there).
 *   - An install of a missing / unreadable part leaves the existing cache file byte-identical; a part is never moved unless the caller validated it.
 *   - Nothing here ever touches the legacy save/card_a, save/DobutsunomoriP_MURA.gci or any other path than the ones it is handed.
 */
#ifndef PC_TOWN_CACHE_H
#define PC_TOWN_CACHE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_TOWN_KEY_LEN        30                       /* 16 + 1 + 4 + 1 + 8 (without NUL) == PC_CHARACTER_TOWNKEY_LEN */
#define PC_TOWN_GCI_FILENAME   "DobutsunomoriP_MURA.gci"
#define PC_TOWN_GCI_SIZE       467008u                  /* 64-byte CARDDir header + 0x72000 */
#define PC_TOWN_CHUNK_DATA     1000u                    /* TOWN_CHUNK payload bytes */
#define PC_TOWN_DEFAULT_ROOT   "save/mp/towns"
#define PC_TOWN_PATH_MAX       400

/* ---- M-A: runtime Card-A directory ---- */
/* Sets the town directory (e.g. save/mp/towns/<key>). Returns 1 on success; 0 (and nothing changes) for an empty / too long path or when a DIFFERENT town
 * dir was already set (it is set once per process). Setting the same dir again is a harmless 1. */
int         pc_card_set_town_dir(const char* town_dir);
const char* pc_card_town_dir(void);    /* NULL while unset (default save/card_a layout) */
int         pc_card_town_dir_active(void);
const char* pc_card_a_dir(void);       /* "save/card_a" or "<town_dir>/card_a" */
const char* pc_gci_path(void);         /* pc_card_a_dir() + "/DobutsunomoriP_MURA.gci" */
const char* pc_gci_tmp_path(void);     /* pc_gci_path() + ".tmp" */
void        pc_card_reset_town_dir_for_test(void); /* unit test only */

/* ---- town identity / key / paths ---- */
typedef struct PCTownId {
    uint8_t  land_name[8];
    uint16_t land_id;
    uint32_t terrain_hash;
} PCTownId;

int  pc_town_id_equal(const PCTownId* a, const PCTownId* b);
void pc_town_key_format(const PCTownId* t, char out[PC_TOWN_KEY_LEN + 1]);
int  pc_town_key_valid(const char* key);

typedef struct PCTownPaths {
    char town_dir[PC_TOWN_PATH_MAX];   /* <root>/<key> */
    char card_dir[PC_TOWN_PATH_MAX + 64];   /* <root>/<key>/card_a */
    char gci[PC_TOWN_PATH_MAX + 64];        /* <root>/<key>/card_a/DobutsunomoriP_MURA.gci */
    char incoming_dir[PC_TOWN_PATH_MAX + 64];/* <root>/<key>/incoming */
    char part[PC_TOWN_PATH_MAX + 64];       /* <root>/<key>/incoming/town.part */
    char origin_ini[PC_TOWN_PATH_MAX + 64]; /* <root>/<key>/origin.ini */
} PCTownPaths;
/* root == NULL -> PC_TOWN_DEFAULT_ROOT. Returns 1, or 0 for an invalid key / too long path (out zeroed). */
int pc_town_paths(const char* root, const char* key, PCTownPaths* out);

/* mkdir -p (creates every missing component). Returns 1 when the directory exists afterwards. */
int pc_town_mkdirs(const char* path);

/* ---- origin.ini ---- */
typedef struct PCTownOrigin {
    char     server_name[64];
    char     address[64];
    uint16_t port;
    int64_t  last_fetch;  /* unix time */
} PCTownOrigin;
int pc_town_origin_write(const char* path, const PCTownOrigin* o);  /* tmp + atomic replace; creates the parent dir */
int pc_town_origin_read(const char* path, PCTownOrigin* o);         /* 0 when missing / malformed (address and port are required) */
/* Scans <root>/<key>/origin.ini for address+port; among several matches the newest last_fetch wins. A match counts only if the town's GCI exists.
 * Returns 1 and fills key_out/paths_out (paths_out may be NULL). */
int pc_town_cache_find_by_server(const char* root, const char* address, uint16_t port, char key_out[PC_TOWN_KEY_LEN + 1], PCTownPaths* paths_out);

/* ---- CRC32 (IEEE 802.3, the zlib/Python zlib.crc32 value) ---- */
uint32_t pc_town_crc32_update(uint32_t crc, const void* data, size_t len); /* start with crc = 0 */
uint32_t pc_town_crc32(const void* data, size_t len);
int      pc_town_file_crc(const char* path, uint32_t* crc_out, uint32_t* size_out); /* 0 when unreadable */

/* ---- the .part writer (strictly in-order) ---- */
typedef struct PCTownPart {
    void*    fp;        /* FILE* */
    uint32_t expected;  /* next offset */
    uint32_t crc;
    int      failed;
    char     path[PC_TOWN_PATH_MAX];
} PCTownPart;
/* Creates <incoming> (mkdir -p) and TRUNCATES the part at `path` (the previous part is deleted only by the next fetch). Returns 1 on success. */
int  pc_town_part_begin(PCTownPart* p, const char* incoming_dir, const char* part_path);
/* Appends `len` bytes iff offset == the next expected offset and the total would not exceed max_total. Returns 1 on success; any error marks the part failed. */
int  pc_town_part_append(PCTownPart* p, uint32_t offset, const void* data, uint32_t len, uint32_t max_total);
/* Flush + close. Returns 1 iff not failed; fills the final size and CRC. */
int  pc_town_part_finish(PCTownPart* p, uint32_t* size_out, uint32_t* crc_out);
void pc_town_part_abort(PCTownPart* p);   /* closes the handle; the file stays (deleted only by the next fetch) */

/* ---- atomic install ---- */
/* Moves `part` onto `gci` (creating gci's directory): MoveFileExA(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) on Windows, rename() elsewhere. On failure the
 * existing `gci` is untouched. Returns 1 on success. The caller MUST have verified size + CRC32 + GCI validity first. */
int pc_town_cache_install(const char* part, const char* gci);

/* M-B (town transfer): the PRE-BOOT town fetch of a client started with --town-fetch. Call it after pc_platform_init() and BEFORE boot_main() / before
 * pc_net_game_start_client(): it runs its own single-purpose connection (pc_net_init ... pc_net_shutdown, no game state touched), downloads the host's last
 * saved GCI into save/mp/towns/<townkey>/ (verified: size, CRC32, GCI validity, town == TOWN_INFO; installed atomically, a failed fetch never replaces a valid
 * cache) and selects that directory as Card A (pc_card_set_town_dir). `progress` (may be NULL) receives window-title texts; `server_name` (may be NULL) is stored
 * in origin.ini; on PC_TOWN_PREFETCH_ERROR `err` holds a message for the user. Returns one of: */
#define PC_TOWN_PREFETCH_ERROR       0 /* no usable town at all (err filled): the caller shows a message and does not boot a wrong town */
#define PC_TOWN_PREFETCH_FETCHED     1 /* a new / updated cache was installed and selected */
#define PC_TOWN_PREFETCH_UP_TO_DATE  2 /* the local cache already equals the host's file; selected */
#define PC_TOWN_PREFETCH_CACHE       3 /* the fetch failed / was refused: an existing cache of this server is selected (offline-capable) */
#define PC_TOWN_PREFETCH_LEGACY      4 /* the fetch failed / was refused: the legacy save/card_a is used unchanged */
typedef void (*PCNetGameTownProgressFn)(const char* text);
int pc_net_game_town_prefetch(const char* host_ip, uint16_t port, uint32_t timeout_ms, PCNetGameTownProgressFn progress, const char* server_name,
                              char* err, size_t err_cap);

/* ---- implemented in pc_m_card.c (game-linked; NOT part of the pure module / its unit test) ---- */
/* Validates a whole GCI image (exact size 64 + 0x72000, "GAF" game code, Save_t readable) and derives its town identity exactly like the live
 * pcnetgame_capture_town_identity(). Returns nonzero on success. */
int      pc_save_validate_gci_buffer(const unsigned char* buf, size_t len, PCTownId* town);
int      pc_save_validate_gci_file(const char* path, PCTownId* town);
/* Number of durable Card-A GCI writes since process start (the host's TOWN_INFO.town_gen). */
unsigned pc_save_town_gen(void);

/* M-G (sanitized town transfer): the replacement templates for pc_town_sanitize() (built once, lazily; NULL if allocation failed) and the flag "the loaded Card-A GCI
 * is a sanitized transfer image" (set by pc_save_read_gci; client only). */
struct PCTownSanitizeTpl;
int      pc_save_build_sanitize_templates(void);
const struct PCTownSanitizeTpl* pc_save_sanitize_templates(void);
extern int g_pc_save_sanitized;

#ifdef __cplusplus
}
#endif

#endif /* PC_TOWN_CACHE_H */
