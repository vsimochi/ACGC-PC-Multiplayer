#!/usr/bin/env python3
"""wire_baseline.py - shared SOURCE-AUDIT helper: "no wire change vs HEAD", checked on CONTENT, not on file diffs.

Several source audits used to assert `git diff --name-only -- <file>` is empty for net_spike_lib.py / pc_net_game.c /
pc_net_game.h. That is wrong once those files legitimately change for host-only / client-only logic. The intent is "the
WIRE CONTRACT is unchanged", which this module enforces by comparing the working tree against `git show HEAD:<path>`:

  * protocol version constant (pc_net_game.h) - identical;
  * every `typedef struct|enum PCNet*` block of pc_net_game.c (message-id enum PCNetGameMsgType, reject reasons and every
    wire struct), comment-stripped - identical, EXCEPT the host-only PCNetGameHostPeerState (which may only gain members:
    every HEAD member must remain, in order) and the new host-only PCNetGameIdentityClass;
  * pc_net_game.h: every typedef block identical, every HEAD `#define` still present with the same value;
  * pc_net.c / pc_net.h transport: PCNET_WIRE_* / MAGIC / HEARTBEAT / TIMEOUT defines and all typedef blocks identical;
  * net_spike_lib.py: every wire constant / format string line (`PC_NETGAME_* = ...`, `PCNET_* = ...`, `*_FMT = ...`) identical.
PROTOCOL v8 (D3 + X1, deliberate): the audit now accepts ONLY the documented v8 additions and still fails on any other wire
change: protocol constant 7u -> EXPECTED_PROTOCOL_VERSION (8u), message ids 47..50 (RECORD_HELLO/BEGIN/CHUNK/ACK) and, since X1
(host-transactional pickup/drop/bury, v8 still unreleased so NO further bump), 51 TXN_COMMIT / 52 TXN_RESULT appended to
PCNetGameMsgType (EXPECTED_MAX_MSG_ID = 52; 53/54 are reserved for X2), the pinned structs PCNetGameRecord{Hello,Begin,Chunk,Ack}Msg
and PCNetGameTxn{Tag,CommitMsg,ResultMsg} (exact normalised text below), the matching net_spike_lib constants
(PC_NETGAME_MSG_RECORD_*, PC_NETGAME_REC_*, RECORD_*_FMT, PC_NETGAME_MSG_TXN_*, PC_NETGAME_TXN_*, TXN_*_FMT, the version line) and
new host-only members of PCNetGameHostPeerState. After the v8 commit HEAD already contains them and the audit keeps passing unchanged.
It FAILS when a message struct, enum value, format string or protocol constant changes (see `--selftest`, which mutates
in-memory copies and requires each mutation to be detected).

Usage in a test: `import wire_baseline; wire_baseline.run(check_fn, repo_root)` where check_fn(desc, cond) records a check.
Tier: SOURCE AUDITED. Needs git (read-only `git show`)."""
import os
import re
import subprocess
import sys

# Host/client-local state, never on the wire: PCNetGameRecRange/RecSlot/ClientRec (D3); X1: PCNetGameHostInteraction (gains the
# reservation's pocket_slot / commit_path), PCNetGameTxnLog + PCNetGameTxnResident (the host's per-resident journal) and
# PCNetGameClientTxn (the real client's one in-flight transaction, added by X1b).
# Town services milestone 1: PCNetGameTsHost (the host's per-service mirror slot) and PCNetGameTsOp (the client's one UI-seam operation) are local, never on the wire.
# X3: PCNetGameFieldActionPending (the CLIENT-only field-action queue entry, never on the wire) lost its `local_grant` member: a grant-carrying
# dig request is now a host-transactional grant owned by PCNetGameClientTxn, no longer a queue entry.
HOST_ONLY = ("PCNetGameHostPeerState", "PCNetGameIdentityClass", "PCNetGameRecRange", "PCNetGameRecSlot", "PCNetGameClientRec",
             "PCNetGameHostInteraction", "PCNetGameTxnLog", "PCNetGameTxnResident", "PCNetGameClientTxn", "PCNetGameFieldActionPending",
             "PCNetGameTsHost", "PCNetGameTsOp", "PCNetGameMailOp", "PCNetGameMboxHost", "PCNetGameTakeOp",
             "PCNetGameGuest",  # guests G1: the host's guest table entry (local, never on the wire)
             "PCNetGameAdmission", "PCNetGameAdmissionKind",  # M-D: the host's local admission decision (never on the wire)
             "PCNetGameHouseHost", "PCNetGameHouseClient",
             "PCNetRoomNpcLease",
             "PCNetWorkChar", "PCNetWorkJobDef", "PCNetWorkMirror")  # indoor villagers: the host's per-room pose lease (local, never on the wire)  # furniture sync: the host's canonical house copy / the client's house state (local, never on the wire)
# Client-only (never on the wire) structs that a reviewed change DELETED: absent from the current tree is the only acceptable state
# (it must not come back changed). X1b: the M9-D G2-3 committed-bury claim record, replaced by the host-transactional commit.
REMOVED_CLIENT_ONLY = ("PCNetGameBuryCommitted",)
# The highest message id of PCNetGameMsgType (ids are contiguous 1..EXPECTED_MAX_MSG_ID). Tests assert against THIS constant
# instead of a literal, so the next deliberate id addition is one reviewed edit here.
# Town services milestone 1 (deliberate, v8 still unreleased): 53 / 54 are ENUMERATED as reserved ids (X2: TXN_QUERY / TXN_STATUS, never sent) so
# the ids stay contiguous, and 55 = TOWN_SVC_STATE (the generic host -> client service mirror).
# Mail milestone 2 (deliberate, v8 still unreleased): 56 = MAILBOX_LETTER (host -> the owning client only: one slot of the host-held house mailbox).
# Guests G1 (deliberate, v8 still unreleased): 57 = IDENTITY_EXT (client -> host, the guest claim sent BEFORE the frozen IDENTITY) and 58 = IDENTITY_TOKEN
# (host -> the admitted guest only, sent right after the frozen IDENTITY_ACK). The 32-byte IDENTITY / IDENTITY_ACK structs themselves stay frozen.
# Furniture sync Stage 1 (deliberate, v8 still unreleased): 59 = HOUSE_BEGIN (both directions, 36 B), 60 = HOUSE_CHUNK (both, 1012 B), 61 = HOUSE_ACK (host -> owner, 24 B).
# Town transfer (M-B, deliberate, v8 still unreleased): 62 = TOWN_FETCH_REQ (C -> H, 40 B), 63 = TOWN_INFO (H -> C, 40 B), 64 = TOWN_CHUNK (H -> C, 1012 B, u32 offset), 65 = TOWN_DONE (C -> H, 12 B).
# Guest -> resident promotion (M-F, deliberate, v8 still unreleased): 66 = RESIDENT_HANDOFF (H -> C, 48 B, sent to a promoted guest right before REJECT reason 6 PROMOTED).
# Indoor villagers (Patch 2, deliberate, v8 still unreleased): 67 = ROOM_NPC (both directions, UNRELIABLE, 28 B): the pose of the villager standing in its own house.
# Nook Work Mode (Patch 3, deliberate, v8 still unreleased): 68 = WORK_STATE (host -> the requesting client, RELIABLE, 28 B): the character's job as the host holds it.
EXPECTED_MAX_MSG_ID = 68

# The ONE source of truth for "what protocol version must the tree speak" (tests import this; net_spike_lib.PROTOCOL_VERSION
# is audited against it).
EXPECTED_PROTOCOL_VERSION = 8
PREVIOUS_PROTOCOL_VERSION = 7  # what an audit against a pre-v8 HEAD may still see

