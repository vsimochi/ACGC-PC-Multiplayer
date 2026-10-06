/* pc_character.h - player-owned CHARACTERS (M1/M2): the local, per-PC character store save/mp/characters/<uuid>/ (pure C, libc + OS file APIs).
 *
 * A character is the player-owned PORTABLE SEED of a guest: name, gender, face, home_town, player_id, land_id (+ a local-only `uuid` = 128-bit CSPRNG used for
 * the directory name). The WIRE identity is unchanged: home PersonalID = (name, home_town, player_id, land_id) exactly like a guest profile; the uuid never
 * leaves this PC. Everything a TOWN owns (pockets, wallet, bank, mail, house, quests, ...) lives on the host and is NOT stored here. See
 * docs/multiplayer-guest-roadmap.md "Characters and town memberships".
 *
 * Layout under <dir> (default save/mp):
 *   characters/<uuid32hex>/character.ini                       identity (flat key = value, atomic create-only write, corrupt files never touched)
 *   characters/<uuid>/towns/<townkey>/membership.ini           role (resident|guest), town_pid (hex, 20 B), last_server hint
 *   characters/<uuid>/towns/<townkey>/token.dat                the existing PCMpGtk token file, exactly ONE entry (this town): no 4-slot recycling
 *   characters.ini                                             default = <uuid> (last used); the registry itself is the directory scan
 * <townkey> = <host land name 16 hex>_<land_id 4 hex>_<terrain_hash 8 hex> (the host TOWN identity, not the server address).
 *
 * Legacy guest profiles (guest.ini, guest_<name>.ini + their token files) are never moved or deleted: they are listed as LEGACY characters (adapter); an explicit
 * import (pc_character_import_legacy) copies the identity and the token entries into the store and records legacy_profile. */
#ifndef PC_CHARACTER_H
#define PC_CHARACTER_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_CHARACTER_UUID_LEN 32
#define PC_CHARACTER_MAX 64
#define PC_CHARACTER_TOWNKEY_LEN 30 /* 16 + 1 + 4 + 1 + 8 (without NUL) */

enum { PC_CHARACTER_STORAGE_NONE = 0, PC_CHARACTER_STORAGE_LEGACY = 1, PC_CHARACTER_STORAGE_STORE = 2 };

typedef struct PCCharacter {
    int      storage;                       /* LEGACY (guest.ini / guest_<name>.ini) | STORE (characters/<uuid>/character.ini) */
    char     uuid[PC_CHARACTER_UUID_LEN + 1]; /* 32 lower-case hex; "" for LEGACY */
    char     name[9];                       /* ASCII, 1..8 */
    uint8_t  name_bytes[8];                 /* raw game font codes (== padded name for a guest; raw bytes for a future imported resident) */
    char     home_town[9];
    uint8_t  home_town_bytes[8];
    int      gender;
    int      face;
    uint16_t player_id;
    uint16_t land_id;
    uint32_t created;                       /* unix time */
    int      has_legacy;                    /* legacy_profile key present (STORE) / always 1 (LEGACY) */
    char     legacy_profile[17];            /* folded profile name; "" = the default guest.ini */
    char     path[420];                     /* the file this was read from */
} PCCharacter;

/* Result codes of load / resolve. */
enum { PC_CHARACTER_OK = 0, PC_CHARACTER_ABSENT = 1, PC_CHARACTER_AMBIGUOUS = 2, PC_CHARACTER_ERR = -1 };

/* Home PersonalID in the canonical BE form of the wire / token files (name 8, home town 8, player_id BE, land_id BE). */
void pc_character_home_pid_be(const PCCharacter* c, uint8_t out[20]);

/* Pure text form. parse validates every key (all required except legacy_profile, no unknown key, no repeat); format returns the length or 0. */
int    pc_character_parse(const char* text, size_t len, PCCharacter* out, char* err, size_t errcap);
size_t pc_character_format(const PCCharacter* c, char* out, size_t cap);

/* Loads characters/<uuid>/character.ini. OK / ABSENT / ERR (err names the file and the problem; the file is never touched). */
int pc_character_load(const char* dir, const char* uuid, PCCharacter* out, char* err, size_t errcap);

