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

Status: Not started. Planned: a foreigner init without `mEv_SetGateway` (generalise `mSDI_StartDataInitObserver`), a "Join as Guest" title-menu item for the CLIENT role that runs
the same arrival after `pc_save_reload()` (failures return to the title with a message instead of exiting), a same-position scene reload after the first adopt when
gender / face differ from the placeholder, real-client reconnect tests (kill and reconnect within and after the 2.5 s eviction window).

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
* Arrival still runs `mSDI_StartDataInit(MODE_PAK)` (gateway flag set) and fires in the title demo; the RIDE_OFF_DEMO / station-master arrival is not visually verified.
* The guest profile name charset is ASCII on purpose (`A-Za-z0-9 .'-`); no in-game name editor yet.
* The home land of a guest may by chance equal the host town's land id (probability 1 / 65534); the host refuses it and the guest then edits `land_id` in guest.ini (no automatic re-draw yet).

## Deferred

* Town transfer from the host (so a guest needs no local copy), in-game profile creation UI (vanilla name editor), changing the look of an existing guest, villager -> guest
  letters, catalog ordering for guests, per-feature guard messages (G5), the `max_guests` setting (G4), REJECT_INFO (G6).
