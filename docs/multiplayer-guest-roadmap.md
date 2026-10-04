# Multiplayer guests: roadmap G2 .. G6

A guest is an extra player ("foreigner") who visits a host's town with a character of their own. A guest never occupies a resident slot: the host keeps its
record in `save/mp/guests.dat` (outside the town save), keyed by the guest's home PersonalID, protected by a host-issued token. G1 (fresh persistent guest
characters, hardening) is complete and committed. This file tracks the remaining milestones.

Legend: Complete / In progress / Not started. Commit hashes are filled in by whoever commits.

## G2 - guest profile, `--guest`, host-enforced empty economy

Status: **Complete** (all focused tests green, see results). Commit hash: 91453aa

What was implemented:

* **G2.1 host-enforced empty economy.** On a guest's FIRST upload (the "client wins once" MIGRATE into the host's blank record) the host runs
  `pc_mp_guest_record_fresh_check()` (pure, `pc/src/pc_mp_guests.c`, natively testable) over the canonical big-endian record image, called from exactly one place
  (`pcnetgame_rec_process_upload`, only for `kind == MIGRATE_UPLOAD && idx >= PLAYER_NUM`). Only identity, appearance and designs come from the client; economy and
  progress must equal what `pc_guest_build_fresh_record` produces: pockets empty and item conditions 0, wallet 0, bank 0, loan exactly 100, lotto 0/0, equipment empty,
  no gift on any letter, catalog orders empty, at most 3 catalog bits (the arrival init sets 3), no active quest, hint count / fortune / completion flags / aircheck /
  maps / tortimer / golden / e-Card progress zero, `state_flags == 1`. Accepted as the guest's own choice: gender 0|1, face 0..7, a starter shirt in
  ITM_CLOTH000..015 (idx == item - 0x2400), the Able Sisters designs, the date-dependent calendar / day-counter tail. A violation is REFUSED through the existing
  `RECORD_ACK INVALID_FIELD` path (detail low byte 13 = GUEST_NOT_FRESH, reason in the high byte; no wire change, no protocol bump), logged with the reason, nothing stored;
  the guests.dat entry stays unconfirmed with the blank rev-0 record, and the same connection may still send a legal record. Later uploads, returning guests and
  residents are not affected.
* **G2.2 guest profile module.** `pc/src/pc_guest_profile.c/.h` (pure C, in CMake `PC_SOURCES`): `save/mp/guest.ini`, flat `key = value`. Keys (all required):
  `name` (1..8 of `A-Za-z0-9 .'-`, not `SERVER`), `gender` (0|1), `face` (0..7), `home_town` (1..8 chars), `player_id`, `land_id` (1..0xFFFE, decimal or 0x-hex).
  Missing file: created with name `Guest`, home town `GuestVil`, two ids from the OS CSPRNG, gender / face derived from the identity hash, atomic tmp -> replace write.
  Existing file: parsed and validated; any problem is an error naming the file and the bad key, the file is never rewritten, regenerated or moved. The ids are permanent
  (same file = same guest). `name`, `home_town`, `player_id`, `land_id` are the guest KEY: changing them after joining creates a NEW guest; `gender` / `face` only seed a new guest.
* **G2.3 `--guest`.** CLIENT only: requires `--connect`; refused with exit 2 and a usage line together with `--host`, `--dedicated`, `--host-observer`,
  `--bootstrap-resident` or `--bootstrap-guest`. Loads / creates the profile during startup validation (a bad profile exits 2 before any initialisation) and then drives the
  same arrival path as `--bootstrap-guest` (the profile becomes the same spec string; `pc_bootstrap_guest_poll` is untouched). `--bootstrap-guest` stays as the test hook.

Tests (all on disposable fixtures, one build):

| test | tier | result |
|---|---|---|
| test_guest_g2_unit.py (predicate + profile module, python-mirror fresh records as oracle) | native unit | 116 checks, 0 failed (110 native harness checks + compile / wrapper checks) |
| test_guest_g2_src.py | source audit | 44/44 |
| test_guest_g2_cli.py (`--guest` argument validation, scratch cwd) | CLI, real exe, refusals only | 77/77 |
| test_guest_g2_protocol.py | scripted clients vs REAL host | 66/66 |
| test_guest_g2_real.py (create -> reconnect -> host restart -> same character; profile creation) | REAL host + REAL client | 34/34 |
| test_guest_g1_src / test_guest_src / test_guest_storage / test_guest_bootstrap_cli | existing, unchanged | 55/55, 81/81, 95/95 (75 native), 86/86 |
| test_guest_protocol / test_guest_persist | existing, scripted vs REAL host | 174/174, 51/51 |
| test_guest_real_client | existing, REAL host + REAL client | 43/43 |

## G3 - arrival without the gateway, title-menu entry, appearance rebuild

Status: **Complete, with limitations** (every focused test green, see results; the title-menu click and the cutscene were NOT exercised / seen). Commit hash: 90d4cb5

What was implemented:

