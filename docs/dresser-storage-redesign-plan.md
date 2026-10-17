# Dresser storage redesign: per-dresser, 27 total, gyroid-style (plan, worker 3, 2026-10-11)

Status: PLAN + CHECKPOINT. The feature default is OFF again (`AC_MP_DRESSER_STORAGE=1` enables) until this redesign is verified.
The current Sprint-2 implementation (per-RESIDENT pool of 60, choice-list UI) stays in the tree and is modified in place.

## 1. What exists today (inspected)

- Vanilla keep slots: `FTR_ACTOR.items[aFTR_KEEP_ITEM_COUNT = 3]`, stored in the house image in the upper layers (1..3) at the
  dresser's origin cell (`aMR_KeepItem2Fg` / `aMR_MakeItemDataInFurniture`). They travel with the dresser when it is pushed / pulled /
  rotated; picking the dresser up moves them into the pockets if there is room (`aMR_JudgePickUpCommon`, `ac_my_room_move.c_inc:1620`,
  `:1850`), otherwise the pickup is refused.
- Gyroid (haniwa) UI: a submenu overlay `mSM_OVL_HANIWA` (`src/game/m_haniwa_ovl.c`) opened from the inventory overlay
  (`m_inventory_ovl.c:2086`, modes `mSM_HANIWA_OPEN_ENTRUST` / `_TAKE`); it draws a 4-slot panel beside the pocket grid
  (`HANIWA_ITEM_HOLD_NUM = 4`, data read straight from `Save_Get(homes[data1]).haniwa.items`), the cursor moves between the pocket
  table and `mTG_TABLE_HANIWA` (`m_tag_ovl.c:136`: 4 columns x 1 line), item names come from the tag overlay, the short status
  lines are the built-in strings of `mHW_make_cond_message` ("Choose one.", "You don't have room.", "Got it!" ...).
- Host side: TXN kind 18 (put/take, 64-byte pre-image), msgs 83/84, sidecar `dresser_store.dat` v1 keyed by the resident identity
  (16 B), 60 slots, persisted journal, write-ahead save, in-room furniture-sync gate + baseline rebase.

## 2. Target data model

- One store record per DRESSER IDENTITY = { owner house PersonalID (the homes[] owner, not the visitor), floor (0..2), layer (0/1),
  origin ut_x, ut_z, base furniture id (rotation bits masked) }, packed into the existing 16-byte key slot of the keyed table
  (owner PersonalID digest 8 B + floor/layer/ut/ftr 8 B; collisions of the 8-byte digest are refused, never merged).
- Capacity: 27 TOTAL per dresser = the 3 vanilla slots (unchanged, in the house image) + 24 extra slots in the sidecar. The host
  checks `vanilla_count(house image) + extra_count <= 27` is implied by the fixed 24 extra slots; a FULL put is refused (REJECTED,
  nothing moves).
- Sidecar `dresser_store.dat` **version 2** (new record layout: 24 slots, identity key, per-identity journal). A v1 file is never
  deleted or migrated automatically: on first v2 start it is renamed to `dresser_store.v1.bak` (if no such backup exists yet) and the
  v2 store starts empty (the v1 pools were unreleased test data); the rename is logged.
- Wire (kind 18 / 83 / 84 never shipped, so they change in place; exact-size dispatch, static asserts, the TXN 64-byte pre-image and
  the 72-byte TXN_COMMIT unchanged):
  - msg 84 becomes DRESSER_SELECT/QUERY (new fixed size: house, floor, layer, ut_x, ut_z, ftr id): the host verifies the furniture is
    at that cell of the requester's OWN house image and remembers it as the peer's selected dresser, then answers 83;
  - msg 83 STATE (new fixed size): the identity + 24 extra items + rev + digest;
  - kind-18 COMMIT keeps its tag layout (`aux_cond` = op, `aux_item` = extra slot); `tag.flags` carries an 8-bit selection
    generation so a COMMIT can only apply to the dresser the peer selected last (stale selection -> REJECTED, nothing moves); the
    host re-verifies the identity against its house image at COMMIT time.

## 3. ARCHITECTURAL BLOCKER: identity under furniture moves (needs a lead decision)

A placed dresser has no stable id in the save: only (cell, floor, layer, furniture id). Vanilla's 3 items survive moves because they
live INSIDE the actor and are re-written at the new origin by the teardown. The extra 24 live on the host, keyed by position, so every
push / pull / rotate / pickup changes or destroys the key, and the host only learns the move later, in an OWNER_COMMIT image diff
(several moves can be folded into one commit; two identical dressers can swap; a dresser can be picked up and re-placed elsewhere).
A deterministic, conservation-safe re-key is only possible when exactly one furniture of that id vanished and exactly one appeared on
that floor/layer between two committed images. Options:

