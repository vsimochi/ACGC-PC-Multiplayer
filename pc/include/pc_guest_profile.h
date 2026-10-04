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
 * start (--guest exits with status 2). */
#ifndef PC_GUEST_PROFILE_H
#define PC_GUEST_PROFILE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_GUEST_PROFILE_PATH "save/mp/guest.ini"
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

#ifdef __cplusplus
}
#endif
#endif
