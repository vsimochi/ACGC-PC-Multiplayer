# Dresser storage capacity - design (agent 12, sprint 2026-10-11)

Status: DESIGN + groundwork store module only. Nothing is wired into gameplay. Single-player and the existing
multiplayer behaviour are unchanged.

## 1. Findings

- Vanilla keep storage is `FTR_ACTOR.items[aFTR_KEEP_ITEM_COUNT]` (= `mCoBG_LAYER_NUM - 1` = 3,
  `include/ac_furniture.h:15,258`). The items are packed into the extra BG layers of the room
  (`src/actor/ac_my_room.c:2170`), i.e. they live inside the fixed house image (0x1A44 / `mHm_hs_c` 0x26B0).
  They cannot grow without changing fixed save/wire structs. **Not an option.**
- There is no existing extension point for more keep slots (no expanded gyroid/closet inventory anywhere).
- The UI is hard-wired to <= 3 items: `aMR_MsgInitOwnerWhichPutOut` (`ac_my_room_msg_ctrl.c_inc:220-264`, three static
  name buffers, `mChoice_Set_choice_data` with count 1/2/3), take-out `aMR_MsgControlOwnerWhichPutOut` (`:266-305`),
  `aMR_MsgInitOwnerExistItem` (`:320-334`), put-in via the inventory submenu `mSM_IV_OPEN_PUTIN_FTR`
  (`:459-490`, `:529-560`, both call `aMR_ItemPutInFurniture`, `ac_my_room.c:259`), and the count switch that picks
  the ROM message (`ac_my_room_move.c_inc:2838-2875`). The ROM messages embed the item count (1/2/3 item strings).
  `mChoice` supports at most 6 choices (`include/m_choice.h:117`).
- MP already gates these flows with `aMR_PcEditLocked()` (`ac_my_room_msg_ctrl.c_inc:171`), and the 3 vanilla slots
  replicate as part of the house image commit. That stays as it is.
- Transaction framework (`pc/src/pc_net_game.c`): 64-byte `PCNetGameTxnTag` (`:1475`), TXN_COMMIT 51 / TXN_RESULT 52
  (`:2627-2666`), kinds 1..17 in use (`:2483-2613`, last = EVNPC 17), reasons 0..36 (last = EVNPC_DONE 36).
  The per-resident journal is 16 entries (`PC_NETGAME_TXN_RING`, `:16774`), **in memory, host_session scoped** - it
  is cleared on host restart (`:13404`). Reservation-less one-phase kinds are dispatched at `:28858-28880`
  (`pcnetgame_handle_host_ts_txn`, `:19684`). Durable sidecar precedent: `work_jobs.dat` via `pc_tabfile`
  (`pc/src/pc_tabfile.c`, atomic MoveFileEx/rename, magic, version, FNV check; path set in `pc_server.c:299`).

**Decision:** a safe complete implementation needs (a) a new client UI flow in `ac_my_room` (new message states,
a paged choice window, PC-built strings, a wait-for-result state with the pocket lock), (b) a host handler + client
sender for a new TXN kind, (c) a durable sidecar with write-ahead roll-forward, (d) two new messages. (a) alone is a
large vanilla UI change and cannot be finished and tested safely in a 20-minute slot. Only (c)'s pure store module
was built (groundwork, unit-tested, not called).

## 2. Data layout (implemented: `pc/include/pc_dresser_store.h`, `pc/src/pc_dresser_store.c`)

- One **pool per character identity** (16-byte key: resident PersonalID bytes / guest key), shared by all wardrobes
  of that player's house. Rationale: moving, selling or storing a dresser can never orphan extra items.
- `PCDresserRec` (680 B, static-asserted): key[16], rev, nonce, max_seq, fenced[4], head/n, `items[60]` (u16,
  0 = empty), journal `jr[16]` of `PCDresserJrnl` (32 B: nonce, seq, msg_hash, pre/post pocket digests, rev, op,
  outcome, reason, slot, item).
- File `save/mp/dresser_store.dat` (next to `work_jobs.dat`; path to be added to `pc_server.c` as
  `pc_server_dresser_path()`), pc_tabfile v1, magic `0x52534344` ("DCSR"). Missing file = all pools empty (pre-feature
  save). Corrupt/wrong version = load returns -1, store comes up empty; the host must rename the bad file to
  `dresser_store.dat.bad-<time>` and **disable the feature for the session** (never overwrite it).
- Idempotency is persisted: same (nonce, seq, hash) -> REPLAYED (also after a host restart); same (nonce, seq) other
  hash -> CONFLICT; seq <= max_seq not in the journal, or a superseded nonce -> FENCED.

## 3. Wire

- **TXN kind 18 `PC_NETGAME_TXN_KIND_DRESSER`** (next free kind; confirm with the lead that nobody took 18), sent as
  a normal 72-byte TXN_COMMIT (51), reservation-less one-phase like SHOP_BUY. Tag use:
  - `aux_cond` = op (1 PUT pocket->pool, 2 TAKE pool->pocket); `aux_item` = pool slot 0..59 (PUT: 0xFF = first free).
  - PUT: dest NONE, `slot` = source pocket, `item` = the pocket item (must be cond NORMAL and pass the vanilla
    `mSM_IV_OPEN_PUTIN_FTR` filter re-checked on the host). TAKE: dest POCKET, `slot` = a free pocket, `item` = the item
    the client expects in the pool slot.
  - `pre_pockets/pre_conds/pre_wallet`, `base_epoch/base_rev` exactly as every TXN (64-byte pre-image convention).
