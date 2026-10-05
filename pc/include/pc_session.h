/* pc_session.h - the process-wide CONNECT SESSION (M2, minimal): which character this client plays and where its per-town token file lives.
 * Filled by pc_main.c from the CLI (--guest / --guest-profile / --character). M3 will extend it with servers. The old shims (g_pc_bootstrap_guest,
 * pc_guest_profile_select) remain the source of the legacy flow. */
#ifndef PC_SESSION_H
#define PC_SESSION_H

#include <stddef.h>
#include <stdint.h>
#include "pc_character.h"
#include "pc_mp_guests.h"
#include "pc_servers.h"

#ifdef __cplusplus
extern "C" {
#endif

enum { PC_SESSION_JOIN_NONE = 0, PC_SESSION_JOIN_GUEST = 1, PC_SESSION_JOIN_RESIDENT = 2 /* reserved for M2b */ };

typedef struct PCConnectSession {
    int         role;                 /* 0 none, 1 host, 2 client (pc_main g_pc_net_role) */
    char        host[96];
    int         port;
    char        server_name[48];      /* M3 */
    int         join_kind;            /* PC_SESSION_JOIN_* */
    int         storage;              /* PC_CHARACTER_STORAGE_NONE | LEGACY | STORE */
    int         creating;             /* a NEW store character is being created in the Rover scene (nothing written yet) */
    PCCharacter character;            /* valid when storage != NONE */
    char        guest_spec[96];       /* the --bootstrap-guest spec built from the character */
    uint8_t     resident_pid[20];     /* M-C: join_kind == RESIDENT: the PersonalID (20 BE bytes) of the resident this character plays in the fetched town */
    char        town_key[PC_CHARACTER_TOWNKEY_LEN + 1]; /* M-C: the townkey of the fetched / READY town ("" until known) */
} PCConnectSession;

PCConnectSession* pc_session(void);

/* Token path selection per host town: STORE character -> characters/<uuid>/towns/<townkey>/token.dat (installed as the pc_guest_token_path() override);
 * LEGACY / none -> nothing changes (pc_guest_token_path() stays the legacy file). Returns 1 iff the active token path CHANGED (the caller reloads). */
int pc_session_select_town(const uint8_t land_name[8], uint16_t land_id, uint32_t terrain_hash);

/* READ-ONLY lookup of (town, home pid) in the character's LEGACY token file (when it has a legacy_profile). 1 = found (*out filled). */
int pc_session_legacy_token_lookup(const uint8_t land_name[8], uint16_t land_id, uint32_t terrain_hash, const uint8_t home_pid[20], PCMpGtkEntry* out);

/* M3: fills host / port / server_name from a saved server profile (a destination only; the character is independent of it). */
void pc_session_apply_server(const PCServer* s);

/* M-C: called ONCE per client connection when the host handshake reaches READY. Records client-side metadata only: servers.ini last_town (a UI hint) for the saved server of
 * host:port, and - for a STORE character - characters/<uuid>/towns/<townkey>/membership.ini role=guest|resident with town_pid = `home_pid` (20 BE bytes). An existing
 * resident membership is never downgraded to guest; a matching one is not rewritten. Nothing here is sent anywhere or decides anything for the host. */
void pc_session_note_ready(const uint8_t land_name[8], uint16_t land_id, uint32_t terrain_hash, const uint8_t home_pid[20], int resident);

/* Writes the first-run creation result as characters/<uuid>/character.ini (create-only). 1 = created, 0 = exists, -1 = error. */
int pc_session_store_create_finish(const char* name, int gender, int face, char* err, size_t errcap);

#ifdef __cplusplus
}
#endif
#endif
