/* pc_roommates.h - ROOMMATES / MOVE-IN (docs/multiplayer-guest-roadmap.md "Roommates / move-in"), pure logic (no game state; native selftest pc_room_selftest()).
 *
 * A GUEST (a player without a resident slot: player_no >= 4, its own .gst record) asks a resident to move in at that resident's GYROID placed outdoors. The resident (the
 * MAIN house holder) accepts or denies the request at the same gyroid and can later remove the roommate again. A roommate keeps its OWN identity, pockets, wallet and
 * record; only the house is shared (it may enter the holder's house, see the live room and use its dresser storage). Nothing here touches the town save.
 *
 * Identities are the 20-byte canonical keys of the membership layer (player_name[8] + land_name[8] + player_id BE + land_id BE): the resident's PersonalID (the house
 * holder) and the guest key (the guest's HOME PersonalID that guests.dat binds a connection to).
 *
 * The host keeps the relations in roommates.ini next to ops.ini (servers/<id>/ on a dedicated server, save/mp/ on a listen host): versioned text, tolerant parser,
 * written atomically (tmp -> replace) with the previous file kept as roommates.ini.bak:
 *   # comments
 *   version = 1
 *   room = <state> <owner key: 40 hex> <guest key: 40 hex> <unix time> <guest note> <owner note> <owner name: 16 hex> <guest name: 16 hex>
 * Unknown keys / malformed lines are skipped (counted); a later version is read as far as it is understood (extra trailing fields are ignored).
 *
 * Message 104 ROOMMATE (PCRoomMsg, fixed 300 bytes, RELIABLE; protocol v8 extended in place): client -> host requests (QUERY / REQUEST / CANCEL / ACCEPT / DENY / REMOVE /
 * LEAVE, always naming the gyroid's origin tile + tile value), host -> client answers (INFO / RESULT) and pushes (STATUS: "you are a roommate of house h", NOTICE: the other
 * party's decision, also delivered at the next join when it happened offline). A client sends 104 only after the host's 102 CAPS carried PC_OFURN_C4_ROOMMATES. */
#ifndef PC_ROOMMATES_H
#define PC_ROOMMATES_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_ROOM_KEY_LEN      20
#define PC_ROOM_NAME_LEN     8   /* raw game-charset bytes of a player name */
#define PC_ROOM_MAX          64  /* relations + tombstones of the whole host */
#define PC_ROOM_MAX_ROOMMATES 4  /* roommates per house holder */
#define PC_ROOM_MAX_PENDING  4   /* open requests per house holder */
#define PC_ROOM_VERSION      1
#define PC_ROOM_DENY_COOLDOWN_S 600u /* a denied / removed guest may ask the same holder again after 10 minutes (persisted with the tombstone) */
#define PC_ROOM_REQUEST_GAP_MS 15000u /* host memory: one request per guest per 15 s (spam limit) */

/* entry states */
#define PC_ROOM_ST_PENDING  1 /* the guest asked, the holder has not decided */
#define PC_ROOM_ST_ROOMMATE 2 /* accepted: the guest shares the holder's house */
#define PC_ROOM_ST_DENIED   3 /* tombstone: carries the guest's notice + the re-request cooldown */
#define PC_ROOM_ST_REMOVED  4 /* tombstone: removed by the holder / moved out / the house is gone */

/* notices waiting for the GUEST (gnote) */
#define PC_ROOM_GN_NONE     0
#define PC_ROOM_GN_ACCEPTED 1
#define PC_ROOM_GN_DENIED   2
#define PC_ROOM_GN_REMOVED  3
#define PC_ROOM_GN_HOUSE_GONE 4 /* the holder's house / resident slot no longer exists */
/* notices waiting for the HOLDER (onote) */
#define PC_ROOM_ON_NONE     0
#define PC_ROOM_ON_REQUEST  1 /* a new move-in request */
#define PC_ROOM_ON_LEFT     2 /* a roommate moved out by itself */