# Documented v8 additions (comment-stripped, whitespace-normalised struct bodies, pinned exactly).
V8_NEW_STRUCTS = {
    "PCNetGameRecordHelloMsg": "uint8_t msg_type; uint8_t flags; uint16_t _reserved0; uint32_t record_size; uint32_t local_digest; "
                               "uint32_t last_host_session; uint32_t last_epoch; uint32_t last_rev;",
    "PCNetGameRecordBeginMsg": "uint8_t msg_type; uint8_t kind; uint8_t chunk_count; uint8_t rsv; uint32_t xfer_id; uint32_t epoch; "
                               "uint32_t rev; uint32_t total_size; uint32_t digest; uint32_t host_session;",
    "PCNetGameRecordChunkMsg": "uint8_t msg_type; uint8_t chunk_idx; uint16_t len; uint32_t xfer_id; uint16_t offset; uint16_t rsv; "
                               "uint8_t data[PC_NETGAME_REC_CHUNK_DATA];",
    "PCNetGameRecordAckMsg": "uint8_t msg_type; uint8_t status; uint16_t detail; uint32_t xfer_id; uint32_t epoch; uint32_t rev; "
                             "uint32_t host_session;",
    # X1: the reusable 64-byte transaction tag, the 72-byte TXN_COMMIT (C->H) and the 76-byte TXN_RESULT (H->C)
    "PCNetGameTxnTag": "uint32_t txn_nonce; uint32_t txn_seq; uint8_t dest; uint8_t slot; uint16_t item; uint8_t flags; "
                       "uint8_t aux_cond; uint16_t aux_item; uint32_t base_epoch; uint32_t base_rev; uint16_t pre_pockets[15]; "
                       "uint16_t _rsv0; uint32_t pre_conds; uint32_t pre_wallet;",
    "PCNetGameTxnCommitMsg": "uint8_t msg_type; uint8_t kind; uint16_t _rsv0; uint32_t request_id; PCNetGameTxnTag tag;",
    # town services milestone 1: the result's reserved u16 became svc_seq16 (low 16 bits of the service-blob seq a MUSEUM_DONATE / POLICE_CLAIM produced)
    "PCNetGameTxnResultMsg": "uint8_t msg_type; uint8_t kind; uint8_t outcome; uint8_t reason; uint32_t request_id; "
                             "uint32_t txn_nonce; uint32_t txn_seq; uint32_t host_session; uint32_t epoch; uint32_t rev; "
                             "uint32_t cdig; uint8_t dest; uint8_t slot; uint16_t item; uint16_t post_pockets[15]; "
                             "uint16_t svc_seq16; uint32_t post_conds; uint32_t post_wallet;",
    # town services milestone 1: the generic host -> client service mirror (variable length on the wire: offsetof(blob) + len)
    "PCNetGameTownSvcStateMsg": "uint8_t msg_type; uint8_t service; uint16_t len; uint32_t seq; uint32_t digest; "
                                "uint8_t blob[PC_NETGAME_TS_BLOB_MAX];",
    # mail milestone 2: one slot of the host-held house mailbox, host -> the owning client (316 B: 16 B header + the 298 B canonical BE letter + 2 B pad)
    "PCNetGameMailboxLetterMsg": "uint8_t msg_type; uint8_t house; uint8_t mbox_idx; uint8_t flags; uint32_t seq; uint32_t digest; "
                                 "uint16_t used_count; uint16_t _rsv0; uint8_t letter[PC_NETGAME_MAIL_WIRE_SIZE]; uint16_t _rsv1;",
    # guests G1: the guest claim (client -> host, 42 B, before IDENTITY) and the token delivery (host -> guest, 20 B, after IDENTITY_ACK)
    "PCNetGameIdentityExtMsg": "uint8_t msg_type; uint8_t flags; uint16_t _reserved0; uint8_t home_player_name[PC_NETGAME_NAME_LEN]; "
                               "uint8_t home_land_name[PC_NETGAME_LAND_LEN]; uint16_t home_player_id; uint16_t home_land_id; "
                               "uint8_t token_present; uint8_t token[PC_NETGAME_GUEST_TOKEN_LEN]; uint8_t _reserved1;",
    "PCNetGameIdentityTokenMsg": "uint8_t msg_type; uint8_t flags; uint8_t guest_slot; uint8_t table_size; "
                                 "uint8_t token[PC_NETGAME_GUEST_TOKEN_LEN];",
    # furniture sync Stage 1: the house transfer (36 B BEGIN both directions, 1012 B CHUNK, 24 B ACK host -> owner)
    "PCNetGameHouseBeginMsg": "uint8_t msg_type; uint8_t kind; uint8_t chunk_count; uint8_t house; uint32_t xfer_id; uint32_t host_session; "
                              "uint32_t house_seq; uint32_t rec_epoch; uint32_t rec_rev; uint16_t total_size; uint16_t rsv; uint32_t digest; uint32_t rsv2;",
    "PCNetGameHouseChunkMsg": "uint8_t msg_type; uint8_t chunk_idx; uint16_t len; uint32_t xfer_id; uint16_t offset; uint16_t rsv; "
                              "uint8_t data[PC_NETGAME_HOUSE_CHUNK_DATA];",
    "PCNetGameHouseAckMsg": "uint8_t msg_type; uint8_t status; uint16_t detail; uint32_t xfer_id; uint32_t house_seq; uint32_t epoch; uint32_t rev; "
                            "uint32_t host_session;",
    # town transfer (M-B): the pre-boot cache fetch (40 B request, 40 B info, 1012 B chunk with a u32 offset, 12 B done)
    "PCNetGameTownFetchReqMsg": "uint8_t msg_type; uint8_t flags; uint16_t _rsv0; uint32_t protocol_version; uint32_t have_crc32; uint32_t have_size; "
                                "uint8_t have_land_name[PC_NETGAME_LAND_LEN]; uint16_t have_land_id; uint16_t _rsv1; uint32_t have_terrain_hash; uint8_t _rsv2[8];",
    "PCNetGameTownInfoMsg": "uint8_t msg_type; uint8_t status; uint16_t chunk_size; uint32_t xfer_id; uint32_t total_size; uint32_t crc32; "
                            "uint8_t land_name[PC_NETGAME_LAND_LEN]; uint16_t land_id; uint16_t flags; uint32_t terrain_hash; uint32_t town_gen; uint32_t chunk_count;",
    "PCNetGameTownChunkMsg": "uint8_t msg_type; uint8_t _rsv0; uint16_t len; uint32_t xfer_id; uint32_t offset; uint8_t data[PC_NETGAME_TOWN_CHUNK_DATA];",
    "PCNetGameTownDoneMsg": "uint8_t msg_type; uint8_t status; uint16_t _rsv0; uint32_t xfer_id; uint32_t crc32;",
    # guest -> resident promotion (M-F): host -> the promoted guest (48 B): the new resident's town PID + resident token + slot
    "PCNetGameResidentHandoffMsg": "uint8_t msg_type; uint8_t flags; uint8_t res_slot; uint8_t rsv; uint8_t town_pid[20]; "
                                   "uint8_t token[PC_NETGAME_GUEST_TOKEN_LEN]; uint8_t rsv2[8];",
    # indoor villagers (Patch 2): the villager-house pose (28 B), both directions, unreliable
    "PCNetGameRoomNpcMsg": "uint8_t msg_type; uint8_t scene_id; uint8_t action_type; uint8_t sender; uint16_t owner; uint16_t npc_id; uint32_t frame; "
                           "float pos_x, pos_y, pos_z; int16_t facing_angle; int16_t _reserved;",
    # Nook Work Mode (Patch 3): the character's job as the host holds it (28 B), host -> the requesting client, reliable
    "PCNetGameWorkStateMsg": "uint8_t msg_type; uint8_t flags; uint8_t state; uint8_t job_type; uint32_t job_id; uint32_t reward; uint32_t jobs_done; "
                             "uint32_t last_rewarded; uint16_t obj_item; uint16_t carried_item; uint16_t target_villager; uint8_t obj_state; uint8_t tip_kind; uint32_t tip_value; "
                             "uint32_t request_id;",
}
# Guest -> resident promotion (M-F): exact C lines (size / offset asserts) that must stay as they are.
HANDOFF_C_PINS = ('_Static_assert(sizeof(PCNetGameResidentHandoffMsg) == 48,', "offsetof(PCNetGameResidentHandoffMsg, town_pid) == 4",
                  "offsetof(PCNetGameResidentHandoffMsg, token) == 24")
# Town transfer (M-B): exact C lines (constants + size asserts) that must stay as they are.
TOWN_C_PINS = ("#define PC_NETGAME_TOWN_INFO_FLAG_SANITIZED 0x0001u", "#define PC_NETGAME_REC_HELLO_FLAG_NO_MIGRATE 0x02u", "#define PC_NETGAME_TOWN_CHUNK_DATA   1000u", "#define PC_NETGAME_TOWN_FILE_SIZE    467008u",
               "#define PC_NETGAME_TOWN_STATUS_STREAM        0u", "#define PC_NETGAME_TOWN_STATUS_UP_TO_DATE    1u",
               "#define PC_NETGAME_TOWN_STATUS_UNAVAILABLE   2u", "#define PC_NETGAME_TOWN_STATUS_BUSY          3u",
               "#define PC_NETGAME_TOWN_STATUS_REFUSED       4u", "#define PC_NETGAME_TOWN_STATUS_BAD_REQUEST   5u",
               "#define PC_NETGAME_TOWN_DONE_OK       0u", "#define PC_NETGAME_TOWN_DONE_BAD_CRC  1u", "#define PC_NETGAME_TOWN_DONE_BAD_GCI  2u",
               "#define PC_NETGAME_TOWN_DONE_IO       3u", "#define PC_NETGAME_TOWN_DONE_ABORT    4u",
               '_Static_assert(sizeof(PCNetGameTownFetchReqMsg) == 40,', '_Static_assert(sizeof(PCNetGameTownInfoMsg) == 40,',
               '_Static_assert(sizeof(PCNetGameTownChunkMsg) == 1012,', '_Static_assert(sizeof(PCNetGameTownDoneMsg) == 12,')
# What HEAD may still contain for a struct whose v8 text was deliberately changed in place by a later (still unreleased) milestone.
V8_PREV_STRUCTS = {
    "PCNetGameTxnResultMsg": "uint8_t msg_type; uint8_t kind; uint8_t outcome; uint8_t reason; uint32_t request_id; "
                             "uint32_t txn_nonce; uint32_t txn_seq; uint32_t host_session; uint32_t epoch; uint32_t rev; "
                             "uint32_t cdig; uint8_t dest; uint8_t slot; uint16_t item; uint16_t post_pockets[15]; "
                             "uint16_t _rsv0; uint32_t post_conds; uint32_t post_wallet;",
    # M-G: TOWN_INFO._rsv0 became flags (bit0 SANITIZED); HEAD still has the reserved name
    "PCNetGameTownInfoMsg": "uint8_t msg_type; uint8_t status; uint16_t chunk_size; uint32_t xfer_id; uint32_t total_size; uint32_t crc32; "
                            "uint8_t land_name[PC_NETGAME_LAND_LEN]; uint16_t land_id; uint16_t _rsv0; uint32_t terrain_hash; uint32_t town_gen; uint32_t chunk_count;",
    # Work Mode errands: WORK_STATE grew from 28 to 36 bytes (job type / villager / step / tip); HEAD may still have the first layout
    "PCNetGameWorkStateMsg": "uint8_t msg_type; uint8_t flags; uint8_t state; uint8_t job_type; uint32_t job_id; uint32_t reward; uint32_t jobs_done; "
                             "uint32_t last_rewarded; uint16_t obj_item; uint16_t obj_count; uint32_t request_id;",
}
# X3 (host-transactional dig / catch GRANTS; still v8, extended IN PLACE, deliberate): the ONLY two pre-existing wire structs that change are
# FIELD_ACTION_REQUEST (12 -> 76 bytes) and CATCH_REQUEST (20 -> 84 bytes): each gains the trailing 64-byte PCNetGameTxnTag. Pinned exactly
# (the old text is pinned too, so a HEAD that predates X3 is recognised); any other change to either struct, or to any other pre-existing
# struct, still fails the audit.
X3_OLD_STRUCTS = {
    "PCNetGameFieldActionRequestMsg": "uint8_t msg_type; uint8_t kind; uint8_t ut_x; uint8_t ut_z; uint32_t request_id; "
                                      "uint8_t hole_variant; uint8_t _reserved0; uint16_t _reserved1;",
    "PCNetGameCatchRequestMsg": "uint8_t msg_type; uint8_t _reserved0[3]; uint32_t entity_id; uint32_t generation; "
                                "uint32_t request_id; int32_t claimed_species;",
}
X3_CHANGED_STRUCTS = {
    "PCNetGameFieldActionRequestMsg": X3_OLD_STRUCTS["PCNetGameFieldActionRequestMsg"] + " PCNetGameTxnTag tag;",
    "PCNetGameCatchRequestMsg": X3_OLD_STRUCTS["PCNetGameCatchRequestMsg"] + " PCNetGameTxnTag tag;",
}
X3_SIZE_ASSERTS = ('_Static_assert(sizeof(PCNetGameFieldActionRequestMsg) == 76,', '_Static_assert(sizeof(PCNetGameCatchRequestMsg) == 84,',
                   '_Static_assert(offsetof(PCNetGameFieldActionRequestMsg, tag) == 12,',
                   '_Static_assert(offsetof(PCNetGameCatchRequestMsg, tag) == 20,')
