/* pc_guest_profile.h - Guests G2.2: the client's GUEST PROFILE file save/mp/guest.ini (pure C, libc + the OS random source of pc_mp_guests.c).
 *
 * The profile is what a person edits to create a guest character: name, gender, face, home town; plus the two PERMANENT identity ids (player_id,
 * land_id) which are drawn ONCE from the OS CSPRNG (pc_mp_guests_random_bytes) when the file is first created. The home PersonalID = (name, home_town,
 * player_id, land_id) is the guest's KEY on every host (guests.dat / guest_token.dat are keyed by it), so:
 *   - the same file => the same guest on every run (ids are never regenerated while the file exists);
 *   - changing `name`, `home_town`, `player_id` or `land_id` after a token exists makes a NEW guest (a new key; the old character stays on the host).
 *     These four are IMMUTABLE in practice; there is no migration. Edit them only before the first join.
 *   - `gender` / `face` are only the SEED of a brand-new guest (the host record wins for a returning guest).
 *
 * Format: flat `key = value` lines like settings.ini ('#' / ';' comment lines, blank lines and '[section]' lines are ignored; no inline comments; CR LF ok;
 * a UTF-8 BOM is skipped). Keys (ALL required, none may repeat, no unknown key):
 *   name      = 1..8 characters of [A-Za-z0-9 .'-] (leading / trailing blanks trimmed, not blank, not the reserved name SERVER; pc_mp_guests_name_valid)
 *   gender    = 0 (male) | 1 (female)
 *   face      = 0..7
 *   home_town = 1..8 characters of the same set (the guest's home land name)
 *   player_id = decimal or 0x-hex, 1..0xFFFE
 *   land_id   = decimal or 0x-hex, 1..0xFFFE
 * The charset is ASCII on purpose: ASCII letters / digits / blank / . ' - are valid, identical game font codes (a comma would break the --bootstrap-guest spec).
 *
 * Load-or-create: a missing file is created with defaults (name "Guest", home_town "GuestVil", fresh CSPRNG ids, gender / face derived DETERMINISTICALLY from
 * the identity hash exactly like the game's own derivation) with an atomic write (tmp -> flush + commit -> replace). A file that exists but cannot be read or
 * parsed or fails validation is NEVER overwritten, regenerated or moved: the result is an error naming the file and the bad key, and the caller refuses to
 * start (--guest exits with status 2).
 *
 * Several guests on one PC: `--guest-profile NAME` selects an independent profile (see "named guest profiles" below); the default stays save/mp/guest.ini. */
#ifndef PC_GUEST_PROFILE_H
#define PC_GUEST_PROFILE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_GUEST_PROFILE_PATH "save/mp/guest.ini"
#define PC_GUEST_TOKEN_DEFAULT_PATH "save/mp/guest_token.dat" /* == PC_MP_GUEST_TOKEN_PATH (pc_mp_guests.h): the DEFAULT profile's token file */
#define PC_GUEST_PROFILE_DIR "save/mp"
#define PC_GUEST_PROFILE_ARG_MAX 16 /* --guest-profile NAME: 1..16 characters */
#define PC_GUEST_PROFILE_NAME_LEN 8
#define PC_GUEST_PROFILE_MAX_FILE 4096
#define PC_GUEST_PROFILE_DEFAULT_NAME "Guest"
#define PC_GUEST_PROFILE_DEFAULT_HOME "GuestVil"

typedef struct PCGuestProfile {
    char     name[PC_GUEST_PROFILE_NAME_LEN + 1];      /* trimmed ASCII, NUL terminated, 1..8 chars */
    char     home_town[PC_GUEST_PROFILE_NAME_LEN + 1]; /* trimmed ASCII, NUL terminated, 1..8 chars */
    int      gender;                                   /* 0 | 1 */
    int      face;                                     /* 0..7 */
    uint16_t player_id;                                /* 1..0xFFFE, permanent */
    uint16_t land_id;                                  /* 1..0xFFFE, permanent */
} PCGuestProfile;

enum {
    PC_GUEST_PROFILE_LOADED = 0,   /* an existing file was read and validated */
    PC_GUEST_PROFILE_CREATED = 1,  /* the file did not exist: a default profile with fresh ids was written */
    PC_GUEST_PROFILE_ABSENT = 2,   /* pc_guest_profile_read only: the file does not exist (nothing was created) */
    PC_GUEST_PROFILE_ERR = -1      /* error: `err` of the call names the file and the problem; nothing was modified */
};

