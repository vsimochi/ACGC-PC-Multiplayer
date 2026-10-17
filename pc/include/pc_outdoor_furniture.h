/* pc_outdoor_furniture.h - OUTDOOR FURNITURE (stage C1), the PURE logic (no game state; native selftest pc_ofurn_selftest()).
 * docs/multiplayer-guest-roadmap.md "Outdoor furniture (C1) and the operator list".
 *
 * Two actions on a furniture item outdoors:
 *   DROP  = the vanilla leaf drop (anyone; unchanged; a host may refuse it with outdoor_furniture_leaf_drop = 0).
 *   PLACE = residents only: the piece stands on the tile as its real model, rotated, with collision.
 * SOURCE OF TRUTH (design "C", hybrid): the town field tile keeps the furniture item id with the rotation in its low 2 bits (synced and saved like every tile: an
 * old client simply sees a leaf). The HOST keeps a sidecar of PLACED markers (outdoor_furniture.dat: tile, base id, rotation, placer identity + name, time); a marker
 * is valid only while its tile still holds exactly base | rotation (sweeps, pickups, new structures: the marker is garbage-collected).
 * Wire (protocol v8 extended in place): TXN_COMMIT kind 22 OUTDOOR_FURNITURE (ops PLACE / ROTATE / PICKUP), message 98 OUTDOOR_FURN (marker list + deltas + the
 * peer's permissions), town service 10 OFURN_CAPS (the host supports kind 22). */
#ifndef PC_OUTDOOR_FURNITURE_H
#define PC_OUTDOOR_FURNITURE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- kind 22 ops (tag.aux_cond) ---- */
#define PC_OFURN_OP_PLACE  1 /* dest NONE, slot = the pocket slot holding the item, item = the pocket item, flags = rotation 0..3, aux_item = tile (ut_z << 8 | ut_x) */
#define PC_OFURN_OP_ROTATE 2 /* dest NONE, slot 0, item = the tile value the client sees, flags = the NEW rotation, aux_item = tile */
#define PC_OFURN_OP_PICKUP 3 /* dest POCKET, slot = a FREE pocket slot, item = the tile value the client sees, flags 0, aux_item = tile */
#define PC_OFURN_OP_MAX    3

/* ---- reasons (PC_NETGAME_TXN_REASON_* 55..61, allocated to this block) ---- */
#define PC_OFURN_REASON_OFF         55 /* the host has the Place action off (AC_MP_OUTDOOR_FURNITURE=0 / outdoor_furniture_place = 0) */
#define PC_OFURN_REASON_NOT_ALLOWED 56 /* a guest places / picks up / rotates a placed piece (residents, operators and the host only) */
#define PC_OFURN_REASON_TOO_BIG     57 /* the furniture is not 1x1 ("too big to place outside"; it can still be dropped as a leaf) */
#define PC_OFURN_REASON_BAD_TILE    58 /* the tile is occupied / not placeable / out of reach / next to a building */
#define PC_OFURN_REASON_LIMIT       59 /* the technical safety ceiling (outdoor_furniture_max_town / _max_acre) */
#define PC_OFURN_REASON_RATE        60 /* anti-spam rate limit (generous) */
#define PC_OFURN_REASON_GONE        61 /* the placed piece is no longer there / changed (ROTATE / PICKUP) */

/* ---- safety ceiling (NOT a gameplay limit: protects the renderer / collision / save), configurable in settings.ini [Network] ---- */
#define PC_OFURN_TOWN_MAX_DEFAULT 512
#define PC_OFURN_ACRE_MAX_DEFAULT 128
#define PC_OFURN_TABLE_CAP        1024 /* hard table size: outdoor_furniture_max_town is clamped to it */

/* ---- placer kinds (== PC_OPS_KIND_*) ---- */
#define PC_OFURN_PLACER_RESIDENT 1
#define PC_OFURN_PLACER_GUEST    2
#define PC_OFURN_PLACER_HOST     3 /* the listen host's own player / a console placement */

#define PC_OFURN_KEY_LEN  20
#define PC_OFURN_NAME_MAX 20

typedef struct PCOfurnMarker {
    uint8_t  ut_x, ut_z;    /* town-scene unit coordinates (16..95, 16..111) */
    uint8_t  rot;           /* 0..3 = S / E / N / W (== the item's low 2 bits) */
    uint8_t  placer_kind;   /* PC_OFURN_PLACER_* (0 = unknown, a client mirror) */
    uint16_t base;          /* furniture item id with the rotation bits cleared */
    uint16_t _rsv;
    uint32_t placed_time;   /* unix seconds (host clock) */
    uint8_t  placer_key[PC_OFURN_KEY_LEN];
    char     placer_name[PC_OFURN_NAME_MAX];
} PCOfurnMarker; /* 52 bytes; the sidecar stores exactly this layout */

