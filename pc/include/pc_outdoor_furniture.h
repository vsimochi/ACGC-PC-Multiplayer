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
#define PC_OFURN_OP_MOVE   4 /* C2: dest NONE, slot 0, item = the tile value the client sees, flags = direction 0..3 (S/E/N/W the piece moves) | PC_OFURN_MOVE_PULL,
                              * aux_item = the ORIGIN tile; the piece moves ONE tile (sent only to a host that announced C2 on message 102) */
#define PC_OFURN_OP_MAX    4
#define PC_OFURN_MOVE_PULL 0x04u /* flags bit of MOVE: the player pulls (walks backwards, away from the piece); 0 = push */

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

/* ---- C2 footprints (multi-tile furniture). The cells follow the INDOOR engine exactly (ac_my_room.c aMR_SetInfoFurnitureTable / aMR_GetTypeBPlaceInfo with
 * shape_type = l_typeB0_table[rot]): 1x1 = the origin; 2x1 = the origin + one cell: rot 0 (TYPEB_0) +x, rot 1 (TYPEB_90) -z, rot 2 (TYPEB_180) -x, rot 3 (TYPEB_270)
 * +z (the second cell swings around the origin when the piece rotates); 2x2 = origin, +x, +z, +x+z for every rotation. The MODEL position is the one of
 * aMR_UnitNumber2Position: the origin cell centre (1x1, 2x1), the shared corner of the 4 cells (2x2). The ORIGIN tile holds the item id | rotation (an old client:
 * a leaf), the other cells hold PC_OFURN_FILLER. ---- */
#define PC_OFURN_FP_1X1      0
#define PC_OFURN_FP_2X1      1
#define PC_OFURN_FP_2X2      2
#define PC_OFURN_FP_NUM      3
#define PC_OFURN_FP_MAX_CELLS 4
/* the extra (non-origin) footprint cells of a placed multi-tile piece: RSV_FE1F (m_name_table.h), a STABLE value for the field diff (pc_field_authority.c: never
 * withheld, unlike RSV_NO), never pickable / droppable-onto / buryable, drawn as nothing (see the C2 doc for the vanilla evidence) */
#define PC_OFURN_FILLER      0xFE1Fu

