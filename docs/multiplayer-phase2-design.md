# Multiplayer phase 2 design: resident credentials, town cache, town transfer, promotion, admission

Status: **design**. Milestones M-A .. M-F are implemented in order. Implementers update this document with an "Implementation status" section as each milestone lands.

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
- Reconnect does not fetch again (the process is already booted). Switching servers is a new relaunch, so a new fetch.

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

- **Keep the relaunch.** The role audit forbids starting a role after startup.
- **Change** `pc_relaunch_build_args` to add `--town-fetch`.
- **Then, in `pc_main`, after the fetch:** resolve the character's membership for town K.
  - `resident` → set `pc_session()->join_kind = RESIDENT` and `resident_pid`. After the save loads, bind through the `--bootstrap-resident` path by PID instead of by index (the slot is found by `CmpPersonalID`).
  - `guest`, or none → the existing guest arrival. A new character goes through the existing Rover first-run creation.
- **Failures:** SDL message box with Retry / Play offline (use the cache) / Quit.
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
| **M-F** promotion | — | — | too large for one autonomous run (see below) |

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
- Host option `town_serve`: **default OFF**, `settings.ini [Network] town_serve = 0|1` or `--town-serve on|off` (host only, exit 2 otherwise). The design left the default open; OFF is the safer choice because the file contains every resident's pockets, mail, diary and designs, unsanitized. The privacy trade-off is documented in `docs/multiplayer-guest-roadmap.md`.
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