typedef struct PCOfurnTable {
    int           n;
    uint32_t      gen;  /* +1 per change */
    PCOfurnMarker m[PC_OFURN_TABLE_CAP];
} PCOfurnTable;

void     pc_ofurn_table_init(PCOfurnTable* t);
int      pc_ofurn_find(const PCOfurnTable* t, int ut_x, int ut_z);    /* index or -1 */
int      pc_ofurn_add(PCOfurnTable* t, const PCOfurnMarker* m);       /* 1 = added / replaced the one on that tile, 0 = table full */
int      pc_ofurn_remove_at(PCOfurnTable* t, int idx);                /* 1 = removed (the last entry moves into idx) */
int      pc_ofurn_count_acre(const PCOfurnTable* t, int acre);
uint16_t pc_ofurn_tile_value(const PCOfurnMarker* m);                 /* base | rot */
int      pc_ofurn_acre_of(int ut_x, int ut_z);                        /* 0..29 (az * 5 + ax) or -1 (not a town FG tile) */
/* the item ids the system knows: FTR0 0x1000-0x1FFF / FTR1 0x3000-0x3FFF (low 2 bits = rotation) */
int      pc_ofurn_is_ftr(uint16_t item);
uint16_t pc_ofurn_base(uint16_t item);
/* 0 ok, 1 = the town ceiling, 2 = the acre ceiling */
int      pc_ofurn_caps_check(const PCOfurnTable* t, int acre, int max_town, int max_acre);

/* ---- sidecar (outdoor_furniture.dat): u32 magic 'OFUR', u32 version 1, u32 count, count x 52-byte markers, u32 FNV-1a of everything before it; atomic tmp -> rename ---- */
#define PC_OFURN_FILE_MAGIC   0x5255464Fu /* "OFUR" little endian */
#define PC_OFURN_FILE_VERSION 1u
enum { PC_OFURN_FILE_BAD = -1, PC_OFURN_FILE_MISSING = 0, PC_OFURN_FILE_OK = 1 };
int pc_ofurn_save(const char* path, const PCOfurnTable* t);           /* 1 = written + renamed, 0 = failed (old file untouched) */
int pc_ofurn_load(const char* path, PCOfurnTable* t, int* dropped);   /* PC_OFURN_FILE_*; *dropped = records refused (bad tile / not furniture / duplicates) */

/* ---- message 98 OUTDOOR_FURN (host -> client, RELIABLE, fixed 268 bytes) ---- */
#define PC_OFURN_MSG_ID      98u
#define PC_OFURN_MSG_ENTRIES 32
#define PC_OFURN_MK_BEGIN 1 /* a full list follows: total = the count, gen = the host table generation */
#define PC_OFURN_MK_CHUNK 2 /* count entries of the list */
#define PC_OFURN_MK_END   3 /* the list is complete (the client swaps it in) */
#define PC_OFURN_MK_SET   4 /* delta: these markers exist now (upsert) */
#define PC_OFURN_MK_DEL   5 /* delta: these tiles hold no marker any more */
#define PC_OFURN_MK_PERMS 6 /* this peer's rights: flags = PC_OFURN_PERM_* */
#define PC_OFURN_PERM_PLACE     0x01u /* may Place (a resident, Place on) */
#define PC_OFURN_PERM_PICKUP    0x02u /* may Pick up / Rotate a placed piece (resident / operator / host) */
#define PC_OFURN_PERM_OP        0x04u /* is an operator */
#define PC_OFURN_PERM_LEAF_DROP 0x08u /* the host accepts furniture leaf drops */
typedef struct PCOfurnWireEntry {
    uint8_t  ut_x, ut_z;
    uint16_t item;        /* base | rot */
    uint32_t placed_time;
} PCOfurnWireEntry;
typedef struct PCOfurnMsg {
    uint8_t  msg_type;  /* 98 */
    uint8_t  kind;      /* PC_OFURN_MK_* */
    uint8_t  count;     /* entries used (<= 32) */
    uint8_t  flags;     /* PERMS: PC_OFURN_PERM_* */
    uint32_t gen;
    uint32_t total;
    PCOfurnWireEntry e[PC_OFURN_MSG_ENTRIES];
} PCOfurnMsg;
int pc_ofurn_msg_valid(const PCOfurnMsg* m); /* structural check of a received message */

/* ---- town service 10 OFURN_CAPS (8 bytes): u8 version 1, u8 flags (bit 0 Place on, bit 1 leaf drop on), u16 LE max_town, u16 LE max_acre, u16 0 ---- */
#define PC_OFURN_CAPS_LEN     8u
#define PC_OFURN_CAPS_VERSION 1u
void pc_ofurn_caps_build(uint8_t* blob, int place_on, int leaf_on, int max_town, int max_acre);
int  pc_ofurn_caps_valid(const uint8_t* blob);

/* ---- the kind 22 tag shape (1 = legal); dest 0 NONE / 1 POCKET ---- */
int pc_ofurn_shape_ok(int op, unsigned dest, unsigned slot, unsigned item, unsigned flags, unsigned aux_item, unsigned pocket_count);