# Town services milestone 1: exact C lines (constants + size / offset asserts) that must stay as they are.
TS_C_PINS = ("#define PC_NETGAME_TS_POLICE   1u", "#define PC_NETGAME_TS_MUSEUM   2u", "#define PC_NETGAME_TS_SHOP     3u",
             # batch A (A1): service 4 = HOST_CONFIG (authoritative wildlife mode), array bound 5, 8-byte blob
             "#define PC_NETGAME_TS_HOSTCFG  4u", "#define PC_NETGAME_TS_NUM      6u", "#define PC_NETGAME_TS_HOSTCFG_LEN 8u",
             # events: service 5 = EVENT_STATE (214-byte decision-state blob), array bound 6
             "#define PC_NETGAME_TS_EVENT    5u", "#define PC_NETGAME_TS_EVENT_LEN 214u",
             "#define PC_NETGAME_TS_BLOB_MAX 340u", "#define PC_NETGAME_TS_POLICE_LEN 40u", "#define PC_NETGAME_TS_MUSEUM_LEN 63u",
             "#define PC_NETGAME_TXN_KIND_MUSEUM_DONATE 8u", "#define PC_NETGAME_TXN_KIND_POLICE_CLAIM  9u",
             '_Static_assert(sizeof(PCNetGameTownSvcStateMsg) == 352,', "offsetof(PCNetGameTownSvcStateMsg, blob) == 12",
             # town services milestone 2 (shop): the mirrored Shop_c length, the two transaction kinds, the stock codes, the sale ratio, the new reasons
             "#define PC_NETGAME_TS_SHOP_LEN   320u", '_Static_assert(PC_NETGAME_TS_SHOP_LEN == sizeof(Shop_c),',
             "#define PC_NETGAME_TXN_KIND_SHOP_BUY  10u", "#define PC_NETGAME_TXN_KIND_SHOP_SELL 11u",
             "#define PC_NETGAME_SHOP_STOCK_COUNTED   0xFDu", "#define PC_NETGAME_SHOP_STOCK_RARE      0xFEu",
             "#define PC_NETGAME_SHOP_STOCK_UNLIMITED 0xFFu", "#define PC_NETGAME_SHOP_SELL_RATIO      4u",
             "#define PC_NETGAME_TXN_REASON_NO_FUNDS        19u", "#define PC_NETGAME_TXN_REASON_NOT_SELLABLE    20u",
             "#define PC_NETGAME_TXN_REASON_PRICE_MISMATCH  21u", "#define PC_NETGAME_TXN_REASON_NO_ROOM         22u",
             # mail milestone 1: the MAIL_SEND kind (TXN_COMMIT, no new message id) and its three reasons
             "#define PC_NETGAME_TXN_KIND_MAIL_SEND 12u", "#define PC_NETGAME_TXN_REASON_NO_SUCH_ADDRESS 23u",
             "#define PC_NETGAME_TXN_REASON_MAILBOX_FULL    24u", "#define PC_NETGAME_TXN_REASON_PO_FULL         25u",
             "#define PC_NETGAME_REC_FIELD_MAIL_PRESENT 12u",
             # mail milestone 2: MAILBOX_LETTER (316 B, id 56), the MAIL_TAKE kind (TXN_COMMIT kind 13, tag.flags = the destination mail slot) and its two reasons
             "#define PC_NETGAME_TXN_KIND_MAIL_TAKE 13u", "#define PC_NETGAME_TXN_REASON_NO_SUCH_LETTER  26u",
             "#define PC_NETGAME_TXN_REASON_MAIL_CHANGED    27u", "#define PC_NETGAME_MBOX_SLOTS      10u",
             "#define PC_NETGAME_MBOX_FLAG_EMPTY 0x01u", "#define PC_NETGAME_MAIL_WIRE_SIZE  298u",
             '_Static_assert(sizeof(PCNetGameMailboxLetterMsg) == 316,', "offsetof(PCNetGameMailboxLetterMsg, letter) == 16",
             # guest-first town: the paid HOUSE_PURCHASE kind (TXN_COMMIT kind 14, no new message id), its three reasons, the auto-house byte and the host price
             "#define PC_NETGAME_TXN_KIND_HOUSE_PURCHASE 14u", "#define PC_NETGAME_TXN_REASON_NO_RESIDENCE    28u",
             "#define PC_NETGAME_TXN_REASON_INVALID_HOUSE   29u", "#define PC_NETGAME_TXN_REASON_NAME_TAKEN      30u",
             "#define PC_NETGAME_HOUSE_AUTO                 0xFFu",
             "#define PC_NETGAME_HOUSE_PRICE_DIRECT         (1000u + (uint32_t)mPlayer_DEBT0)",
             '_Static_assert(PC_NETGAME_HOUSE_PRICE_DIRECT == 18400u',
             # Nook's shop manual restock: TXN_COMMIT kind 15 (no new message id), reason 31, the host price and wait
             "#define PC_NETGAME_TXN_KIND_SHOP_RESTOCK 15u", "#define PC_NETGAME_TXN_REASON_RESTOCKING 31u",
             "#define PC_NETGAME_RESTOCK_PRICE         500u", "#define PC_NETGAME_RESTOCK_WAIT_MS       60000u")
# Guests G1: exact C lines (constants + size / offset asserts) that must stay as they are.
GUEST_C_PINS = ("#define PC_NETGAME_IDEXT_FLAG_GUEST      0x01u", "#define PC_NETGAME_IDTOKEN_FLAG_NEW      0x01u",
                "#define PC_NETGAME_IDTOKEN_FLAG_KNOWN    0x02u", "#define PC_NETGAME_GUEST_TOKEN_LEN       16u",
                "#define PC_NETGAME_GUEST_MAX             8", "#define PC_NETGAME_IDEXT_FLAG_RESIDENT   0x02u", "#define PC_NETGAME_IDTOKEN_FLAG_RESIDENT 0x04u",
                "#define PC_NETGAME_RESIDENT_TABLE_SIZE   4", "#define PC_NETGAME_REC_CLASS_RESIDENT 0u", "#define PC_NETGAME_REC_CLASS_GUEST    1u",
                '_Static_assert(sizeof(PCNetGameIdentityExtMsg) == 42,', "offsetof(PCNetGameIdentityExtMsg, token) == 25",
                '_Static_assert(sizeof(PCNetGameIdentityTokenMsg) == 20,', "offsetof(PCNetGameIdentityTokenMsg, token) == 4",
                '_Static_assert(sizeof(PCNetGameIdentityMsg) == 32,', '_Static_assert(sizeof(PCNetGameIdentityAckMsg) == 32,',
                '_Static_assert(offsetof(PCNetGameIdentityMsg, protocol_version) == 4,')
# WEEDS (v8 unreleased, same FIELD_ACTION_REQUEST / RESULT pair, no layout change): the two appended FIELD_ACTION kinds.
WEEDS_C_PINS = ("#define PC_NETGAME_FIELD_ACTION_KIND_WEED_PULL      10u", "#define PC_NETGAME_FIELD_ACTION_KIND_FLOWER_TRAMPLE 11u",
                "#define PC_NETGAME_FIELD_ACTION_KIND_SNOWMAN_BREAK 9u")
V8_NEW_ENUMS = [("PC_NETGAME_MSG_RECORD_HELLO", "47"), ("PC_NETGAME_MSG_RECORD_BEGIN", "48"),
                ("PC_NETGAME_MSG_RECORD_CHUNK", "49"), ("PC_NETGAME_MSG_RECORD_ACK", "50"),
                ("PC_NETGAME_MSG_TXN_COMMIT", "51"), ("PC_NETGAME_MSG_TXN_RESULT", "52"),
                ("PC_NETGAME_MSG_TXN_RESERVED_53", "53"), ("PC_NETGAME_MSG_TXN_RESERVED_54", "54"),
                ("PC_NETGAME_MSG_TOWN_SVC_STATE", "55"), ("PC_NETGAME_MSG_MAILBOX_LETTER", "56"),
                ("PC_NETGAME_MSG_IDENTITY_EXT", "57"), ("PC_NETGAME_MSG_IDENTITY_TOKEN", "58"),
                ("PC_NETGAME_MSG_HOUSE_BEGIN", "59"), ("PC_NETGAME_MSG_HOUSE_CHUNK", "60"), ("PC_NETGAME_MSG_HOUSE_ACK", "61"),
                ("PC_NETGAME_MSG_TOWN_FETCH_REQ", "62"), ("PC_NETGAME_MSG_TOWN_INFO", "63"), ("PC_NETGAME_MSG_TOWN_CHUNK", "64"), ("PC_NETGAME_MSG_TOWN_DONE", "65"),
                ("PC_NETGAME_MSG_RESIDENT_HANDOFF", "66"), ("PC_NETGAME_MSG_ROOM_NPC", "67"), ("PC_NETGAME_MSG_WORK_STATE", "68")]
