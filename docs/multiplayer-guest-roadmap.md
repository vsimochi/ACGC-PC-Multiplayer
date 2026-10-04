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

## G4 - capacity and identity separation under concurrency

Status: **Complete, with limitations** (every focused test green; the real-client tier runs 2 guests + 1 resident, the harness limit, higher counts are scripted). Commit hash: 8b9ebdd

What was implemented:

* **Host-local cap `max_guests`** (default **4**, valid **1..8**). Read from `settings.ini` (`max_guests = N` under `[Network]`, parsed in `pc_settings.c`, written by the defaults text and the
  settings writer; an out-of-range value is ignored and the default stays) and overridable per process with `--max-guests N` (1..8; a missing / 0 / 9 / non-numeric value exits 2 before any
  initialisation). The flag wins over the file; both are clipped to the guest table size (8). It is a HOST setting only, nothing is sent to clients (no wire change, no protocol bump).
* **Enforced at guest ADMISSION only** (`pcnetgame_host_guest_cap_refusal`, ONE call site in `pcnetgame_host_process_identity`, inside the guest-only `if (is_guest)` block): after the guest
  claim was authenticated (key / token / name rules) and after the duplicate-binding handling, BEFORE any token is minted / re-minted or a table slot is created. A NEW key and a KNOWN
  returning guest are treated alike: when the number of currently bound guests (READY peers bound as GUEST, excluding the asking peer) is >= `max_guests`, the peer is refused through the existing
  path `REJECT(SERVER_FULL)` with the host log line `guest limit reached (N of max_guests=M guests are connected)`. Nothing is minted, created or written. A returning guest whose OLD session is
  still bound is the existing duplicate case (parked; a silent old session is evicted after 2.5 s) and replaces ITS OWN binding without being counted twice. Residents never reach the gate (none
  of the cap helpers is referenced by a resident branch); a resident is admitted while the guest cap is full. Behaviour under the cap is unchanged.
* **Transport reserve** so guests can never lock a resident out: besides `max_guests`, a new guest is refused (same path, log `... held for residents ...`) when the occupied transport peers plus
  the number of resident records of the town that are not the host's own and not connected would exceed `PC_NET_MAX_PEERS`. With a playing host (3 possible resident clients) at most 5 guests fit
  even with `max_guests 8`; a dedicated / observer host (4 possible residents) fits at most 4. The transport itself is unchanged: a 9th peer is still dropped silently with no REJECT (the reserve
  keeps guests from filling the table).
* Dedicated console `status` shows `guests: bound=N max_guests=M` (`players` already lists the class / slot of every peer).
* No identity defect was found (see below), so no identity code changed.

Capacity facts (read from the code, then measured):