- **A (recommended, smallest provably safe):** a dresser whose EXTRA storage is non-empty cannot be moved, rotated or picked up by a
  client with the feature (TARGET_PC gate in the room push/pull/rotate/pickup judges, the vanilla refusal path + the gyroid-style line
  "It's too full to move."); the player first takes the extra items out (the vanilla 3 still travel as before). Host safety net for
  peers WITHOUT the gate (old client, modified client, the host's own player while offline): on an applied OWNER_COMMIT, a pooled
  identity whose cell no longer holds that furniture is re-keyed ONLY in the unique 1-vanished/1-appeared case; otherwise it becomes
  ORPHANED (kept in the sidecar, never deleted, logged, listed by a console command, and re-attached to the next identical dresser the
  owner places on that floor). No item is ever lost or duplicated; the worst case is a temporarily unreachable pool.
- **B (full move support):** the client reports each dresser actor move (old cell -> new cell, it knows the actor) in a REKEY op of
  kind 18 sent right after the move ends; the host applies it only when the next committed image confirms it (pending re-keys matched
  against the commit diff, otherwise dropped). Correct for swaps and multi-moves, but adds a second ordering-sensitive path coupled to
  furniture sync (in-room commits / leave commits / reconnect taint); estimated 3-4 extra 20-minute stages + tests.

## 4. UI (gyroid style) - second decision

The gyroid panel is a fixed 4-slot table (`mTG_TABLE_HANIWA`, 4 x 1, fixed frame art). 27 slots in that style need either
- **U1 (recommended):** the gyroid panel reused with PAGES of 4 (7 pages: page 1 = the 3 vanilla slots + extra slot 1 ...), page change
  with the shoulder buttons / C-stick left-right on the panel, a small "1/7" page number drawn with the existing font; entrust / take
  through the existing inventory+haniwa overlay flow (`mSM_HANIWA_OPEN_ENTRUST` / `_TAKE` equivalents for the dresser), item names /
  icons / cursor / "You don't have room." / "Choose one." reused; or
- **U2:** a taller panel (e.g. 9 x 3) = new frame art + new tag geometry (larger change, not the original look).

## 4a. U3: 3 x 9 single page (implemented, replaces U1)

All 27 slots on ONE page in a 9 columns x 3 rows grid, in the gyroid overlay's dresser mode. There is no paging, no L/R page turn and no
"N of 7" indicator; L/R do nothing in dresser mode. The gyroid portrait backdrop, the dialogue frame and the dialogue text are not drawn,
and their space holds the grid.

Measurements, read at runtime from the ROM vertex data. Units are screen px of the 320 x 240 overlay space, origin at the centre, y up.

| Element | x | y | Note |
|---|---|---|---|
| Gyroid window (`hni_win_v` union) | -126..90 | 14..110 | 216 x 96 |
| Portrait backdrop `hni_nwin_3DmT` | -116..-68 | 39..87 | not drawn in U3 |
| Dialogue frame `hni_nwin2_wakuT` | -89..86 | 62..97 | not drawn in U3; its text is not drawn either |
| 4-cell strip `hni_nwin2_win2T` | -47..49 | 34..58 | 24 px per cell; not drawn in U3 |
| Pocket grid (`mTG_item_col_pos` / `_line_pos`) | -77..19 | -10 / -34 / -58 | 5 x 3, 24 px cells, below the window; stays visible and reachable |

- Item icons: 24 px; the icon draw maps x by 0.975 (`mSM_set_dl_item`).
- **Limitation:** 9 cells at the native 24 px pitch would be 216 px, the full window width including its border. U3 therefore uses a 22 px pitch.
  - The grid is 198 x 66, centred at x -18, with row centres y 84 / 62 / 40.
  - The margins are 9 px left/right and 18 px top/bottom.
  - Icons are drawn at 7/8 size (21 px).
  - Nothing else shrinks.
- Cells use the gyroid's own cell texture and colours (win2T setup), with its strip quad re-spanned to 9 cells for each of the 3 rows. An empty slot shows its empty cell.
- Mapping: grid index = row * 9 + col. Grid 0..2 are the 3 vanilla keep slots; grid 3..26 are extra slots 0..23.
- Navigation:
  - The stick or D-pad moves within the grid.
  - Down from the bottom row goes to the nearest pocket column in the top pocket row.
  - Up from the top pocket row goes to the nearest grid column in the bottom row.
  - Grid edges stop the cursor.
- Feedback: the gyroid's sounds and the existing tag option windows ("Take it" / "Put this in", option row 0). There is no dialogue line.
- Evidence (runtime-verified): `test_dresser_real.py --ui-gyroid` 16/16.
  - The scripted pad input reached all 27 cells, with 0 presses not consumed.
  - TAKE of extra slot 1 was applied by the host.
  - The cursor went down to the pockets; put went into a vanilla slot.
  - Close, room exit and the leave-commit (OWNER_COMMIT APPLIED) all passed.
- Not verifiable here: the visual look. No visual testing is allowed, so the layout is checked by geometry only.
- Test input note: the tag handler drops a press made while the hand is still moving or the item tag is still growing. A has no key repeat, so the scripted input waits for the hand and tag to be idle, then holds each press until the menu reports it used it.

## 5. Stages after the decisions (each <= 20 min, each builds)

1. Store v2 (identity key, 24 slots, journal) + v1 backup rename + native selftest.
2. Host: select/query by identity (84 new size), STATE by identity (83 new size), kind-18 COMMIT validated against the selected
   identity + the host house image (furniture at that cell, requester owns the house, vanilla+extra <= 27), FULL refusal.
3. Client: per-identity snapshot + sender; protocol FakeClient tests (two dressers independent, 27 limit, replay, restart).
4. Option A gates (move/rotate/pickup refusal while extra non-empty) + host commit re-key / orphan; tests (unique move re-key,
   ambiguous -> orphan kept, nothing lost).
5. UI U3 (was U1): dresser mode of the inventory+haniwa overlays, one 9 x 3 page; UI-driven real run + leave-commit run.
6. Default ON only after 2-5 pass.

## 6. Decisions (lead, 2026-10-11)

- Moves: **Option A**. A client with the feature refuses to move / rotate / pick up a dresser whose EXTRA storage is non-empty (an
  existing-style refusal line if cheap). Host safety net: re-key only the unambiguous single-move case (exactly one furniture of that
  id vanished and exactly one appeared on that floor/layer in one applied commit); otherwise the storage becomes ORPHANED (kept, never
  deleted, logged) and is re-attached deterministically to the next identical dresser that owner places on that floor (lowest cell
  index first). Everything logged.
- UI: **U1** (gyroid 4-slot panel, 7 pages of 4, page 1 = the 3 vanilla slots + extra slot 1, shoulder / C-stick page turn, "1/7").
  **Superseded by U3 (user requirement, 2026-10-11): see section 4a.**
- Sidecar v1 -> `dresser_store.v1.bak` happens ONCE and never overwrites an existing backup (the `.v1.bak` is then migrated lazily, see 6b; a timestamped name
  `dresser_store.v1.bak-<time>` is used if `dresser_store.v1.bak` already exists).
- Default stays OFF until stages 2-5 pass with real evidence.

## 6a. Hardening: identity under swaps and multi-moves (implemented, protocol-verified)

**What travels with a piece.** A dresser's 3 vanilla keep items live in the upper layers (layer + 1 .. 3) at the piece's origin cell. Both the vanilla
`aMR_KeepItem2Fg` and the PC snapshot `aMR_pc_export_home` write them there, so they move with the piece in every house commit. The furniture id
also carries its rotation in the low 2 bits.

The per-piece **fingerprint** is (raw id with rotation, keep item 1, keep item 2, keep item 3), computed by `pcnetgame_dresser_img_fp`.

Fields that do NOT identify a piece:
- `ftr_switch` is a per-cell bit and is 0 for storage pieces.
- `haniwa_step` is gyroid-only.
- No per-piece serial exists in the save. Adding one would change vanilla data, which was rejected.

**Rule (host, phase 0 of the house-commit hook, `pcnetgame_dresser_follow_fingerprints`).** A stored dresser follows its piece only when all of these hold:
- its piece really LEFT its cell (the cell no longer holds that id);
- its fingerprint is unique among that owner's pieces of that floor, layer and id, in the old image AND in the new image;
- the matching piece stands on a cell that newly holds that id.

Moves go through temporary keys with a collision check, so records never merge. Everything else keeps the earlier rule: single-move re-key, otherwise
orphan, then deterministic re-attach.

**Limitation (unresolvable without changing vanilla data):** two identical dressers that SWAP cells keep their storage bound to the CELL.
- Reason: their cells keep the same id, so the commit image of such a swap is byte-identical to moving a vanilla keep item from one dresser to the other.
  That is a normal in-room action. Following fingerprints there would wrongly exchange two 24-slot storages.
- Nothing is lost: both storages stay with the owner.
- When the two pieces also have identical vanilla contents (e.g. both empty), they are indistinguishable to the player as well.
- With the feature, clients cannot move a dresser that holds extras at all (Option A). Only an old client can produce the case.

**Evidence.** `test_dresser_storage_protocol.py`: **55/55** on a real host with the real OWNER_COMMIT path (protocol-verified).
- M1 single move re-keyed.
- M2/M3 pickup gives ORPHANED, then RE-ATTACHED.
- N: two identical dressers with identical contents moved in one commit are both ORPHANED, then RE-ATTACHED deterministically, {23, 1} kept.
- S1: putting a keep item into a dresser changes nothing.
- S2: the "swap equals keep-item transfer" image stays cell-bound (nothing followed or orphaned).
- S3: an old client moves BOTH identical dressers in one commit with distinct contents, and both storages FOLLOWED their own pieces. The lowest-cell
  orphan rule would have crossed them.
- D8/D9: after the host restart the contents are unchanged and conservation holds.

## 6b. Migration of the unreleased v1 shared pool (implemented, protocol- and native-verified)

The v1 file held one 60-slot pool per resident (`dresser_store.dat` v1, 680 B records).

**Behaviour**
1. **Backup.** At host start the v1 file is renamed atomically to `dresser_store.dat.v1.bak`. It is never deleted or overwritten. If `.v1.bak` already
   exists, a later v1 file becomes `.v1.bak-<time>`; that file is kept and logged but **never migrated**, because it could repeat items.
2. **Migration source.** `.v1.bak` is loaded read-only (`pc_dresser_v1_load`, with a digest of the whole file). A v1 file is never read as v2
   (the version check refuses it).
3. **Lazy, per resident.** Migration happens at that resident's first probe or SELECT, when the host knows the resident's house image.
   - The v1 items, in slot order, fill the FREE extra slots of that house's item-storage dressers.
   - Dressers are ordered (floor, layer, cell) ascending, at most 24 each, and existing v2 items are kept.
   - The rest goes to the resident's **legacy overflow** records: key floor 0xFD, pages 0..2, 24 per page. That is enough for 60.
4. **Marker.** Overflow page 0 is the per-resident marker (`_r = 1`, `nonce` = the v1 digest). The marker and all assigned items reach disk in ONE
   atomic v2 save (tmp, then MoveFileEx). If that save fails, the in-memory change is undone by reloading the file.
5. **Restart safety, from any state:**

   | State at restart | Result |
   |---|---|
   | v1 at the main path | Renamed, then migrated |
   | `.v1.bak` and no marker | Migrated from the unchanged `.v1.bak` |
   | Marker saved | Skipped |

   Every v1 item ends up exactly once.
6. **Claim path.** When the resident SELECTs one of their dressers that has free room, overflow items move into it (page and slot order, saved
   atomically). Overflow is never sent on the wire, never shown as a dresser, and excluded from the identity scans (floor 0xFD is no real floor).
7. **Not migrated (logged, kept in the backup):** pools of identities that are not a resident of this town; a `.v1.bak` that cannot be read.

**Evidence**
- Native `dresser_store_selftest.c` V7: **PASS**.
  - Full 60-item pool with 2 dressers, one of them already holding 20: 4 + 24 placed, 32 to overflow, in v1 slot order.
  - Crash before the save gives nothing migrated, and the redo gives the same result.
  - Crash after the save means the marker stops a second run.
  - Conservation: 60/60 items, each exactly once.
  - The claim moves 24, then stops when full, and the marker survives.
  - No dressers: 60 go to the overflow.
  - 5 items with 2 dressers: all 5 go to the first dresser.
  - A v1 file is never loaded as v2.
- `test_dresser_migration_protocol.py`: **19/19** on a real host (protocol-verified).
  - G1: `.v1.bak` is kept.
  - G2: migrated once, 48 items into 2 dressers and 12 to the overflow; the main file is now v2.
  - G3: deterministic slot order.
  - G4: two TAKEs, then a re-SELECT claims 2 (10 left).
  - G5: after a host restart there is no second migration and the contents are the same.
  - G6: conservation 50/50 visible plus 10 in the overflow; the `.v1.bak` with the unknown identity's pool is untouched.

**Known gap:** the overflow is not visible in the UI. The player gets the items only when a dresser has free room and is opened.

## 6c. Option-A refusal through the REAL input path (runtime-verified)

`test_dresser_real.py --ui-lock`: **12/12** on a real client and a real host.
- **Setup:** one extra item is seeded into a chest. The client is in its own room; a test hook places the player on a free unit next to the chest,
  facing it.
- **B (pickup)** goes through the real pad path (`Player_actor_CheckController_forPickup`, then `aMR_SearchPickupFurniture`, then `aMR_JudgePickUpCommon`).
  It is **REFUSED**: notice 3, the refusal line is logged, the chest stays and is not in a pocket.
- **A (push / pull / rotate grab)** goes through the contact-grab judge. It is **REFUSED**: no grab state, the chest stays. Rotation needs a grab, so
  it is covered too.
- **After the extra item is taken out** (real dresser page, kind-18 TAKE APPLIED), B again: the pickup is **ALLOWED** and the chest is in a pocket.
- **Leave-commit:** the room exit commits the pickup (OWNER_COMMIT APPLIED), with no conservation failure or taint.
- **Limit:** the player is placed by the test hook rather than walked there; everything from the button press on is the real game path.

## 6d. Hardening verification summary (one exe, 2026-10-11 23:12 build)

| Test | Result | Label |
|---|---|---|
| `test_dresser_real.py --ui-gyroid` (UI-driven take / put / leave-commit) | 16/16 | runtime |
| `test_dresser_real.py --ui-lock` (Option-A refusal on the real input path) | 12/12 | runtime |
| `test_dresser_real.py --ui-edit` | 19/19 | runtime |
| `test_dresser_real.py` (sender round trip) | 9/9 | runtime |
| `test_dresser_storage_protocol.py` (incl. S phase and host restart) | 55/55 | protocol |
| `test_dresser_migration_protocol.py` | 19/19 | protocol |
| native `dresser_store_selftest` | PASS | native |
| `test_dresser_ui_source` | 16/16 | source |
| `test_house_sync_src` | 85/85 | source |
| `test_dresser_msg_patch` | 13/13 | source |

- In --ui-edit V1, the single move of a dresser with a unique fingerprint is now logged by the fingerprint pass ("FOLLOWED") instead of the
  single-move rule. The outcome is the same, and the check accepts either line.
- Unverified:
  - the visual look of the U3 grid (no visual testing allowed);
  - the overflow claim path through a real client UI (protocol-verified only);
  - a real OLD client binary performing the swap / multi-move (simulated by the FakeClient commit path).

## 6e. Visual defect, direct open, item-type policy (investigation 2026-10-12)

**Verification status: NOT TESTED (investigation only).** No code was changed and no build was made. The only new artefact is the pure-python
`test_dresser_layout_geometry.py` (8/8 on the measured geometry).

### Observed defect (user, 23:12 build)
The 3x9 grid is drawn over the gyroid window's large orange border, so the screen looks crowded and wrongly layered.

### Root cause (source + measured vertices)
In dresser mode, `m_haniwa_ovl.c mHW_pc_dresser_frame` draws `hni_win_modelT[0..17]` unchanged:
- w1..w12: the shadow copies of the border pieces;
- w13: the inner background;
- `hni_nwin2_winT_model` (winT): the window panel. Its IA textures ue1..ue7 are tinted orange (prim 245,125,55). The pieces form a 24-unit-thick
  BORDER RING (top y 86..110, bottom y 14..38, left x -126..-102, right x 66..90) around an inner fill (ue4 quads + w13) of only
  **x -102..66, y 38..86 (168 x 48)**.

The 23:12 layout measured the window as the union of all quads (x -126..90, y 14..110) and placed 198 x 66 of cells at x -117..81, y 29..95.
That covers 15 units of each side band and 9 units of the top and bottom bands. `test_dresser_layout_geometry.py` C1 counts 37 cell/ring overlaps.

The grid appears ABOVE the border for two reasons:
- the cell quads (a re-spanned win2T) are appended after winT in the same POLY_OPA list;
- the icons are drawn afterwards by `draw_item_proc`.

There is no Z or scissor in this 2D path, so draw order decides.

Not the cause:
- **Scaling:** frame and icons share the overlay projection; icon x is mapped by 0.975 in `mSM_set_dl_item`, which is already compensated in `col_pos`.
- **Other overlays:** in dresser mode the portrait (`set_haniwaPortrait_proc`), the dialogue text (`mHW_set_message`), the dialogue frame (wakuT),
  the portrait backdrop (3DmT) and the 4-cell strip (win2T) are not drawn. The inventory overlay below and the hand/tag windows above are unchanged.

### Intended fix (minimal; files: m_haniwa_ovl.c, m_tag_ovl.c, pc_dresser_ui.c)
1. **Enlarge the window by 9-slice instead of shrinking the grid.**
   - Edge and corner pieces keep their pixel size and move outward; the fill stretches.
   - Map x: x <= -102 -> x - 18; x >= 66 -> x + 18; between -> linear onto -120..84.
   - Map y: y <= 38 -> y - 12; y >= 86 -> y + 8; between -> linear onto 26..94.
   - Result: window x -144..108, y 2..118, inner 204 x 68. It stays above the pockets' top edge (y 2) and inside 320 x 240.
   - Implementation: remap the runtime-loaded `hni_win_v[]` in place at `pc_dresser_ui_begin`, keeping a backup, and restore it at
     `pc_dresser_ui_end`. As a guard, `mHW_set_frame_dl` restores it whenever it draws a non-dresser gyroid window.
   - emu64 reads vertex memory at DL execution (`dl_G_VTX`, no cache), so the change is visible the same frame.
2. **Cells:** 9 x 3 at a 22-unit pitch, centres x = -18 + 22(c - 4), y = 82 / 60 / 38. The cells are drawn after winT, now inside its inner
   rectangle. Fill the uncovered strip hole (P6: x -53..63, y 22..27) with one ue4 quad copied from winT's state.
3. **Icons and cursor:** icons stay at 21 px (7/8). `mTG_pc_dresser_line_pos` becomes {82, 60, 38}; `col_pos` is unchanged (x / 0.975).

### Direct open (proposal)
**Current path**
1. A in front of storage furniture: `ac_my_room_action.c_inc aMR_JudgeDemoStart` (:1160).
2. `aMR_SetOpenFtrDemoData` (:1141) sets WAIT_MSG and the open demo.
3. `ac_my_room_move.c_inc aMR_SetFtrDemoMsg` (:2837) picks OWNER_NO_ITEM / OWNER_EXIST_ITEM / _COUNT2 (0-2 items, "More storage" patched in by
   `pc_dresser_msg_arm`, :2891) or OWNER_WHICH_PUT_OUT1 (3 items).
4. The choice goes to `aMR_PcDresserMoreChoice` / `aMR_MsgInitOwnerWhichPutOut` (:427), then `aMR_PcDresserOpenStorage` (:211), then
   `aMR_MSG_STATE_PC_DRESSER_GYROID`.
5. That state (`aMR_MsgControlPcDresserGyroid`, :1939) needs no message window and already closes the piece like vanilla
   (`mPlib_request_main_close_furniture_type1` -> WAIT_CLOSE_FTR).

**Change** (`aMR_SetFtrDemoMsg`, TARGET_PC). Applies only when all of these hold:
- owner, own house;
- `aMR_PcDresserOn()` and `aMR_pc_ftr_is_item_storage(item)`;
- `!aMR_PcEditLocked()`.

Then:
- Select the piece and stay in WAIT_MSG until `pc_net_game_dresser_view_ready()`, then request PC_DRESSER_GYROID.
- If no answer within about 180 frames (host slow, refused or disconnected), fall back to the unchanged vanilla OWNER_* message, so the dresser is
  never unusable. (Superseded by 6x: the wait is 3 s of wall clock, not frames.)

Unchanged: visitors, NPC houses, other furniture, the edit-lock path, host checks, and close/commit.

### Item-type policy (proposal)
**Today:** the host `pcnetgame_dresser_item_ok` (`pc_net_game.c`:20793) mirrors the vanilla `mSM_check_item_for_furniture`:
- ITEM1 only;
- no fish, insects or turnips;
- no knife and fork, no exercise cards.

It is used by the host commit check (:20932), the client STATE check (:21535), the UI predicate (`pc_net_game_dresser_item_storable`, :28611)
and the put-in tag (`m_tag_ovl.c`, :7400). The pocket condition must be NORMAL (`pc_net_game_dresser_pocket_item`).

**Proposal for the extra slots:**
- **Accept:**
  - every ITEM1 category except KABU (turnips age by their item id; storing them would dodge spoilage): paper, money bags, tools, clothing and
    umbrellas, carpets, wallpaper, fruit, plants and seeds, music, diary, tickets, lucky bags;
  - fish and insects except the Wisp spirits (`ITEM_IS_WISP`);
  - FTR0/FTR1 furniture with (item & 3) == 0 (a pocket furniture id is rotation-free, so no metadata is needed).
- **Reject:**
  - EMPTY_NO / RSV, ITEM0, ITEM2, WARP / STRUCT / ACTOR / PROPS;
  - anything failing `mNT_check_unknown`;
  - knife and fork, exercise cards;
  - any pocket condition other than NORMAL: PRESENT (wrapped gifts) and QUEST (work parcels and deliveries) would need a condition field. The
    608-byte record (24 x u16) has no room, so this would mean a v3 format.
- **The 3 vanilla slots keep the vanilla filter.** Their items live in house layers 1..3 and are read back as keep items. Furniture, fish and insects
  go to the EXTRA slots only: `pc_dresser_ui_put` must skip the vanilla slots when `mSM_check_item_for_furniture` fails.
- Stacking: AC GC has no stacks. Money bags are single items.

### Test plan (after approval)
- `test_dresser_layout_geometry.py` (exists): extend it to read the runtime `hni_win_v` dump.
- Source test: the direct-open branch (owner + item storage + feature + not locked) with its fallback timeout.
- `--ui-gyroid` variant that opens with A through the real pad path (the `--ui-lock` placement); also empty and occupied pieces.
- Protocol matrix (FakeClient): one PUT/TAKE per item class, accepted or rejected as above; host restart persistence; furniture taken back with
  rotation 0.

## 6f. Implementation of 6e (approved 2026-10-12) - SOURCE + TESTS ONLY, NOT BUILT

**Verification status:**
- **Visual layout NOT verified.** The user inspects it manually.
- **Nothing was built.** `build_dev.sh` was not run; `pc\build64\bin\AnimalCrossing.exe` is unchanged.
- **Runtime and protocol scripts:** written, not run (they need a build).
- **Verified:** source tests, native selftests and gcc `-fsyntax-only` with the ac_pc flags.

### 1. Layout: 9-slice enlargement of the gyroid window
New pure module `pc/src/pc_dresser_layout.c`, `pc/include/pc_dresser_layout.h`.

**Remap**
- x: x <= -102 -> x - 18; x >= 66 -> x + 18; between -> linear onto -120..84.
- y: y <= 38 -> y - 12; y >= 86 -> y + 8; between -> linear onto 26..94.
- Result: window x -144..108, y 2..118, inner rectangle 204 x 68.

**Texture coordinates**
- **Fill quads** (w13 background v0..3, ue4 fill v31..40) keep their texel density. Their s,t follow the original affine mapping (ds/dx = 42.667,
  dt/dy = -42.667, measured on the real table). The tiles use GX_MIRROR, so they repeat instead of stretching.
- **Border pieces** keep their UVs. Corners only move; edge pieces stretch along their length (horizontal x1.21, the side bands vertical x1.42).
  Not verified visually.

**Backup and restore**
- The whole 0x800-byte table is backed up byte-exact. The remap is always computed from that backup.
- A second begin is a no-op. End restores byte-exact. End without begin is a no-op.
- A table that is not the measured gyroid window is left untouched.
- Restore points:
  - every draw of a NON-dresser gyroid window: guard in `mHW_set_frame_dl`, before the display list;
  - the overlay destroy (`mHW_haniwa_ovl_destruct`);
  - `pc_dresser_ui_end`.
- emu64 reads vertex memory when the display list runs (`dl_G_VTX`, no cache).

**Drawing**
- `m_haniwa_ovl.c` draws `hni_win_modelT[0..12]`.
- Then one extra background quad (w13's own texture setup) under the hole the skipped strip leaves: x -54..64, y 22..28.
- Then `[13..17]` (state + winT ring/fill), then the 27 cells (pitch 22, rows y 82/60/38, centres x -18+22(c-4)).
- Icons are drawn at 7/8 size (21 px).
- `m_tag_ovl.c` `mTG_pc_dresser_line_pos` = {82, 60, 38}.
- Navigation is unchanged. No portrait, dialogue or strip is drawn.

### 2. Direct open
`ac_my_room_move.c_inc` `aMR_PcDresserDirectOpen` is called first in `aMR_SetFtrDemoMsg` (WAIT_MSG of the open demo).

- **Conditions** (anything else = unchanged vanilla):
  - owner, own house, not an NPC house;
  - `aMR_PcDresserOn()` (feature available, house link reconciled);
  - not edit-locked;
  - item-storage furniture.
- **Flow:**
  1. Select the piece.
  2. If `pc_net_game_dresser_view_ready()`, request `aMR_MSG_STATE_PC_DRESSER_GYROID`. No message window is needed; that state opens the overlay and
     closes the piece like vanilla.
  3. Otherwise wait in WAIT_MSG.
  4. After `aMR_PC_DIRECT_OPEN_TIMEOUT` (180 frames) with no view (slow, refusing or disconnected host), fall back to the vanilla OWNER_* message
     flow, which is unchanged. (Superseded by 6x: `aMR_PC_DIRECT_OPEN_TIMEOUT_MS` = 3000 ms of wall clock.)

### 3. Item types
New pure module `pc/src/pc_dresser_items.c` (item-id rules, macros of `m_name_table.h` only). It is ONE predicate:
- `pcnetgame_dresser_item_ok` = `pc_dresser_item_extra_ok`;
- used by the host commit shape check, the client STATE guard, `pc_net_game_dresser_item_storable` and the put-in tag.

**Extra slots**
- **Accept:**
  - every ITEM1 item with a known index (stationery and tickets carry their quantity in the id), except turnips, knife and fork, exercise cards and
    the Wisp spirits;
  - fish and insects;
  - furniture FTR0/FTR1 with rotation bits 0 and a known index. A picked-up piece IS rotation 0:
    `aMR_UnitNum2FtrItemNoFtrID` -> `mRmTp_FtrIdx2FtrItemNo(name, SOUTH)`.
- **Reject:** every other id type, unknown ids, rotated ids.
- **Pocket condition:** NORMAL only (the host checks it: `pocket slot does not hold the item (or it is not a normal item)`). Wrapped presents and
  quest parcels stay out. No format change: the 608-byte record stores the bare u16, and every accepted class round-trips as the same u16.

**Vanilla slots** keep the vanilla filter (`pc_dresser_item_vanilla_ok`, = `mSM_check_item_for_furniture`). It is applied in `pc_dresser_ui_put`
(ordinary items still fill the vanilla slots first) and again in `aMR_PcGyroidVanillaPut`. Furniture, fish and insects go straight to the extras.

### 4. Move lock for occupied extra storage
**Client** (`aMR_PcDresserMoveLocked` + `pc_net_game_dresser_move_state`):
- Only item-storage furniture can be locked.
- **Allowed only when:**
  - the host answered a gen-matched SELECT of THIS dresser in THIS room visit with 0 extras. A visit = each own-room actor construction,
    `pc_net_game_dresser_room_visit`; `aMR_PcDresserSelectFtr` re-asks every dresser once per visit.
  - or no extra storage can exist: offline / not connected, host feature OFF, or the probe stayed unanswered (old host).
- **Extras > 0:** refused (notice 3).
- **Unknown** (probe pending, no answer this visit): refused (notice 3) and the host is asked, so a later attempt decides.

**Host** (decision: "cannot bypass"), `pcnetgame_house_process_commit` step 9b, `pcnetgame_dresser_commit_locked`:
- Any OWNER_COMMIT in which a stored dresser with extras > 0 no longer has the identical raw id (base AND rotation) at its cell is REFUSED as
  INVALID_CELL, detail 0x2000 | floor << 8 | cell. This covers removed, moved and rotated pieces.
- It runs BEFORE anything is applied.
- **Side effects:** this is the same path as any refused commit (`pcnetgame_house_reject`: ACK, `hsent_seq` reset -> the canonical house is re-pushed,
  stale-push of the record). The client logs "REFUSED ... house + pockets are rolled back by the host's pushes" and sets rollback / room_stopped; old
  clients of this fork use the same path. EVERY edit of that commit (other furniture, keep items, pocket changes of the visit) is rolled back
  together. Nothing is lost or duplicated (conservation), but legitimate edits of that visit are undone.
- Re-key, orphan and fingerprint-follow now only ever act on records with no items.

**Known gaps**
- Host-player (listen-server) local edits of the host's own house are not commits, so step 9b does not see them.
- A host that restarts with the feature OFF does not protect storage.
- An OLD host (no probe answer) makes the client refuse storage-furniture moves for about 15 s after connecting (5 probe tries).

### Tests

| Test | Result | Label |
|---|---|---|
| `dresser_layout_selftest.c` on the REAL table (`extract_hni_win_v.py`, read-only from the user's disc into a scratch dir) and on a synthetic table | PASS | native |
| `dresser_items_selftest.c` (34-case class matrix; sidecar PUT / save / reload / TAKE for the 19 accepted classes) | PASS | native |
| `dresser_store_selftest.c` | PASS | native |
| `test_dresser_layout_geometry.py` (incl. P8 = the native layout selftest on the real table) | 9/9 | source/native |
| `test_dresser_ui_source.py` (13 new 6f checks; HEAD equality of the overlay / room files with TARGET_PC removed) | 29/29 | source |
| `test_house_sync_src.py` | 85/85 | source |
| `test_dresser_msg_patch.py` | 13/13 | source |
| gcc -fsyntax-only with the ac_pc defines/includes (ac_my_room.c with and without PC_NET_TEST_HOOKS) | 0 errors | syntax |

Written, not run (need a build):
- `test_dresser_real.py --ui-direct {empty,occupied,timeout,off}`: new hook mode `direct` and the TEST-ONLY host flag `AC_TEST_DRESSER_IGNORE_SELECT`.
- `test_dresser_items_protocol.py`: class matrix on a real host plus restart persistence.
- `test_dresser_storage_protocol.py`, M phase rewritten for the host lock: M1 move / M2 rotate / M3 pickup / M4 partial storage refused, M5 empty
  extras plus a vanilla keep item moves freely, M6 unrelated pieces unaffected, D8/D9 adapted.
- `test_dresser_real.py --ui-edit`: V1 now expects the host REFUSAL. The `--ui-lock` and `--ui-gyroid` expectations are unchanged.

## 6g. Focused test results of the 6f build + the rollback-scope proposal (2026-10-12)

**Build:** `build_w3.sh` builds only `pc\build64_w3`:
- `cmake -G "MinGW Makefiles" -DPC_ALLOW_64BIT=ON -DPC_TEST_HOOKS=ON -DPC_CONSOLE=OFF` with the `pc\build64` compilers.
- Final exe: `pc\build64_w3\bin\AnimalCrossing.exe`, 02:37, 15,502,933 bytes. It was copied ONLY into the fresh disposable fixture
  `pc\build64\bin_fixture4_w3x`.

**Product defect found and fixed** (direct-open run):
- In vanilla, a dresser is OPENED by a short A tap: grab, release, open demo. The 6f lock sat at the grab itself, so a dresser holding extras, or one
  whose state was still unknown, could not be opened at all.
- Fix: the grab stays free. The actual pull / push / rotate STARTS refuse instead: `ac_my_room_action.c_inc` `aMR_PlacePullFurniture` /
  `aMR_PlacePushFurniture` / `aMR_PlaceKurukuruFurniture`, right before the move begins. Pickup is unchanged.
- Source test U5 updated.

**Test-hook fixes** (TEST-ONLY code; none of it runs in normal play):
- The lock pad has a PULSE mode. The room hook does not run while the overlay is open.
- The probe-time chest seed now also runs when the feature is OFF.
- The room warp proceeds when the probe gave up.
- The `direct` hook mode runs against an OFF host.

**Results** (final exe, fixture `bin_fixture4_w3x`):

| Test | Result | Label |
|---|---|---|
| `test_dresser_real.py --ui-direct empty` | 8/8 | runtime |
| `test_dresser_real.py --ui-direct occupied` | 8/8 | runtime |
| `test_dresser_real.py --ui-direct timeout` | 7/7 (vanilla menu after 181 frames) | runtime |
| `test_dresser_real.py --ui-direct off` | 6/6 (vanilla menu, no direct-open line) | runtime |
| `test_dresser_items_protocol.py` | 33/33 | protocol |
| `test_dresser_storage_protocol.py` | 49/49 | protocol |
| `test_dresser_real.py --ui-edit` | 19/19 | runtime |

- Items: 17 accepted classes round-trip; 7 rejected classes; the wrapped present is PRECOND; furniture survives a host restart. Each refused shape
  is a record violation (3 close the connection), so the test uses a fresh connection per rejected class.
- Storage protocol: M1 move / M2 rotate / M3 pickup / M4 partial storage are refused; M5 empty extras plus a vanilla keep item move freely; M6
  unrelated pieces are unaffected; M7 is the rollback scope (below); the host restart passes.
- `--ui-edit`: V1 is REFUSED INVALID_CELL 0x2032, and the client logs "a FULL record push was adopted -> own house 1 restored to the host's state ...
  local furniture edits discarded together with the pocket edits". V2 found the chest back at its cell and took the item.

**Rollback evidence (step 9b refusal):**
- **(a) Conservation holds.**
  - No CONSERVATION failure anywhere.
  - The extras are intact: M1 23, M7 23.
  - FakeClient pockets are unchanged (M7).
  - The real client's pockets are restored with the record push (`--ui-edit` V1).
- **(b) Sync holds.**
  - The host image is unchanged.
  - The client's canonical copy equals the pre-commit host image (M7).
  - The real client restores its own house to the host's state (V1); the next visit sees the dresser at its cell (V2).
- **(c) Legitimate edits ARE lost.** In M7 a commit moving a plain piece AND the occupied dresser is refused AS A WHOLE, and the plain move is undone
  too. It only succeeds when committed again on its own. With the real client, every furniture edit and pocket change of that room visit is
  discarded together.

### Proposed safer design (NOT implemented)
**Preferred: a client-side pre-commit guard.** It needs no wire change and is safe for old hosts.
- Before an OWNER_COMMIT is sent (room exit / in-room commit), compare each item-storage cell of the outgoing image with the last canonical image.
- If a piece whose cached or fresh extras count is > 0 moved or rotated, revert that ONE piece in the outgoing image to its canonical cell, rotation
  and keep items.
- Revert the matching actor-side change: the commit is built from the save, which the room already wrote, so the local save copy of that piece must
  be restored in the same step.
- Log the revert and show notice 3.
- The host step-9b refusal stays as the authoritative backstop for clients without the guard.
- Risk: the revert must not break conservation (a picked-up dresser in a pocket must leave the pocket again). That is the same pair the existing
  conservation pre-check counts, so it can be verified with the existing counters.

**Alternative: host partial accept.** The host applies the commit with that piece's cells reverted to canonical and answers a new APPLIED_PARTIAL
code; the client re-pulls the house. This needs a new ACK status (older clients treat any unknown non-APPLIED status as a refusal = today's full
rollback) and a careful conservation recount on the host. More invasive; not recommended first.

Since the 6f client gate refuses the pull / push / rotate / pickup itself, the full rollback only happens for clients without the gate, or under
races (state unknown at a move that the gate allowed: impossible by design, unknown = refused).

## 6h. Narrow movement guard: client pre-commit revert of ONLY the illegal dresser change (implemented, runtime-verified)

### Investigation
**Client commit path** (`pc_net_game.c`): `pcnetgame_house_client_commit_tick` (:25043) runs every 200 ms.
- **In the room** (`last_own_unsafe`): it takes a quiescent LIVE snapshot. `pcnetgame_hcl_snapshot` (:24357) calls `aMR_pc_export_home`, which builds the
  house from the room actors including keep items, into `s_hcl_snap_img`.
- **Out of the room:** the image is the in-memory save (`pcnetgame_house_export_be(&Save_Get(homes[h]))`). The quit flush uses the same.
- `pcnetgame_hcl_build_pair` (:25008) assembles house + record in `s_hcl_tx`. The in-room path then runs the conservation pre-check, and
  `pcnetgame_hcl_start_commit` (:25112) sends.
- **Last safe point:** the end of `pcnetgame_hcl_build_pair`. The outgoing image is final there, and the baseline is the host's canonical image
  `s_hcp.canon_img[h]`, which the host commit is checked against.

**Identity:**
- The canonical image holds the piece's raw id (base + rotation bits) at its origin cell, layer 0/1, with its keep items in layers L+1..3 of that cell.
- A move shows as: the old cell no longer holds that raw id, and exactly one cell newly holds that base id (vanilla only moves into empty cells:
  `aMR_JudgeFurnitureMove`). A rotation shows as: same cell, base equal, rotation bits differ. A pickup shows as: no cell newly holds it, and the piece is
  in a pocket.

**Knowledge:** `pc_net_game_dresser_move_state` (6f).
- **0** = a FRESH host answer (a gen-matched SELECT of THIS dresser in THIS room visit) says 0 extras, or no extra storage can exist (offline / host
  without the feature).
- **1** = it holds extras.
- **-1** = unknown.
- The guard touches a piece only when the state is not 0. An unknown state is conservative, but only for that piece.
- Empty dressers are always selected (fresh) before the in-room gate lets them move, so their legitimate moves pass.

**Live room:** an actor that already moved cannot be snapped back safely from the network layer. No API moves an actor of the OWN live room;
`pcnetgame_hcl_live_apply` explicitly never rebuilds the own house. So:
- In the room, the guard only DETECTS: no in-room commit (`room_stopped`, notice 3), the edit is held until the room exit.
- At the exit the teardown writes the save. The out-of-room commit reverts only the dresser change in the outgoing image AND writes the same correction
  into the local save (`pcnetgame_hcl_write_save`), so client and host images stay equal. The next entry rebuilds the room from that save.

### Implementation
`pcnetgame_dresser_commit_guard` (:24949), called from `pcnetgame_hcl_build_pair`. It is active only when the host answered the capability probe with
the feature enabled (old hosts, the feature OFF and offline play stay byte-identical).
- **Rotated in place:** the canonical raw id is restored; the current keep items stay.
- **Moved to exactly one new cell**, with the vacated cell empty in layers L..3 and the destination empty in the canonical image: the piece and its
  CURRENT keep items go back to the original cell, and the destination becomes empty again (= canonical). Only positions change, so conservation is
  unchanged.
- **Anything else is not reverted** (logged): a pickup into a pocket (reverting would need a pocket change, and keep items picked up with it are not
  identifiable), several identical pieces moved, or an occupied destination. These cases fall to the unchanged host step-9b refusal + full rollback
  (`pcnetgame_house_process_commit` :15394).

No wire or format change. Old client + new host: no guard, so the host's 9b refusal + full rollback applies (6g). New client + old host: no feature,
no guard.

### Results
Build: `build_w3.sh` -> `pc\build64_w3\bin\AnimalCrossing.exe`, Oct 12 10:05, 15,506,078 bytes. Fresh fixture `pc\build64\bin_fixture4_w3z`.

| Test | Result | Label |
|---|---|---|
| `test_dresser_real.py --ui-edit` | 22/22 | runtime |
| `test_dresser_storage_protocol.py` | 49/49 | protocol |
| `test_house_sync_src.py` | 85/85 | source |
| `test_dresser_ui_source.py` | 31/31 (2 new 6h checks) | source |
| `test_dresser_layout_geometry.py` | 9/9 | source/native |

**`--ui-edit` V1:** one commit carries the chest (1 extra) moved 50 -> 41, an unrelated plain piece moved 17 -> 42, and a pocket move 1 -> 14.
- Client log: "commit guard: ... MOVE floor 0 cell 50 -> 41 reverted in the outgoing image (the other edits are kept)" and notice 3.
- The host APPLIED it, with no 9b refusal and no rollback. The APPLIED image has cell 50 = 0x1068, cell 41 = 0, cell 17 = 0, cell 42 = 0x30F8, and
  pocket 14 = 0x1304 / pocket 1 = 0.
- V2/V3: empty-extras moves are applied.

**Storage protocol:** M1–M7 are unchanged. The FakeClient has no guard, so it shows the host backstop: occupied move / rotate / pickup / partial
storage refused, empty moves allowed, extras intact, restart OK.

### Remaining risks and unverified
- **In-room snapshot commits** stay held for the rest of the visit after an illegal change. The edit is committed at the exit (with the dresser
  reverted), so nothing is lost; it is only later.
- **Rotation and in-room hold paths are not runtime-tested.** The guard's rotation branch was exercised only by source review; the real-client test
  covers the move branch.
- **Pickup into a pocket** of an occupied dresser that slips past the in-room gate is still handled only by the host's full rollback.

## 6i. Client crash 0xC0000005 in mHW_haniwa_ovl_move (fixed 2026-10-12)

**Report:** in the manual session, B on grid row 2 col 7 crashed the client. The fault was at `mHW_haniwa_ovl_move + 0x59`, build64_w3 exe from 10:05.

**Disassembly** (`objdump`, 0x723f02..0x723f19): the instruction `mov 0x14(%rsi),%eax` faults with `rsi = submenu->overlay->haniwa_ovl` (overlay +0xe18)
== NULL. This is `haniwa_ovl->msg_counter` in `mHW_make_message`, inlined, at `src/game/m_haniwa_ovl.c` `mHW_make_message` (`ctr = haniwa_ovl->msg_counter`),
called from `mHW_haniwa_ovl_move`.

**Root cause** (frame-timing race, from the source):
1. In HANIWA_ENTRUST mode the inventory menu below OBEYs the gyroid menu and slides out together with it.
2. `mHW_haniwa_ovl_move` first runs `menu_info->pre_move_func` (the inventory move). With the PC port's variable dt, the inventory can reach its End
   inside that call, on the SAME frame on which the gyroid menu reaches its own End in `ovl_move_proc[proc_status]`.
3. The gyroid's `mSM_return_func` then finds no menu left (`pre_menu_type` NONE). It runs every overlay's `dt_proc`, and
   `mHW_haniwa_ovl_destruct` sets `haniwa_ovl = NULL`.
4. `mHW_haniwa_ovl_move` then continues into `mHW_make_message` and crashes.

The same window exists in the vanilla gyroid code on PC. It was not reproduced by the scripted runs: the timing race depends on a long frame at the
moment of closing.

**Fix** (TARGET_PC, `m_haniwa_ovl.c`):
- `mHW_haniwa_ovl_move` returns immediately when `haniwa_ovl` is NULL after `pre_move_func` and again after `ovl_move_proc` (logged).
- `mHW_haniwa_ovl_draw` returns when `haniwa_ovl` or `tag_ovl` is NULL after `pre_draw_func`.
- The dresser code in `m_tag_ovl.c` runs only from the gyroid move (tag_move_func) and the draw, so it is covered by these guards.

**Regression:** `test_dresser_real.py --ui-sweep {b,start}`.
- The scripted pad presses A, L, R and Z on EVERY one of the 27 cells. On the filled cell, A opens the tag window, L/R/Z go inside it, and B closes the
  tag window.
- It then closes the overlay with B, or START, on row 2 col 7.
- Pad bits START = 256 and Z = 512 were added to the test pad path.

**Results** (exe `pc\build64_w3\bin\AnimalCrossing.exe` Oct 12 10:23, 15,507,196 bytes; fresh fixture `bin_fixture4_w3s`):

| Test | Result |
|---|---|
| `--ui-sweep b` | 10/10 |
| `--ui-sweep start` | 10/10 |
| `--ui-gyroid` | 16/16 |
| `--ui-direct occupied` | 8/8 |
| `--ui-edit` | 22/22 |
| `test_dresser_storage_protocol.py` | 49/49 |
| `test_dresser_ui_source.py` | 32/32 (new 6i pin) |
| `test_house_sync_src.py` | 85/85 |
| `test_dresser_layout_geometry.py` | 9/9 |

The guard's log line did not appear in these runs: the race was not hit, so the fix is verified by analysis plus the no-crash sweep, not by a reproduction.

## 6j. Direct open lost after moving a dresser in the same visit (investigation checkpoint 2026-10-12, NOT fixed yet)

**Proven from the user's session logs** (read only: `bin_session2_client/client2.log`, `bin_session2_host/host.log`):
1. The first three opens of the chest at cell (2,3) used direct open: "direct open: dresser 0x1068 (floor 0 cell 2,3) -> 27-slot screen".
2. After the player moved the chest to cell (2,1), every A sent a SELECT for (2,1) (gen 2, 3, 4). The HOST refused each one: "DRESSER_SELECT refused
   (house 1 floor 0 layer 0 cell 2,1 item 0x1068 gen N): not a furniture of that id in its own house".
3. The client therefore timed out ("no host view ... after 181 frames -> the vanilla menu (fallback)") and showed the vanilla menu with "More storage".
4. During the whole visit the client sent NO OWNER_COMMIT: there is no "OWNER_COMMIT ... started" line, although in-room commits were armed ("entered
   own house 1: in-room commits armed").
5. The host's image of house 1 therefore still had the chest at (2,3). The SELECT check (`pcnetgame_dresser_cell_ftr` against the HOST image) correctly
   refused (2,1).

**Root cause, part 1 (proven):** direct open (and the "More storage" path) needs a host-authorized SELECT, and the host only knows committed positions.
A dresser moved earlier in the same visit is not selectable until the room edit is committed.

**Root cause, part 2 (NOT yet proven):** why that visit's in-room commit never started.
- In a scripted reproduction (`test_dresser_real.py --ui-direct moved`, a real PULL of the empty chest through the pad path, exe 10:37), the in-room
  commit DID start and was APPLIED within the wait: "OWNER_COMMIT xfer 1 house 1 started FROM INSIDE THE ROOM ... APPLIED".
- So the commit machinery works for a pull. The user's edit moved the chest 2 cells, which is consistent with a pickup + placement from the pocket.
- Candidates still to check with the new verbose diagnostic "in-room commit not possible now (armed / stopped / rollback / active / live_room / quiet)"
  in `pcnetgame_house_client_commit_tick`:
  - a room-quiet blocker that stays set after a placement (`aMR_pc_room_quiet`: `rsv_ftr[].exist_flag`, `parent_ftr`, `pickup_flag`);
  - the record gates (`s_crec` / `pcnetgame_hcl_gates_pending`) after a pickup.
- No speculative patch was made.

**Fix direction** (after part 2 is proven): option (a) from the lead. Make that in-room commit land; then the next A selects the committed cell and
opens directly. Host authority stays as it is: option (c) is rejected.

**Test status:** `--ui-direct moved` written. Run once (10:37 exe, fixture `bin_fixture4_w3m`): 8/10.
- PASS: first direct open, the real pull moved the chest, the in-room commit was APPLIED, the host never refused a SELECT.
- FAIL: the second open was "nothing opened". The test placed the player relative to the pre-pull position, a test geometry bug. The hook re-reads the
  piece position at the start of the second open; that read happened mid-animation.

### 6j (cont.) Part 2: the user's sequence reproduced (2026-10-12, checkpoint 2, NOT fixed)
**Reproduction:** `test_dresser_real.py --ui-direct moved` (hook mode "moved"). Exe `pc\build64_w3\bin\AnimalCrossing.exe` 10:47, fixture
`bin_fixture4_w3m`, ports 17510–17550.
1. The first A opens directly at (2,3).
2. A REAL B pickup puts the empty chest into pocket 3.
3. The chest is placed again two cells away at (2,1) through the inventory's own drop path. That is `mTG_drop_furniture`'s calls
   (`judge_breed_new_ftr_proc` + `reserve_ftr_proc`, pocket emptied, `mMkRm_ReportChangePlayerRoom`); only the menu navigation is not scripted.
4. After a 300-frame wait, A again: "select dresser floor 0 layer 0 cell 2,1 (gen 2/3)", the host logs "DRESSER_SELECT refused ... cell 2,1 ... not a
   furniture of that id in its own house", and the client logs "no host view ... -> the vanilla menu (fallback)".
   This is EXACTLY the user's session. No OWNER_COMMIT was sent in the visit.

**Diagnostic** (verbose only; `pcnetgame_house_client_commit_tick` and `aMR_pc_room_quiet`'s reason):
- Before the pickup the in-room snapshot EQUALS the host image (cell 50 = 0x1068, cell 18 empty): nothing to commit, as expected.
- Right after the placement the room is NOT quiet: "room-quiet: reserved furniture 0/18/26". The placed piece is still a reservation (`rsv_ftr[0]`,
  `frames` 18 of 26), and then a birth animation. The in-room commit gate needs the live room quiet for 30 frames (`PC_NETGAME_HCL_QUIET_FRAMES`) and a
  2000 ms commit gap.
- At the next A press the player is in the furniture-open state (main index 24), and the open demo plus the up to 180-frame wait of direct open (then
  the vanilla menu) keep the room non-quiet the whole time. A player who interacts again shortly after placing therefore never gives the in-room commit
  its quiet window. Each retry repeats the cycle. The user's log shows three such attempts and no commit.
- The heartbeat is printed only every 2 s, so it does not show whether a quiet window opened between the countdown and the A press. The 300-frame wait
  in the test was evidently not enough: no commit started in that window.

**Not proven yet:** whether, given a long enough idle period after the placement, the in-room commit lands. The same-cell placement run (10:41)
committed in-room ("OWNER_COMMIT ... FROM INSIDE THE ROOM ... APPLIED"), but only after the second open had closed.

**Options** (for the lead / user; none implemented):
- **(a) Wait for the commit before selecting.** When direct open sees that the dresser's cell is NOT in the client's copy of the host image
  (`s_hcp.canon_img`: an uncommitted placement), do not start the open demo / SELECT at once. Show notice "saving the room ..." (or nothing) and let the
  room stay quiet so the in-room commit lands; the next A opens directly.
  - Smallest code: an early FALSE plus the vanilla fallback is NOT acceptable (that is the bug).
  - Instead, before `aMR_SetOpenFtrDemoData` (`aMR_JudgeDemoStart`), refuse the open of an uncommitted dresser with a short notice. Vanilla is not
    affected when the feature is off.
- **(b) Treat the in-progress open as quiet for the house commit.** That changes the house-sync quiet rule (the snapshot is taken while a furniture
  demo runs), so it is a design change to furniture sync; not recommended.
- **(c) Host-side pending image:** rejected (weakens authority).

### 6j (cont.) Proof and fix (2026-10-12)

**Proof (runtime, `--ui-direct moved` with a 900-frame idle after the placement, 10:49 exe):**
- The room was not quiet right after the placement: first "reserved furniture 0/18/26", then "furniture actor not stopped 2/13/0" (the new piece's
  birth state 13), for roughly 300 game frames.
- With the player idle, the in-room commit then STARTED between idle frames 300 and 360 and was APPLIED ("OWNER_COMMIT xfer 1 ... FROM INSIDE THE ROOM
  ... APPLIED"). The next snapshot equals the host image (cell 50 empty, cell 18 = 0x1068).
- The record / save gates never fired ("house dirty but the commit waits" never logged). The gates were the room-quiet rule (reservation, birth state)
  plus 30 quiet frames; the 2 s gap did not apply (no earlier commit).
- So an idle room lets the commit land. An A press inside that window (open demo + 180-frame wait) keeps the room non-quiet and fails as the user saw.

**Fix (option (a), no change to house sync, host authority, the occupied guard or any format):**
- `aMR_JudgeDemoStart` (`ac_my_room_action.c_inc`), TARGET_PC, before ANY open demo, calls `aMR_PcDresserOpenRefused` (`ac_my_room_msg_ctrl.c_inc`). It
  applies to the owner, in the own house (not an NPC house), with the feature on, not edit-locked, on item-storage furniture.
- `pc_net_game_dresser_open_gate` then decides:
  - **the cell is in the client's copy of the host image** (`pc_net_game_dresser_cell_committed`): the open proceeds as before;
  - **not committed:** the open is REFUSED for now (no demo, no SELECT), notice 4 "The room is being saved..." (`pc_pause_menu.c`), and the player
    stays free so the room goes quiet and commits;
  - **in-room commits cannot land** (not armed / stopped / rollback) **or still not committed 8 s after the first refusal:** the open proceeds and
    `aMR_PcDresserDirectOpen` goes straight to the vanilla menu. The furniture stays usable.
- **No doomed SELECT for an uncommitted cell:** neither the vanilla path's "More storage" arm/select nor the move lock's "ask the host" sends one.
- **Bug found while testing:** the 8 s cap compared `now - (now | 1)` unsigned. Fixed to a signed compare.
- **Test-only additions:** hook mode "moved" (real B pickup, inventory drop path two cells away, repeated A); `AC_TEST_HCL_HOLD_MS` (holds in-room commits
  after the placement, so the not-yet-committed window is deterministic); `AC_TEST_HCL_NO_INROOM_COMMIT` (stops in-room commits for the visit); verbose
  commit-tick diagnostics.

**Results** (exe `pc\build64_w3\bin\AnimalCrossing.exe` 11:13, fixture `bin_fixture4_w3m`):

| Test | Result | Label |
|---|---|---|
| `--ui-direct moved` | 11/11 (twice) | runtime |
| `--ui-direct moved-stuck` | 9/9 | runtime |
| `--ui-direct empty` | 8/8 | runtime |
| `--ui-direct timeout` | 7/7 | runtime |
| `--ui-direct off` | 6/6 | runtime |
| `test_dresser_storage_protocol.py` | 49/49 | protocol |
| `test_dresser_ui_source.py` | 33/33 | source |
| `test_house_sync_src.py` | 85/85 (pinned count updated) | source |
| `test_dresser_layout_geometry.py` | 9/9 | source |

- `moved`: refused with notice 4 while not committed, the commit APPLIED, then A opens directly at (2,1), and the host never refused a SELECT.
- `moved-stuck`: the vanilla menu opens at once, and no SELECT is sent for the uncommitted cell.

**Remaining:**
- While a just-moved EMPTY dresser is uncommitted, a second pickup or move of it is refused with notice 3. Its state for the new cell is unknown until
  the commit lands. This is conservative, and the notice text is misleading there.
- The user's exact menu navigation (the inventory "drop") is replaced by its internal calls.

## 6k. Session-3 report ("reverted to vanilla-like behaviour"), investigation 2026-10-12, NOT fixed

Evidence (read only): `pc\build64\evidence_session3_20261012_114418\{host,client}`.

### Observed facts
1. **The feature stayed ON the whole time.**
   - Host: "extra dresser storage ON, sidecar ... absent (every pool empty)".
   - Client: "the host offers dresser storage (enabled=1)".
   - No feature-off, sidecar or backup failure.
2. **NO extra-slot transaction ever happened.** The host never logged a PUT/TAKE. Every host SELECT answered "(gen N, 0 item(s))": cells (2,3), (2,1),
   (2,2), (2,1). The host save copy has NO `dresser_store.dat` (`save_copy\mp` holds only `records.dat*`): the sidecar was never written.
3. **Every successful put went into the 3 VANILLA keep slots:** "put pocket 0 item 0x240D into a vanilla slot -> 1", "put pocket 2 item 0x2037 ...".
4. **Furniture put-in was refused.** "put pocket 1" was the pocket furniture 0x1304, which can only go to the EXTRA slots. It was refused locally:
   "dresser storage refused inside the own house: the room holds uncommitted edits". This came right after a vanilla put in the SAME overlay session
   (client log lines 1970 -> 1983, and again at 2003).
5. **Pickup/move attempts right after a move were refused with a misleading notice.** At the just-moved dresser at (2,2): 20 refusals of the form
   "move / rotate / pickup of a dresser holding extra items REFUSED (notice 3 ...)" + "extra storage state UNKNOWN in this room visit", although it was
   EMPTY.
6. **No host refusal, conservation failure or rollback at all.** All 6 OWNER_COMMITs were APPLIED.

### Root causes (proven)
- **Symptom 1, "could no longer put furniture/items in":**
  - Furniture, fish and insects go ONLY to the extras, and extras are host transactions.
  - The client refuses an extras transaction while the live room differs from the host image (`pcnetgame_dresser_begin_blocked`, the in-room
    conservation gate: the pockets of the pending room edit would not match the host's record).
  - A vanilla-slot put is itself a room edit. So in one overlay session, the first ordinary item (vanilla slot) makes the room dirty, and every later
    extras put is refused until the room commits. The room cannot commit while the overlay is open (not quiet).
  - A just-completed pickup + placement has the same effect until its commit lands.
  - The overlay shows a refusal with no explanation.
- **Symptom 2, "could pick up a dresser that had stored items":**
  - The stored items were VANILLA keep items. Vanilla allows picking up a dresser with keep items: they go to the pockets
    (`aMR_JudgePickUpCommon` checks pocket space).
  - The Option-A lock deliberately covers only the 24 extras. Extras were never non-empty in this session, so the lock was never bypassed.
  - The 27-slot UI does not tell the player which slots are vanilla.
- **Lock re-verified on the 11:13 exe:** `--ui-lock`, fresh fixture `bin_fixture4_w3n`, port 17900. With 1 extra item, a real B pickup is REFUSED
  (notice 3), the chest stays and is not in a pocket.
  - The A step of that test is now obsolete: since 6g, A on a dresser opens it instead of grabbing it.

### Conservation
Nothing was lost, duplicated or detached:
- extras never existed (no sidecar);
- the vanilla keep items travelled through APPLIED house commits that passed the host's conservation check.

### Options (decision needed; none implemented)
- **(1) Extras FIRST for all storable items while the extras have room; vanilla slots only when the extras are full.** Then ordinary use never mixes a
  local room edit with host transactions in one overlay session. This changes the agreed order ("ordinary items fill vanilla first").
- **(2) Make the 3 vanilla slots LOCKED with the extras in multiplayer:** no pickup while ANY of the 27 slots holds an item. This matches the user's
  expectation of "stored items". It changes vanilla pickup behaviour only when the feature is on.
- **(3) Clear feedback when an extras put is refused because of uncommitted room edits:** a notice "Close the dresser to save, then store more".
- **(4) Treat a just-moved EMPTY dresser (uncommitted) as movable,** instead of "unknown -> refused with notice 3". It has no host record at its new
  cell; the host re-keys/re-attaches at commit. Or at least use a correct notice ("The room is being saved").

## 6l. Session-3 fixes: slot order kept + truthful notices + 27-slot lock (2026-10-12)

Exe: `pc\build64_w3\bin\AnimalCrossing.exe` (12:13, 15,526,188 bytes). No wire, save or sidecar format change. The feature default stays OFF.

### Slot order decision: (d) keep vanilla-first, refuse truthfully, the next put works after the commit
- **(a) Extras first.** Not taken: it changes the agreed order, and the vanilla slots would still be dirtied once the extras are full. The same refusal
  stays possible.
- **(b) Allow an extras transaction on a dirty room.** Unsafe. The kind-18 pre-image (pockets) is validated against the host record ("pocket slot does
  not hold the item", base_rev). An uncommitted vanilla put changes the pockets locally. The in-room conservation baseline would also no longer match.
- **(c) Commit the room while the dresser screen is open.** Impossible without new machinery: the room is not quiet while the overlay or demo is
  active, so no in-room commit can land.
- **(d) Adopted.** The order is unchanged. The refused extras put shows notice 5 ("Close the dresser to save room changes, then reopen it to store more
  items."), and nothing moves. After closing, the room commits, and the reopened screen stores the item in an extra slot.
  - Verified by `--ui-store` (A-1..A-8).
- The 27-slot mapping is unchanged: 0..2 vanilla, 3..26 extras.

### Refusal feedback (`pcnetgame_dresser_begin_blocked` -> `pc_net_game_dresser_begin`)
Every refused extra-slot begin now sets a notice (pc_pause_menu.c). None of them ever says "full".

| Notice | Text | Cause |
|---|---|---|
| 5 | "Close the dresser to save room changes, / then reopen it to store more items." (2 lines) | The room holds uncommitted edits. |
| 2 | "Leave the house to resync" | Offline taint, rollback, or in-room commits stopped. Only the room exit resolves it. |
| 6 | "Try again in a moment" | Transient: another transaction or commit in flight, baseline not armed, export failed, or a pickup/drop/bury pending. |
| 7 | "Not connected - extra storage is unavailable" | Link not READY. |

The latest refusal wins (notice 3 and the open-gate notice 4 clear it).

### Move lock with UNKNOWN state
`aMR_PcDresserMoveLocked` uses notice 3 only for real stored items.
- UNKNOWN because the piece's cell is not committed -> notice 4 (or notice 2 when in-room commits are stopped).
- UNKNOWN on a committed cell -> notice 6, and the host is asked (SELECT).
- No SELECT is ever sent for an uncommitted cell. The 8 s cap and the occupied protections are unchanged.

### 27-slot lock (feature ON only): the vanilla keep slots lock the piece too
Pickup behaviour changes when the feature is on: a dresser whose vanilla keep slots hold anything can no longer be picked up, pushed, pulled or rotated.
Take the items out first. With the feature off, vanilla behaviour is unchanged.

- **Host (authoritative), step 9b `pcnetgame_dresser_commit_locked`:**
  - Any item-storage piece (layers 0..1) whose keep cells (layers L+1..3 at its origin cell) in the host's PRE-commit image are not all empty, and
    whose raw id at that cell changes, is REFUSED.
  - Same INVALID_CELL detail as the extras rule (0x2000|floor<<8|cell).
  - The rule is judged on the old image, so a commit that empties the keep slots AND moves the piece is refused as well.
  - No record or protocol is needed: the keep items are part of the house image.
- **Client commit guard `pcnetgame_dresser_commit_guard`:** canonical keep items count like extras. In the room the guard detects and holds; outside
  the room it reverts a rotation or an unambiguous move. Anything else is left to step 9b.
- **Client gates (pickup/push/pull/rotate):** before the extras state, the gates check `pc_net_game_dresser_keep_lock`:
  - live keep items (`ftr_actor->items`) -> notice 3;
  - live slots empty but the host's canonical image still holds keep items (take-out not committed) -> notice 4;
  - a later attempt succeeds once the commit lands.

### Verification (runtime on fresh fixture `bin_fixture4_w3q`, ports 18300-18350)
- **`--ui-store`: 12/12.** The vanilla put went to a vanilla slot. The furniture put was REFUSED with notice 5, and nothing moved. After the commit,
  the extra PUT was APPLIED. Both items came back, and the host logged exactly 1 PUT + 1 TAKE.
- **`--ui-lock`: 22/22.**
  - Extras: B, push (A held + stick into the chest), pull (A held + stick away) and rotate (A held + stick sideways) were all refused.
  - Keep item: B was refused with notice 3; push, pull and rotate were refused.
  - Keep take-out: B right after it was refused with notice 4. B on the empty, committed chest was ALLOWED, and the pickup commit was APPLIED.
  - The host never had to refuse.
- **`--ui-direct moved`: 12/12.** B on the moved, uncommitted chest -> notice 4 (D-3b).
- **`test_dresser_storage_protocol.py`: 49/49.**
- **Source tests:** `test_dresser_ui_source.py` 37/37 (4 new 6l checks), `test_house_sync_src.py` 85/85, `test_dresser_layout_geometry.py` 9/9.

### Not verified
- The step-9b keep-item refusal was not exercised by a client without the gate (an old client); it is source-checked only.
- The commit-guard revert of a keep-item piece is source-checked only.
- The notices 2/6/7 text paths are source-checked only.
- The visual rendering of the notices was not tested (no visual testing).

## 6m. Final automated verification + diagnostic cleanup (2026-10-12)

Exe: `pc\build64_w3\bin\AnimalCrossing.exe` (12:37, 15,528,762 bytes). No wire, save, sidecar or 27-slot mapping change. The feature default stays OFF.

### Move-refusal counters
Both counters are accounting only: when an action is refused is unchanged.
- **`pc_net_game_dresser_move_denied_count()`** is unchanged and now documented as **per refused frame**. The pull/push/rotate gates run every frame while
  the input is held.
- **NEW `pc_net_game_dresser_move_attempt_count(action)`** counts **one per refused attempt**. Actions: 0 = all, 1 pickup, 2 pull, 3 push, 4 rotate.
  - A refusal of the same (action, item, cell) within 30 game frames of the previous one extends the same attempt.
  - A longer release, another action or another dresser starts a new attempt.
  - The four gate callers now pass their action.
- Measured on the real pad path (`--ui-lock`), one held input = 1 attempt each time. The per-frame counter for the same inputs:

  | Action | Per-frame refusals (extras run / keep run) |
  |---|---|
  | Pull | 48 / 48 |
  | Rotate | 115 / 115 |
  | Push | 13 / 15 |

### Fix
The notice for the latest refusal used an unsigned age check on a timestamp stored as `now | 1`. When read in the same millisecond, the stamp is 1 ms
ahead and the notice counted as expired. It now uses a signed compare. Found by `--ui-store` A-2 (notice 0 on one run).

### New automated coverage
- **Host step 9b keep-item rule (`test_dresser_storage_protocol.py` M8).** This is a protocol-harness run on a real host process. The FakeClient has no
  client gate, like an old client, but it is NOT an old client binary.
  - Moving or rotating a dresser with a keep item is refused: INVALID_CELL 0x2000|cell, and the host image is re-pushed unchanged.
  - Emptying the keep slot and moving in the same commit is refused, and the pockets are unchanged.
  - Taking the keep item out on its own is applied. Moving the now-empty dresser is then applied.
  - Extras protection still refuses. With the feature off, moving a dresser with a keep item is applied.
- **Client commit guard with a canonical keep item (`--ui-keepguard`, real client).**
  - The keep item is committed in the room.
  - The swap hook runs outside the room with `AC_TEST_HOUSE_CLIENT_SWAP_KEEP=1`: the keep layers travel with the piece.
  - The guard counts the keep item and reverts only the chest move. The unrelated plain move stays in the same commit, and the host applies it with no
    step-9b refusal.
- **Notice selection (`test_dresser_ui_source.py` N2..N8, C1..C2): source only.** Selection logic and texts are pinned; rendering and line fit are not covered.

### Still not exercised at runtime
- The in-room hold for a keep-item dresser (the client gates prevent driving it). It uses the same code path as extras (6h).
- The ambiguous / not-revertible guard path for a keep item.
- Notices 2, 6 and 7 (source only).

## 6n. Build-specific default of the host switch (2026-10-12)

The CMake option `PC_DRESSER_STORAGE_DEFAULT_ON` defaults to OFF.
- When it is ON, it appends `PC_DRESSER_STORAGE_DEFAULT_ON=1` to `pc_net_game.c` only (`PC_NET_TEST_HOOKS` is kept).
- Only the local dev build `pc\build64` is configured with `-DPC_DRESSER_STORAGE_DEFAULT_ON=ON`.
- Release builds, `pc\build64_w3` and any fresh configure keep the default OFF: they are opt-in with `AC_MP_DRESSER_STORAGE=1`.

The host gate is the pure function `pc_dresser_feature_enabled_from_env(env, build_default)` in `pc_dresser_store.h`:

| `AC_MP_DRESSER_STORAGE` | Result |
|---|---|
| exactly `1` | ON |
| exactly `0` | OFF |
| unset, empty or anything else | the build default |

Host log lines:
- `ON (dev-build default; AC_MP_DRESSER_STORAGE=0 disables)`
- `ON (AC_MP_DRESSER_STORAGE=1)`
- `OFF (AC_MP_DRESSER_STORAGE=0)`
- `OFF (release default; AC_MP_DRESSER_STORAGE=1 enables)`

### Disabling it temporarily
Set `AC_MP_DRESSER_STORAGE=0` in the HOST's environment before launching. The HOST alone decides. A client only sees the storage when its host
answers the capability probe, so a client connected to a host with the feature OFF (or an older host) gets vanilla dressers.

### Tests
The test suites set `AC_MP_DRESSER_STORAGE` explicitly (`=1`, or `=0` for OFF runs) and run against `build64_w3` fixture exes. None relies on "unset = OFF".

## 6o. Bug from the user's test: stored furniture could not be taken out (2026-10-12)

Evidence: `pc\build64\bin_regular_client2\client.log` and `bin_regular_host\host.log`, read only.

### What happened in the user's session
1. Furniture 0x1304 was put in. The host APPLIED it in pool slot 0, which shows at grid 3. Then 2 ordinary items went into vanilla slots 0 and 1.
2. After closing, the in-room commit was refused locally: "in-room commit NOT sent: ... do not conserve ...: 0x1304 (1 -> 0) -- in-room commits stopped
   until you leave the room".
3. Every later extra-slot TAKE was refused with notice 2.

### Root cause
`pc_net_game.c:pcnetgame_dresser_rebase_room` rebased the client's in-room conservation baseline only for ITEM1 ids. An applied FURNITURE (also fish
or insect) PUT or TAKE left the baseline stale. The next in-room commit then failed its own conservation pre-check, and in-room commits stopped.

### Fix
- The rebase now uses `pcnetgame_house_count_class`, which was split out of `pcnetgame_house_count_id` with identical rules. That is the same class the
  conservation count uses: furniture maps to its rotation-free item class.
- No wire, save, sidecar, layout or host change.

### "Top-middle" slot
This is correct, not a bug. Grid cells 0..2 are the 3 vanilla keep slots, which take ordinary items only. Furniture goes to the first free EXTRA slot:
extra 0 is grid 3 (4th cell of the top row), extra 1 is grid 4. Both were verified at runtime, and the screen now logs which grid cell draws which item.

### Tests
| Test | What it covers | Before fix | After fix |
|---|---|---|---|
| `--ui-ftrmix` | The user's sequence | Reproduced exactly: in-room commits stopped, take refused with notice 2 (5/10) | 10/10 |
| `--ui-ftr empty` | Pad-driven put and take of furniture | — | 12/12; drawn at grid 3, taken from grid 3 |
| `--ui-ftr prefill` | Extra 0 pre-filled | — | 12/12; furniture drawn at grid 4 and taken from grid 4 (extra 1) |

Regressions after the fix: `--ui-store` 12/12, `--ui-edit` 22/22. Source tests 52/52 and 85/85.

## 6p. Dedicated-server session 14:0x: the vanilla + extra "wait" and the moving background (2026-10-12)

Evidence (read only): `bin_regular_client2\client_dresser_test.log` and `bin_regular_host\host_dresser_test.log`.

### What the logs show
- The host APPLIED every furniture TAKE that was sent (host log 1074, 1106, 1200, 1679). There is no conservation stop in this session.
- Every failed take or put was the client-side gate "the room holds uncommitted edits" (notice 5, client log 971, 984, 1048, 1678, 1841, 1964). Each one
  followed a vanilla-slot put or take of clothes 0x240D or paper 0x2037 earlier in the SAME open screen.
- After closing the screen, the in-room commit landed (OWNER_COMMIT APPLIED, client log 1006, 1070, 1184, 1702, 1855). The next open then worked
  (TAKE APPLIED at 1894).
- So issues 1 and 2 have the same cause. The take was not broken. The design-(d) gate refused mixing vanilla-slot and extra-slot operations in one open
  screen, because the room could not commit while the screen was open (it was never "quiet").
- The grid mapping is correct: the furniture is drawn at grid 3 = extra 0 (client log 789, 1107, 1239).

### Fix (implemented; unverified at runtime)
- **The settled 27-slot screen counts as a quiet live room.**
  - The settled screen is defined as: the overlay is active in its play state, the room's message state is the dresser screen's, nothing else is requested,
    and the opened piece is the screen's piece.
  - The screen is modal. Its only room edits are the synchronous vanilla keep-slot put/take: the pocket and the keep slot change in the same call.
  - `aMR_pc_room_quiet` skips only the room's own demo/message terms and the opened piece. Every other term still applies: other furniture, reserves,
    wall/floor.
  - `pcnetgame_hcl_live_room_house` skips the submenu/demo/message/player-pose terms, but room-quiet still decides.
  - Result: the in-room commit can land while the screen stays open.
- **An extra-slot put or take refused ONLY for kind 5 (uncommitted room edits) now waits** (pending, notice 4 "The room is being saved...").
  - `pc_dresser_ui_poll` retries it and sends it as soon as the commit has landed.
  - It gives up after about 600 polls, with nothing moved and the cause logged. Closing the screen drops an unsent deferred op (nothing moved).
  - Every other refusal reason is unchanged.
- No wire, save, sidecar, slot-mapping, host-validation or move-lock change.

### Issue 3: the moving background
The gyroid window's background pattern scrolls with the menus' shared `menu_control.texture_pos` (vanilla `mHW_set_frame_dl` tile offset). In dresser mode
the offset is now 0; every other screen keeps the vanilla scroll.

### Item classes
Paper (0x20xx) and clothes (0x24xx) are ITEM1 and count as themselves; furniture counts under its rotation-free class. After 6o the baseline rebase and the
live/host count share `pcnetgame_house_count_class`. This session shows no class mismatch.

### Tests
| Test | Status |
|---|---|
| `--ui-mixopen` (furniture + clothes + paper in ONE open screen, then take the furniture: waits, the room commits while open, the take is sent and APPLIED) | Written, not run (needs a game process; the user's session was active) |
| `--ui-store` A-2 (now expects DEFERRED / notice 4 / dropped) | Updated, written, not run |
| Source / native suites | Run (results in the worker report) |

## 6q. The user's three requests: aligned list, moving occupied dressers, "Move" items (2026-10-12)

**Deliberate rule changes (user decisions):**
- The vanilla-first rule and the "0..2 vanilla / 3..26 extra" view are replaced by ONE aligned list.
- Occupied dressers may be pushed, pulled and rotated. Only picking them up stays locked.

### S1: aligned list (view mapping + put policy only; no persistent format change)
- Grid 0..23 = extra slots 0..23. Grid 24..26 = the 3 vanilla keep slots (`pc_dresser_ui_slot`; inverse `pc_dresser_ui_col`).
- EVERY storable item goes to the first free extra slot through a host PUT (auto slot), which is the first free cell.
- The vanilla keep slots take ordinary items only once the 24 extras are full (the vanilla filter is kept). Items already in keep slots show in the last 3
  cells and are taken out as before.
- Side effect: ordinary puts are host transactions now, so the old "vanilla edit + extra op" conflict (6k/6p) no longer happens in normal use.
- The 6p deferral stays as the safety net.

### S2: move / rotate occupied dressers, pickup locked
- **Client gate (`aMR_PcDresserMoveLocked`):** push, pull and rotate return "not locked". Pickup keeps the full lock: keep items, extras, unknown state,
  and notices 3/4/6.
- **Host step 9b (`pcnetgame_dresser_commit_locked`, both the extras and the keep rule):** refuses only a REMOVAL, i.e. fewer cells of that floor/layer hold
  the piece after the commit. A rotation in place never counts as a removal.
- **Moves:** carried by the existing house-commit hook. An unambiguous single move is re-keyed; identical pieces are followed by unique fingerprint;
  anything else is orphaned (kept) and re-attached. Storage is never lost or merged.
- **Keep items** travel with the piece in the house image.
- **Client commit guard:** flags only removals. Removals are not revertible: in the room, commits are held; outside, the host refuses. Moves and rotations
  are no longer reverted.
- **Known limitation (kept):** a commit that picks up an occupied dresser AND places an identical one elsewhere keeps the count, so it is treated as a
  move. The storage follows the new piece (not lost).

### S3: "Move" option (kind-18 op 3; the wire size and the sidecar record layout are unchanged)
- **Wire:** the TXN tag carries op 3 in `aux_cond` and src | dst << 8 in `aux_item`, with dest NONE. Pockets are unchanged and the 64-byte pre-image is
  still validated.
- **Store:** `PC_DRESSER_OP_MOVE`. An empty destination means a move; an occupied one means a swap; the source must hold the item. Each operation is
  journalled, replayed and fenced like PUT/TAKE.
  - The destination slot is stored in the journal entry's reserved byte `_r`. This is a new VALUE in an existing field of the unchanged 32-byte entry.
  - The roll-forward ignores MOVE (no pocket side).
- **Host:** writes no record and no record rev; sends STATE plus APPLIED.
- **Client:** try-send re-checks the source; apply changes nothing locally; no conservation rebase.
- **UI:** the item tag in dresser mode is "Take it / Move / Quit" (the vanilla gyroid keeps its own tag).
  - "Move" picks the source. The next A on a cell is the target; A on the same cell, A on a pocket item, or closing the screen cancels. A hint line is shown.
  - The vanilla keep cells (24..26) are not movable (notice 8, "Items in the last 3 slots can only be taken out").

### Tests
| Kind | Tests | Status |
|---|---|---|
| Source | `test_dresser_ui_source.py` (6q S1/S2/S3 pins; U3/6h pins superseded) | Run |
| Native | `dresser_store_selftest` (MOVE: move, swap, replay, NOT_AVAILABLE, BAD_SHAPE, conservation) | Run |
| Build | `build64_w3` | Compiles |
| Runtime | `test_dresser_storage_protocol.py` Q1..Q6, `--ui-moveitem`, `--ui-mixopen` (aligned list), and the updated `--ui-lock`, `--ui-edit`, `--ui-keepguard`, `--ui-ftr`, `--ui-gyroid`, `--ui-sweep` expectations | Written, NOT run (the user's session was active) |

`--ui-store` and `--ui-ftrmix` are superseded: they relied on vanilla-first.

## 6r. A1: all 27 cells are host slots + B: Grab (2026-10-12, user decisions)

### Why A1 (the house image cannot hold furniture in keep slots)
The vanilla room creates layer-1 furniture actors (`aMR_MakeFurnitureActor(LAYER1)`, ac_my_room.c:1820) BEFORE it reads the keep items out of the layers
(`aMR_MakeItemDataInFurniture`, :1824). Keep slot 0 of a floor dresser is layer 1, so a furniture id stored there would spawn as a piece AND be a keep item:
a duplicate. This happens on every client, old clients included.

### A1: sidecar v3, 27 host slots per dresser (approved format change)
- **Record:** `PC_DRESSER_SLOTS` = 27 and `PC_DRESSER_VERSION` = 3. The record grows to 616 bytes (key, rev, fencing, 27 items, the same 16-entry journal).
  The v2 layout stays as the read-only type `PCDresserRecV2` (608 bytes).
- **Load:** a v2 file is loaded (`pc_dresser_store_load` returns 2) and converted in memory, losslessly. Every field is copied, the 24 items keep their
  slots, and slots 24..26 start empty.
- **Host on a v2 file:** it first byte-copies the file to `.v2.bak` (or `.v2.bak-<time>`; never overwritten or deleted), then writes v3. If either step
  fails, the feature stays OFF for that session and the v2 file is untouched.
- **v1 migration:** fills 27 per dresser.
- **Wire:** DRESSER_STATE v3 = 27 items plus an explicit zero pad (76 bytes; v2 was 68). The client accepts only v3. All slot bounds (PUT/TAKE/MOVE/SWAP)
  use 27.
- **Pickup lock:** counts all 27 slots plus any residual keep items. It is still one record per dresser, so re-key, orphan, follow and rotation logic is
  unchanged.
- **Residual vanilla keep items:** they stay in the house image. When the 27-cell screen opens, they are taken out to free pockets through the vanilla
  take-out (the pocket and keep slot change in one call, and the room commit carries it under the conservation check).
  - Notice 9: "Old drawer items were moved to your pockets".
  - If the pockets are full, the rest stays in the keep slots (still under the pickup lock). Notice 10 asks to make room; the next open retries.
  - Nothing is lost or duplicated.

### B: Grab (replaces "Move")
- **Tags in dresser mode only** (the vanilla lists are restored everywhere else):
  - a cell offers "Take it / Grab / Quit";
  - a pocket item offers "Put Away / Grab / Quit".
- **The carry is a view state only.** The hand draws the carried item like a held item (`mHD_draw_item`); nothing changes until the placement. A hint line
  is shown while carrying.
- **One operation per placement:**

| From | To | Operation |
|---|---|---|
| Cell | Cell | MOVE (op 3; an occupied target means a swap inside the pool) |
| Cell | Empty pocket | TAKE into that pocket |
| Pocket | Empty cell | PUT into that slot |
| Cell | Occupied pocket, or pocket to occupied cell | New op 4 SWAP |
| Pocket | Pocket | The local pocket swap (vanilla inventory behaviour) |

- **Cancelling:** A on the origin, B, or closing the screen cancels with nothing moved. The carried item is re-checked at its origin before every placement.
- **SWAP (kind-18 op 4):** `aux_item` = pool slot, `slot` = pocket, `item` = the pool item, dest POCKET.
  - The host requires the pocket item to be NORMAL and storable: the predicate applies on both sides. A non-storable pocket item gets notice 11.
  - Store step: the slot gets the pocket item and the pocket gets the pool item, in one journalled entry (the journal keeps the item that entered the
    pocket). Replay-safe and fenced; the restart roll-forward handles SWAP.
  - Client: apply sets the pocket, and both classes are rebased.

### Tests
| Kind | Tests | Status |
|---|---|---|
| Native | `dresser_store_selftest`: 27 slots, v1 migration at 27, MOVE, SWAP, v2 -> v3 conversion (lossless, journal replay, byte-identical .v2.bak, never-overwrite, v3 reload) | Run |
| Source | `test_dresser_ui_source.py`: 6r checks; superseded 6f/6q pins removed or updated | Run |
| Runtime | `test_dresser_storage_protocol.py` (STATE v3, 27-slot fill, Q4b SWAP + replay); `test_dresser_migration_protocol.py` (27 + 27, v3 header); `--ui-grab` (move / swap / put / cancel through the pad) | Written, NOT run (the user's session was active) |

## 6s. Visual fixes after the 15:13 test (2026-10-12)

1. **Icon centring.** `mTG_pc_dresser_col_pos` changed from {-109,-86,-64,-41,-18,4,27,49,72} (x / 0.975) to {-106,-84,-62,-40,-18,4,26,48,70}, the
   drawn cell centres. This follows the vanilla convention: `mTG_haniwa_col_pos` {-35,-11,13,37} and line 46 ARE the gyroid strip's cell centres.
   - The old values pushed every icon outward by 2.5 % of x: -2.7 units in column 0 up to +1.8 in column 8.
   - Rows (82 / 60 / 38) and the pocket table (vanilla) are unchanged.
   - `test_dresser_layout_geometry.py` P9 now reads the real tables (`m_tag_ovl.c` and the `m_haniwa_ovl.c` cell quads) and asserts icon centre == cell
     centre for all 27 cells.
2. **Grab origin hidden.** The icon of the grabbed cell or pocket is not drawn while the item is carried; the hand draws it. This is view only and is
   restored on place, cancel or close.
3. **Solid backdrop.** Dresser mode draws one opaque full-screen rectangle (RGB 52/40/30, widescreen stretched) BEFORE the menus (`mSM_menu_ovl_draw`).
   The frozen room and gyroids are no longer visible. Every other menu is unchanged.

## 6t. Follow-up to the 15:27 test (2026-10-12)

1. **Icon offset: still unresolved, so a debug view was added.** Set `AC_DRESSER_DEBUG_GRID=1` and the screen draws two sets of markers:
   - RED marks at the drawn cell centres, through the window's own transform.
   - GREEN marks at the icon anchors, through the icon's own mapping (`160 + (int)(X * 0.975)`, `120 - Y`).

   Their distance on screen is the remaining offset per cell. The model-space numbers (rows exact; x below) cannot decide which x mapping the PC renderer
   really uses for the window, so the user's report from this debug view decides the final x values.
2. **The centre hint text while carrying is removed.**
3. **Backdrop.** The 6s POLY_OPA texrect came out as a tint. It is replaced: in dresser mode the prerendered frozen scene (the full-screen COPY rect in
   `makeBumpTexture`) is NOT drawn. A FILL-mode rectangle draws instead: the opaque path the frame clear uses, with no texture, no blend and no Z, in RGB
   52/40/30 and widescreen-stretched. Every other submenu keeps the frozen scene.

## 6u. After the debug-grid screenshot (2026-10-12)

- **Icon offset.** The user's `AC_DRESSER_DEBUG_GRID=1` screenshot shows the red cell-centre marks and the green icon-anchor marks overlapping in every
  cell; the remaining gap is sub-pixel. The column values {-106..70} stay as they are. The debug grid stays (env-only).
- **Backdrop.** The 6t backdrop is REVERTED: the frozen room scene is drawn behind the dresser screen again, as behind every submenu.
- **Window.** The w13 "hole" quad is no longer drawn. It filled the skipped gyroid strip with the gyroid-pattern texture and an orange bar, which showed up
  as a dark block with gyroid figures in row 3. Instead, right after the window panel:
  - an OPAQUE solid tan panel (RGB 236/205/150) covers the enlarged inner rectangle (x -120..84, y 26..94), so nothing shows through the grid area;
  - the bottom border gap the skipped strip leaves (rx(-47)..rx(49), y 22..26) gets the border's orange (245/125/55);
  - then the window's XLU mode is restored for the cells.

## 6v. Flat window interior (2026-10-12)

- **Background layers.** In dresser mode the window's background layers w1..w13 are drawn with colour = ENVIRONMENT (RGB 215/185/155, the window
  border's own env tan) and alpha = TEXEL1 (each layer's shape mask). Normally they use the gyroid/house pattern texture (hni_win_mode: colour = TEXEL0).
  The result: the whole oval interior is one flat tan, and the orange outline and the shape are kept.
- **Panel.** The opaque panel behind the grid uses the same tan, so no edge line shows where it meets the rest of the interior. The 6u orange "gap" quad is
  removed: that band lies inside the window, not on its border.
- **Unchanged:** the pockets area and every other menu.

## 6w. Flat interior, mask kept (2026-10-12)

- **What went wrong in 6v.** The ENVIRONMENT/TEXEL1 combiner override lost the layers' mask alpha in the 15:48 build. The w-layers drew as opaque white quads:
  a white rectangle around the window and a white bar under it.
- **Fix.** The combiner, render mode, blend and TEXEL1 mask of hni_win_mode stay vanilla. Only tile 0 (TEXEL0, the gyroid pattern) is re-pointed, before
  w1..w13, at a 4x4 GX RGBA8 texture of the exact tan (215,185,155) in `mHW_pc_dresser_flat_tex`. RGBA8 is used instead of an RGB5A3 palette so the
  colour is not quantised to 5 bits. The layers keep their oval shape: the area outside it is transparent and the room shows through.
- **Panel.** The interior as rendered is winT's inner tan (env 215,185,155) blended at its own alpha over the flat tan layers, which gives exactly
  215,185,155. The opaque panel uses the same macros (`MHW_PC_DRESSER_TAN_*`), so the interior and the panel are the same colour.
- **Not visually verified.** A screenshot of the new build is needed.

## 6x. The first open after re-entering the house showed the vanilla menu (frame-rate bug, fixed 2026-10-16; source + harness verified, real run blocked)

- **Symptom (user).** After leaving and re-entering the house, the FIRST A on a dresser showed the vanilla 3-slot menu with no notice; the second A worked.
- **Root cause (confirmed in the source).** The user's client runs uncapped (`settings.ini` vsync = 0, max_fps = 0; only a dedicated server is forced to
  60 Hz, `pc_main.c`). The direct-open wait counted RENDERED frames: `#define aMR_PC_DIRECT_OPEN_TIMEOUT 180` compared with `my_room->msg_timer`, which
  `aMR_SetFtrDemoMsg` increments once per frame. That is 3 s at 60 fps but 0.2 to 0.4 s at the user's frame rate. A new room visit forgets the selection
  and the contents, so the first open needs a full SELECT -> STATE round trip. The host was busy applying the exit commit, the answer missed the
  window, and the code fell back silently. The SELECT re-ask (`++waited < 180` in `aMR_PcDresserSelectFtr`) was frame-counted too. Each re-ask bumped the
  generation, and a STATE for an older generation is ignored, so a slow answer was thrown away and the wait started again.
- **Fix.**
  - The direct-open wait is wall clock (`pc_net_game_now_ms()`). `aMR_PC_DIRECT_OPEN_TIMEOUT_MS` is 3000 ms. The start time resets with every open
    demo (`msg_timer <= 1`) and whenever a different piece is opened.
  - After `aMR_PC_DIRECT_OPEN_NOTICE_MS` (500 ms) the new notice 12 shows: "Asking the host for your stored items...". It is cleared when the view
    arrives.
  - At the cap the vanilla menu opens with notice 6 ("Try again in a moment"), so the furniture stays usable when the host is down.
  - The SELECT re-ask is wall clock: 3 s after the last SELECT.
  - A re-ask of the same piece in the same visit, while it is still unanswered, keeps the generation (`pc_net_game_dresser_select`, logged as
    "re-ask: same generation"). A slow answer is adopted instead of discarded. Any other selection, or a new visit, still bumps the generation.
  - Row 5: the direct open of an UNCOMMITTED dresser (the open gate's 8 s cap, or in-room commits stopped) is no longer silent.
    `pc_net_game_dresser_direct_uncommitted` logs one line and shows notice 4 (still saving) or 2 (only the room exit resolves it), then the vanilla
    menu opens.
- **Not done (optional item 3).** No SELECT is sent early on room entry. The host keeps ONE selection per peer, so a prefetch would have to guess the
  piece. With the wall-clock wait and the kept generation, the first press already succeeds; it only costs one round trip.
- **Same bug class elsewhere (audit, see the roadmap note "Frame-rate independence of PC waits").** These are now wall clock too:
  - `PC_DRESSER_UI_DEFER_MS` = 10000 (was 600 polls).
  - The shop restock eject grace (500 ms) and hard cap (`aSI_PC_EJECT_HARD_CAP` = 300000 ms, was 18000 frames).
  - The in-room commit quiet window: `PC_NETGAME_HCL_QUIET_FRAMES` 30 AND `PC_NETGAME_HCL_QUIET_MS` 500.
- **TEST-ONLY.**
  - `AC_TEST_DRESSER_SELECT_DELAY_MS` on the host holds a SELECT for that many ms; the real handler answers it later.
  - `AC_TEST_DRESSER_UI_REENTER` on the client: the "direct" hook leaves, walks in again and opens again.
  - The "direct" hook's own give-ups are wall clock.
- **Tests.**
  - `test_dresser_ui_source.py` FR1-FR7. FR1 pins that no frame constant decides the direct-open fallback; FR6 models the generation logic.
  - `test_house_sync_src.py`: the quiet-window ms check.
  - `test_shop_restock_talk_src.py`: the eject grace and cap are wall clock.
  - `test_dresser_real.py --ui-direct reenter | slow | timeout` (written; the client runs uncapped with `--no-framelimit`). These are NOT RUN yet: the
    Samsung display was not connected.

## 7. Stage 6 (NOT done, backlog): full move / swap support (Option B)

- Client: when a dresser actor whose extra storage is non-empty finishes a push / pull / rotate (the furniture actor's move state
  returns to STOP at a new origin cell or rotation), send a kind-18 REKEY op {old floor/layer/ut/ftr -> new ut/ftr rotation-free} with
  the usual (nonce, seq) journal; the client keeps a local pending list until the next house commit is ACKed.
- Host: a REKEY is recorded as PENDING (not applied). When the next OWNER_COMMIT of that house is APPLIED, every pending REKEY is
  checked against the image diff (the old cell lost that furniture id, the new cell gained it); confirmed ones are applied in order
  (chains A->B->C collapse; swaps resolved because each rekey names its own old cell); unconfirmed ones are dropped (the identity stays,
  and the Option-A orphan rule still applies to whatever moved without a REKEY). A REJECTED / rolled-back commit drops nothing: the
  pending list waits for the next APPLIED commit or a reconnect (taint / reconcile clears it).
- Tests: swap of two identical pooled dressers, a chain move, a move + rollback, a move while disconnected (taint path).
