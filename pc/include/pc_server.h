#ifndef PC_SERVER_H
#define PC_SERVER_H

/* pc_server.h - the DEDICATED SERVER's own storage tree (servers/<server-id>/), separate from the client tree (save/).
 *
 *   servers/<server-id>/
 *       server.ini                    [server] id / name / port / created, [town] land_name / land_id / terrain_hash / key / origin (the server's AUTHORITATIVE town identity)
 *       town/card_a/DobutsunomoriP_MURA.gci   the authoritative town (Card A of the dedicated process: pc_card_set_town_dir(servers/<id>/town))
 *       residents/members.dat         resident credentials (PCMpMemberFile) + the promotion handoff entries
 *       residents/records.dat         the per-resident record lineage sidecar (D3)
 *       citizens/guests.dat           the guest table (guest key -> token + saved record); characters are PLAYER-owned (save/mp/characters/<uuid>), the server only keeps what it
 *                                     holds ABOUT a guest it admitted, never a copy of a character
 *       backups/                      reserved for authoritative server backups (the guest / member / record files still put their own .bak-<time> copies next to themselves)
 *       logs/server.log               the server's own log (-logfile default for a dedicated host, plus the [DEDICATED] notices)
 *       shop_restock.ini              the running manual shop restock (host restart resume)
 *
 * SERVER IDENTITY is the directory name <server-id> ([A-Za-z0-9_-], 1..32, always "default" for now: one server id = one persistent town, no command-line option): it is stable and says nothing about the town. The TOWN identity is the
 * game's own PCTownId (land_name, land_id, terrain_hash; its text form is the townkey) and lives in server.ini [town]; the townkey is NOT the directory name. The same PCTownId /
 * townkey keeps being the identity of client caches (save/mp/towns/<townkey>/, origin.ini) and of membership.ini: it is the TOWN identity there, the CACHE identity of a client, and in
 * this tree only a metadata value.
 *
 * Only a --dedicated host uses this tree (and not with --town-dir, which keeps the legacy layout: Card A at DIR/card_a, host sidecars in save/mp). Everything else (a client, a
 * single-player / GUI host) is unchanged. Pure C + libc + pc_town_cache. */
#include <stddef.h>
#include <stdint.h>
#include "pc_town_cache.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PC_SERVER_ROOT       "servers"
#define PC_SERVER_DEFAULT_ID "default"
#define PC_SERVER_ID_MAX     32

int pc_server_id_valid(const char* id);

/* Opens (creates when missing) servers/<id>/ with its subdirectories + server.ini, selects its town directory as Card A (pc_card_set_town_dir) and decides the first-launch state.
 * Legacy adoption: for the DEFAULT id, a server that never had a town and a valid legacy save/card_a/DobutsunomoriP_MURA.gci -> the legacy town (and the legacy host sidecars
 * save/mp/{guests,members,records}.dat) is COPIED into the tree (originals untouched, nothing overwritten); for any other id a new server generates its own town.
 * Returns 1 on success, 0 with a message in err (the caller exits 2; nothing of an existing town was touched). */
int pc_server_open(const char* id, uint16_t port, char* err, size_t errcap);

int         pc_server_active(void);            /* 1 once pc_server_open succeeded in this process */
const char* pc_server_id(void);
const char* pc_server_dir(void);               /* servers/<id> */
const char* pc_server_town_dir(void);          /* servers/<id>/town */
int         pc_server_town_missing(void);      /* 1 = first launch: no authoritative town exists yet, the host generates one (pc_m_card.c pc_host_observer_poll) */
int         pc_server_adopted_legacy(void);    /* 1 = the town of this process was copied from the legacy save/card_a */
const char* pc_server_log_path(void);

/* Host-owned file paths: servers/<id>/... while a server is active, otherwise the legacy save/mp/... defaults (PC_MP_*_PATH). Never NULL. */
const char* pc_server_guests_path(void);
const char* pc_server_members_path(void);
const char* pc_server_records_path(void);
const char* pc_server_restock_path(void);

/* The NAME of a town this server is about to generate (first launch only). The dedicated console asks for it (pc_dedicated.c pc_dedicated_prompt_town_name) and hands it to the
 * game's own new-town initialiser. The game's town-name field takes 1..8 characters of the keyboard editor's charset (pc_typing.c pc_utf8_to_game_code: letters, digits and the
 * punctuation the font has, accented letters): pc_server_town_name_encode() validates exactly that and returns the 8 game character codes (space padded). 1 = valid. A name is
 * never truncated or altered; leading / trailing spaces are invalid. */
int  pc_server_town_name_encode(const char* utf8, uint8_t out[8], char* err, size_t errcap);
void pc_server_set_town_name(const uint8_t codes[8]);
void pc_server_town_name_codes(uint8_t out[8]); /* the chosen name, else the fallback "Village" (stdin closed at the prompt / no console) */
void pc_server_town_name_text(const uint8_t codes[8], char out[24]); /* game codes -> text for the console ('?' for a code outside ASCII); trailing spaces dropped */

/* The authoritative town identity is known (the host loaded / generated its town). First time: records it in server.ini [town]. Later: compares. Returns 1 = ok / recorded,
 * 0 = server.ini names ANOTHER town (land_name / land_id differ): the caller must not serve it. A terrain_hash difference only logs. generated = 1 when this process generated it. */
int pc_server_note_town(const PCTownId* id, int generated);

/* Appends one line (with a time stamp) to servers/<id>/logs/server.log; a no-op without an active server. */
void pc_server_log(const char* fmt, ...);

#ifdef __cplusplus
}
#endif
#endif