/* ---- geometry (40-unit tiles; angle = the s16 binangle, 0 = facing +z / south) ---- */
int  pc_ofurn_dir4(int angle_s16);                 /* 0 +z (S), 1 +x (E), 2 -z (N), 3 -x (W) */
int  pc_ofurn_rot_for_facing(int angle_s16);       /* the piece faces the player who places it: (dir4 + 2) & 3 */
void pc_ofurn_front_tile(float px, float pz, int angle_s16, int* ut_x, int* ut_z); /* the orthogonal tile in front of a player at (px, pz) */
int  pc_ofurn_reach_ok(int player_ut_x, int player_ut_z, int ut_x, int ut_z);       /* 1 = one of the 8 neighbours of the player's tile (never the tile itself) */

/* ---- anti-spam (generous): at least min_gap_ms between ops and at most per_min ops per rolling minute ---- */
typedef struct PCOfurnRate {
    uint32_t last_ms, win_ms;
    int      win_n, used;
} PCOfurnRate;
int pc_ofurn_rate_ok(PCOfurnRate* r, uint32_t now_ms, uint32_t min_gap_ms, int per_min); /* 1 = allowed (counted), 0 = refused */

/* ---- client notice line (pc_pause_menu.c draws it for ~4 s) ---- */
void        pc_ofurn_notice_set(const char* text, uint32_t now_ms);
const char* pc_ofurn_notice_get(uint32_t now_ms); /* NULL when none */
const char* pc_ofurn_reason_text(int reason);      /* the player-facing line of a refusal */

int pc_ofurn_selftest(void); /* 0 = pass, else the number of failures */

/* ---- game-side seams (pc_ofurn_draw.c / pc_net_game.c; TARGET_PC game code only) ---- */
/* the VIEW table the renderer draws (host: the authoritative table, client: the mirror of message 98); NULL when the feature is not active */
const PCOfurnTable* pc_net_game_ofurn_view(void);
uint32_t pc_net_game_ofurn_view_gen(void);
/* the tag menu: 1 = offer "Place" for this pocket item outdoors (a resident of a host with Place on / the listen host), 0 = vanilla menu only */
int  pc_net_game_ofurn_place_offer(int item);
/* the tag menu's "Place": 1 = requested / done (close the menu), 0 = refused (the notice says why) */
int  pc_net_game_ofurn_place(int pocket_slot, int item, int ut_x, int ut_z, int rot);
/* A in front of a placed piece: rotate it by 90 degrees (1 = handled: the A press is consumed) */
int  pc_net_game_ofurn_rotate(int ut_x, int ut_z);
/* 1 = this process may pick up / rotate a placed piece (client: the host's PERMS; host: always) */
int  pc_net_game_ofurn_may_pickup(void);
/* 1 = the host accepts a furniture LEAF drop outdoors (client: caps / PERMS; host: settings) */
int  pc_net_game_ofurn_leaf_drop_ok(void);
/* B in front of a placed piece: pick it up (1 = handled: the vanilla pickup is skipped this frame) */
int  pc_net_game_ofurn_pickup(int ut_x, int ut_z);
/* 1 = (ut_x, ut_z) holds a placed piece the VIEW knows and its tile still holds it */
int  pc_net_game_ofurn_placed_at(int ut_x, int ut_z);
/* the notice line to draw now (NULL = none) / show a line (~4 s) */
const char* pc_net_game_ofurn_notice(void);
void        pc_net_game_ofurn_notice_text(const char* text);
/* HOST: the reusable operator check of a READY peer (ops.ini; the listen host's own player is implicitly an operator) */
int  pc_net_game_peer_is_op(int peer);
/* dedicated console: op 0 = `ops` (list), 1 = `op <player>`, 2 = `deop <player>`; 1 = done, 0 = refused (msg says why) */
int  pc_net_game_dedicated_ops(int op, const char* sel, char* msg, int cap);
/* dedicated console: `furniture list [acre]` / `furniture remove ...` / `furniture clear ...` (one call per output line) */
void pc_net_game_dedicated_furniture(const char* args, void (*out)(const char*));

/* ---- ac_my_room.c (TARGET_PC): one furniture model WITHOUT the room actor (catalog-style; never the move_proc) ---- */
#define PC_OFURN_SHAPE_1X1 4 /* == aFTR_SHAPE_TYPEA (asserted in ac_my_room.c) */
int  aMR_PC_FtrItemShape(int item); /* aFTR_SHAPE_* or -1 */
int  aMR_PC_StandaloneDrawable(int item);
int  aMR_PC_StandaloneBankSize(void);
int  aMR_PC_StandaloneMoveBgType(int item); /* mCoBG_FTR_TYPE* or -1 = no collision */

#ifdef __cplusplus
}
#endif
#endif