_V8_ENUM_RE = r"PC_NETGAME_MSG_(?:RECORD_(?:HELLO|BEGIN|CHUNK|ACK)|TXN_(?:COMMIT|RESULT|RESERVED_5[34])|TOWN_SVC_STATE|MAILBOX_LETTER|IDENTITY_(?:EXT|TOKEN)|HOUSE_(?:BEGIN|CHUNK|ACK)|TOWN_(?:FETCH_REQ|INFO|CHUNK|DONE)|RESIDENT_HANDOFF|ROOM_NPC|WORK_STATE)\s*=\s*\d+,"
# net_spike_lib lines that may exist in the working tree but not in a pre-v8 HEAD (the version line is checked separately).
V8_LIB_ADD_RE = re.compile(r"^(?:PC_NETGAME_MSG_RECORD_(?:HELLO|BEGIN|CHUNK|ACK)|PC_NETGAME_REC_\w+|RECORD_(?:HELLO|BEGIN|CHUNK|ACK)_FMT"
                           r"|PC_NETGAME_MSG_TXN_\w+|PC_NETGAME_TXN_\w+|TXN_(?:COMMIT|RESULT|TAG)_FMT"
                           r"|PC_NETGAME_MSG_TOWN_SVC_STATE|PC_NETGAME_TS_\w+|PC_NETGAME_SHOP_\w+|TOWN_SVC_STATE_FMT"
                           r"|PC_NETGAME_MSG_MAILBOX_LETTER|PC_NETGAME_MBOX_\w+|PC_NETGAME_MAIL_WIRE_SIZE|MAILBOX_LETTER_FMT"
                           r"|PC_NETGAME_MSG_IDENTITY_(?:EXT|TOKEN)|PC_NETGAME_IDEXT_\w+|PC_NETGAME_IDTOKEN_\w+|PC_NETGAME_GUEST_\w+|PC_NETGAME_RESIDENT_\w+|PC_NETGAME_REJECT_(?:RESIDENT_CREDENTIAL|PROMOTED)|PC_NETGAME_MSG_RESIDENT_HANDOFF|RESIDENT_HANDOFF_FMT|build_resident_ext"
                           r"|PC_NETGAME_REC_CLASS_\w+|IDENTITY_(?:EXT|TOKEN)_FMT"
                           r"|PC_NETGAME_MSG_HOUSE_(?:BEGIN|CHUNK|ACK)|PC_NETGAME_HOUSE_\w+|PC_NETGAME_HOSTCFG_\w+|HOUSE_(?:BEGIN|CHUNK|ACK)_FMT"
                           r"|PC_NETGAME_MSG_TOWN_(?:FETCH_REQ|INFO|CHUNK|DONE)|PC_NETGAME_TOWN_\w+|TOWN_(?:FETCH_REQ|INFO|CHUNK|DONE)_FMT"
                           r"|PC_NETGAME_MSG_(?:FIELD_ACTION|CATCH)_(?:REQUEST|RESULT)|(?:FIELD_ACTION|CATCH)_(?:REQUEST|RESULT)_FMT) = ")


def c_message_ids(game_c_text):
    """[(name, value)] of every PC_NETGAME_MSG_* = N in the PCNetGameMsgType enum of pc_net_game.c text (comments included in the
    scan, but only `NAME = N,` at the start of a line counts), in source order."""
    body = game_c_text[game_c_text.index("typedef enum PCNetGameMsgType {"):game_c_text.index("} PCNetGameMsgType;")]
    return [(m.group(1), int(m.group(2))) for m in re.finditer(r"^\s*(PC_NETGAME_MSG_\w+)\s*=\s*(\d+),", body, re.M)]