* **G3.1 arrival init without the gateway.** `mSDI_StartDataInitGuest()` (`src/game/m_start_data_init.c`, `#ifdef TARGET_PC`, declared in its own TARGET_PC block of the header) is
  `mSDI_StartDataInit(.., MODE_PAK)` for a bound foreigner MINUS the three visitor-only side effects of `mSDI_StartInitPak`: `mEv_SetGateway` (persists the visitor gateway flag, which
  makes `mEv_LivePlayer(4)` false and freezes the local event schedule / music), `mNpc_SetReturnAnimal` (a no-op for a foreigner) and `mNpc_SendRegisteredGoodbyMail`. KEPT: the
  stale-flag clear `mEv_UnSetGateway()` (player_no 4), the binding guard, the valid-save guard and every player-neutral call of the PAK init in the same order
  (`mFM_SetBlockKindLoadCombi`, `mEv_init_force`, `mHsRm_GetHuusuiRoom(4)`, `mCkRh_DecideNowGokiFamilyCount(4)`, `mSP_ExchangeLineUp_InGame`, `mNpc_SetRemoveAnimalNo`, `mMkRm_MarkRoom`,
  `mRmTp_SetDefaultLightSwitchData(2)`) and then `mSDI_StartInitAfter(game, FALSE, mSDI_MALLOC_FLAG_ZELDA)`. It runs no new-town / new-player init, allocates no house and never
  touches `private_data[]` / `homes[]`. (Unlike the host observer's init it does not set `scene_from_title_demo` / the RTC, matching the PAK path for player_no >= 4.) Single player
  and residents never reach it; every pre-existing `mSDI_*` function body is byte-identical to the G2 commit. The arrival logs `guest init ran WITHOUT the gateway ... gateway flag
  after init = 0 ..., player_no = 4`.
* **G3.2 real entry.** The arrival is ONE function, `pc_guest_arrive()` (`pc/src/pc_m_card.c`), shared by `--bootstrap-guest` / `--guest` (`pc_bootstrap_guest_poll`, which keeps its
  one-shot / readiness waits and `exit(2)` on any failure) and the new title-menu item. Order: client role, a ready title scene (play_main, no wipe, a player actor), **`pc_save_reload()`**
  (the very call of the vanilla Start path, `aAL_title_game_data_init_start_select`; the title demo mutates the save in RAM), valid-save check, spec check, resident-name check, fresh
  record, bind (the previous `now_private` / `player_no` / rtc flag are restored if the init or the scene change fails), `mSDI_StartDataInitGuest`, station `SCENE_FG` + RIDE_OFF_DEMO.
  The PC title menu (`PC_ENHANCEMENTS`) shows a 4th item "Join as Guest (NAME)" after Start Game for any `--connect` client (Start Game, Join as Guest, Options, Quit Game; a
  non-client menu is unchanged); it needs the same three readiness conditions as Start Game and calls `pc_guest_title_join()`: it re-reads `save/mp/guest.ini` (a missing file is created
  as for `--guest`, a broken one is never touched), then runs the arrival. ANY failure there sets a word-wrapped message drawn under the menu for 8 s (existing `pc_menu_draw_centered`)
  and the title keeps running: no `exit`. `--guest` / `--bootstrap-guest` still arrive without a click on the first title-demo frame, now after the save reload.
* **G3.3 look refresh after adopt (guests only; residents untouched).** The local avatar's skeleton (boy / girl object bank) and face textures are bound at scene load from
  `Now_Private->gender / face` (`m_scene.c` `Object_Exchange_keep_new_Player`). When the adopted record of a returning guest differs from the placeholder built from `guest.ini`:
  face only -> `mPlib_change_player_face()` rebuilds the face texture + palette in place (the call `Player_actor_ct` makes; same-size banks), no scene change; gender ->
  ONE same-position scene reload (`goto_other_scene` to the current `SCENE_FG` at the player's position, demo profiles cleared so the train arrival is not replayed), performed once the
  player is idle in the town (no fade / wipe, `mPlib_able_submenu_type1`, submenu idle) after the record is SYNCED; if the player is elsewhere the next scene load rebuilds the banks.
  Log lines `[NET][LOOK] ...`. Purely additive in `pc_net_game.c`; no wire change, no protocol bump.
* **G3.4 stale actor / reconnect** (test only, no host code change): see `test_guest_g3_real.py`.

Tests (disposable fixtures only, ONE build, protocol version unchanged):

| test | tier | result |
|---|---|---|
| test_guest_g3_src.py (new) | source audit | 35/35 |
| test_guest_g3_real.py (new: arrival, abrupt kill + restart, gender look reload, face look rebuild, host regions) | REAL host + REAL client | 43/43 |
| test_guest_g2_real.py | REAL host + REAL client | 34/34 |
| test_guest_real_client.py | REAL host + REAL client | 43/43 |
| test_guest_g2_src.py / test_guest_g1_src.py / test_guest_src.py | source audit (pins updated, see below) | 44/44, 55/55, 81/81 |
| test_observer_src.py (touched file m_start_data_init.c, run once as a regression check) | source audit | 65/65 |
| test_guest_bootstrap_cli.py / test_guest_g2_cli.py | CLI, real exe, refusals only | 86/86, 77/77 (the first g2_cli run printed 76/77: a stale `save/mp/guest.ini` left in the fixture's save dir; removed, rerun 77/77) |

`test_guest_g3_real.py` asserts (state / binding / network only): the arrival log order (save re-read -> FRESH record -> guest init -> station), gateway flag 0 and player_no 4,
host `GUEST slot 0` + MIGRATE applied as resident 4; the three residents the host does not play (private_data[1..3]) and all four houses homes[1..3] byte-identical to the baseline
in the host's GCI after the session and after each disconnect (the host's own resident 0 / house 0 only show the envelope a host-only control run shows by itself: 1 byte / 214
bytes after the host's ~60 s re-save); after `TerminateProcess` of the client the host logs the transport disconnect and the destruction of the remote-player slot (open-slot balance
0); a restarted client is the SAME guest (KNOWN, slot 0, token verified, no MIGRATE, one guests.dat entry, identical record); an edited gender in `guest.ini` -> look change logged and
exactly one same-position reload accepted (`goto_other_scene res=1`) with the session staying connected; an edited face -> rebuilt in place, no reload.
Pins updated (G3 moved the arrival into `pc_guest_arrive`): test_guest_g1_src (A / H checks now read the poll + `pc_guest_arrive`: spec / resident-name / build / bind order, 9 failure
paths = `return 0`, ONE `exit(2)` in the poll, the init is `mSDI_StartDataInitGuest` not `mSDI_StartDataInit(MODE_PAK)`, the log literal takes a `%s` tag, `mEv_CheckGateway(` allowed as the
one read-only probe, the `m_start_data_init.c` no-diff pin is replaced by the strict additive pin in test_guest_g3_src), test_guest_src (G2 / G1.1 poll checks), test_guest_g2_src
(the "no G3 work" / "poll not edited" pins now say: the profile module is referenced only by the title functions, never by the poll / arrival).

Limitations:

* The title-menu item and its failure message were NOT exercised by any process test (no UI automation); they are covered by source audits only. The `--guest` path exercises the same
  `pc_guest_arrive`, but fires on the first title-demo frame, not from the menu.
* The arrival cutscene (RIDE_OFF_DEMO, station master) and the reload / face rebuild were NOT visually verified: the tests prove state, bindings and network behaviour only.
* The gender look fix is one extra fade-out scene load of the town after the first adopt (only when the gender differs).
* The host's own resident / house bytes in the GCI change by themselves on its periodic save; the test therefore pins slots 1..3 strictly and slot 0 by a control envelope.

## G4 - capacity

Status: Not started. Planned: host setting `max_guests` (default 4, clamp 0..8), clean refusal ("guest limit reached"), reply instead of a silent drop of a 9th transport peer,
soak test with several real guests plus residents.

## G5 - gameplay audit and guards

Status: Not started. Planned: `player_no == 4` safety patches (birthday read in `update_schedule_today`, spirit read in the pull net include, `now_home` NULL check in the tag
overlay), client-side early refusals with vanilla-style messages for museum / mail send / mailbox / house, real-client pickup / drop / shop round trips.

## G6 - join-failure reasons and token recovery

Status: Not started. Planned: an additive REJECT_INFO message with a reason code and short text (no protocol bump), shown on the title menu and in the pause menu; host console
commands `guests`, `guest-reset`, `guest-remove` for token recovery; corrupt `guests.dat` / `guest.ini` handling messages; host shutdown with a dirty guest.

## Known limitations

* A guest needs a manually copied copy of the HOST's town save (`save/card_a/DobutsunomoriP_MURA.gci`): there is no town transfer, the town identity (land name, id, terrain
  hash) must match the host's or the join is refused (LAND_MISMATCH / no matching save). A freshly generated town is not acceptable.
* The empty-economy rule only constrains a NEW guest's first upload. Afterwards the client-owned ranges (pockets, wallet, bank, ...) are uploaded after a legality check only,
  exactly as for residents; a modified client can still edit its own inventory by later uploads (pre-existing trust model).
* A guest's `player_id` / `land_id` / `name` / `home_town` cannot be changed without becoming a different guest; guests.dat holds 8 entries for ALL towns, confirmed entries are
  never evicted, and there are no admin commands yet (G6).
* The guest token is a bearer secret over unencrypted UDP (fine for LAN / friends).
* `--guest` / `--bootstrap-guest` still fire on the first title-demo frame (now after a save reload and without the gateway flag); the RIDE_OFF_DEMO / station-master arrival and the
  title-menu "Join as Guest" click are not visually verified / not exercised by a process test (G3).
* The guest profile name charset is ASCII on purpose (`A-Za-z0-9 .'-`); no in-game name editor yet.
* The home land of a guest may by chance equal the host town's land id (probability 1 / 65534); the host refuses it and the guest then edits `land_id` in guest.ini (no automatic re-draw yet).

## Deferred

* Town transfer from the host (so a guest needs no local copy), in-game profile creation UI (vanilla name editor), changing the look of an existing guest, villager -> guest
  letters, catalog ordering for guests, per-feature guard messages (G5), the `max_guests` setting (G4), REJECT_INFO (G6).
