# Multiplayer phase 2 design: resident credentials, town cache, town transfer, promotion, admission

Status: **design**. Milestones M-A .. M-F are implemented in order (M-F in its reduced scope). Implementers update this document with an "Implementation status" section as each milestone lands.

Anchors refer to `ACGC-PC-Multiplayer` at HEAD 0c9bc72 (branch `multiplayer`).

## Code facts the design depends on (checked in the code)

- **Card A path is fixed at compile time.** `pc/src/pc_m_card.c:63-67` defines `PC_CARD_A_DIR`, `PC_GCI_PATH` and `PC_GCI_TMP_PATH` as string-literal macros. There are about 20 use sites in that file: the writer (l.339/346), legacy migration (l.800), the scan (l.836), reload (l.872), load (l.890) and travel (l.2128/2240). `pc/src/pc_card.c:81` has its own `card_dir[2]` and is used by `pc_card_scan_for_gci`.
- **When the save is loaded.** It is loaded in `second_game_init` (`src/second_game.c:101`), after `pc_main.c:1551 pc_net_game_start_client` and inside `boot_main`. Anything that changes which save is read must happen before `boot_main` (`pc_main.c:1584`).
- **The loader renames an orphaned `.tmp` into place** (`pc_m_card.c:908`), and the scan picks up any `*.gci` starting with "GAF". A download must therefore never be staged as `*.tmp` or `*.gci` inside `card_a`.
- **Network clients never write the save** (`pc_m_card.c:2039/2052`). A local town copy is only a read-only cache.
- **IDENTITY_EXT** (`pc_net_game.c:576`, 42 bytes) is ignored as a whole when an unknown flag bit is set (l.22042). An old host therefore drops a new flag and admits the peer the legacy way.
- **IDENTITY_TOKEN** is ignored by a client that did not send a guest claim (l.22625).
- **REJECT handling:** the client turns an unknown reason into "reason N unknown: update the game" (G6.1), and every REJECT is permanent for reconnect.
- **Transfer precedent:** RECORD_CHUNK is 1012 bytes with a `u16 offset`, so it cannot address a 467 KB file. The reliable window is 64 (`pc_net.h:80`). Senders pace with `pc_net_reliable_backlog`.
- **Size of a town file:** a GCI is 64 + 0x72000 = 467,008 bytes, which is 468 chunks of 1000 bytes.
- **Vanilla new resident:** `mSDI_StartInitNewPlayer` (`m_start_data_init.c:458`) runs `mPr_InitPrivateInfo` (`m_private.c:232`). That gives a town PID with `player_id = 0xF000|rand` (unique), `exists = TRUE` and loan 100. House: `mHS_set_use(player_no, house_no)` (`m_house.c:101`) swaps the arrangement and sets `ownerID` through `mHm_InitHomeInfo`.
- **`servers.ini` parser is strict:** an unknown key makes the file CORRUPT. Adding `town_key` to it would make older builds refuse the whole file, so the learned town key must live elsewhere.

## 1. Resident credentials

**Host file `save/mp/members.dat`** (new pure module `pc/src/pc_mp_members.c/.h`, copying the `pc_mp_guests.c` storage pattern: tmp, rotate .bak1/.bak2, atomic replace, corrupt file moved to `.corrupt-<ts>`, UNTRUSTED mode, CRC32).

- Header: `magic "ACMPMBR\0"`, version 1, slot_count 16, entry_size, generation.
- Each entry is 72 bytes: `u8 present, u8 confirmed, u8 res_slot, u8 kind (1 = RESIDENT_TOKEN, 2 = PROMOTION_HANDOFF)`, town key (`land_name[8], u16 land_id, u16 rsv, u32 terrain_hash`), `pid[20]` (BE, the resident's town PID), `token[16]`, `u32 age`, `aux_pid[20]` (handoff only: the guest's home PID).
- It is loaded at host start next to `guests.dat` and written synchronously at mint, before the token is sent. It is not tied to the GCI save.

**Client storage.**