# Exact values of every v8 net_spike_lib wire line (trailing comment stripped): a changed id / kind / status / format fails the audit.
V8_LIB_PINNED = {
    "PC_NETGAME_MSG_RECORD_HELLO": '47',
    "PC_NETGAME_MSG_RECORD_BEGIN": '48',
    "PC_NETGAME_MSG_RECORD_CHUNK": '49',
    "PC_NETGAME_MSG_RECORD_ACK": '50',
    "PC_NETGAME_REC_SIZE": '0x2440',
    "PC_NETGAME_REC_CHUNK_DATA": '1000',
    "PC_NETGAME_REC_CHUNK_COUNT": '10',
    "PC_NETGAME_REC_HELLO_FLAG_HAVE_LAST": '0x01',
    "PC_NETGAME_REC_HELLO_FLAG_NO_MIGRATE": '0x02',
    "PC_NETGAME_REC_KIND_PUSH_FULL": '1',
    "PC_NETGAME_REC_KIND_PUSH_HOSTFIELDS": '2',
    "PC_NETGAME_REC_KIND_UPLOAD": '3',
    "PC_NETGAME_REC_KIND_MIGRATE_UPLOAD": '4',
    "PC_NETGAME_REC_ACK_APPLIED": '0',
    "PC_NETGAME_REC_ACK_STALE_BASE": '1',
    "PC_NETGAME_REC_ACK_BAD_DIGEST": '2',
    "PC_NETGAME_REC_ACK_BAD_SHAPE": '3',
    "PC_NETGAME_REC_ACK_INVALID_FIELD": '4',
    "PC_NETGAME_REC_ACK_RATE_LIMITED": '5',
    "PC_NETGAME_REC_ACK_NOT_BOUND": '6',
    "PC_NETGAME_REC_ACK_BUSY": '7',
    "PC_NETGAME_REC_ACK_MIGRATE_REQUEST": '8',
    "PC_NETGAME_REC_ACK_ADOPT_DEFERRED": '9',
    "PC_NETGAME_REC_ACK_ADOPT_FAILED": '10',
    "PC_NETGAME_REC_FIELD_PLAYER_ID": '1',
    "PC_NETGAME_REC_FIELD_EXISTS": '2',
    "PC_NETGAME_REC_FIELD_WALLET": '3',
    "PC_NETGAME_REC_FIELD_BANK": '4',
    "PC_NETGAME_REC_FIELD_LOAN": '5',
    "PC_NETGAME_REC_FIELD_POCKET": '6',
    "PC_NETGAME_REC_FIELD_ITEM_COND": '7',
    "PC_NETGAME_REC_FIELD_EQUIPMENT": '8',
    "PC_NETGAME_REC_FIELD_ORG_TABLE": '9',
    "PC_NETGAME_REC_FIELD_CATALOG": '10',
    "PC_NETGAME_REC_FIELD_LOTTO": '11',
    "RECORD_HELLO_FMT": '"<BBHIIIII"',
    "RECORD_BEGIN_FMT": '"<BBBBIIIIII"',
    "RECORD_CHUNK_FMT": '"<BBHIHH1000s"',
    "RECORD_ACK_FMT": '"<BBHIIII"',
    # X1
    "PC_NETGAME_MSG_TXN_COMMIT": '51',
    "PC_NETGAME_MSG_TXN_RESULT": '52',
    "PC_NETGAME_TXN_DEST_NONE": '0',
    "PC_NETGAME_TXN_DEST_POCKET": '1',
    "PC_NETGAME_TXN_DEST_WALLET": '2',
    "PC_NETGAME_TXN_SLOT_WALLET": '0xFF',
    "PC_NETGAME_TXN_FLAG_EXCHANGE": '0x01',
    "PC_NETGAME_TXN_OUTCOME_APPLIED": '0',
    "PC_NETGAME_TXN_OUTCOME_REJECTED": '1',
    "PC_NETGAME_TXN_REASON_NONE": '0',
    "PC_NETGAME_TXN_REASON_EXPIRED": '1',
    "PC_NETGAME_TXN_REASON_NOT_PENDING": '2',
    "PC_NETGAME_TXN_REASON_WORLD_CHANGED": '3',
    "PC_NETGAME_TXN_REASON_NOT_SYNCED": '4',
    "PC_NETGAME_TXN_REASON_NOT_BOUND": '5',
    "PC_NETGAME_TXN_REASON_FENCED": '6',
    "PC_NETGAME_TXN_REASON_CONFLICT": '7',
    "PC_NETGAME_TXN_REASON_BAD_IMAGE": '8',
    "PC_NETGAME_TXN_REASON_PRECOND": '9',
    "PC_NETGAME_TXN_REASON_STALE_IMAGE": '10',
    "PC_NETGAME_TXN_REASON_BAD_SHAPE": '11',
    "PC_NETGAME_TXN_REASON_BUSY": '12',
    "PC_NETGAME_TXN_REASON_FAULT": '13',
    "PC_NETGAME_TXN_REASON_REPLAYED": '14',
    "PC_NETGAME_TXN_REASON_ALREADY_DONATED": '15',
    "PC_NETGAME_TXN_REASON_NOT_AVAILABLE": '16',
    "PC_NETGAME_TXN_REASON_NOT_DONATABLE": '17',
    "PC_NETGAME_TXN_REASON_NO_DONOR_SLOT": '18',
    "PC_NETGAME_TXN_REASON_NO_FUNDS": '19',
    "PC_NETGAME_TXN_REASON_NOT_SELLABLE": '20',
    "PC_NETGAME_TXN_REASON_PRICE_MISMATCH": '21',
    "PC_NETGAME_TXN_REASON_NO_ROOM": '22',
    "PC_NETGAME_TXN_REASON_NO_SUCH_ADDRESS": '23',
    "PC_NETGAME_TXN_REASON_MAILBOX_FULL": '24',
    "PC_NETGAME_TXN_REASON_PO_FULL": '25',
    "PC_NETGAME_REC_FIELD_MAIL_PRESENT": '12',
    # mail milestone 2
    "PC_NETGAME_TXN_REASON_NO_SUCH_LETTER": '26',
    "PC_NETGAME_TXN_REASON_MAIL_CHANGED": '27',
    # guest-first town (paid house purchase)
    "PC_NETGAME_TXN_REASON_NO_RESIDENCE": '28',
    "PC_NETGAME_TXN_REASON_INVALID_HOUSE": '29',
    "PC_NETGAME_TXN_REASON_NAME_TAKEN": '30',
    "PC_NETGAME_TXN_KIND_HOUSE_PURCHASE": '14',
    "PC_NETGAME_TXN_KIND_SHOP_RESTOCK": '15',
    "PC_NETGAME_TXN_REASON_RESTOCKING": '31',
    "PC_NETGAME_HOUSE_AUTO": '0xFF',
    "PC_NETGAME_HOUSE_PRICE_DIRECT": '18400',
    "PC_NETGAME_TXN_KIND_MAIL_TAKE": '13',
    "PC_NETGAME_MSG_MAILBOX_LETTER": '56',
    "PC_NETGAME_MBOX_SLOTS": '10',
    "PC_NETGAME_MBOX_FLAG_EMPTY": '0x01',
    "PC_NETGAME_MAIL_WIRE_SIZE": '298',
    "MAILBOX_LETTER_FMT": '"<BBBBIIHH298sH"',
    "PC_NETGAME_TXN_RING": '16',
    "PC_NETGAME_TXN_FENCED_NUM": '4',
    "TXN_TAG_FMT": '"<IIBBHBBHII15HHII"',
    "TXN_COMMIT_FMT": '"<BBHIIIBBHBBHII15HHII"',
    "TXN_RESULT_FMT": '"<BBBBIIIIIIIBBH15HHII"',
    # X3
    "PC_NETGAME_MSG_FIELD_ACTION_REQUEST": '29',
    "PC_NETGAME_MSG_FIELD_ACTION_RESULT": '30',
    "PC_NETGAME_MSG_CATCH_REQUEST": '41',
    "PC_NETGAME_MSG_CATCH_RESULT": '42',
    "PC_NETGAME_TXN_KIND_DIG_BURIED": '4',
    "PC_NETGAME_TXN_KIND_DIG_HOLE": '5',
    "PC_NETGAME_TXN_KIND_DIG_SHINE": '6',
    "PC_NETGAME_TXN_KIND_CATCH": '7',
    # town services milestone 1
    "PC_NETGAME_TXN_KIND_MUSEUM_DONATE": '8',
    "PC_NETGAME_TXN_KIND_POLICE_CLAIM": '9',
    # town services milestone 2 (shop)
    "PC_NETGAME_TXN_KIND_SHOP_BUY": '10',
    "PC_NETGAME_TXN_KIND_SHOP_SELL": '11',
    # mail milestone 1
    "PC_NETGAME_TXN_KIND_MAIL_SEND": '12',
    "PC_NETGAME_SHOP_STOCK_COUNTED": '0xFD',
    "PC_NETGAME_SHOP_STOCK_RARE": '0xFE',
    "PC_NETGAME_SHOP_STOCK_UNLIMITED": '0xFF',
    "PC_NETGAME_SHOP_GOODS_COUNT": '39',
    "PC_NETGAME_SHOP_SELL_RATIO": '4',
    "PC_NETGAME_MSG_TOWN_SVC_STATE": '55',
    "PC_NETGAME_TS_POLICE": '1',
    "PC_NETGAME_TS_MUSEUM": '2',
    "PC_NETGAME_TS_SHOP": '3',
    "PC_NETGAME_TS_HOSTCFG": '4',
    "PC_NETGAME_TS_HOSTCFG_LEN": '8',
    "PC_NETGAME_TS_EVENT": '5',
    "PC_NETGAME_TS_EVENT_LEN": '214',
    "PC_NETGAME_TS_BLOB_MAX": '340',
    "PC_NETGAME_TS_POLICE_LEN": '40',
    "PC_NETGAME_TS_MUSEUM_LEN": '63',
    "PC_NETGAME_TS_SHOP_LEN": '320',
    "TOWN_SVC_STATE_FMT": '"<BBHII"',
    # guests G1
    "PC_NETGAME_MSG_IDENTITY_EXT": '57',
    "PC_NETGAME_MSG_IDENTITY_TOKEN": '58',
    "PC_NETGAME_IDEXT_FLAG_GUEST": '0x01',
    "PC_NETGAME_IDTOKEN_FLAG_NEW": '0x01',
    "PC_NETGAME_IDTOKEN_FLAG_KNOWN": '0x02',
    "PC_NETGAME_IDEXT_FLAG_RESIDENT": '0x02',
    "PC_NETGAME_IDTOKEN_FLAG_RESIDENT": '0x04',
    "PC_NETGAME_RESIDENT_TABLE_SIZE": '4',
    "PC_NETGAME_REJECT_RESIDENT_CREDENTIAL": '5',
    "PC_NETGAME_REJECT_PROMOTED": '6',
    "PC_NETGAME_MSG_RESIDENT_HANDOFF": '66',
    "RESIDENT_HANDOFF_FMT": '"<BBBB20s16s8s"',
    "PC_NETGAME_GUEST_TOKEN_LEN": '16',
    "PC_NETGAME_GUEST_MAX": '8',
    "PC_NETGAME_REC_CLASS_RESIDENT": '0',
    "PC_NETGAME_REC_CLASS_GUEST": '1',
    "IDENTITY_EXT_FMT": '"<BBH8s8sHHB16sB"',
    "IDENTITY_TOKEN_FMT": '"<BBBB16s"',
    # furniture sync Stage 1
    "PC_NETGAME_MSG_HOUSE_BEGIN": '59',
    "PC_NETGAME_MSG_HOUSE_CHUNK": '60',
    "PC_NETGAME_MSG_HOUSE_ACK": '61',
    "PC_NETGAME_HOUSE_IMG_SIZE": '0x1A44',
    "PC_NETGAME_HOUSE_COMMIT_SIZE": '16004',
    "PC_NETGAME_HOUSE_CHUNK_DATA": '1000',
    "PC_NETGAME_HOUSE_COMMIT_CHUNKS": '17',
    "PC_NETGAME_HOUSE_PUSH_CHUNKS": '7',
    "PC_NETGAME_HOUSE_KIND_OWNER_COMMIT": '1',
    "PC_NETGAME_HOUSE_KIND_CANON_PUSH": '2',
    "PC_NETGAME_HOUSE_ACK_APPLIED": '0',
    "PC_NETGAME_HOUSE_ACK_STALE": '1',
    "PC_NETGAME_HOUSE_ACK_BAD_DIGEST": '2',
    "PC_NETGAME_HOUSE_ACK_BAD_SHAPE": '3',
    "PC_NETGAME_HOUSE_ACK_INVALID_CELL": '4',
    "PC_NETGAME_HOUSE_ACK_CONSERVATION": '5',
    "PC_NETGAME_HOUSE_ACK_NOT_OWNER": '6',
    "PC_NETGAME_HOUSE_ACK_BUSY": '7',
    "PC_NETGAME_HOUSE_ACK_RATE_LIMITED": '8',
    "PC_NETGAME_HOSTCFG_FLAG_HOUSE_SYNC": '0x01',
    "HOUSE_BEGIN_FMT": '"<BBBBIIIIIHHII"',
    "HOUSE_CHUNK_FMT": '"<BBHIHH1000s"',
    "HOUSE_ACK_FMT": '"<BBHIIIII"',
    # town transfer (M-B)
    "PC_NETGAME_MSG_TOWN_FETCH_REQ": '62',
    "PC_NETGAME_MSG_TOWN_INFO": '63',
    "PC_NETGAME_MSG_TOWN_CHUNK": '64',
    "PC_NETGAME_MSG_TOWN_DONE": '65',
    "PC_NETGAME_TOWN_CHUNK_DATA": '1000',
    "PC_NETGAME_TOWN_FILE_SIZE": '467008',
    "PC_NETGAME_TOWN_INFO_FLAG_SANITIZED": '0x0001',
    "PC_NETGAME_TOWN_STATUS_STREAM": '0',
    "PC_NETGAME_TOWN_STATUS_UP_TO_DATE": '1',
    "PC_NETGAME_TOWN_STATUS_UNAVAILABLE": '2',
    "PC_NETGAME_TOWN_STATUS_BUSY": '3',
    "PC_NETGAME_TOWN_STATUS_REFUSED": '4',
    "PC_NETGAME_TOWN_STATUS_BAD_REQUEST": '5',
    "PC_NETGAME_TOWN_DONE_OK": '0',
    "PC_NETGAME_TOWN_DONE_BAD_CRC": '1',
    "PC_NETGAME_TOWN_DONE_BAD_GCI": '2',
    "PC_NETGAME_TOWN_DONE_IO": '3',
    "PC_NETGAME_TOWN_DONE_ABORT": '4',
    "TOWN_FETCH_REQ_FMT": '"<BBHIII8sHHI8s"',
    "TOWN_INFO_FMT": '"<BBHIII8sHHIII"',
    "TOWN_CHUNK_FMT": '"<BBHII1000s"',
    "TOWN_DONE_FMT": '"<BBHII"',
    "FIELD_ACTION_REQUEST_FMT": '"<BBBBIBBHIIBBHBBHII15HHII"',
    "FIELD_ACTION_RESULT_FMT": '"<BBBBIHBB"',
    "CATCH_REQUEST_FMT": '"<B3xIIIiIIBBHBBHII15HHII"',
    "CATCH_RESULT_FMT": '"<BBHII"',
}
_LIB_LINE_RE = re.compile(r"^(\w+) = (.*?)(?:\s+#.*)?$")
_LIB_PV_RE = re.compile(r"^PC_NETGAME_PROTOCOL_VERSION = (\d+)\b")


def header_protocol_ok(hdr):
    """True iff pc_net_game.h defines PC_NETGAME_PROTOCOL_VERSION as EXPECTED_PROTOCOL_VERSION (the single check tests use)."""
    return re.search(r"^#define PC_NETGAME_PROTOCOL_VERSION %du\b" % EXPECTED_PROTOCOL_VERSION, hdr, re.M) is not None
FILES = {
    "game_c": "pc/src/pc_net_game.c",
    "game_h": "pc/include/pc_net_game.h",
    "net_c": "pc/src/pc_net.c",
    "net_h": "pc/include/pc_net.h",
    "lib": "pc/tools/net_spike/net_spike_lib.py",
}


def _norm(b):
    return b.decode("utf-8", "replace").replace("\r\n", "\n")


def _strip_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"//[^\n]*", "", s)
    return re.sub(r"\s+", " ", s).strip()


def typedef_blocks(text, pattern=r".*"):
    out = {}
    for m in re.finditer(r"typedef (?:struct|enum) (\w+) \{(.*?)\n\} (\w+);", text, re.S):
        if re.match(pattern, m.group(3)):
            out[m.group(3)] = _strip_comments(m.group(2))
    return out


def defines(text):
    """{name: normalised value} of every `#define NAME value` (function-like macros keep their parameter list)."""
    out = {}
    for m in re.finditer(r"^[ \t]*#define[ \t]+(\w+(?:\([^)]*\))?)[ \t]*(.*)$", text, re.M):
        out[m.group(1)] = _strip_comments(m.group(2))
    return out


def wire_lines_lib(t):
    return sorted(m.group(0).strip() for m in re.finditer(r"^(?:(?:PC_NETGAME|PCNET)_\w+|\w+_FMT) = .*$", t, re.M))


def wire_defines_net_c(t):
    return sorted(m.group(0).strip() for m in re.finditer(r"^#define PCNET_(?:WIRE_|MAGIC|HEARTBEAT|TIMEOUT)\w*.*$", t, re.M))