/* Validates `p` field by field. Returns 1 = valid, else 0 with *bad_key = the key and *why the reason (both static strings; either may be NULL). */
int pc_guest_profile_validate(const PCGuestProfile* p, const char** bad_key, const char** why);

/* Parses the text of a profile file (`text`, `len` bytes; need not be NUL terminated) into `out` and validates it. Returns 1 = ok, else 0 with a message
 * "<key>: <reason>" (or "line N: <reason>") in err. */
int pc_guest_profile_parse(const char* text, size_t len, PCGuestProfile* out, char* err, size_t errcap);

/* Formats the canonical file text. Returns the length (without NUL) or 0 if `cap` is too small. */
size_t pc_guest_profile_format(const PCGuestProfile* p, char* out, size_t cap);

/* Builds a default profile: name "Guest", home_town "GuestVil", ids = fresh CSPRNG values in 1..0xFFFE, gender / face derived from the identity hash.
 * Returns 1, or 0 if the OS random source failed (no weak fallback). */
int pc_guest_profile_make_default(PCGuestProfile* out);

/* Loads `path` or creates it (see above). Returns PC_GUEST_PROFILE_LOADED / _CREATED / _ERR; err (always set on _ERR) reads "<path>: <key>: <reason>". */
int pc_guest_profile_load_or_create(const char* path, PCGuestProfile* out, char* err, size_t errcap);

/* The 8-byte, space padded PersonalID name / land name of a profile (ASCII game font codes). */
void pc_guest_profile_name_bytes(const PCGuestProfile* p, uint8_t out[PC_GUEST_PROFILE_NAME_LEN]);
void pc_guest_profile_home_bytes(const PCGuestProfile* p, uint8_t out[PC_GUEST_PROFILE_NAME_LEN]);

/* The "NAME,LAND,PLAYER_ID,LAND_ID,GENDER,FACE" spec string consumed by pc_bootstrap_guest_poll (the SAME arrival path as --bootstrap-guest).
 * Returns 1, or 0 if `cap` is too small. */
int pc_guest_profile_spec(const PCGuestProfile* p, char* out, size_t cap);

/* ================= named guest profiles (--guest-profile NAME): several independent guests on ONE PC =================
 * Without a selected profile EVERYTHING above is unchanged: save/mp/guest.ini + save/mp/guest_token.dat. With profile NAME:
 *   save/mp/guest_<name>.ini           the profile (same format, same rules)
 *   save/mp/guest_token_<name>.dat     its OWN client token file (a profile is an independent guest on every host)
 * <name> in the FILE NAMES is the profile name folded to lower case ('Alice' and 'alice' are ONE profile: one file, one identity).
 * A profile NAME is 1..16 characters of [A-Za-z0-9-], must not start with '-', and must not be a Windows device name (CON PRN AUX NUL COM0..COM9 LPT0..LPT9).
 * A NEW profile gets its own CSPRNG ids and a default display name = the profile name cut to 8 characters (fallback "Guest" + 2 hex digits of the id when that
 * would be invalid / SERVER / "Guest" itself); the sibling profiles of the same directory (guest.ini, guest_*.ini; read-only, unreadable ones are skipped with a
 * warning) are scanned first and the ids are re-drawn (bounded) until the FULL identity AND the display name differ from every sibling (the host refuses a NEW
 * guest whose name equals another guest's name in the town). An existing / corrupt file is never overwritten (same rule as above). */

/* Validates a profile NAME. Returns 1 = ok, else 0 with the violated rule in err ("profile name ...", may be NULL). */
int pc_guest_profile_name_check(const char* name, char* err, size_t errcap);

/* Lower-cases `name` (ASCII) into out[PC_GUEST_PROFILE_ARG_MAX + 1]; NULL -> "". */
void pc_guest_profile_fold(const char* name, char* out);

/* Builds the profile / token file path in `dir` (NULL = PC_GUEST_PROFILE_DIR). profile == NULL: the DEFAULT names ("<dir>/guest.ini" / "<dir>/guest_token.dat").
 * Returns 1, or 0 if `cap` is too small or the profile name is invalid. */
int pc_guest_profile_file_path(const char* dir, const char* profile, int token_file, char* out, size_t cap);