/* results of the table operations (also the RESULT codes of message 104) */
#define PC_ROOM_R_OK                0
#define PC_ROOM_R_BAD               1  /* malformed request */
#define PC_ROOM_R_NOT_GYROID        2  /* no live placed gyroid at that tile / the requester is not next to it */
#define PC_ROOM_R_NOT_OWNER         3  /* the requester does not hold this gyroid */
#define PC_ROOM_R_NO_HOUSE          4  /* the gyroid's holder has no house (or nobody holds it) */
#define PC_ROOM_R_PENDING_ELSEWHERE 5  /* the guest already has an open request with another holder */
#define PC_ROOM_R_ROOMMATE_ELSEWHERE 6 /* the guest already shares another house */
#define PC_ROOM_R_ALREADY           7  /* already asked / already a roommate here */
#define PC_ROOM_R_COOLDOWN          8  /* denied / removed here less than PC_ROOM_DENY_COOLDOWN_S ago, or the 15 s request gap */
#define PC_ROOM_R_HOUSE_FULL        9  /* the holder has PC_ROOM_MAX_ROOMMATES roommates */
#define PC_ROOM_R_LIST_FULL         10 /* the holder has PC_ROOM_MAX_PENDING open requests, or the table is full */
#define PC_ROOM_R_NOT_FOUND         11 /* no such request / roommate */
#define PC_ROOM_R_BUSY              12 /* the host's roommates.ini is not usable (unreadable: changes refused) */
#define PC_ROOM_R_OFF               13 /* the host switched roommates off */
#define PC_ROOM_R_NOT_GUEST         14 /* a resident asked to move in (residents have a house of their own) */
#define PC_ROOM_R_RATE              15 /* too many messages */
#define PC_ROOM_R_FAULT             16 /* writing roommates.ini failed: nothing changed */
#define PC_ROOM_R_MAX               16

typedef struct PCRoomEntry {
    uint8_t  state;  /* PC_ROOM_ST_* */
    uint8_t  gnote;  /* PC_ROOM_GN_* */
    uint8_t  onote;  /* PC_ROOM_ON_* */
    uint8_t  rsv;
    uint32_t time;   /* unix seconds of the last state change (host clock) */
    uint8_t  owner_key[PC_ROOM_KEY_LEN];
    uint8_t  guest_key[PC_ROOM_KEY_LEN];
    uint8_t  owner_name[PC_ROOM_NAME_LEN];
    uint8_t  guest_name[PC_ROOM_NAME_LEN];
} PCRoomEntry;

typedef struct PCRoomTable {
    int         n;
    uint32_t    gen; /* +1 per change */
    PCRoomEntry e[PC_ROOM_MAX];
} PCRoomTable;

enum { PC_ROOM_FILE_BAD = -1, PC_ROOM_FILE_MISSING = 0, PC_ROOM_FILE_OK = 1 };

void pc_room_init(PCRoomTable* t);
int  pc_room_find(const PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN]); /* index or -1 (any state) */
int  pc_room_find_state(const PCRoomTable* t, const uint8_t guest[PC_ROOM_KEY_LEN], int state);                    /* the guest's first entry in `state`, or -1 */
int  pc_room_count(const PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], int state);
/* the operations (now = unix seconds). Every one returns PC_ROOM_R_*; R_OK changed the table (gen + 1). */
int  pc_room_request(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN], const uint8_t* owner_name, const uint8_t* guest_name,
                     uint32_t now);
int  pc_room_cancel(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN]);           /* the guest withdraws its request */
int  pc_room_accept(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN], uint32_t now);
int  pc_room_deny(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN], uint32_t now);
int  pc_room_remove(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN], uint32_t now);  /* holder: demote */
int  pc_room_leave(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN], uint32_t now);   /* roommate: move out */
/* housekeeping: the holder's house is gone (resident deleted / house reassigned): roommates -> REMOVED (note HOUSE_GONE), requests -> DENIED (note HOUSE_GONE).
 * Returns the number of entries changed. */
int  pc_room_owner_gone(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], uint32_t now);
int  pc_room_drop_guest(PCRoomTable* t, const uint8_t guest[PC_ROOM_KEY_LEN]); /* the guest no longer exists as a guest (removed / promoted): all its entries deleted */
/* tombstones without a waiting notice whose cooldown has passed are deleted (a time in the future by more than a day = a clock jump: expired). Returns the count. */
int  pc_room_gc(PCRoomTable* t, uint32_t now);
void pc_room_delete_at(PCRoomTable* t, int i);
/* text form */
int  pc_room_parse(PCRoomTable* t, const char* text, size_t len, int* bad_lines);
int  pc_room_format(const PCRoomTable* t, char* out, size_t cap); /* length, or -1 when cap is too small */
int  pc_room_load(const char* path, PCRoomTable* t, int* bad_lines, int* from_bak); /* PC_ROOM_FILE_*; a missing file falls back to <path>.bak */
int  pc_room_save(const char* path, const PCRoomTable* t); /* 1 = written (old file kept as <path>.bak), 0 = failed (nothing changed on disk) */
const char* pc_room_state_name(int state);
const char* pc_room_result_text(int r); /* plain ASCII, short (a notice line) */