def audit_texts(head, cur):
    """head/cur: {key: text} for the FILES keys. Returns [(description, ok)]."""
    out = []
    add = lambda d, c: out.append((d, bool(c)))

    pv = lambda t: re.findall(r"^#define PC_NETGAME_PROTOCOL_VERSION\s+(\S+)", t, re.M)
    ev, pvv = "%du" % EXPECTED_PROTOCOL_VERSION, "%du" % PREVIOUS_PROTOCOL_VERSION
    add("wire: PC_NETGAME_PROTOCOL_VERSION is the expected v%d (%s) and HEAD is v%d or v%d (%s)"
        % (EXPECTED_PROTOCOL_VERSION, pv(cur["game_h"]), PREVIOUS_PROTOCOL_VERSION, EXPECTED_PROTOCOL_VERSION, pv(head["game_h"])),
        pv(cur["game_h"]) == [ev] and pv(head["game_h"]) in ([pvv], [ev]))

    cb, hb = typedef_blocks(cur["game_c"], r"PCNet\w*"), typedef_blocks(head["game_c"], r"PCNet\w*")
    diff = sorted(k for k in set(cb) | set(hb) if cb.get(k) != hb.get(k) and k not in HOST_ONLY and k not in V8_NEW_STRUCTS
                  and k not in X3_CHANGED_STRUCTS  # the two request structs that gained the tag are pinned by the X3 check below
                  and k != "PCNetGameMsgType"  # the id enum has its own v8-aware check below
                  and k != "PCNetGameRejectReason"  # the reject enum has its own check below (M-E reason 5)
                  and not (k in REMOVED_CLIENT_ONLY and k not in cb))  # a deliberately deleted client-only struct (X1b)
    add("wire: every PCNet* typedef struct/enum of pc_net_game.c identical to HEAD except the host-only %s, the deleted client-only %s and the pinned v8 "
        "structs (changed: %s) [%d blocks]" % ("/".join(HOST_ONLY), "/".join(REMOVED_CLIENT_ONLY), diff, len(cb)), not diff and len(cb) > 50)
    add("wire: the %d v8 record + transaction + town-service structs exist with EXACTLY the documented layout (pinned) and HEAD has none, the same, "
        "or the documented previous text" % len(V8_NEW_STRUCTS),
        all(cb.get(k) == v and hb.get(k) in (None, v, V8_PREV_STRUCTS.get(k)) for k, v in V8_NEW_STRUCTS.items()))
    add("wire: X3 -- FIELD_ACTION_REQUEST (12 -> 76 B) and CATCH_REQUEST (20 -> 84 B) are the ONLY pre-existing structs that changed: EXACTLY the "
        "old layout + the trailing PCNetGameTxnTag (pinned; HEAD has the old or the same layout) and the 76 / 84 size and tag-offset asserts exist",
        all(cb.get(k) == v and hb.get(k) in (X3_OLD_STRUCTS[k], v) for k, v in X3_CHANGED_STRUCTS.items())
        and all(a in cur["game_c"] for a in X3_SIZE_ASSERTS))
    add("wire: town services -- the service ids, blob length constants and the 352-byte / offset _Static_asserts of PCNetGameTownSvcStateMsg are pinned in pc_net_game.c",
        all(a in cur["game_c"] for a in TS_C_PINS))
    add("wire: guests G1 -- IDENTITY_EXT (42 B) / IDENTITY_TOKEN (20 B) constants + size / offset _Static_asserts are pinned in pc_net_game.c, and the "
        "frozen 32-byte IDENTITY / IDENTITY_ACK size + protocol_version offset asserts are still there",
        all(a in cur["game_c"] for a in GUEST_C_PINS))
    add("wire: town transfer (M-B) -- the status / done / size constants and the 40 / 40 / 1012 / 12 byte _Static_asserts of ids 62..65 are pinned in pc_net_game.c",
        all(a in cur["game_c"] for a in TOWN_C_PINS))
    add("wire: guest -> resident promotion (M-F) -- the 48-byte _Static_assert and the town_pid / token offsets of PCNetGameResidentHandoffMsg (id 66) are pinned in pc_net_game.c",
        all(a in cur["game_c"] for a in HANDOFF_C_PINS))
    add("wire: WEEDS -- FIELD_ACTION kinds 10 WEED_PULL / 11 FLOWER_TRAMPLE are pinned in pc_net_game.c (and 9 SNOWMAN_BREAK still precedes them); "
        "the request / result structs they ride are the already pinned ones (no layout change)",
        all(a in cur["game_c"] for a in WEEDS_C_PINS))
    strip_v8 = lambda b: re.sub(_V8_ENUM_RE, "", b).strip()
    ids = lambda b: re.findall(r"(PC_NETGAME_MSG_(?:RECORD_(?:HELLO|BEGIN|CHUNK|ACK)|TXN_(?:COMMIT|RESULT|RESERVED_5[34])|TOWN_SVC_STATE|MAILBOX_LETTER|IDENTITY_(?:EXT|TOKEN)|HOUSE_(?:BEGIN|CHUNK|ACK)|TOWN_(?:FETCH_REQ|INFO|CHUNK|DONE)|RESIDENT_HANDOFF|ROOM_NPC|WORK_STATE))\s*=\s*(\d+),", b)
    # HEAD may contain none of the v8 ids (pre-v8), the D3 ids 47-50 only (a D3-only commit), D3 + X1 (47-52), + town services (47-55), + mailbox (47-56) or all of them
    head_ids_ok = (ids(hb["PCNetGameMsgType"]) in ([], V8_NEW_ENUMS[:4], V8_NEW_ENUMS[:6], V8_NEW_ENUMS[:9], V8_NEW_ENUMS[:10], V8_NEW_ENUMS[:12], V8_NEW_ENUMS[:15], V8_NEW_ENUMS[:19], V8_NEW_ENUMS[:-2], V8_NEW_ENUMS[:-1], V8_NEW_ENUMS)
                   if "PCNetGameMsgType" in hb else False)
    add("wire: message-id enum PCNetGameMsgType identical to HEAD except the documented v8 ids 47-68 (appended, in order; 53/54 enumerated as reserved; 62-65 = town transfer; 66 = promotion handoff; 67 = indoor villager pose; 68 = Work Mode job state)",
        "PCNetGameMsgType" in cb and "PCNetGameMsgType" in hb
        and " ".join(strip_v8(cb["PCNetGameMsgType"]).split()) == " ".join(strip_v8(hb["PCNetGameMsgType"]).split())
        and ids(cb["PCNetGameMsgType"]) == V8_NEW_ENUMS and head_ids_ok
        and cb["PCNetGameMsgType"].rstrip(" ,").endswith("PC_NETGAME_MSG_WORK_STATE = 68"))
    nums = [v for _n, v in c_message_ids(cur["game_c"])]
    add("wire: message ids are unique and contiguous 1..EXPECTED_MAX_MSG_ID (%d) in pc_net_game.c (max %s)"
        % (EXPECTED_MAX_MSG_ID, max(nums) if nums else None),
        len(nums) == len(set(nums)) and sorted(nums) == list(range(1, EXPECTED_MAX_MSG_ID + 1)))
    # M-E: the frozen reasons 1..4 are unchanged; reason 5 RESIDENT_CREDENTIAL (8-byte form) is the ONE deliberate addition (pinned exactly; comments are stripped by typedef_blocks)
    # M-F: reason 6 PROMOTED (8-byte form, preceded by a RESIDENT_HANDOFF) is the second pinned addition
    _rej_new = "PC_NETGAME_REJECT_RESIDENT_CREDENTIAL = 5,"
    _rej_new6 = "PC_NETGAME_REJECT_PROMOTED = 6,"
    _rej_cur = " ".join(cb.get("PCNetGameRejectReason", "").split())
    _rej_head = " ".join(hb.get("PCNetGameRejectReason", "").split())
    _strip_rej = lambda t: " ".join(t.replace(_rej_new, "").replace(_rej_new6, "").split())
    add("wire: PCNetGameRejectReason values identical to HEAD except the pinned M-E reason 5 RESIDENT_CREDENTIAL and M-F reason 6 PROMOTED (8-byte forms)",
        "PCNetGameRejectReason" in cb and (_rej_cur == _rej_head
                                            or (_rej_cur.endswith(_rej_new6) and _rej_new in _rej_cur and _strip_rej(_rej_cur) == _strip_rej(_rej_head)
                                                and _rej_head.count("REJECT_PROMOTED") == 0)))
    def _subseq(small, big):
        it = iter(big)
        return all(any(x == y for y in it) for x in small)
    members = lambda b: [m.strip() for m in b.split(";") if m.strip()]
    add("wire: host-only PCNetGameHostPeerState and PCNetGameHostInteraction (X1: + pocket_slot, commit_path) only GAINED members "
        "(every HEAD member still present, in order)",
        all(k in cb and k in hb and _subseq(members(hb[k]), members(cb[k]))
            for k in ("PCNetGameHostPeerState", "PCNetGameHostInteraction")))

    gh, hh = typedef_blocks(cur["game_h"]), typedef_blocks(head["game_h"])
    add("wire: pc_net_game.h typedef struct/enum blocks identical to HEAD [%d]" % len(gh), gh == hh)
    gd, hd = defines(cur["game_h"]), defines(head["game_h"])
    changed = sorted(k for k in hd if gd.get(k) != hd[k] and k != "PC_NETGAME_PROTOCOL_VERSION")
    add("wire: every HEAD #define of pc_net_game.h still present with the same value (changed/removed: %s)" % changed,
        not changed and len(hd) > 5)

    add("wire: pc_net.c transport constants (PCNET_WIRE_*, MAGIC, HEARTBEAT, TIMEOUT) identical to HEAD",
        wire_defines_net_c(cur["net_c"]) == wire_defines_net_c(head["net_c"]) and len(wire_defines_net_c(cur["net_c"])) >= 4)
    add("wire: pc_net.c / pc_net.h typedef struct/enum blocks identical to HEAD",
        typedef_blocks(cur["net_c"]) == typedef_blocks(head["net_c"]) and typedef_blocks(cur["net_h"]) == typedef_blocks(head["net_h"])
        and len(typedef_blocks(cur["net_c"])) >= 3)
    nd, nh = defines(cur["net_h"]), defines(head["net_h"])
    add("wire: every HEAD #define of pc_net.h still present with the same value",
        all(nd.get(k) == v for k, v in nh.items()))

    wl, wh = wire_lines_lib(cur["lib"]), wire_lines_lib(head["lib"])
    not_pv = lambda ls: [x for x in ls if not _LIB_PV_RE.match(x)]
    removed = [x for x in not_pv(wh) if x not in wl]
    added = [x for x in not_pv(wl) if x not in wh and not V8_LIB_ADD_RE.match(x)]
    cur_pv = [m.group(1) for m in map(_LIB_PV_RE.match, wl) if m]
    cur_vals = {m.group(1): m.group(2) for m in map(_LIB_LINE_RE.match, wl) if m}
    pin_bad = sorted(k for k, v in V8_LIB_PINNED.items() if cur_vals.get(k) != v)
    extra_v8 = sorted(k for k in cur_vals if V8_LIB_ADD_RE.match(k + " = ") and k not in V8_LIB_PINNED)
    add("wire: net_spike_lib wire constants / format strings (PC_NETGAME_*, PCNET_*, *_FMT) identical to HEAD except the v8 "
        "version line and the documented RECORD_* additions (removed/changed: %s, unexpected additions: %s) [%d lines]"
        % (removed, added, len(wl)), not removed and not added and len(wl) > 50 and cur_pv == [str(EXPECTED_PROTOCOL_VERSION)])
    add("wire: every v8 net_spike_lib wire constant/format has exactly the pinned value and none is unpinned (bad: %s, unpinned: %s)"
        % (pin_bad, extra_v8), not pin_bad and not extra_v8)
    return out