typedef struct PCOfurnMarker {
    uint8_t  ut_x, ut_z;    /* town-scene unit coordinates (16..95, 16..111) of the ORIGIN cell */
    uint8_t  rot;           /* 0..3 = S / E / N / W (== the item's low 2 bits) */
    uint8_t  placer_kind;   /* PC_OFURN_PLACER_* (0 = unknown, a client mirror) */
    uint16_t base;          /* furniture item id with the rotation bits cleared */
    uint8_t  shape;         /* C2: PC_OFURN_FP_* (sidecar v1 records hold 0 here = 1x1: the v1 -> v2 conversion is the identity) */
    uint8_t  flags;         /* C2: 0 (reserved) */
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

/* ---- C2 footprint geometry (pure) ---- */
int  pc_ofurn_fp_from_shape(int aftr_shape);   /* aFTR_SHAPE_TYPEB_* (0..3) -> 2X1, TYPEA (4) -> 1X1, TYPEC (5) -> 2X2, else -1 */
int  pc_ofurn_fp_cells(int fp, int rot, int ox, int oz, int* xs, int* zs); /* the cells (origin first); returns the count (1 / 2 / 4), 0 = bad fp */
int  pc_ofurn_marker_cells(const PCOfurnMarker* m, int* xs, int* zs);
int  pc_ofurn_fp_covers(int fp, int rot, int ox, int oz, int x, int z);
int  pc_ofurn_fp_in_town(int fp, int rot, int ox, int oz);                /* 1 = every cell is a town FG tile (pc_ofurn_acre_of >= 0) */
int  pc_ofurn_find_cell(const PCOfurnTable* t, int x, int z);             /* the marker whose footprint covers (x, z), or -1 */
int  pc_ofurn_fp_overlaps(const PCOfurnTable* t, int fp, int rot, int ox, int oz, int skip_idx); /* 1 = a cell is covered by another marker (skip_idx excluded) */
void pc_ofurn_fp_model_pos(int fp, int ox, int oz, float* wx, float* wz); /* the model position (aMR_UnitNumber2Position) */
int  pc_ofurn_fp_reach_ok(int fp, int rot, int ox, int oz, int pux, int puz); /* 1 = the player tile is an 8-neighbour of a cell and on none of them */
void pc_ofurn_dir_delta(int dir, int* dx, int* dz);                       /* 0 S (+z), 1 E (+x), 2 N (-z), 3 W (-x) */
/* MOVE geometry: the new origin of a one-tile move and the player's follow tile; 1 = legal shape (the player is orthogonally next to the footprint on the right side:
 * PUSH = behind the piece (the move goes away from the player), PULL = in front of it (the player steps back to *fx, *fz, which must not be a new cell)) */
int  pc_ofurn_move_geometry(int fp, int rot, int ox, int oz, int dir, int pull, int pux, int puz, int* nox, int* noz, int* fx, int* fz);
/* the 1 / 2 / 4 cells of the NEW footprint that the OLD one does not cover (the cells a MOVE / ROTATE must validate); returns the count */
int  pc_ofurn_fp_new_cells(int fp, int rot0, int ox0, int oz0, int rot1, int ox1, int oz1, int* xs, int* zs);

/* ---- sidecar (outdoor_furniture.dat): u32 magic 'OFUR', u32 version, u32 count, count x 52-byte markers, u32 FNV-1a of everything before it; atomic tmp -> rename.
 * Version 1 (C1, 0.3.0): 1x1 only, PCOfurnMarker.shape/flags were a zero u16. Version 2 (C2): the same 52-byte record with shape / flags meaningful. The loader takes
 * both (v1 records are 1x1 = shape 0: lossless); the host then keeps a <path>.v1.bak copy and rewrites the file as v2. A 0.3.0 host refuses a v2 file as corrupt
 * (its placed pieces stay leaves, Place is refused) -- never misreads a multi-tile marker as a 1x1 one. ---- */
#define PC_OFURN_FILE_MAGIC   0x5255464Fu /* "OFUR" little endian */
#define PC_OFURN_FILE_VERSION 2u
#define PC_OFURN_FILE_VERSION_C1 1u
enum { PC_OFURN_FILE_BAD = -1, PC_OFURN_FILE_MISSING = 0, PC_OFURN_FILE_OK = 1 };
int pc_ofurn_save(const char* path, const PCOfurnTable* t);           /* 1 = written (v2) + renamed, 0 = failed (old file untouched) */
/* PC_OFURN_FILE_*; *dropped = records refused (bad tile / not furniture / bad shape / a footprint outside the town / overlapping footprints); *version = the file's
 * version (1 or 2) on OK */
int pc_ofurn_load(const char* path, PCOfurnTable* t, int* dropped, int* version);
int pc_ofurn_copy_file(const char* from, const char* to);             /* 1 = copied (the .v1.bak of a converted sidecar) */

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

/* ---- C2: message 102 OUTDOOR_FURN2 (host -> one READY client, RELIABLE, the SAME 268-byte PCOfurnMsg layout with msg_type 102). Why a new id and not new kinds /
 * flags in 98: a 0.3.0 client takes 98 only at exactly 268 bytes and its pc_ofurn_msg_valid() refuses any unknown kind / PERMS bit (the message is dropped whole),
 * and it would draw a multi-tile entry as a 1x1 model on the origin with a 1x1 collision. So 98 keeps carrying the 1x1 pieces ONLY (unchanged for 0.3.0 clients: a
 * multi-tile piece is a plain leaf on its origin + invisible filler tiles for them), and 102 carries EVERY piece (1x1 too) for C2 clients, which use the 102 list as
 * their view once the host's CAPS arrived (the 98 list stays the view of a C1 host). A 0.3.0 client ignores id 102 (the dispatcher's 'unrecognized: ignore').
 * Kinds: BEGIN / CHUNK / END / SET / DEL as in 98; 7 CAPS (count 0, flags = PC_OFURN_C2_*; sent first, at join, before the list: a client sends MOVE / a multi-tile
 * PLACE only after it); 8 MOVE (count 2: e[0] = the old origin, e[1] = the new origin + item: a one-tile move or a rotation of the same piece, so the client keeps
 * the model and slides it). The shape of an entry is derived from its item (the furniture profile), never sent. ---- */
#define PC_OFURN_MSG2_ID  102u
#define PC_OFURN_MK_CAPS  7
#define PC_OFURN_MK_MOVE  8
#define PC_OFURN_C2_MULTI 0x01u /* multi-tile placement (2x1 / 2x2) */
#define PC_OFURN_C2_MOVE  0x02u /* kind 22 op 4 MOVE (grab / push / pull) */
#define PC_OFURN_C2_ALL   (PC_OFURN_C2_MULTI | PC_OFURN_C2_MOVE)
int pc_ofurn_msg2_valid(const PCOfurnMsg* m);

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
/* 1 = (ut_x, ut_z) is a cell of a placed piece the VIEW knows and its origin tile still holds it (C2: any footprint cell) */
int  pc_net_game_ofurn_placed_at(int ut_x, int ut_z);
/* ---- C2 seams (grab / push / pull / rotate, pc_ofurn_draw.c) ---- */
int      pc_net_game_ofurn_marker_at(int ut_x, int ut_z, PCOfurnMarker* out);           /* the view's marker covering the cell (1 = found) */
int      pc_net_game_ofurn_cells_free(const PCOfurnMarker* mk, int rot, int nox, int noz); /* local pre-check of a rotate / move (the host decides) */
int      pc_net_game_ofurn_may_move(int* rotate_only);                                   /* 1 = may grab; *rotate_only = 1 against a C1 host */
int      pc_net_game_ofurn_rotate_to(int ut_x, int ut_z, int rot);                       /* 1 begun / done, 0 refused, -1 busy */
int      pc_net_game_ofurn_move(int ut_x, int ut_z, int dir, int pull);                  /* 1 begun / done, 0 refused, -1 busy */
uint32_t pc_net_game_ofurn_op_serial(void);                                              /* the handle of the op begun last */
int      pc_net_game_ofurn_op_outcome(uint32_t serial, int* reason);                     /* 0 pending, 1 applied, 2 refused / superseded */
/* a recent MOVE of the piece now at (ux, uz) holding item: its old model pose (consumed); the renderer slides the new model from there */
int      pc_net_game_ofurn_take_slide(int ut_x, int ut_z, int item, float* x, float* z, int* rot, uint32_t* age_ms);
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