/* The process-wide SELECTED profile (the single source of truth for --guest, --guest-profile, the title-menu item and the token file). NULL / "" = default.
 * select() validates; returns 1 / 0 (nothing changes on 0). */
int pc_guest_profile_select(const char* name);
const char* pc_guest_profile_selected(void); /* the NAME as given, or NULL when the default profile is in use */
const char* pc_guest_profile_selected_path(void); /* the selected profile's ini path (static buffer) */
const char* pc_guest_token_path(void);            /* the selected profile's client token file path (static buffer) */

/* Reads `path` WITHOUT ever creating or modifying anything: LOADED / ABSENT (missing file) / ERR (err names file + problem, file untouched). */
int pc_guest_profile_read(const char* path, PCGuestProfile* out, char* err, size_t errcap);

/* Load-or-create profile `profile` (NULL = default, exactly pc_guest_profile_load_or_create of <dir>/guest.ini) in `dir`: an existing file is loaded, a missing
 * one is created with unique ids as described above (sibling scan). */
int pc_guest_profile_load_or_create_in(const char* dir, const char* profile, PCGuestProfile* out, char* err, size_t errcap);
/* The same for the SELECTED profile in PC_GUEST_PROFILE_DIR. */
int pc_guest_profile_load_or_create_selected(PCGuestProfile* out, char* err, size_t errcap);
/* The same read-only: LOADED / ABSENT / ERR for the SELECTED profile (the title menu uses it to draw its label without creating anything). */
int pc_guest_profile_read_selected(PCGuestProfile* out, char* err, size_t errcap);

/* The default display name of a new profile (see above); `id` is the drawn player_id used by the fallback. out >= 9 bytes. */
void pc_guest_profile_default_name(const char* profile, uint16_t id, char* out);

/* Draws a profile identity for `profile` that differs (full identity AND display name) from every one of `sib[0..nsib)`. Returns 1, or 0 when the random source
 * failed / no unique identity was found within 32 draws. */
int pc_guest_profile_make_unique(const char* profile, const PCGuestProfile* sib, int nsib, PCGuestProfile* out);

/* ================= First-run guest creation (the REAL vanilla Rover scene creates the profile) =================
 * `--guest-profile NAME` with NO guest_<name>.ini does not auto-create the file any more: prepare_new draws the permanent ids + a placeholder name IN MEMORY
 * (unique among the sibling profiles, exactly like load_or_create_in, but nothing is written); the Rover scene (SCENE_START_DEMO2) then lets the player
 * choose name / gender / face and pc_guest_profile_create_exclusive() writes the file ONCE, create-only (never replaces an existing file). */

/* Draws the identity of a NEW named profile in `dir` (NULL = PC_GUEST_PROFILE_DIR) WITHOUT writing anything. Returns 1, or 0 with err: the profile name is invalid
 * / the profile file ALREADY exists (use read) / guest_token_<name>.dat exists WITHOUT the ini (an orphan token: refused, never deleted) / the random source failed. */
int pc_guest_profile_prepare_new(const char* dir, const char* profile, PCGuestProfile* out, char* err, size_t errcap);
int pc_guest_profile_prepare_new_selected(PCGuestProfile* out, char* err, size_t errcap); /* the SELECTED named profile in PC_GUEST_PROFILE_DIR */

/* Writes `p` (validated) to `path` atomically (tmp file, flush, then a move that does NOT replace). Returns 1 = created, 0 = `path` already exists (nothing
 * written), -1 = error (err filled). */
int pc_guest_profile_create_exclusive(const char* path, const PCGuestProfile* p, char* err, size_t errcap);

/* The 8 game font code bytes of a player name -> the profile's ASCII name (trailing blanks trimmed). Returns 1 only when the name is EXACTLY representable
 * in a profile (allowed charset, no leading blank, valid, not SERVER) so that name_bytes() gives back the same 8 bytes; else 0 (out = ""). out >= 9 bytes. */
int pc_guest_profile_name_from_game(const uint8_t name[PC_GUEST_PROFILE_NAME_LEN], char* out);

/* TEST SEAM: replaces the CSPRNG id source (NULL = the OS CSPRNG, the default and the only production value). */
typedef int (*PCGuestProfileIdSource)(uint16_t* out);
void pc_guest_profile_test_set_id_source(PCGuestProfileIdSource fn);

#ifdef __cplusplus
}
#endif
#endif
