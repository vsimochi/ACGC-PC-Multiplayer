/* pc_ops.h - the host's OPERATOR LIST ("ops", like Minecraft ops), pure logic (no game state; native selftest pc_ops_selftest()).
 *
 * An operator is identified by its AUTHENTICATED identity, the same key the host binds a connection to, never by a display name alone:
 *   kind 1 RESIDENT: the host save's PersonalID of that resident (name, town name, player id, land id)
 *   kind 2 GUEST:    the guest key (the guest's HOME PersonalID) the guest table binds the connection to
 * The 20-byte key is the canonical form the membership layer uses: player_name[8] + land_name[8] + player_id (big endian) + land_id (big endian).
 *
 * File (servers/<id>/ops.ini on a dedicated server, save/mp/ops.ini on a listen host), text, tolerant parser, written atomically (tmp -> rename):
 *   # comments
 *   version = 1
 *   op = resident <40 hex digits> <display name (informational only)>
 *   op = guest <40 hex digits> <display name>
 * Unknown keys, malformed lines and unknown kinds are skipped (counted), never fatal. A later `version` is read as far as it is understood. */
#ifndef PC_OPS_H
#define PC_OPS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_OPS_MAX       64
#define PC_OPS_KEY_LEN   20
#define PC_OPS_NAME_MAX  24
#define PC_OPS_VERSION   1

#define PC_OPS_KIND_RESIDENT 1
#define PC_OPS_KIND_GUEST    2

enum { PC_OPS_BAD = -1, PC_OPS_MISSING = 0, PC_OPS_OK = 1 };

typedef struct PCOpsEntry {
    uint8_t kind;                 /* PC_OPS_KIND_* */
    uint8_t key[PC_OPS_KEY_LEN];  /* canonical identity */
    char    name[PC_OPS_NAME_MAX]; /* ASCII display name at the time of `op` (informational) */
} PCOpsEntry;

typedef struct PCOpsList {
    int        n;
    uint32_t   gen; /* +1 per change (add / remove / load) */
    PCOpsEntry e[PC_OPS_MAX];
} PCOpsList;

void pc_ops_init(PCOpsList* l);
int  pc_ops_find(const PCOpsList* l, int kind, const uint8_t key[PC_OPS_KEY_LEN]);                  /* index or -1 */
int  pc_ops_add(PCOpsList* l, int kind, const uint8_t key[PC_OPS_KEY_LEN], const char* name);      /* 1 added, 0 already an op, -1 list full / bad kind */
int  pc_ops_remove(PCOpsList* l, int kind, const uint8_t key[PC_OPS_KEY_LEN]);                     /* 1 removed, 0 not an op */
/* parses `text` (len bytes, need not be NUL-terminated) into `l` (replacing it). *bad_lines (optional) = lines skipped. Returns the number of entries. */
int  pc_ops_parse(PCOpsList* l, const char* text, size_t len, int* bad_lines);
/* formats `l` as the file text; returns the length (excluding the NUL) or -1 when `cap` is too small. */
int  pc_ops_format(const PCOpsList* l, char* out, size_t cap);
int  pc_ops_load(const char* path, PCOpsList* l, int* bad_lines); /* PC_OPS_OK / PC_OPS_MISSING (empty list) / PC_OPS_BAD (unreadable: empty list) */
int  pc_ops_save(const char* path, const PCOpsList* l);           /* 1 = written + renamed, 0 = failed (the old file is untouched) */
const char* pc_ops_kind_name(int kind);
int  pc_ops_selftest(void); /* 0 = pass, else the number of failures */

#ifdef __cplusplus
}
#endif
#endif