- Reasons: reuse existing numbers (module enums equal them): NO_ROOM 22 (slot taken/pool full), NOT_AVAILABLE 16
  (slot does not hold the expected item), CONFLICT 7, FENCED 6, STALE_IMAGE 10, PRECOND 9 (not owner / not in own
  house / item not storable / pocket not free), BAD_SHAPE 11, BUSY 12, REPLAYED 14. One NEW reason
  **37 `DRESSER_DISABLED`** (sidecar corrupt or feature off) - only if the lead confirms 37 is free.
- **Msg 83 `DRESSER_STATE`** (H->C, RELIABLE, 136 B packed + `_Static_assert`): u8 type, u8 version(1), u16 nslots(60),
  u32 rev, u32 digest(FNV of items), u32 _rsv, u16 items[60]. Sent after an APPLIED dresser TXN and on query.
- **Msg 84 `DRESSER_QUERY`** (C->H, 8 B): type, rsv, u16 rsv, u32 known_rev. Host answers with 83 only for a READY,
  bound peer in its own house scene; rate-limited 1/s.
- Capability: a HOST_CONFIG feature bit (lead allocates a free bit). A client never shows the extra UI unless the bit is
  set; an old host ignores 83/84 (unknown ids silently ignored) and never sets the bit. An old client never sends kind 18
  and sees only the vanilla 3 slots; its pool items stay safely in the sidecar.

## 4. Host functions (pc_net_game.c, new block, ids 83/84 + kind 18 only)

- `pcnetgame_dresser_init/load()` at host session start (after `work_jobs.dat`), `pcnetgame_dresser_flush()`.
- `pcnetgame_handle_host_dresser_txn(peer, in)`: READY + binding + scene (own house) gate -> journal check
  (`pcnetgame_txn_journal_find`) -> validate pre-image vs mirror (STALE_IMAGE) -> compute post pockets ->
  **write-ahead order**: `pc_dresser_store_apply()` then `pc_dresser_store_save()` (atomic) BEFORE the mirror pocket
  change + `pcnetgame_txn_journal_add` + TXN_RESULT. If the save fails: undo in memory, REJECTED FAULT.
- **Restart roll-forward:** on load, for each identity, `pc_dresser_store_last_applied()`; if the resident mirror's
  pocket digest == entry `pre_pocket_dig` (the mirror save was lost) apply the pocket side (post image); if it equals
  `post_pocket_dig` nothing to do; anything else -> log, leave both (the pool wins; no grant twice).
- Dispatch: one line in the TXN_COMMIT branch (`:28865` pattern) + one `pcnetgame_txn_kind_name` entry (`:5788`).

## 5. Client functions + UI hook points

- `pc_net_game_dresser_available()` (bit set, connected, own house, not edit-locked), `pc_net_game_dresser_view()`
  (last 83 snapshot), `pc_net_game_dresser_begin(op, pool_slot, pocket_slot, item)` (reuses `pcnetgame_txn_begin`,
  resend every 2 s, the existing inventory lock), `pc_net_game_dresser_poll()` -> PENDING/APPLIED/REJECTED.
- `ac_my_room_move.c_inc:2850-2863`: owner with any count -> if available, new state `aMR_MSG_STATE_PC_DRESSER_MENU`.
- `ac_my_room_msg_ctrl.c_inc`: new TARGET_PC states MENU (choices: vanilla "Take out"/"Put in" + "More storage"),
  PAGE (paged list, 4 items + Next + Cancel, PC-built strings, `mChoice_Set_choice_data`), WAIT (blocks input until
  poll != PENDING, full-pocket / reason text), and PUT_IN reusing `mSM_IV_OPEN_PUTIN_FTR` but calling
  `pc_net_game_dresser_begin` instead of `aMR_ItemPutInFurniture` (`:482/:552`). The pocket removal is NOT done locally:
  it comes from TXN_RESULT post_pockets like every TXN kind.
- Only wardrobe-type furniture (not music players); NPC houses never (`:2847`).

## 6. Migration

Pre-feature saves: no sidecar -> empty pools. The 3 vanilla slots are untouched. Removing the feature later: pool
items remain in `dresser_store.dat`; a one-shot "return to mail" tool would be needed - never silently delete it.

## 7. Tests

- Done: `pc/tools/net_spike/dresser_store_selftest.c` (put/take, replay, conflict, fence, nonce reconnect, eviction,
  full pool, identity isolation, save/reload/replay, corrupt and junk files).
- To do: FakeClient protocol test (kind 18 APPLIED/REJECTED, duplicate COMMIT replayed, lost TXN_RESULT retry, wrong
  scene/owner, old-host ignores 83/84); restart test (kill host between sidecar save and mirror save -> roll-forward);
  unit test of the 83 digest; UI test via an AC_TEST_ hook driving the new states.

## 8. Risks

- Two durable stores (sidecar + mirror) -> needs the write-ahead roll-forward above; get it wrong and items duplicate.
- The in-memory TXN journal is cleared on restart; the sidecar journal covers dresser kinds only.
- UI: new message states inside vanilla `ac_my_room` (demo/message timing, submenu return, cancel paths).
- Guest identity churn (guest key eviction from the guest store) must not delete the pool: pools are keyed by stable
  identity and are only removed by an explicit admin action.
- HOST_CONFIG bit and reason 37 / kind 18 numbers must be confirmed against concurrent work.