| limit | value | source |
|---|---|---|
| transport peers (all clients: residents + guests together) | **8** (`PC_NET_MAX_PEERS`; the host is not a peer; a 9th is dropped silently) | pc_net.h, pc_net.c |
| resident clients | up to 3 with a playing host, up to 4 with `--dedicated` / `--host-observer` | resident records 0..3 |
| guest record slots | 4..11 (`PLAYER_NUM + guest table slot`) | pc_net_game.c |
| guests.dat | 8 entries over ALL host towns, confirmed entries never evicted (table full -> `guest table is full`, SERVER_FULL) | pc_mp_guests.h |
| puppets | `PC_REMOTE_PLAYER_SLOT_COUNT` = 9, indexed by TRANSPORT PEER id (host = 8), never by player_no | pc_remote_player.c |
| per-peer host state | `s_host_peer`, link, record rx / tx buffers, dedup tables: all `[PC_NET_MAX_PEERS]`, indexed by peer id | pc_net_game.c |
| `player_no` 4 | every guest client has exactly one local foreigner with player_no 4; the host never uses the number (a guest's host-derived context player_no is `PLAYER_NUM`; the record slot comes from the binding) | pc_net_game.c |
| default `max_guests` | 4 (4 guests + up to 3 resident clients = 7 peers; 4 + 4 on a dedicated host = 8) | this milestone |

So the total is bounded by the 8 transport peers, not by the table: residents + guests <= 8 at once; the useful guest maximum is 8 minus the residents that may connect, i.e. 5 with a playing host
and 4 dedicated. The default of 4 keeps the puppet / appearance-texture memory and the 8-way message fan-out well inside what was tested.

Identity separation under concurrency (read + tested; no defect found):

* Binding is host-derived (`bound_class` / `bound_guest_slot` from the validated IDENTITY_EXT + token, never from a message); the record slot of a peer is derived ONLY from the binding
  (`pcnetgame_peer_rec_slot`). Two guests with the same shared `player_no` 4 therefore never share a record, puppet, friendship / mail sender key (the guest key) or transaction journal.
* Scripted tests: a guest key with ANOTHER guest's token, with no token, a new key carrying a connected guest's name, and a guest-flagged claim of a resident are all refused with every live
  session unaffected; a guest leaving destroys only ITS puppet slot; a restart of the host keeps every record separate.
* The money rock / shine-stone / gateway state of player_no 4 is process-local (each guest has its own process), not shared on the host.

Tests (disposable fixtures only, ONE build, protocol version unchanged):

| test | tier | result |
|---|---|---|
| test_guest_g4_src.py (new: settings parse / flag, cap placement, reserve, identity keys, wire, additive diff) | source audit | 27/27 |
| test_guest_g4_protocol.py (new: 4 concurrent guests; cap 4 / 2 / 1 via default / flag / settings.ini; 5 guests + 3 residents on `--max-guests 8`; cross-token; records after restart; table full; CLI) | scripted clients vs REAL host | 65/65 |
| test_guest_g4_real.py (new: 2 REAL guests + 1 REAL resident client, abrupt kill, restart, host restart) | REAL host + 3 REAL clients | 36/36 |
| test_guest_protocol.py / test_guest_persist.py | existing, scripted vs REAL host | 174/174, 51/51 |
| test_guest_g3_real.py | existing, REAL host + REAL client | 43/43 |
| test_guest_src / g1_src / g2_src / g3_src, test_dedicated_src, test_observer_src | source audits | 81/81, 55/55, 44/44, 35/35 (one pin updated), 87/87, 65/65 |

Pin updated: test_guest_g3_src "CLI path keeps exit(2)" asserted that `pc_main.c` has NO diff vs the G2 commit; G4 legitimately adds the `--max-guests` option, so the pin now reads exactly 11 added /
0 removed lines (everything else in that check is unchanged; test_guest_g4_src.py pins the same on the G3 commit).

What the scripted test asserts: four guests at once get slots 0..3 and four distinct tokens; the 5th is refused with the cap line and nothing is minted; a resident is admitted with the cap full;
cross-token / token-less / name-clash / resident-claim attempts are refused with all sessions untouched; one guest leaving removes only its puppet; the refused guest then joins (slot 4); a known
guest returning at a full cap is refused, but a returning guest replacing its own silent old binding is admitted; every guest is pushed ITS OWN record (byte-equal to its upload) at each reconnect
and after the host restarts; guests.dat full: the 9th new guest gets `guest table is full`, not the cap line; with `--max-guests 8` and 3 resident records unconnected the 6th guest is refused
(`held for residents`) and then all three residents are admitted (8 peers, 8 open puppet slots); with `--max-guests 2` / settings.ini `max_guests = 1` the 3rd / 2nd guest is refused naming the
cap, and a new key at a full table AND a full cap gets the cap line. The real test asserts: three real processes bound as guest slot 0, guest slot 1 and resident 1 on three different peers, each
client's guest_token.dat holds its OWN token (matching its guests.dat entry), three open host puppet slots, `private_data[2..3]` and `homes[1..3]` byte-identical to the baseline; after
TerminateProcess of guest A only A's peer / puppet slot disappears (B and the resident stay connected, their slots open); A restarts into the SAME slot as the SAME guest (KNOWN, token verified,
no MIGRATE, record byte-identical); after a host restart both guests reconnect to their own slot and byte-identical records.

Limitations:

* 8 simultaneous REAL clients were NOT run (the harness runs at most 3 client processes); 4 / 5 guests and 8 peers are scripted (FakeClient: no rendering, no puppet textures). The memory / frame cost
  of 8 real puppets is still unmeasured, which is why the default stays 4.
* A guest peer that is bound but silent keeps counting against the cap until the transport times it out or its own key returns (eviction at 2.5 s idle); a NEW guest meanwhile gets the cap refusal.
* A refused guest only sees the numeric REJECT (`reason=2`) and goes offline; the readable reason (guest limit vs table full vs token) is G6.
* The cap is per host process; guests.dat still holds 8 entries for all towns (no admin removal yet, G6).
* `settings.ini` gets a `[Network]` section the next time the in-game settings are saved (the loader ignores unknown keys, so older builds still read the file).

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
  letters, catalog ordering for guests, per-feature guard messages (G5), REJECT_INFO (G6), a soak test with 8 real clients (G4 follow-up).