def _git_show(repo, rel):
    return _norm(subprocess.run(["git", "-C", repo, "show", "HEAD:" + rel], capture_output=True, check=True, timeout=60).stdout)


def load(repo):
    head, cur = {}, {}
    for k, rel in FILES.items():
        head[k] = _git_show(repo, rel)
        with open(os.path.join(repo, rel.replace("/", os.sep)), "rb") as f:
            cur[k] = _norm(f.read())
    return head, cur


def run(check, repo):
    """Records every wire check through check(desc, cond); a git failure is a failed check."""
    try:
        head, cur = load(repo)
    except Exception as exc:  # noqa: BLE001
        check("wire baseline unavailable (git show HEAD failed: %s)" % exc, False)
        return
    for desc, ok in audit_texts(head, cur):
        check(desc, ok)


def selftest(repo):
    """Mutate in-memory copies of the working files; every wire mutation must be reported, host-only edits must not."""
    head, cur = load(repo)
    ok_all = True

    def failing(mut):
        c = dict(cur)
        mut(c)
        return [d for d, ok in audit_texts(head, c) if not ok]

    base = [d for d, ok in audit_texts(head, cur) if not ok]
    print("baseline failures:", base)
    ok_all &= not base
    msg_id = re.search(r"^(\s*)(PC_NETGAME_MSG_\w+)( = \w+)?,", cur["game_c"], re.M)
    cases = {
        "protocol version": lambda c: c.update(game_h=re.sub(r"(PC_NETGAME_PROTOCOL_VERSION\s+)8u", r"\g<1>9u", c["game_h"], count=1)),
        "protocol reverted": lambda c: c.update(game_h=re.sub(r"(PC_NETGAME_PROTOCOL_VERSION\s+)8u", r"\g<1>7u", c["game_h"], count=1)),
        "enum value": lambda c: c.update(game_c=c["game_c"].replace(msg_id.group(0), msg_id.group(0).replace(",", " + 1,"), 1)),
        "extra enum id 62": lambda c: c.update(game_c=c["game_c"].replace("} PCNetGameMsgType;", "    PC_NETGAME_MSG_EXTRA = 62,\n} PCNetGameMsgType;", 1)),
        "guest ext enum id moved": lambda c: c.update(game_c=c["game_c"].replace("PC_NETGAME_MSG_IDENTITY_EXT          = 57,", "PC_NETGAME_MSG_IDENTITY_EXT          = 67,", 1)),
        "guest token enum id moved": lambda c: c.update(game_c=c["game_c"].replace("PC_NETGAME_MSG_IDENTITY_TOKEN        = 58,", "PC_NETGAME_MSG_IDENTITY_TOKEN        = 68,", 1)),
        "guest ext struct field": lambda c: c.update(game_c=c["game_c"].replace("    uint8_t  _reserved1;     /* 0 */\n} PCNetGameIdentityExtMsg;", "    uint8_t  _reserved1;     /* 0 */\n    uint8_t  extra;\n} PCNetGameIdentityExtMsg;", 1)),
        "guest token struct field": lambda c: c.update(game_c=c["game_c"].replace("    uint8_t token[PC_NETGAME_GUEST_TOKEN_LEN];\n} PCNetGameIdentityTokenMsg;", "    uint8_t token[PC_NETGAME_GUEST_TOKEN_LEN];\n    uint8_t extra;\n} PCNetGameIdentityTokenMsg;", 1)),
        "guest ext size assert": lambda c: c.update(game_c=c["game_c"].replace("_Static_assert(sizeof(PCNetGameIdentityExtMsg) == 42,", "_Static_assert(sizeof(PCNetGameIdentityExtMsg) == 43,", 1)),
        "guest flag": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_IDEXT_FLAG_GUEST      0x01u", "#define PC_NETGAME_IDEXT_FLAG_GUEST      0x02u", 1)),
        "guest record class": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_REC_CLASS_GUEST    1u", "#define PC_NETGAME_REC_CLASS_GUEST    2u", 1)),
        "guest table size": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_GUEST_MAX             8", "#define PC_NETGAME_GUEST_MAX             9", 1)),
        "frozen identity size assert": lambda c: c.update(game_c=c["game_c"].replace('_Static_assert(sizeof(PCNetGameIdentityMsg) == 32,', '_Static_assert(sizeof(PCNetGameIdentityMsg) == 36,', 1)),
        "guest lib ext fmt": lambda c: c.update(lib=c["lib"].replace('IDENTITY_EXT_FMT = "<BBH8s8sHHB16sB"', 'IDENTITY_EXT_FMT = "<BBH8s8sHHB16sBB"', 1)),
        "guest lib token fmt": lambda c: c.update(lib=c["lib"].replace('IDENTITY_TOKEN_FMT = "<BBBB16s"', 'IDENTITY_TOKEN_FMT = "<BBBB16sB"', 1)),
        "guest lib id": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_MSG_IDENTITY_EXT = 57", "PC_NETGAME_MSG_IDENTITY_EXT = 59", 1)),
        "guest lib class": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_REC_CLASS_GUEST = 1", "PC_NETGAME_REC_CLASS_GUEST = 2", 1)),
        "guest lib unlisted": lambda c: c.update(lib=c["lib"] + "\nPC_NETGAME_GUEST_EXTRA = 1\n"),
        "weed kind moved": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_FIELD_ACTION_KIND_WEED_PULL      10u", "#define PC_NETGAME_FIELD_ACTION_KIND_WEED_PULL      12u", 1)),
        "trample kind moved": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_FIELD_ACTION_KIND_FLOWER_TRAMPLE 11u", "#define PC_NETGAME_FIELD_ACTION_KIND_FLOWER_TRAMPLE 10u", 1)),
        "mailbox enum id moved": lambda c: c.update(game_c=c["game_c"].replace("PC_NETGAME_MSG_MAILBOX_LETTER        = 56,", "PC_NETGAME_MSG_MAILBOX_LETTER        = 66,", 1)),
        "mailbox struct field": lambda c: c.update(game_c=c["game_c"].replace("    uint16_t _rsv1;\n} PCNetGameMailboxLetterMsg;", "    uint16_t _rsv1;\n    uint32_t extra;\n} PCNetGameMailboxLetterMsg;", 1)),
        "mailbox struct size assert": lambda c: c.update(game_c=c["game_c"].replace("_Static_assert(sizeof(PCNetGameMailboxLetterMsg) == 316,", "_Static_assert(sizeof(PCNetGameMailboxLetterMsg) == 314,", 1)),
        "mailbox flag": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_MBOX_FLAG_EMPTY 0x01u", "#define PC_NETGAME_MBOX_FLAG_EMPTY 0x02u", 1)),
        "take kind moved": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TXN_KIND_MAIL_TAKE 13u", "#define PC_NETGAME_TXN_KIND_MAIL_TAKE 14u", 1)),
        "take reason moved": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TXN_REASON_MAIL_CHANGED    27u", "#define PC_NETGAME_TXN_REASON_MAIL_CHANGED    37u", 1)),
        "mailbox lib fmt": lambda c: c.update(lib=c["lib"].replace('MAILBOX_LETTER_FMT = "<BBBBIIHH298sH"', 'MAILBOX_LETTER_FMT = "<BBBBIIHH298sHH"', 1)),
        "mailbox lib id": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_MSG_MAILBOX_LETTER = 56", "PC_NETGAME_MSG_MAILBOX_LETTER = 57", 1)),
        "take lib kind": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TXN_KIND_MAIL_TAKE = 13", "PC_NETGAME_TXN_KIND_MAIL_TAKE = 14", 1)),
        "take lib reason": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TXN_REASON_NO_SUCH_LETTER = 26", "PC_NETGAME_TXN_REASON_NO_SUCH_LETTER = 28", 1)),
        "mailbox lib unlisted": lambda c: c.update(lib=c["lib"] + "\nPC_NETGAME_MBOX_EXTRA = 3\n"),
        "ts enum id moved": lambda c: c.update(game_c=c["game_c"].replace("PC_NETGAME_MSG_TOWN_SVC_STATE        = 55,", "PC_NETGAME_MSG_TOWN_SVC_STATE        = 65,", 1)),
        "reserved 53 removed": lambda c: c.update(game_c=c["game_c"].replace("    PC_NETGAME_MSG_TXN_RESERVED_53       = 53,", "    PC_NETGAME_MSG_TXN_RESERVED_X        = 53,", 1)),
        "ts struct field": lambda c: c.update(game_c=c["game_c"].replace("    uint8_t  blob[PC_NETGAME_TS_BLOB_MAX];\n} PCNetGameTownSvcStateMsg;", "    uint8_t  blob[PC_NETGAME_TS_BLOB_MAX];\n    uint32_t extra;\n} PCNetGameTownSvcStateMsg;", 1)),
        "ts blob size": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TS_BLOB_MAX 340u", "#define PC_NETGAME_TS_BLOB_MAX 341u", 1)),
        "txn result svc echo reverted": lambda c: c.update(game_c=c["game_c"].replace("    uint16_t svc_seq16;", "    uint16_t _rsv1;", 1)),
        "ts lib fmt": lambda c: c.update(lib=c["lib"].replace('TOWN_SVC_STATE_FMT = "<BBHII"', 'TOWN_SVC_STATE_FMT = "<BBHIII"', 1)),
        "ts lib kind": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TXN_KIND_POLICE_CLAIM = 9", "PC_NETGAME_TXN_KIND_POLICE_CLAIM = 10", 1)),
        "ts lib reason": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TXN_REASON_ALREADY_DONATED = 15", "PC_NETGAME_TXN_REASON_ALREADY_DONATED = 25", 1)),
        "shop kind moved": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TXN_KIND_SHOP_BUY  10u", "#define PC_NETGAME_TXN_KIND_SHOP_BUY  12u", 1)),
        "hostcfg service id": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TS_HOSTCFG  4u", "#define PC_NETGAME_TS_HOSTCFG  5u", 1)),
        "hostcfg lib len": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TS_HOSTCFG_LEN = 8", "PC_NETGAME_TS_HOSTCFG_LEN = 9", 1)),
        "event service id": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TS_EVENT    5u", "#define PC_NETGAME_TS_EVENT    6u", 1)),
        "event lib len": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TS_EVENT_LEN = 214", "PC_NETGAME_TS_EVENT_LEN = 215", 1)),
        "event blob len": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TS_EVENT_LEN 214u", "#define PC_NETGAME_TS_EVENT_LEN 215u", 1)),
        "shop blob len": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TS_SHOP_LEN   320u", "#define PC_NETGAME_TS_SHOP_LEN   321u", 1)),
        "shop stock code": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_SHOP_STOCK_RARE      0xFEu", "#define PC_NETGAME_SHOP_STOCK_RARE      0xFCu", 1)),
        "shop reason": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TXN_REASON_NO_FUNDS        19u", "#define PC_NETGAME_TXN_REASON_NO_FUNDS        29u", 1)),
        "shop sell ratio": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_SHOP_SELL_RATIO      4u", "#define PC_NETGAME_SHOP_SELL_RATIO      5u", 1)),
        "shop lib kind": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TXN_KIND_SHOP_SELL = 11", "PC_NETGAME_TXN_KIND_SHOP_SELL = 12", 1)),
        "shop lib reason": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TXN_REASON_NO_ROOM = 22", "PC_NETGAME_TXN_REASON_NO_ROOM = 23", 1)),
        "shop lib stock": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_SHOP_STOCK_COUNTED = 0xFD", "PC_NETGAME_SHOP_STOCK_COUNTED = 0xFC", 1)),
        "shop lib unlisted": lambda c: c.update(lib=c["lib"] + "\nPC_NETGAME_SHOP_EXTRA = 1\n"),
        "mail kind moved": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TXN_KIND_MAIL_SEND 12u", "#define PC_NETGAME_TXN_KIND_MAIL_SEND 13u", 1)),
        "mail reason moved": lambda c: c.update(game_c=c["game_c"].replace("#define PC_NETGAME_TXN_REASON_MAILBOX_FULL    24u", "#define PC_NETGAME_TXN_REASON_MAILBOX_FULL    34u", 1)),
        "mail lib kind": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TXN_KIND_MAIL_SEND = 12", "PC_NETGAME_TXN_KIND_MAIL_SEND = 13", 1)),
        "mail lib reason": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TXN_REASON_PO_FULL = 25", "PC_NETGAME_TXN_REASON_PO_FULL = 26", 1)),
        "mail lib field": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_REC_FIELD_MAIL_PRESENT = 12", "PC_NETGAME_REC_FIELD_MAIL_PRESENT = 13", 1)),
        "mail lib unlisted": lambda c: c.update(lib=c["lib"] + "\nPC_NETGAME_TXN_REASON_MAIL_EXTRA = 26\n"),
        "ts lib unlisted": lambda c: c.update(lib=c["lib"] + "\nPC_NETGAME_TS_EXTRA = 4\n"),
        "extra enum id 53 dup": lambda c: c.update(game_c=c["game_c"].replace("PC_NETGAME_MSG_TXN_RESULT            = 52,", "PC_NETGAME_MSG_TXN_RESULT            = 52,\n    PC_NETGAME_MSG_EXTRA = 53,", 1)),
        "txn enum id moved": lambda c: c.update(game_c=c["game_c"].replace("PC_NETGAME_MSG_TXN_COMMIT            = 51,", "PC_NETGAME_MSG_TXN_COMMIT            = 61,", 1)),
        "txn tag field": lambda c: c.update(game_c=c["game_c"].replace("    uint32_t pre_wallet;\n} PCNetGameTxnTag;", "    uint32_t pre_wallet;\n    uint32_t extra;\n} PCNetGameTxnTag;", 1)),
        "txn result field": lambda c: c.update(game_c=c["game_c"].replace("    uint16_t post_pockets[15];", "    uint16_t post_pockets[16];", 1)),
        "txn lib fmt": lambda c: c.update(lib=c["lib"].replace('TXN_RESULT_FMT = "<BBBBIIIIIIIBBH15HHII"', 'TXN_RESULT_FMT = "<BBBBIIIIIIIBBH15HHIII"', 1)),
        "txn lib reason": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TXN_REASON_FENCED = 6", "PC_NETGAME_TXN_REASON_FENCED = 66", 1)),
        "txn lib unlisted": lambda c: c.update(lib=c["lib"] + "\nPC_NETGAME_TXN_REASON_EXTRA = 15\n"),
        "txn max id": lambda c: c.update(game_c=c["game_c"].replace("PC_NETGAME_MSG_TXN_RESULT            = 52,", "PC_NETGAME_MSG_TXN_RESULT            = 56,", 1)),
        "X3 fa request reverted": lambda c: c.update(game_c=c["game_c"].replace("    uint16_t _reserved1;\n    PCNetGameTxnTag tag;    /* X3: all zero = no grant; see the doc above */\n} PCNetGameFieldActionRequestMsg;", "    uint16_t _reserved1;\n} PCNetGameFieldActionRequestMsg;", 1)),
        "X3 fa request field": lambda c: c.update(game_c=c["game_c"].replace("    PCNetGameTxnTag tag;    /* X3: all zero = no grant; see the doc above */\n} PCNetGameFieldActionRequestMsg;", "    PCNetGameTxnTag tag;\n    uint8_t extra;\n} PCNetGameFieldActionRequestMsg;", 1)),
        "X3 catch request field": lambda c: c.update(game_c=c["game_c"].replace("    int32_t  claimed_species;\n    PCNetGameTxnTag tag;\n} PCNetGameCatchRequestMsg;", "    int32_t  claimed_species;\n    PCNetGameTxnTag tag;\n    uint32_t extra;\n} PCNetGameCatchRequestMsg;", 1)),
        "X3 fa size assert": lambda c: c.update(game_c=c["game_c"].replace("_Static_assert(sizeof(PCNetGameFieldActionRequestMsg) == 76,", "_Static_assert(sizeof(PCNetGameFieldActionRequestMsg) == 77,", 1)),
        "X3 other struct": lambda c: c.update(game_c=c["game_c"].replace("typedef struct PCNetGameCatchResultMsg {\n    uint8_t  msg_type;     /* PC_NETGAME_MSG_CATCH_RESULT */", "typedef struct PCNetGameCatchResultMsg {\n    uint8_t  extra;\n    uint8_t  msg_type;     /* PC_NETGAME_MSG_CATCH_RESULT */", 1)),
        "X3 lib fa fmt": lambda c: c.update(lib=c["lib"].replace('FIELD_ACTION_REQUEST_FMT = "<BBBBIBBHIIBBHBBHII15HHII"', 'FIELD_ACTION_REQUEST_FMT = "<BBBBIBBHIIBBHBBHII15HHIII"', 1)),
        "X3 lib kind": lambda c: c.update(lib=c["lib"].replace("PC_NETGAME_TXN_KIND_CATCH = 7", "PC_NETGAME_TXN_KIND_CATCH = 8", 1)),
        "X3 lib unlisted": lambda c: c.update(lib=c["lib"] + "\nCATCH_EXTRA_RESULT_FMT = \"<B\"\n"),
        "host-only member removed": lambda c: c.update(game_c=c["game_c"].replace("    uint8_t  hole_variant;      /* World Ecology T3: bury only", "    uint8_t  hole_variant_x;      /* World Ecology T3: bury only", 1)),
        "v8 enum id moved": lambda c: c.update(game_c=c["game_c"].replace("PC_NETGAME_MSG_RECORD_BEGIN          = 48,", "PC_NETGAME_MSG_RECORD_BEGIN          = 58,", 1)),
        "v8 struct field": lambda c: c.update(game_c=c["game_c"].replace("    uint32_t digest;      /* FNV-1a32 of the whole BE record */", "    uint32_t digest;\n    uint32_t extra;", 1)),
        "v8 lib fmt": lambda c: c.update(lib=c["lib"].replace('RECORD_ACK_FMT = "<BBHIIII"', 'RECORD_ACK_FMT = "<BBHIIIII"', 1)),
        "lib unlisted constant": lambda c: c.update(lib=c["lib"] + "\nPC_NETGAME_MSG_EXTRA = 51\n"),
        "struct field": lambda c: c.update(game_c=re.sub(r"(typedef struct PCNetMoveMsg \{)", r"\1\n    uint8_t extra;", c["game_c"], count=1)),
        "lib format string": lambda c: c.update(lib=re.sub(r'^(\w+_FMT = ")', r'\1x', c["lib"], count=1, flags=re.M)),
        "transport constant": lambda c: c.update(net_c=re.sub(r"(#define PCNET_TIMEOUT_MS\s+)5000u", r"\g<1>5001u", c["net_c"], count=1)),
    }
    for name, mut in cases.items():
        f = failing(mut)
        print("mutation %-18s -> %s" % (name, "DETECTED" if f else "MISSED"))
        ok_all &= bool(f)
    f = failing(lambda c: c.update(game_c=c["game_c"] + "\nstatic int pcnetgame_host_only_extra;\n"))
    print("host-only addition -> %s" % ("no failure (correct)" if not f else "FALSE POSITIVE %s" % f))
    ok_all &= not f
    return 0 if ok_all else 1


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    if "--selftest" in sys.argv:
        sys.exit(selftest(root))
    res = []
    run(lambda d, c: (res.append(c), print(("PASS - " if c else "FAIL - ") + d)), root)
    sys.exit(0 if all(res) else 1)