- A store character keeps its token in `characters/<uuid>/towns/<townkey>/token.dat` (existing PCMpGtk format, one entry, `home_pid` = the resident's town PID) and `membership.ini` with `role=resident` and `town_pid`.
- A legacy or CLI resident (no character) uses a new file `save/mp/resident_token.dat` in the same PCMpGtk format, keyed by (town, resident PID).

**Wire (no protocol bump; v8 is extended in place as before).**

- New flag `PC_NETGAME_IDEXT_FLAG_RESIDENT 0x02`. It is the same 42-byte struct: `home_*` = the resident PID and must equal the IDENTITY's name, player_id, land_name and land_id. `token_present` and `token` as for guests. Exactly one of GUEST or RESIDENT may be set.
- The host's validator (l.22042) accepts either single bit.
- The reply is IDENTITY_TOKEN with the new flag `PC_NETGAME_IDTOKEN_FLAG_RESIDENT 0x04` together with NEW or KNOWN. `guest_slot` carries the resident index and `table_size` is 4.
- The client gets a new `s_client_resident_claim_sent` gate. The "DIFFERENT token: refuse the host" rule is the same as for guests.

**Compatibility with old peers.**

- Old host: drops the flag, admits the legacy way, sends no token, and the client keeps playing without a credential.
- Old client: sends no EXT. Under `tofu` it is admitted only while the slot has no credential. Under `required` it is refused.

**New REJECT reason 5, `RESIDENT_CREDENTIAL`** (8-byte form). A new client shows "resident credential missing or wrong: ask the operator for resident-reset/arm". An old client shows "unknown reason 5: update the game", which is correct advice. The frozen reasons 1-4 keep their meaning.

**Policy `resident_tokens = off|tofu|required`** (settings.ini `[Network]`, overridden by `--resident-tokens`).

- **off (default):** the code path is byte-identical. The flag is parsed and ignored, nothing is minted. CLI, dev and every existing test are unaffected.
- **tofu:**
  - Slot has a credential: a matching token gives KNOWN; a missing or wrong token gets REJECT 5.
  - Slot has no credential and the client sent a RESIDENT EXT: mint, write `members.dat`, then send NEW.
  - Slot has no credential and the client sent no EXT (legacy): admit without minting and log "resident N has no credential (legacy client)".
- **required:** like tofu, but a slot without a credential is refused unless the operator armed it.
- **UNTRUSTED `members.dat`:** under `required` every resident is refused. Under `tofu` residents are admitted legacy-style with no mint and nothing is written.

**Console commands** (dedicated host; same order of checks as G6.2: world ready, not UNTRUSTED, selector, refused while bound, a `confirm` word, backup `members.dat.bak-<ts>`, then the change):

- `residents` lists slot, name, credential state and confirmed flag. It never prints a token.
- `resident-reset <slot|name> confirm` deletes the credential, so the next claim mints a new one.
- `resident-arm <slot|name> confirm` allows one mint under `required`: in memory only, 10 minutes, one use.

## 2. Per-server town cache without swapping `card_a`

- Replace the macros with `pc_card_a_dir()` and `pc_gci_path()` / `pc_gci_tmp_path()`, backed by static buffers set once through `pc_card_set_town_dir(const char*)`. This must happen before `boot_main`, and `pc_card.c` gets a matching setter for `card_dir[0]`.
- The default stays `save/card_a`, so behaviour is byte-identical when nothing is set.
- When a town dir is set:
  - Skip `pc_save_migrate_legacy`, because it would rename `save/DobutsunomoriP_MURA.gci` into the new dir.
  - `pc_ensure_save_dirs` creates the town dir.
- **Layout:** `save/mp/towns/<townkey>/card_a/DobutsunomoriP_MURA.gci`, `.../incoming/town.part`, and `.../origin.ini` (server name, address, port, last fetch time).
  - The town key is the M2 `townkey` and comes from the host's TOWN_INFO.
  - The cache is shared by all characters.
  - Clients never write it except through a fetch.
- **Mapping server to town:** scan `towns/*/origin.ini`. `servers.ini` is not touched (strict-parser problem above).
- **Hidden CLI flag `--town-dir DIR`** for tests and the fallback, client only.
- **Existing `save/card_a` is never moved or deleted.**
  - Optional adopt-on-match: after a READY whose host town equals the local `save/card_a` town and with no cache present, copy the file (never rename) into `towns/K`.
  - The host keeps using `save/card_a`.

## 3. Town transfer (pre-boot fetch, new ids 62..65)

**Where it runs.** `pc_net_game_town_prefetch(host, port, timeout)` is called from `pc_main.c` after `pc_platform_init` (so a window exists for progress in the window title and an SDL message box for errors) and before `boot_main`. It only runs when `--town-fetch` is given; the legacy `--connect` is unchanged.

**It is a separate, single-purpose connection:**

1. `pc_net_init`, `pc_net_client_connect`, then pump `pc_net_poll` / `pc_net_next_event` itself.
2. Exchange the TOWN messages.
3. `pc_net_disconnect(INVALID)` and `pc_net_shutdown()`.
4. Set the town dir.
5. The normal `pc_net_game_start_client` follows.

This keeps `pc_net_game_poll`, which touches game state, out of the pre-boot phase.

**Messages (all RELIABLE):**

| id | name | direction | size | fields |
|---|---|---|---|---|
| 62 | TOWN_FETCH_REQ | C→H | 40 B | `type, flags 0, rsv16, u32 protocol_version, u32 have_crc32, u32 have_size, have_land_name[8], u16 have_land_id, rsv16, u32 have_terrain_hash, rsv[8]` |
| 63 | TOWN_INFO | H→C | 40 B | `type, status, u16 chunk_size=1000, u32 xfer_id, u32 total_size, u32 crc32, land_name[8], u16 land_id, rsv16, u32 terrain_hash, u32 town_gen, u32 chunk_count` |
| 64 | TOWN_CHUNK | H→C | 1012 B | `type, rsv, u16 len, u32 xfer_id, u32 offset, data[1000]` (u32 offset fixes the RECORD_CHUNK limit) |
| 65 | TOWN_DONE | C→H | 12 B | `type, status (0 OK, 1 BAD_CRC, 2 BAD_GCI, 3 IO, 4 ABORT), rsv16, u32 xfer_id, u32 crc32` |

- TOWN_INFO status values: 0 STREAM, 1 UP_TO_DATE, 2 UNAVAILABLE (no saved town), 3 BUSY, 4 REFUSED by policy, 5 BAD_REQUEST.
- `wire_baseline.EXPECTED_MAX_MSG_ID` goes from 61 to 65 (66 with promotion).

**What the host serves:** its last durably saved GCI file (`PC_GCI_PATH`), read into a malloc'd buffer when the request arrives.

- The snapshot keeps a concurrent atomic save from tearing the stream.
- The file is checksummed and is exactly what the client loader expects, so no new serializer is needed.
- `town_gen` is a counter bumped in `pc_save_write_gci` on the host.
- It can be up to about 60 s stale. That is acceptable because the READY snapshot plus records, houses and town services bring the session up to date.

**Host serving rules:**

- Only for a HANDSHAKE peer that has not sent IDENTITY. That connection can never bind an identity; IDENTITY on it is ignored.
- At most 2 concurrent fetches, reusing the per-IP limiter pattern of `pcnetgame_guest_mint_allowed`.
- Paced with `backlog < 32` per host poll.
- The connection is closed after TOWN_DONE or after 30 s.
- Host option `town_serve = on|off`.

**Client rules:**

- Chunks must arrive in order at offset = expected offset. They are written to `towns/K/incoming/town.part`.
- After the last chunk: check size and CRC32, then run a new exported `pc_save_validate_gci_file(path, &town)` (wrapping the static `pc_read_gci_land_info`) and require the town to equal TOWN_INFO.
- Install with `MoveFileExA(REPLACE_EXISTING|WRITE_THROUGH)` into `card_a`.
- A partial or invalid file never replaces a valid cache. The `.part` file is deleted only by the next fetch.
- UP_TO_DATE: when the client's (crc, size) equal the host's, the host does not stream.
- Refresh policy is "refetch whenever different", every connect (about 1 s on a LAN). There is no stale-cache logic to get wrong.
- Fallback when TOWN_INFO does not arrive within 3 s (old host) or the fetch fails:
  1. An existing `towns/K` found through `origin.ini` (offline-capable).
  2. Otherwise the legacy `save/card_a`.
  3. Otherwise a message box ("host has no town transfer; copy the save").
- Reconnect does not fetch again (the process is already booted). Switching servers is a new connect, so a new fetch (Play Online connects in-process now; the promoted-guest handoff still relaunches).

**Privacy, stated plainly:** the GCI contains every resident's pockets, mail, diary and designs. The first version serves it unsanitized, which is acceptable only for supervised friends/LAN use, and this must be documented.

- **M-B2 (deferred) sanitizes** other residents' client-owned ranges. It must first make sure a sanitized record can never be MIGRATEd back: a host-side "rev 0 → host wins when the client's cache came from a transfer" rule. Otherwise D3's "client wins once" would upload an emptied record and lose data.

## 4. Guest to resident promotion (smallest correct form)

**Host console command `promote <guest> <slot|auto> <house|auto> confirm`.**

Preconditions:

- Dedicated host with a ready world; the guest is offline; `guests.dat` and `members.dat` are trusted.
- A free slot `s` (`mPr_CheckPrivate(&priv[s]) != TRUE`) and a house `h` with a null `ownerID`.
- The name is unique among residents.
- Backups are made of all three files.

New routine `pc_mp_promote_create()` (TARGET_PC, in `pc_m_card.c` next to the guest builders), run on the game thread:

1. `mPr_ClearPrivateInfo` + `mPr_InitPrivateInfo(&priv[s])`.
2. Copy name, gender, face and shirt from the guest's BE record (through the existing record swap). Carry over pockets, wallet and bank. Everything else gets the vanilla new-player defaults.
3. `mHS_set_use(s, h)`.
4. Seed the records lineage for `s` at rev 1, so the host pushes the record and never MIGRATEs.
5. Write `members.dat`: a resident token for the new PID plus a PROMOTION_HANDOFF entry (guest home PID → new PID, slot, token).
6. `pc_save_write_authoritative()`.
7. Remove the `guests.dat` entry last.

Crash safety on load: a handoff whose PID is not in the GCI is invalid, so the guest entry is kept. A handoff with the resident present is valid.

**Client side:**

- The guest connects as usual with its GUEST EXT. `guest_check` finds the handoff with a matching guest token.
- The host sends `RESIDENT_HANDOFF` (66, H→C, 48 B: `type, flags, res_slot, rsv, town PID[20], token[16], rsv[8]`) and then REJECT reason 6 `PROMOTED`.
- The client rewrites `membership.ini` (`role=resident`, `town_pid`) and `token.dat`, and relaunches with the same arguments.
- The new fetch brings a GCI that contains the resident, which then binds by PID with its credential. The handoff is deleted at the first confirmed resident login.
- An old client drops 66 and shows "unknown reason 6".

**Hard parts (not solved by this design):**

- Villager memories, letters and friendship keyed by the guest PID are lost.
- Whether the promoted resident triggers Nook's first-job/intro event is unverified.
- The house contents left by an earlier owner.
- A full town (4 residents) cannot promote.
- All of it changes the town save outside the vanilla flow.

## 5. Play Online

- **Keep the relaunch.** The role audit forbids starting a role after startup. (SUPERSEDED: Play Online now connects in-process, see docs/multiplayer-guest-roadmap.md "Play Online without relaunch"; only the promoted-guest relaunch remains.)
- **Change** `pc_relaunch_build_args` to add `--town-fetch`.
- **Then, in `pc_main`, after the fetch:** resolve the character's membership for town K.
  - `resident` → set `pc_session()->join_kind = RESIDENT` and `resident_pid`. After the save loads, bind through the `--bootstrap-resident` path by PID instead of by index (the slot is found by `CmpPersonalID`).
  - `guest`, or none → the existing guest arrival. A new character goes through the existing Rover first-run creation.
- **Failures:** SDL message box with Retry / Use saved copy (the cache; M-J: formerly "Play offline") / Quit.
- **Visual check is possible from the parent session.** The computer-use tools available there can launch the exe from a disposable fixture copy, `request_access` the game window (full tier), navigate the menu with the keyboard and take screenshots. No visual check was done during the design. A scripted alternative (a test-only menu script plus a `glReadPixels` dump; `pc_gx.c:2075` already reads pixels) is deferred.

## 6. Admission restructure

**Do not replace `pcnetgame_host_classify_identity`.** `pc_mp_membership_lookup` compares raw bytes, while classify uses `mPr_CheckCmpPersonalID` plus `exists`, so they are not proven equivalent. Instead, add `pcnetgame_host_admission_decide(in, ext)` that returns `{RESIDENT idx | GUEST | REFUSE(reason, text)}` and is built from the existing steps in the same order:

1. `has_save` / town checks.
2. Classify.
3. AMBIGUOUS → refuse.
4. UNKNOWN + no GUEST EXT → `NO_SAVE`.
5. UNKNOWN + GUEST EXT → `pcnetgame_host_guest_check`, including the promotion-handoff probe.
6. RESIDENT + GUEST EXT → refuse.
7. RESIDENT → own-resident check, then the new `pcnetgame_host_resident_credential_check(peer, idx, ext, &mode)` (before the duplicate-park step, so an impostor cannot evict a live peer).

After that:

- Duplicate park (unchanged).
- Guest cap and reserve, plus new `allow_new_guests = 0|1` (default 1). It refuses NEW keys with SERVER_FULL inside `guest_mode != 1`, before mint or create.
- Mint (guest create or re-mint, or resident mint).
- ACK, then IDENTITY_TOKEN.

`pc_mp_membership_lookup` is used only as a logged cross-check, until a native equivalence test lets it become the primary decider.

**What "slot" means for each:**

- Resident: the fixed `private_data` index matched by PID; the host never chooses it at admission.
- Guest: the `guests.dat` table slot, chosen at first contact; the record slot is `PLAYER_NUM + gslot`.
- Promotion: the operator or host picks an empty vanilla slot.

**Source audits to re-pin strictly:**

- They reference `process_identity`: `test_guest_src`, `test_guest_g4_src`, `test_guest_g6_src`, `test_identity_validation_src` (S2/S9/S11 already failing), `test_identity_handshake_timing`, `test_observer_src`, `test_txn_src`.
- They reference classify: `test_guest_src`, `test_identity_validation_src`, `test_observer_src`.
- They reference `card_a`: `test_d3_record_src` ("P hook" already failing), `test_guest_src`, `test_guest_g6_src`.
- The EXT flag mask is in the G1 audits.
- Also update `wire_baseline.py`.

## Milestones

| milestone | files | test tier | notes |
|---|---|---|---|
| **M-A** runtime card dir | `pc_m_card.c`, `pc_card.c`, `pc_main.c` (`--town-dir`) | real process alone: the log shows the load from the town dir, `save/card_a` is byte-identical, legacy file not migrated; plus source audit | low risk |
| **M-B** town transfer | `pc_net_game.c` (TOWN block, host serve + prefetch), new `pc_town_cache.c/.h` (key, paths, atomic install, `origin.ini`), `pc_m_card.c` (`pc_save_validate_gci_file`, `town_gen`), `pc_main.c` (`--town-fetch`), `wire_baseline.py` | native unit (cache module); scripted FakeClient against a real host (streamed bytes equal the host GCI md5, BUSY, rate limit, abort mid-stream); real host + client with an empty save dir reaching READY | depends on M-A |
| **M-C** Play Online wiring | `pc_relaunch.c`, `pc_main.c`, `pc_session.c` (membership resolve, resident by PID), `pc_play_online_menu.c` | `test_servers.py` extension; real process; screenshots manual / computer-use | depends on M-B |
| **M-D** admission refactor | `pc_net_game.c` (`admission_decide`, `allow_new_guests`), `pc_settings.c` | behaviour-identical: all guest/identity scripted suites must pass unchanged; re-pin audits | — |
| **M-E** resident credentials | `pc_mp_members.c/.h`, `pc_net_game.c` (EXT/TOKEN flags, reason 5, credential check, client resident token), `pc_dedicated.c` (commands), `pc_settings.c`, CMake | native storage unit; scripted per mode against a real host; one real resident client in tofu | depends on M-D |
| **M-F** promotion | `pc_m_card.c` (`pc_mp_promote_create`), `pc_net_game.c` (`promote`, handoff 66 / reason 6), `pc_dedicated.c`, `wire_baseline.py` | scripted FakeClient against a real host | reduced scope implemented, see Implementation status |

**M-F, safe reduced scope:** host `promote` + handoff + message 66/reason 6, with a scripted FakeClient test checking the GCI holds the new resident, the guest gets the handoff, and `records.dat` rev is 1. The client auto-adopt/relaunch, the first-job and house behaviour, and the visual check follow as a separate manual-heavy run.

M-B2 (sanitized transfer) stays deferred.

## Highest-value validations

1. **M-A:** a legacy layout plus `--town-dir` never touches `save/card_a` or the legacy file (md5 before and after).
2. **M-B fetch:** a scripted fetch's bytes equal the host GCI. An interrupted fetch leaves the previous cache byte-identical and no `*.gci` / `*.tmp` in `card_a`.
3. **M-B join:** a real client with an empty save dir plus `--town-fetch` reaches READY with no LAND_MISMATCH. Against an old/unsupported host it falls back within 3 s.
4. **M-D:** every existing guest/identity scripted suite passes byte-identically under `resident_tokens=off`.
5. **M-E:** tofu mint → restart → KNOWN. A wrong token gives reason 5. A legacy client is refused once a credential exists. Under `required` + arm, exactly one mint. A corrupt `members.dat` gives UNTRUSTED with the file preserved.
6. **M-F (reduced):** promotion survives a host restart, with the handoff → resident flow consistent.

## Implementation status

### M-A (runtime card dir) and M-B (town transfer): implemented (uncommitted, branch `multiplayer`)

**M-A.**

- `pc_card_a_dir()`, `pc_gci_path()`, `pc_gci_tmp_path()` and `pc_card_set_town_dir()` live in the new pure module `pc/src/pc_town_cache.c` (`pc/include/pc_town_cache.h`), not in `pc_m_card.c`, so `pc_m_card.c` and `pc_card.c` share one source of truth and the module stays natively testable. The default is `save/card_a`, so behaviour is byte-identical when nothing is set.
- All 35 uses of the three macros in `pc_m_card.c` became accessor calls (the macros are gone, only the `*_LEGACY` ones stay). The static name table of the GCI scan is built at runtime. `pc_card.c` `get_card_dir(0)` follows the runtime dir (so `pc_card_scan_for_gci` sees the town dir).
- With a town dir set: `pc_save_migrate_legacy` is skipped, `pc_ensure_save_dirs` / `CARDInit` create `<town>/card_a` (mkdir -p) and do NOT create `save/card_a`.
- The setter is "set once": a second, different dir is refused. A town dir is applied in `pc_main.c` right after the option validation, long before `boot_main`.
- Hidden `--town-dir DIR` (client only, needs `--connect`; exit 2 + usage otherwise; also exit 2 together with `--town-fetch`).
- Not done: adopt-on-match (copying an existing `save/card_a` into `towns/K`). It is not cheap to do safely (needs the READY town and a decision about duplicates), so it stays documented only: copy the file into `towns/<key>/card_a` by hand.
- NES game saves (`famicom.cpp pc_nes_save_dir`) still use `save/card_a` / `save/card_b` directly. They are not part of the town save; unchanged.

**M-B.**

- Wire ids 62..65 exactly as in the table of section 3 (`PCNetGameTownFetchReqMsg` 40 B, `PCNetGameTownInfoMsg` 40 B, `PCNetGameTownChunkMsg` 1012 B, `PCNetGameTownDoneMsg` 12 B), each with a `_Static_assert`. `wire_baseline.EXPECTED_MAX_MSG_ID` is 65, the four structs, the enum ids, the constants and the `net_spike_lib` lines are pinned there. The literal 1..61 id-range checks of the older source audits (`test_guest_g1/g2/g3/g4/g5/g6_src`, `test_guest_src`, `test_mail_src`, `test_mail2_src`, `test_ts_src`, `test_txn_src`, `test_txn_x3_src`, `test_house_sync_src`) were re-pinned to 1..65. `test_d3_record_src.py` follows the renamed accessors (`pc_gci_path()`).
- Host (`pc_net_game.c`, block "TOWN TRANSFER HOST"): the last durably saved GCI is read into a malloc'd snapshot at request time and validated (exact size, `GAF`, town identity; `pc_save_validate_gci_buffer` computes the same FNV-1a terrain hash as `pcnetgame_capture_town_identity`). Only for a HANDSHAKE peer that has not sent IDENTITY / IDENTITY_EXT (otherwise REFUSED, the identity handshake is untouched). From the first accepted request on, the connection is a FETCH connection: IDENTITY / IDENTITY_EXT on it are ignored and logged, and only TOWN_DONE counts. At most 2 concurrent streams and 10 requests per address per 60 s (ring log like `pcnetgame_guest_mint_allowed`), BUSY otherwise; chunks paced by `pc_net_reliable_backlog() < 32` per host poll; the connection ends 1.5 s after TOWN_DONE (see the deviation below) or after 30 s.
- The per-peer fetch state is a separate array (`TownSrvPeer s_townsrv[]`), so the pinned `PCNetGameHostPeerState` is untouched; it is reset through `pcnetgame_reset_all_host_peer_state` (the single dispatch point).
- `town_gen` = number of durable GCI writes since start (`pc_save_town_gen()`, bumped in `pc_save_write_gci` after a real write); it is informational.
- Client (`pc_net_game_town_prefetch`, same file, block "TOWN TRANSFER CLIENT"): as designed (separate connection, own pump, `pc_net_disconnect` + `pc_net_shutdown`, then the normal `pc_net_game_start_client`). The role start for `--connect` moves behind the fetch only when `--town-fetch` is given. Chunks must be in order and of the exact expected length; size, CRC32, GCI validity and town equality are checked before the atomic install (`MoveFileExA(REPLACE_EXISTING | WRITE_THROUGH)`); `origin.ini` is written after the install. Window-title progress via a callback, errors via an SDL message box (suppressed by the test hook env `AC_TOWN_NO_MSGBOX`, then exit code 3).
- Fallback ladder as designed: no TOWN_INFO within 3 s / refused / busy / unavailable / any failure -> the cached town of this address:port (found through `origin.ini`) -> the legacy `save/card_a` -> message box and exit 3.
- Host option `town_serve`: **default AUTO** (later change: ON/sanitized for a `--dedicated` host, OFF for a plain `--host`; see "Dedicated host defaults" in the roadmap), `settings.ini [Network] town_serve = auto|0|1|2` or `--town-serve off|on|full` (host only, exit 2 otherwise). The design left the default open; OFF was the original safer choice for a plain host because the file contains every resident's pockets, mail, diary and designs, unsanitized. The privacy trade-off is documented in `docs/multiplayer-guest-roadmap.md`.
- New client flag `--town-fetch` (client only, needs `--connect`/`--server`, not together with `--town-dir`).

**Deviations from the text above (the code disagreed or a safer choice existed).**

1. `pc_net_game_town_prefetch` takes `(host, port, timeout_ms, progress_cb, server_name, err, err_cap)` and returns a status code (`PC_TOWN_PREFETCH_*`), not just `(host, port, timeout)`; it also selects the town dir itself. Its declaration (and the status codes) are in `pc_town_cache.h`, not in `pc_net_game.h`: several source audits pin `pc_net_game.h` against the git baseline (`numstat`), so the header stays byte-identical.
2. `pc_net_game_start_client` used to run BEFORE `pc_platform_init` (design: "after pc_platform_init"). With `--town-fetch` it now runs after the fetch (after `pc_platform_init`); without the flag the order is unchanged.
3. The host does NOT drop the transport peer inside the TOWN_DONE handler. The first real run showed why: the DONE, the client's DISCONNECT and a NEW connection that reused the slot index can sit in one transport poll batch; `pc_net_disconnect(peer)` from the DONE handler then killed the newcomer (its request was silently lost). The snapshot is released at once and the slot is dropped from the tick 1.5 s later.
4. The rate limiter counts only requests that reach the snapshot stage (STREAM and UP_TO_DATE); malformed / refused ones do not consume it.
5. `--town-dir` and `--town-fetch` are mutually exclusive (a fetch picks its own directory).
6. The GCI check does not verify the Save_t checksum (the loader does not either; the transport checks size + CRC32).
7. A server is mapped to its town by the newest `last_fetch` among all `towns/*/origin.ini` with the same address and port (a host that was reset to another town leaves two entries); the town GCI must exist.

**Tests (all new, none run against the live save).** `pc/tools/net_spike/`:

- `test_town_cache_unit.py`: native unit (`town_cache_selftest.c` + `pc_town_cache.c`, `-Wall -Wextra` warning-free: key / paths, origin.ini round trip, find-by-server, CRC32, in-order part writer, atomic install keeps the old cache on a failed / partial / corrupt download, nothing staged in card_a), source audits, CLI refusals and the M-A real-process check on a disposable `bin_fixture4_town` copy (loads from the town dir, `save/card_a` and the legacy file md5-identical, no legacy migration into an empty town dir).
- `test_town_transfer_all.py` (= `test_town_transfer_protocol.py` P1 / P2 + `test_town_fetch_real.py` R1..R4): streamed bytes equal a version of the host GCI, crc32 and town identity equal TOWN_INFO and the host's own identity, UP_TO_DATE, BAD_REQUEST, abort mid-stream, IDENTITY on a fetch connection ignored, BUSY (concurrency and per-address limiter), OFF by default (REFUSED); a real client with an EMPTY save dir and `--town-fetch` reaches READY without LAND_MISMATCH, the cache GCI equals the host's, the client never writes it; refused host -> cached town; refused / no host and no cache -> `NO USABLE TOWN`, exit 3, within ~3 s of silence.

**Results.** `test_town_cache_unit.py`: 86 / 86. `test_town_transfer_all.py`: 57 / 57 (P1 + P2 protocol, R1..R4 real host + real client; R3 refusal took 0.7 s, R4 silence gave up 3.7 s after start, i.e. ~3 s of waiting). The first run of the protocol test found deviation 3 above.

**Not run / known gaps.**

- The re-pinned older source audits (list above) and `test_d3_record_src.py` were edited but NOT executed (test budget); only `wire_baseline.py` (audit + `--selftest`) was run and is green. `test_identity_validation_src.py` S9 (pc_m_card.c diff vs HEAD touches no GCI layout line) was already failing and now also sees the new accessor / validator lines; it was not touched.
- Not verified: the SDL message box and the window-title progress (no screenshot; the box is bypassed by `AC_TOWN_NO_MSGBOX` in the test), a real OLD host (the "no TOWN_INFO within 3 s" path was exercised against a closed port, which gives the same silence), a lossy / slow link, and `town_serve = 1` through `settings.ini` (only the CLI override was exercised; the parser line is source-audited).
- Remaining design items: adopt-on-match (copy only), M-B2 (sanitized transfer), Play Online passing `--town-fetch` (M-C).

### M-D (admission refactor) and M-E (resident credentials): implemented (uncommitted, branch `multiplayer`)

User documentation: `docs/multiplayer-guest-roadmap.md`, section "Resident credentials and admission".

**M-D.** `pcnetgame_host_admission_decide(peer, in, ext, ext_valid, &adm)` returns `{RESIDENT idx | GUEST | REFUSE(reason, text)}` as DATA, built from the existing steps in the same order
(has_save / town, classify, AMBIGUOUS, UNKNOWN + no GUEST claim = NO_SAVE, UNKNOWN + GUEST claim = `pcnetgame_host_guest_check`, RESIDENT + GUEST claim refused, own-resident check, then the M-E
credential check); `pcnetgame_host_admission_apply_refusal` produces the original logs / wire answers (the has_save / LAND_MISMATCH log lines and the 24-byte forms are byte-identical; the
guest-check refusals stay inside `pcnetgame_host_guest_check`, flagged HANDLED). `pcnetgame_host_process_identity` keeps the duplicate park, the guest cap / reserve, the mint, the ACK and the
IDENTITY_TOKEN. `pcnetgame_host_classify_identity` is NOT replaced; `pc_mp_membership_lookup` is only a logged cross-check (`pcnetgame_host_admission_crosscheck`, a line only when it disagrees).
`allow_new_guests = 0|1` (settings.ini `[Network]`, `--allow-new-guests 0|1`, default 1) refuses a NEW guest key (`guest_mode == 0` only) with SERVER_FULL inside the `guest_mode != 1` block before the
rate limiter and before `pcnetgame_guest_create`; a known key (token verified) and an operator re-mint are unaffected. Deviation: the function takes `peer` (the guest check logs / tears down per peer) and
`ext_valid` as well, not just `(in, ext)`.

**M-E.** New pure module `pc/src/pc_mp_members.c` / `pc/include/pc_mp_members.h` (members.dat v1). Wire (v8 extended in place): `PC_NETGAME_IDEXT_FLAG_RESIDENT 0x02`, `PC_NETGAME_IDTOKEN_FLAG_RESIDENT 0x04`,
REJECT reason 5 `RESIDENT_CREDENTIAL` (8-byte form), all pinned in `wire_baseline.py` and `net_spike_lib.py`. Policy `resident_tokens = off|tofu|required` (settings.ini `[Network]`, `--resident-tokens`,
default off = the credential check returns before any file access; `members.dat` is only loaded at world ready when the policy is not off). Console: `residents`, `resident-reset <slot|name> confirm`,
`resident-arm <slot|name> confirm`; help updated. Client: `s_client_resident_claim_sent`, RESIDENT claim sent whenever the client plays a resident, `pcnetgame_handle_client_resident_token`
(DIFFERENT-token rule), token file = the store character's per-town `token.dat` (+ `membership.ini` `role=resident`) or `save/mp/resident_token.dat`.

**Where the design disagreed with the code, and what was chosen (the safer behaviour).**

1. **Entry size 80, not 72.** The design's own field list (4 + 16 town key + 20 pid + 16 token + 4 age + 20 aux pid) adds up to 80 bytes. The format stores 80 (`entry_size` is in the header and checked), file size 1316.
2. **A token presented for a resident the host has NO credential for is refused (reason 5), never minted over.** Minting would send a token different from the presented one, so the real client would refuse the
   host ("DIFFERENT token") and the host would keep an orphan credential that locks the slot. Applies to `tofu` and `required` (the operator reset / a wiped table needs the client to delete its token too).
3. **Legacy client under tofu:** exactly the design text: admitted without a mint only while the slot has no credential, refused once one exists. Under `required` + armed a legacy client is still refused (it cannot
   receive a token) and the arm is kept.
4. **EXT home must equal the IDENTITY** (name, player id, land name, land id) only when the policy is not off; a mismatch is REJECT 5. Under `off` the flag is ignored entirely. An EXT with both GUEST and
   RESIDENT set is a malformed claim (logged `malformed IDENTITY_EXT ignored`, treated as no claim); `flags == 0` is still accepted as before.
5. **`confirmed`** = the client presented the matching token once (set at the KNOWN admission), not "started a record exchange" as for guests; the mint is rolled back when the ACK / token cannot be queued.
6. **`resident-arm`** is refused unless the policy is `required`, refused when the slot already has a credential, and needs no backup (memory only); `resident-reset` backs `members.dat` up first.
7. The resident claim is **sent by every resident client** (not only "when a token file exists or TOFU first claim"): the client cannot know the host policy, and a first claim has no file yet. A host with `off`
   ignores it (one extra `IDENTITY_EXT cached (resident claim ...)` log line); an old host drops the unknown flag.

**Source audits re-pinned (edited, then run green or reduced to the failures that pre-date this work).** `wire_baseline.py` (host-only types, reason 5, new constants; `--selftest` green),
`test_guest_src.py` (the reviewed `Save_Get(private_data)[...]` function list: + the 8 new host functions and the 2 pre-existing dedicated ones that were already missing), `test_guest_g6_src.py` (reject enum:
the 4 frozen reasons + 5), `test_identity_validation_src.py` (S2 / S3 / S11 now read decide + apply_refusal + process_identity as one ordered text; the 8-site refusal count; `g_pc_dedicated` excluded from the
"no g_pc_ bypass" check; the optional resident-mint block in the park regex), `test_guest_profiles_src.py` (token file load / save sites). Remaining failures of those files and of `test_guest_g1/g2/g3/g4/g5_src`,
`test_observer_src`, `test_txn_src`, `test_d3_record_src`, `test_identity_handshake_timing`, `test_guest_train_src` are unrelated to this change (git-baseline comparisons, `pc_m_card.c`, mail / house lists) and were not touched.

**Tests** (`pc/tools/net_spike/`; all on disposable `pc/build64/bin_fixture4_*` copies, never the live save, `bin_talkfix*` or `bin_fixture4`).

* `test_members_unit.py` (+ `members_selftest.c`): native storage unit, `-Wall -Wextra` warning-free: format and field offsets, every parse refusal, rotation `.bak1`/`.bak2`, recovery from `.bak1`, a CRC-corrupt
  file moved to `.corrupt-<ts>` (byte-identical) and the sticky UNTRUSTED mode, save self-validation, an independent Python writer parsed by the C reader, no token in any output. **69 / 69.**
* `test_resident_credentials_protocol.py`: scripted FakeClients against real `--dedicated` hosts (7 host processes on `bin_fixture4_resid`): off (a resident with and without the claim: READY, no token, no
  members.dat, no `[NET][RESIDENT]` host line; a first guest still gets its NEW token), tofu (mint: IDENTITY_TOKEN `RESIDENT|NEW`, members.dat written before it is sent and holding the sent token; reconnect KNOWN +
  confirmed; wrong / no token / legacy client / mismatching EXT home = REJECT 5 (8 bytes, protocol 8); an impostor cannot evict the live peer; a legacy client of a credential-less slot is admitted with the log line
  and nothing written; `residents` never prints a token; resident-arm refused under tofu; resident-reset refused while bound / without `confirm` / unknown selector / no credential, with `confirm` a byte-identical
  `members.dat.bak-<ts>` backup then deletion; the old token refused, a token-less client mints a NEW different token), host restart (KNOWN, credential persisted), required (no arm = REJECT 5, arm = ONE mint then
  armed=no, the second claim and another resident refused, arming a resident with a credential refused, a legacy client refused on an armed slot), corrupt members.dat (tofu: UNTRUSTED, residents admitted
  legacy-style, bad file preserved, nothing written, resident-reset refused; required: every resident refused), `--allow-new-guests 0` (a NEW guest key = REJECT 2 with the log line and guests.dat byte-identical,
  the known guest returns KNOWN, residents unaffected), plus source audits of the new code. **94 / 94.** (A first attempt failed in the test itself: a FakeClient without RECORD_HELLO is closed by the host after
  5 s; residents now send it.)
* Existing guest admission regression: `test_guest_g4_protocol.py` on a fresh `bin_fixture4_g4reg` copy with the new exe (4 / 8 concurrent guests, cap, reserve, wrong token, table full, `max_guests` flag and
  setting, resident admitted at the cap): **65 / 65**, unchanged.
* Not run: a REAL game client against a `tofu` host (the client half is compiled and source-audited only), no real OLD host / old client, no wildlife tests, no full suites.

**Known gaps.** The RESIDENT claim is sent by every resident client but only a real run proves the token file round trip (`save/mp/resident_token.dat`, store character `token.dat` + `membership.ini`);
M-C will have to pass a resident membership; promotion (M-F) is not started (PROMOTION_HANDOFF entries exist in the format but nothing writes or reads them); the credential is not internet-grade (bearer token over
unencrypted UDP, trust on first use).

### M-C (Play Online wiring): implemented (uncommitted, branch `multiplayer`)

**Final flow.** Title -> Play Online -> server list (Add / Edit / Delete, text entry) -> Connect -> character list (existing / New character) -> (originally: the title process RELAUNCHES the exe; now an in-process connect, see the roadmap) with
`--connect HOST:PORT --character UUID|--guest-profile NAME|--guest --town-fetch --online-ui [forwarded options]` and quits -> the new process fetches the town before boot (M-B) ->
`pc_main_resolve_membership()` reads `characters/<uuid>/towns/<townkey>/membership.ini` for the fetched town (townkey = the town dir name) -> resident: `pc_session()->join_kind = RESIDENT`,
the guest arrival is disarmed, `g_pc_bootstrap_resident_pid` is armed; guest / none: the existing guest arrival (a new character goes through the Rover first-run creation) -> after the save loads
the new thin `pc_bootstrap_resident_pid_poll()` (pc_m_card.c, called from pc_vi.c right before the UNCHANGED `pc_bootstrap_resident_poll()`) finds the slot with `mPr_CheckCmpPersonalID` over
`private_data[]` (exactly one match, else nothing is bound and a log line says so), stores it in `g_pc_bootstrap_resident` and the old path binds -> the M-E client claim (token of the
character's per-town `token.dat`) goes out as before -> at READY `pc_session_note_ready()` (called from `pcnetgame_client_note_membership()`, pc_net_game.c) writes `membership.ini`
(role guest|resident, town_pid = the local PersonalID; a recorded resident membership is never downgraded, a matching one is not rewritten) and servers.ini `last_town` (townkey) of the saved server with that address:port.

**Relaunch decision (SUPERSEDED by the in-process connect, see docs/multiplayer-guest-roadmap.md "Play Online without relaunch"; the text below is the original reasoning).** The role audit assumes the role is fixed at process start (host/client/none initialise differently before `boot_main`), and the pre-boot fetch must pick the Card-A
directory before the save is loaded; starting a client inside the running title process would need both to be re-entrant. The cost is one extra process start (a second or two). The title process
exits only after `CreateProcess` succeeded.

**Relaunch arguments.** `--town-fetch` and the hidden `--online-ui` are always added. `--online-ui` only switches the failure boxes below (scripts and the existing tests never pass it, so the silent
fallback ladder is unchanged). Forwarded from the title process: a FIXED whitelist (`--verbose`/`-v`, `--no-framelimit`, `--framelimit N` digits only, `--uber-shader`; `pc_relaunch_forward_capture`).
`--fullscreen` is not a CLI option here: the window mode is a settings.ini value, which the new process reads again, so it is "forwarded" by construction. Name / host / port validation (no injection) is unchanged.

**Failure UX.**
- Fetch failed but an older copy exists (cache of this server or legacy `save/card_a`): SDL box "Retry / Use saved copy / Quit" (only with `--online-ui`; renamed in M-J). Retry un-selects the fallback directory
  (`pc_card_reset_town_dir_for_test`, the only way to re-pick a possibly different town) and fetches again; Use saved copy continues with the old copy (the client then still tries the server and the usual
  auto-reconnect / REJECT title message apply); Quit exits 0. The reason of the failed fetch is now filled into `err` for the CACHE / LEGACY results.
- No usable town at all: box "Retry / Quit" (`--online-ui`) or the old OK box (without); exit 3 on Quit.
- A server REJECT after boot: the existing title message. A failed relaunch (`CreateProcess` error): the Play Online menu stays open and shows the red message (the title process does not quit; by source, not exercised).

**Visually verified (really).** `computer-use` could not be used (`request_access` resolves Start-menu apps only; the exe is not one, so no grant was possible). Instead the disposable
`bin_fixture4_ui` exe was driven with synthetic key events (`keybd_event`) and window-only screenshots (`CopyFromScreen` of the game window / of the message box). Seen: title with
"Play Online" entry; the server list (empty: "Add server / Back" + hint; with one server: "test  127.0.0.1:7777"); the add form (name, IPv4 address, port screens with titles, hint lines, caret);
"Saved server" status; the server actions page (Connect / Edit / Delete / Back); the character list (empty store: "New character / Back"); the new-character name entry; and, after typing a name,
the real relaunch and the fetch-failure box. Findings fixed: the status message at y=195 overlapped the copyright line (moved to y=176, 64 characters); the middle box button "Play offline (use the saved town)"
was cut off and is now "Play offline" (the fixed build was not re-screenshotted). Observations not changed: the typed text is lower case only because the key injection sent no Shift; the title demo
wipes the scene about every 60 s of inactivity but the menu stays open. NOT seen: a populated character list, the Delete confirm page, the Edit form, the resident bind visually,
an in-game screenshot, the "Retry" round trip, the relaunch-failure message.

**Tests.**
- `test_servers.py` (extended `servers_selftest.c`): the new argument strings (`--town-fetch --online-ui`), forwarded options (whitelist, `--framelimit` digits, junk dropped), injection still refused: 78 / 78.
- `test_play_online_real.py` (new, real host + real client in an EMPTY save dir on disposable `bin_fixture4_mc` / `bin_fixture4_mcclient`): guest path (no membership: fetch, guest arrival, READY, `membership.ini`
  role = guest with the home PID, servers.ini `last_town`) and resident path (membership.ini role = resident + PID of fixture resident 1: resolved after the fetch, slot 1 bound by PID, RESIDENT claim under
  `resident_tokens = tofu`, token stored in the character's `token.dat`, membership stays resident): 14 / 14.

**Limitations / not done.**
- The resident bind needs the PID to be in the fetched save; a stale membership is handled in M-J (box + exit 3).
- Under `resident_tokens = off` hosts no token comes back; `membership.ini` is still written at READY (role from the local player).
- "Use saved copy" keeps the client connecting in the background; no true "offline only" mode (see M-J for why). A new character that never reaches READY writes no membership.
- Guest-to-resident promotion (M-F) is not started; the character list does not show the membership role or the town.
- The title attract demo restarts the scene after ~60 s idle (vanilla); a very slow text entry may meet the wipe.

### M-J (Play Online polish + visual pass): implemented (uncommitted, branch `multiplayer`)

**Stale resident membership.** After the town fetch, `pc_main_resolve_membership()` reads the fetched town's GCI (`pc_gci_path()`, 467,008 B) and runs `pc_town_gci_find_resident()` for the membership's PID.
A resident membership whose PID is not a live resident of that town (resident removed, or the town was reset) now logs `[PC] membership: STALE: ...`, shows a Quit-only box ("This character is no longer a
resident of this town (removed or town reset): ask the operator.") and exits 3 before the network client is started; `membership.ini` is left as it is (never downgraded; the operator may restore the resident).
A GCI that cannot be read in full skips the check. `AC_TOWN_NO_MSGBOX` suppresses the box (the log line and the exit stay). The in-game fallback is kept: if the PID poll after boot still finds no single
match, it also sets a title-screen notice ("Could not play this character: ..."), shown for 10 minutes, instead of leaving the title silent.

**"Use saved copy".** The middle button of the fetch-failure box was "Play offline", which is unsafe to implement for real: the role would be NONE and a save made then would land in the town cache, and a
sanitized town copy is blank. It is renamed "Use saved copy" and the box text says it still connects in the background.

**Character list.** A character whose `membership.ini` for the selected server's `last_town` (servers.ini hint, known without any network access) says `role = resident` is listed as `Name (resident)`. A server that
never reached READY has no `last_town`, so nothing is marked there.

**Hostnames: NOT implemented (decision).** The address is an IPv4 literal in `PCServer.address[16]`, the strict servers.ini parser, `pc_relaunch_build_args`, `--connect` (64-byte host buffer), the origin.ini cache key
(64-byte address, compared as a string) and several pinned selftests that assert hostnames are refused. A hostname would change the struct size, the menu text-entry limits, the cache identity (an IP that moves would
orphan or mix cached towns) and the relaunch injection surface in one go; that is not "contained". Resolution at connect time (`getaddrinfo` in `pc_net_client_connect`) would be the easy part. Left for a milestone
that decides the cache identity first.

**Visual pass (really done).** Method as in M-C: the disposable `bin_fixture4_ui` copy (cwd = that folder, its own `save/mp`: 2 servers, 2 store characters (Alice, Roger with a resident membership of a made-up town key),
1 unimported legacy profile Bob, one made-up cached town), synthetic key events (`keybd_event` with scan codes, only after the game window really had the foreground; a failed focus sends nothing) and screenshots of ONLY
the game window rectangle (plus the message boxes by their own rectangle). Boxes were clicked by `BM_CLICK` on the disposable process. Seen: title; server list (populated, selection highlight); actions page; Edit form
(name, address, port steps, then "Saved server"); Delete confirmation; character list (populated, selected row, `(resident)` and `(profile)` markers, New character, Back); new-character entry; the fetch-failure box
with the new button text (both the legacy-fallback and the cached-copy wording); the stale-resident box (reached for real: cached town + fake PID + "Use saved copy"). Findings fixed: the entry hint used "/", which is
not in the font's safe glyph set and drew a music note ("Enter = next / OK" -> "next or OK"); the 180 dim let the title logo fight with the list rows (215); the Delete page had no hint line (added).
Not seen: anything after a successful connect (in-game), the Retry round trip, the relaunch-failure message, an actual `Use saved copy` run through to the title.
Known and unchanged: the title demo restarts its scene every so often; a slow text entry or a menu step can meet that wipe (the menu page survives, a text entry in progress is lost).

**Tests.** `test_play_online_real.py` gained phase S (a real client, membership = resident with a PID that is not in the fetched town, `AC_TOWN_NO_MSGBOX=1`): asserts the STALE log line, exit 3, no bind, no READY,
`membership.ini` unchanged: 18 / 18 with the existing G and R phases.

### M-F (guest -> resident promotion, reduced scope): implemented (uncommitted, branch `multiplayer`)

User documentation: `docs/multiplayer-guest-roadmap.md`, section "Guest -> resident promotion". Scope as the milestone table says: host `promote` + handoff + message 66 / reason 6, with the scripted test; the client
adopt is only the cheap part (`membership.ini` + `token.dat` rewrite for a STORE character, the REJECT text), the relaunch / re-fetch stays the manual Play Online path.

**Where the design disagreed with the code, and what was chosen.**

1. **The handoff entry carries the GUEST token, not the resident token.** `members.dat` rejects two entries with the same token, and the handoff has to prove the claimant is the promoted guest: its `token` is the
   guest's own token, `aux_pid` the guest's home PID, `pid` the new resident PID; the resident credential is the separate RESIDENT_TOKEN entry of the same PID (which is also what the message hands over).
2. **The guest check must run before the guest table lookup.** The guest entry is removed LAST, so after a completed promotion the key is unknown to `guests.dat`; the handoff is therefore matched on the members
   table (town, aux_pid = claimed key, presented guest token) inside `pcnetgame_host_guest_check` right after the key validity / conflict checks. It only reads `members.dat` when that file exists (a host that never
   promoted anybody, with `resident_tokens=off`, never touches it).
3. **House / slot come from the vanilla routines, with a safety check the vanilla function lacks.** `mHS_set_use` swaps `house_arrangement` and assumes it is a permutation; `pc_mp_promote_create` refuses to touch an
   arrangement that is not (the swap would corrupt it). `mHm_InitHomeInfo` only copies the ownerID, so a null-owner house (vanilla delete = `mHm_ClearHomeInfo`) already is a valid empty house. Vanilla's house selection
   (ac_intro_demo) also sets `loan = mPlayer_DEBT0`: the promoted resident gets that loan (a house without its debt would be a free house); the catalog bits of the intro are not set.
4. **Designs are copied from the guest, not rebuilt.** `mNW_InitOneMyOriginal` reads ARAM resources, which a dedicated host may not have; the guest record already holds valid designs.
5. **A guest's first record upload must be a fresh character** (the host refuses a first MIGRATE with pockets): a guest with goods only exists after play; the test seeds the stored record directly.
6. **Nook intro / first job:** both are per-slot saved event flags (`mEv_SAVED_FIRSTJOB_PLR0 + n`, `FIRSTINTRO`). A free slot normally has them off; `mEv_ClearPersonalEventFlag(slot)` (the call vanilla's delete makes) is run so
   the slot is certainly in the veteran state: no intro starts for the promoted resident. Verified by source, not by a real client.
7. Under `resident_tokens=off` nothing confirms a resident login, so the handoff entry stays (harmless: it keeps handing the old guest key over).

**Tests** (`pc/tools/net_spike/`): `test_promotion_protocol.py` (see the roadmap section) and `wire_baseline.py` (`EXPECTED_MAX_MSG_ID` 66, the 48-byte struct + offset pins, reject reasons 1..6) with the id-range literals of the source audits
re-pinned (`test_guest_g1/g3/g4/g5/g6_src`, `test_guest_src` (+ the reviewed `private_data` / `homes` / `s_guest_rec` function lists: promote + the handoff check), `test_identity_validation_src` (reason 6),
`test_house_sync_src`, `test_mail_src`, `test_mail2_src`, `test_ts_src`, `test_txn_x3_src`, `test_town_cache_unit`).

### M-G (sanitized town transfer + record-import guard, the former M-B2): implemented (uncommitted, branch `multiplayer`)

User documentation: `docs/multiplayer-guest-roadmap.md`, section "Sanitized town transfer and the record-import guard (M-G)" (what is kept / replaced, the residual leakage, the three layers of the guard).

- New pure module `pc/src/pc_town_sanitize.c` / `pc/include/pc_town_sanitize.h`: `pc_town_sanitize(in, len, out, tpl)`, `pc_town_gci_is_sanitized`, `pc_town_gci_file_is_sanitized`, `pc_town_gci_find_resident`, `pc_town_sanitize_layout` (the table as data, used by the unit test). The templates come from `pc_m_card.c` (`pc_save_build_sanitize_templates`, built at host world-ready and lazily on the first request); every offset of the table is mirrored by `_Static_assert`s there.
- Host (`pc_net_game.c`): `town_serve` is `0 off | 1 sanitized | 2 full`; the snapshot is sanitized into a new buffer, re-validated as a GCI of the same town, cached per (town_gen, input crc); TOWN_INFO `_rsv0` is now `flags` (bit0 SANITIZED; pinned in `wire_baseline.py`, HEAD's `_rsv0` text is the accepted previous layout). A warning is logged at world-ready when the town is served while `resident_tokens` is off (and for `full`).
- Client: the stream is rejected (BAD_GCI) when the marker in the bytes disagrees with the TOWN_INFO flag (also for UP_TO_DATE against the cache). `g_pc_save_sanitized` is set by the loader; a HOST / single-player process refuses a marked file (box, exit 3; the box is suppressed by `AC_TOWN_NO_MSGBOX`), and `pc_save_write_gci_to` never writes while it is set.
- D3 guard: RECORD_HELLO flag `0x02 NO_MIGRATE`; host "HOST WINS" at rev 0 for a resident (flag, `town_serve` sanitized, or a promotion entry in members.dat); the client never uploads a MIGRATE from a sanitized cache.
- Friendship snapshot: the saved villager letter is sent only to the peer whose bound PersonalID the memory belongs to.

**Deviations (the code disagreed with the brief, or a safer choice existed).**

1. The private-record keep range for the PersonalID block ends at 0x17 (exclusive), not 0x18: byte 0x17 is padding before `museum_record` (0x18) and comes from the template.
2. The land id of `Save_t.land_info` is at +0xA, not +8; the sanitizer reads it from there (BE) to stamp the mail block's land id. A town with land id 0 is refused (the loader could not detect the ARAM block order).
3. The cleared `Mail_c` template is taken from the BE image of `Private_c.mail[0]` (`mMl_clear_mail_box` output); there is no public byte-swap for a bare Mail_c and the bytes are identical.
4. The padding between the end of `Save_t` (0x242A0) and 0x26000 is zeroed in the main Save (and so in the backup) instead of kept: unknown content is not copied.
5. The attached present of a villager letter is two bytes (+2, +3), both zeroed; the letter text range is +5..+0xFD (pad included); the letter date and the memory flags stay.
6. The comment / banner / icon area including the 0x20 bytes after `MemcardHeader_c` is kept as is (only the marker is written).
7. The real-client test uses `--bootstrap-resident 1` (as the other town tests do) instead of a membership.ini role.
8. `town_serve = on` in an existing settings.ini now means sanitized; `full` is the old behaviour.

**Resolved open issue: the host slot-1 record digest changing once after the first sanitized session (FC629933 -> CD163618).** Cause: NOT blank / sanitized data. FULL adoption replaces every non-immutable range (client-owned AND host-owned), and the client's one upload right after the adoption changes exactly 2 bytes of the host record, both inside `Private_c.calendar` (0x234C..0x23B4): `played_days[month]` (today's day bit, record +0x2373) and `calendar.month` (+0x23B2). It is the vanilla daily `mCD_calendar_wellcome_on()` run by the client's `mEv_run` new-day path on the adopted host calendar (the fixture's host record was last played the previous month); the same record state results with `--town-serve full` (final digest CD163618 there too, carried by the MIGRATE import). No pockets / wallet / bank / mail / designs byte changes. Diagnostic added: the host logs `upload xfer N changes B byte(s) in R run(s) of the record: 0xOFF+LEN ...` for every accepted UPLOAD. `test_town_sanitize_real.py` now asserts every upload of the first sanitized session is confined to the calendar, no upload in session 2 / 3, the host GCI slot-1 record differs only inside the calendar (wallet intact), and the pushes of sessions 2 and 3 are identical.

**Not done / known gaps.** The client's own diary, letter storage and design storage are empty in a sanitized cache (they were never synced): a follow-up. MAIL_DELIVERED broadcasts and the resident-token-free claim of a leaked name are unchanged (see the residual-leakage paragraph in the roadmap).