/* ---- message 104 ROOMMATE ---- */
#define PC_ROOM_MSG_ID     104
#define PC_ROOM_MSG_SIZE   300
#define PC_ROOM_MSG_ENTRIES 8
/* client -> host */
#define PC_ROOM_OP_QUERY   1 /* what can I do at this gyroid? -> INFO */
#define PC_ROOM_OP_REQUEST 2 /* guest: ask to move in -> RESULT (+ INFO) */
#define PC_ROOM_OP_CANCEL  3 /* guest: withdraw my request here */
#define PC_ROOM_OP_ACCEPT  4 /* holder: e[0].key = the guest */
#define PC_ROOM_OP_DENY    5
#define PC_ROOM_OP_REMOVE  6 /* holder: demote a roommate */
#define PC_ROOM_OP_LEAVE   7 /* roommate: move out of this holder's house */
#define PC_ROOM_OP_CLIENT_MAX 7
/* host -> client */
#define PC_ROOM_OP_INFO    16 /* role + holder name + (holder) the list */
#define PC_ROOM_OP_RESULT  17 /* `aux` = the op answered, `result` = PC_ROOM_R_* */
#define PC_ROOM_OP_STATUS  18 /* role ROOMMATE + house + holder key / name, or role NONE (not a roommate any more) */
#define PC_ROOM_OP_NOTICE  19 /* `aux` = PC_ROOM_GN_* (guest) or 0x80 | PC_ROOM_ON_* (holder), e[0].name = the other party, count = how many (holder requests) */

/* INFO roles */
#define PC_ROOM_ROLE_NONE        0 /* nobody holds this gyroid (or its holder has no house) */
#define PC_ROOM_ROLE_OWNER       1 /* the requester holds it: e[] = requests (state PENDING) + roommates (state ROOMMATE) */
#define PC_ROOM_ROLE_CAN_REQUEST 2 /* a guest that may ask to move in */
#define PC_ROOM_ROLE_PENDING     3 /* a guest whose request here is open (may withdraw it) */
#define PC_ROOM_ROLE_ROOMMATE    4 /* a roommate of this holder (may move out) */
#define PC_ROOM_ROLE_BLOCKED     5 /* a guest that may not ask now: `result` says why */
#define PC_ROOM_ROLE_RESIDENT    6 /* another resident (residents have their own house) */
#define PC_ROOM_ROLE_MAX         6

typedef struct PCRoomWireEntry {
    uint8_t state; /* PC_ROOM_ST_PENDING / _ROOMMATE */
    uint8_t online;
    uint8_t name[PC_ROOM_NAME_LEN];
    uint8_t key[PC_ROOM_KEY_LEN];
    uint8_t rsv[2];
} PCRoomWireEntry; /* 32 bytes */

typedef struct PCRoomMsg {
    uint8_t  msg_type; /* 104 */
    uint8_t  op;       /* PC_ROOM_OP_* */
    uint8_t  result;   /* PC_ROOM_R_* (host messages), 0 from a client */
    uint8_t  count;    /* entries used (<= 8) */
    uint8_t  ut_x, ut_z;
    uint16_t item;     /* the gyroid: origin tile + the tile value the client sees (host messages: echoed, STATUS / NOTICE: 0) */
    uint32_t seq;      /* the client's request serial (echoed by INFO / RESULT; 0 in pushes) */
    uint8_t  role;     /* INFO / STATUS: PC_ROOM_ROLE_* */
    uint8_t  house;    /* STATUS: the shared house 0..3 (0xFF = none) */
    uint8_t  aux;      /* RESULT: the op answered; NOTICE: the note */
    uint8_t  flags;    /* 0 */
    uint8_t  owner_name[PC_ROOM_NAME_LEN];
    uint8_t  owner_key[PC_ROOM_KEY_LEN];
    PCRoomWireEntry e[PC_ROOM_MSG_ENTRIES];
} PCRoomMsg; /* 300 bytes */

/* 1 = a well-formed message of the given direction (from_client: ops 1..7, the reserved fields 0, count 0 except ACCEPT / DENY / REMOVE = 1;
 * host: ops 16..19, count <= 8, result <= R_MAX, role <= ROLE_MAX, entry states PENDING / ROOMMATE, rsv 0) */
int  pc_room_msg_valid(const PCRoomMsg* m, int from_client);
int  pc_room_selftest(void); /* 0 = pass, else the number of failures */

#ifdef __cplusplus
}
#endif
#endif