/* Store characters (sorted by uuid) followed by the LEGACY profiles not yet imported. Unreadable ones are skipped with a warning on stderr (*skipped counts them).
 * Returns the number written (<= cap). */
int pc_character_list(const char* dir, PCCharacter* out, int cap, int* skipped);

/* Draws a NEW character in memory (nothing written): uuid + ids unique among all listed characters (and legacy profiles), default name from `profile_name`
 * (1..16 of A-Za-z0-9-). Returns 1 or 0 with err. */
int pc_character_prepare_new(const char* dir, const char* profile_name, PCCharacter* out, char* err, size_t errcap);
/* Writes characters/<uuid>/character.ini create-only and atomically. 1 = created, 0 = exists (nothing written), -1 = error. */
int pc_character_write_exclusive(const char* dir, const PCCharacter* c, char* err, size_t errcap);

/* Explicit import of a legacy profile (NULL / "" = guest.ini; "default" = guest.ini unless guest_default.ini exists): creates a store character with the SAME
 * identity, copies the token entries of that guest into towns/<key>/token.dat (+ membership.ini role=guest), sets legacy_profile. The legacy files are only READ.
 * Refuses (0, err) when an imported character already has that legacy_profile or the legacy files are unreadable. *tokens = entries copied (optional). */
int pc_character_import_legacy(const char* dir, const char* profile, PCCharacter* out, int* tokens, char* err, size_t errcap);

/* Resolution. resolve_profile: the store character with legacy_profile == fold(profile) (profile NULL/"" = the default one), else the legacy profile file
 * (storage LEGACY), else ABSENT. resolve(spec): legacy_profile match, then exact name (case-insensitive; >1 => AMBIGUOUS), then uuid prefix (>= 4 hex; >1 =>
 * AMBIGUOUS), then a legacy profile file named spec. */
int pc_character_resolve_profile(const char* dir, const char* profile, PCCharacter* out, char* err, size_t errcap);
int pc_character_resolve(const char* dir, const char* spec, PCCharacter* out, char* err, size_t errcap);

/* Town keys and paths. */
void pc_character_town_key_format(const uint8_t land_name[8], uint16_t land_id, uint32_t terrain_hash, char out[PC_CHARACTER_TOWNKEY_LEN + 1]);
int  pc_character_token_path(const char* dir, const char* uuid, const char* townkey, char* out, size_t cap);
int  pc_character_membership_path(const char* dir, const char* uuid, const char* townkey, char* out, size_t cap);
/* Writes membership.ini (atomic replace). role: "guest" | "resident". town_pid = 20 BE bytes. last_server may be NULL. */
int  pc_character_membership_write(const char* dir, const char* uuid, const char* townkey, const char* role, const uint8_t town_pid[20], const char* last_server);
/* M-C: reads membership.ini: 1 = valid (role_out = "guest" | "resident", town_pid filled), 0 = absent / malformed (treat as no membership). */
int  pc_character_membership_read(const char* dir, const char* uuid, const char* townkey, char role_out[16], uint8_t town_pid[20]);
/* Guest-first purchase: extra keys of membership.ini (e.g. `nook_intro = declined | bought`) are PRESERVED by pc_character_membership_write. get_key: 1 + value, 0 = absent.
 * set_key: sets / replaces ONE extra key of an existing file (role / town_pid / last_server are kept); 1 = written, 0 = no file / bad key or value / no room. */
int  pc_character_membership_get_key(const char* dir, const char* uuid, const char* townkey, const char* key, char* out, size_t cap);
int  pc_character_membership_set_key(const char* dir, const char* uuid, const char* townkey, const char* key, const char* value);

/* characters.ini default = <uuid>. get: 1 + uuid in out[33], else 0. set: atomic replace; 1 / 0. */
int pc_character_default_get(const char* dir, char out[PC_CHARACTER_UUID_LEN + 1]);
int pc_character_default_set(const char* dir, const char* uuid);

/* TEST SEAM: replaces the uuid source (NULL = the OS CSPRNG). */
typedef int (*PCCharacterUuidSource)(uint8_t out[16]);
void pc_character_test_set_uuid_source(PCCharacterUuidSource fn);

#ifdef __cplusplus
}
#endif
#endif
