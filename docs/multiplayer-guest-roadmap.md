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

Status: **Complete, with limitations** (source fixes + local refusals + scripted protocol coverage; NO real-client gameplay run and NO visual verification, see Limitations). Commit hash: 8ad36ca

### G5.0 - the three player_no 4 out-of-range accesses (verified in the code, fixed minimally, TARGET_PC only, foreigner / NULL guarded)

| # | where | defect (a guest is the foreigner, `player_no == mPr_FOREIGNER == 4`, `private_data[]` has 4 entries) | fix |
|---|---|---|---|
| a | `src/game/m_event.c` `update_schedule_today` | `priv = &Save_Get(private_data[Common_Get(player_no)])` reads the field behind the array and feeds `priv->birthday` into `event_save_common.dates[BIRTHDAY]` (a bogus birthday event could be scheduled) | after the vanilla init: `if (player_no == mPr_FOREIGNER && Now_Private != NULL) priv = Now_Private;` (the guest's own record) |
| b | `src/game/m_player_main_pull_net.c_inc` `Player_actor_Pull_net_demo_ct` (spirit / wisp catch) | `mPr_GetPossessionItemIdxKindWithCond(Save_GetPointer(private_data[player_no]), SPIRIT0..4)` reads out of range, then `Now_Private->inventory.pockets[item_idx]` indexes with that bogus result | TARGET_PC branch reads `Now_Private` for the foreigner, the unchanged vanilla expression otherwise (`#else` keeps the original lines verbatim) |
| c | `src/game/m_tag_ovl.c` `mTG_client_mail_is_mailbox` (discarding a letter in the inventory) | pointer arithmetic on `Common_Get(now_home)->mailbox` with `now_home == NULL` for the foreigner (m_home.c) | `Common_Get(now_home) != NULL` before the first dereference |

Evidence: test_guest_g5_src.py pins each guard and proves that the working file minus the pinned additions (m_tag_ovl.c: the pinned return undone) is BYTE-IDENTICAL to the baseline blob (e68c90c) for m_event.c, the pull-net include and m_tag_ovl.c, i.e. nothing else in the vanilla bodies changed; resident / single-player behaviour is unchanged (the guards fire only for player_no == 4 or a NULL home).

Bounded grep of the other `private_data[player_no]` / `Common_Get(player_no)` indexed uses (not exhaustive, normal-visit paths only): already guarded by vanilla foreigner checks or by `mLd_PlayerManKindCheck` / `player_no < PLAYER_NUM` (m_calendar.c, m_calendar_ovl.c 24 / 92, m_private.c 70 / 1305 / 1320, m_shop.c, m_start_data_init.c, house doors), or not reachable for a guest (ac_haniwa, ac_douzou, ac_sign: the index comes from a saved sign item `& 3`, the post office loops residents 0..3, the curator's `donator - 1` comes from the museum record). Not changed and recorded as deferred: `m_calendar_ovl.c:404` (`mCD_make_calendar_data_month` birthday read, only reachable through a calendar furniture inside a house, which a guest cannot enter), `ac_shop_level.c:66` (host-side leaflet, loop over resident houses), `m_home.c:350` / `m_needlework.c` / `m_room_type.c` / `ac_my_house_draw.c_inc` (house / design code of resident houses). No out-of-range WRITE reachable with player_no 4 was found.

### G5.1 - refusals (nothing is lost or duplicated; the client never mutates pockets without a host APPLIED)

* **Museum donation (client guard, new):** `pc_net_game_ts_begin_museum_donate` returns 0 for a CLIENT guest before anything is queued or sent (log `MUSEUM_DONATE refused locally`). The curator's existing code maps 0 onto its give-back row (row 2: the item stays with the player). Before G5 the same outcome came only from the host (`NO_DONOR_SLOT`) after a round trip. The host refusal stays (authoritative, not journaled, nothing mutated).
* **Mail to a player (client guard, new):** `pc_net_game_mail_begin_send` returns 0 for a CLIENT guest (log `MAIL_SEND refused locally`); the post girl's existing row 1 ("no such address") hands the letter back; the slot is never touched. Letters to villagers use the MAIL_REQUEST path and are unchanged. Host: `NO_DONOR_SLOT` stays.
* **Mailbox take / read, house entry:** unchanged and already safe: a guest owns no house (`pcnetgame_mbox_house_of` walks `homes[]` only), so `pc_net_game_client_mailbox_usable()` is false and the mailbox stays closed; the vanilla foreigner branch makes the doors knock only.
* **Catalog ordering:** already refused for EVERY network client (guests included) by the earlier town-services batch: `aNSC_msg_win_open_wait2` forces Nook's ORDER_UNAVAILABLE row before any bell is taken, and `aNSC_order_check` defends with ORDER_CANCEL before an order slot is written. No change was needed; there is no host path that could deliver a guest's order (the SHARED `catalog_orders` range is only legality-checked, the post office walks `private_data[]`).
* **Villager letters to a guest:** none arrive (remail is resident-only, the client skips mailbox letters). Documented as deferred.
* The visible message is the game's own dialogue row of each flow (the refusals reuse existing rows; no new on-screen notice was added because both flows already have a refusal row; `pc_net_notice` is only the link-loss notice).

### G5 activity table

| activity | guest status | evidence tier |
|---|---|---|
| movement / puppet / appearance | supported | source audit (G1..G4), real (G3 arrival) |
| pickup, drop, bury, DIG_BURIED, shop buy / sell, police claim | supported | scripted (test_guest_protocol section X, pre-existing) |
| DIG_HOLE / DIG_SHINE bonus grants | supported (guest record only) | scripted (test_guest_g5_protocol M1 / M2) |
| shop buy replay (charged once), record mirror == client | supported | scripted (g5 M3, D) |
| weed pull / flower trample | supported (field action, no record change) | scripted (g5 M4) |
| fish / bug catch | supported (guest record only, one winner per entity) | scripted (g5 M5); the spirit-catch read is fixed (G5.0 b) by source audit |
| disconnect between COMMIT and RESULT (kill / lost result), reconnect with the token | supported: journal replay, no duplicate item / bell, host record == client | scripted (g5 K, D) |
| villager talk / friendship / letters TO villagers | supported (keyed by the guest key) | scripted (test_guest_protocol X, pre-existing) |
| birthday scheduling, spirit catch, discarding an inventory letter | fixed (no out-of-range read / NULL math) | source audit (g5 src A) |
| museum donation | refused (local give-back + host NO_DONOR_SLOT) | source audit (g5 src B) + scripted host refusal (pre-existing) |
| mail to a player | refused (local "no such address" row + host NO_DONOR_SLOT) | source audit + scripted host refusal (pre-existing) |
| mailbox take / read, house / room entry | refused (no house) | source audit (g5 src C) |
| catalog ordering | refused (Nook's ORDER_UNAVAILABLE, all clients) | source audit (g5 src C) |
| villager -> guest letters, museum / mail for guests, bank / loan UI, birthday calendar | deferred | -- |

Exact results (disposable fixtures only, ONE build, protocol version unchanged, no wire change):

| test | tier | result |
|---|---|---|
| test_guest_g5_src.py (new) | source audit | 19/19 |
| test_guest_g5_protocol.py (new: phases M / K / D) | scripted clients vs REAL host | 81/81 |
| test_guest_g4_protocol.py | scripted vs REAL host (re-run on the new exe) | 65/65 |
| test_guest_g3_real.py | REAL host + REAL client (re-run: the client code of pc_net_game.c changed) | 43/43 |
| test_guest_src / g1_src / g2_src / g3_src / g4_src, test_dedicated_src, test_observer_src | source audits | 81/81, 55/55, 44/44, 35/35, 27/27, 87/87, 65/65 |
| test_mail_src, test_mail2_src, test_ts_src, test_stage0_town_safe_degrade | source audits touching the edited functions | 74/74, 77/77, 70/70, 102/102 |

Pins updated: test_mail2_src "O4" pinned the exact old return line of `mTG_client_mail_is_mailbox`; it now pins the new two-line return (the old expression plus the NULL guard, nothing else). test_mail_src "C" forbids the word `mailbox` inside `pc_net_game_mail_begin_send`: the new comment was reworded instead of loosening the test. The wire / protocol is unchanged (message ids 1..58, wire_baseline 18/18).

What test_guest_g5_protocol.py proves (kinds already covered before G5 are NOT repeated, see its header): guest A's DIG_HOLE / DIG_SHINE grants change only A's record (host log `resident 4+slot`, none to any resident 0..3), rev + 1, one FIELD_UPDATE at the observers, guest B and the resident control receive no push or result; byte-identical resends replay APPLIED/REPLAYED (journalled kind / item, the host's current mirror, no second execution); SHOP_BUY resends charge once; weed pull / flower trample are accepted without a TXN_RESULT and without a record change; fish / bug catches apply to the guest only, a resend replays, two guests racing for one entity give one APPLIED and one REJECTED whose record is untouched; a new session of the guest is pushed exactly the record the client applied and guests.dat holds the same inventory; guest B's record stays byte-identical to its first contact. K: `kill_peer_after_commit` between COMMIT and RESULT of a guest pickup: the same-nonce reconnect with the token is the same slot, KNOWN, and is pushed the record that already has the item; the old COMMIT replays (one item, one world change); a new process with the token gets the same record; guests.dat equals it. D: `drop_result` twice on a guest SHOP_BUY: the third resend replays the same post-image, one execution, the wallet is charged once, and a new session's record equals the client's. The fixtures end without a `save/mp` directory.

Limitations:

* **No real-client gameplay evidence beyond arrival.** The existing real-test hooks (`--force-*`, `--txn-test-dig-grant`, ...) act on the host's own resident or need a resident client; there is no UI automation, so a real guest process has NOT been driven through pickup / drop / shop / catch / the refusals. The real tier for guests is still only G3 / G4 (arrival, adoption, reconnect). The new client refusals and the three G5.0 guards are audited at source level only and were not exercised by a running guest, and nothing was visually verified.
* The scripted pre-image of a transaction is the client's own claim (existing trust model): the scripted shop test sets the wallet it spends in the pre-image, exactly like the pre-existing guest test.
* A replay returns the host's CURRENT mirror with the journalled kind / item (later grants included); this is the existing resident behaviour, now confirmed for guest slots.
* The catch race, spawn counts and the wildlife section depend on the RNG spawn burst (the test fails loudly when too few entities spawn).
* A comment-only rewording in `pc_net_game_mail_begin_send` (to satisfy the test_mail_src pin) was made after the single build; the compiled code is identical to the tested exe.

## G6 - reliability and release readiness for guests

Status: **Complete, with limitations** (every focused test green, see results; the on-screen message drawing and the arrival cutscene were NOT visually verified). Commit hash: 91de602

Scope decision: NO wire change and NO protocol bump. The design's additive `REJECT_INFO` message (id 59) was deliberately NOT implemented: every readable message is produced from the EXISTING
frozen reject reasons (1 PROTOCOL_MISMATCH, 2 SERVER_FULL, 3 LAND_MISMATCH, 4 NO_SAVE) and from state the client already has; all admin tools are host-local. Message ids stay 1..58, wire_baseline 18/18,
`pc_net_game.h` gained one function declaration only. Resident and single-player behaviour is unchanged (the touched source files are `pc_net_game.c`, `pc_net_game.h`, `pc_pause_menu.c`, `pc_m_card.c`,
`pc_dedicated.c/.h`, `pc_mp_guests.c/.h`; every new branch fires only for a client refusal / the dedicated console / the guest arrival).

### G6.0 - test debris (`save/mp/guest.ini` left in the disposable fixture)

Mechanism (verified): a real-guest test writes `save/mp/guest.ini` BEFORE its first game launch; `net_spike_lib` snapshots the fixture's whole save dir at that first launch (`_auto_fixture_guard`)
and restores it at process exit, so after the test's own (correct) restore the at-exit restore put the pre-launch `guest.ini` back. Fix: `net_spike_lib.discard_fixture_guest_mp()` removes `save/mp` from the
fixture dir AND from the pending auto-snapshot; it acts only on `bin_fixture4*` dirs and the disposable clone and refuses the live / protected dirs (functional check in `test_guest_g6_src.py`). Called after the
restore by `test_guest_g2_real`, `test_guest_g3_real`, `test_guest_g4_real`, `test_guest_real_client` and the two new G6 tests. Verified after every run: no `save\mp` exists in any build64 dir.

### G6.1 - readable join-failure messages

Every refusal / failure of a CLIENT join now produces one sentence that is (a) printed to the log AND to stderr (`[NET][JOIN] CANNOT JOIN: ...`, the numeric reason stays in the older log line),
(b) kept in a static buffer that survives `pc_net_game_shutdown()` (the client plays on alone after a refusal), and (c) drawn on screen for 30 s by `pc_net_notice_draw()` (`pc_pause_menu.c`), which `graph_main`
calls every frame, so the same overlay works on the title screen and in game (it is the pre-existing "Not connected to the host" notice path; the join message is drawn first, word wrapped, header "Cannot join the
host:"). A new connection attempt clears the message.

| situation | message (abridged) |
|---|---|
| REJECT 1 PROTOCOL_MISMATCH | the host runs another network version (host N, this game M), use the same build |
| REJECT 2 SERVER_FULL, guest | **server full / guest limit reached / all resident slots taken**; the host gives the same answer for: guest table full, guest / name already in use, guest token missing or wrong; plus "lost save/mp/guest_token.dat? ask the host operator to run guest-reset-token" (no token held) or "the host may have reset its guest table" (token presented) |
| REJECT 2 SERVER_FULL, resident | server full / all resident slots taken, or your character is already connected or is the host's own |
| REJECT 3 LAND_MISMATCH | your town (X) is not the host's town (Y): copy the host's town save (DobutsunomoriP_MURA.gci) into save/card_a and restart |
| REJECT 4 NO_SAVE | the host (town Y) has no resident matching your character: play one of its residents or join as a guest |
| unknown reason | refused with reason N unknown to this version: update the game |
| IDENTITY_ACK with another protocol / town | both versions, or both towns, named |
| a different guest token than the one held (old behaviour: silent self shutdown) | "the host gave you a DIFFERENT guest token ... not the host that issued it ... delete guest_token.dat and ask the operator to run guest-reset-token / guest-remove" |
| guest token could not be saved | the host will refuse you next time until the operator resets you |
| transport closed during CONNECTING / HANDSHAKE (older host, stopped host) | the host at ip:port closed the connection before you could join |
| host unreachable (no transport answer for 8 s) | non-final warning "No answer from the host at ip:port after 8 s (host not running, wrong address or port, firewall). Still trying."; withdrawn when the transport answers (the client retries forever, as before) |

Not changed: the code cannot tell the SERVER_FULL variants apart (one frozen reason), so the text lists them. The guest-only title-menu message of G3 and the in-game "Not connected" notice are unchanged.

### G6.2 - host operator tools (dedicated console)

New commands (`help` lists them): `guests` (slot, name, home town, host town, confirmed, rev, bound, RECOVERY-ARMED; never a token, not even a prefix), `guest-remove <slot|name> confirm`,
`guest-reset-token <slot|name> confirm`. The selector is a single digit 0..7 (slot) or a unique case-insensitive guest name (an ambiguous name must be given as a slot). Order of the checks (pinned by the source
audit): host with a ready world -> guests.dat not UNTRUSTED -> selector -> **refused while the guest is bound** -> without `confirm` only the effect is described and nothing changes -> the backup
`guests.dat.bak-<timestamp>` (byte copy, never overwrites; the command is REFUSED if it cannot be made) -> the change. A removal is rolled back if guests.dat cannot be written. Each action is logged
(`[NET][GUEST] ADMIN: ...`).

Semantics decided: `guest-remove` deletes the entry and its stored character (the backup keeps it); the guest can later join as a NEW guest. `guest-reset-token` keeps the character: it ARMS a host-local, in-memory
recovery (10 minutes, ONE use, lost on a host restart) for that key. The next claim of the key that does not carry the stored token is answered with one re-minted token for the SAME slot and the SAME record (the
real guest is pushed its own record byte for byte); the entry becomes unconfirmed until the new token is presented once; the old token is dead. The recovery passes every normal admission gate (key / validity /
UNTRUSTED checks, the bound-peer duplicate handling, the guest cap, the per-address mint limit) and skips only the new-identity name rules. Safety trade-off, stated plainly: during the armed window ANYONE who
claims that exact home PersonalID (public information) receives the character, so arm it only when the guest is about to connect; nothing is stored on disk (no flag, no wire change), so a crash cancels it.
If the guest still holds a stale token in `guest_token.dat` it refuses the new one ("DIFFERENT guest token", see G6.1): delete that entry / file first.

### G6.3 - corrupt / untrusted storage and shutdown safety (checked, no defect found; one client fix)

* (a) corrupt `guests.dat`: empty file, truncated file and FUTURE version (new scripted test, each over a real dedicated host with the file as the ONLY generation) and the earlier bit-flip / every-generation cases
  (`test_guest_persist`, `test_guest_storage` native: truncated / magic / old / future / crc / field): the host does not crash, logs UNTRUSTED mode, refuses a known and a new guest with the existing
  SERVER_FULL reason, admits a resident, never writes a guests.dat, preserves the bad file byte for byte as `guests.dat.corrupt-<timestamp>`, `guests` shows nothing and `guest-remove` is refused.
* (b) `records.dat` for a guest slot: not applicable by design: guest records live inside guests.dat (the lineage of a guest slot is created at admission / loaded from guests.dat, never from records.dat); a corrupt
  records.dat affects residents only (existing D3 coverage).
* (c) shutdown: `guests.dat` is written only after a successful town GCI write (the hook in `pc_net_game_record_after_gci_save`, unchanged; pinned). `stop` with a guest session in progress exits 0, leaves a valid,
  format-size guests.dat holding the same token and the byte-identical record, writes it at most once during the shutdown (0 times when nothing changed), and after a restart the guest reconnects (KNOWN) and is pushed the
  byte-identical record. The earlier persistence test additionally proves the write happens AFTER the GCI save line for a dirty guest.
* (d) home land == host town (1 in 65534): the CLIENT now refuses to arrive before anything is bound (`pc_guest_arrive`), with a message naming guest.ini (`land_id`) and the consequence (a different land_id or
  home_town is a NEW guest). `--guest` exits 2 with the message; the title-menu item shows it on the title. The host's own check stays.

### Tests (disposable fixtures only, ONE build, protocol version unchanged)

| test | tier | result |
|---|---|---|
| test_guest_g6_src.py (new) | source audit (+ functional check of the python cleanup helper) | 50/50 |
| test_guest_g6_protocol.py (new: P1 operator tools incl. token loss / recovery / removal, P2-P3 stop with a guest connected + restart, P4-P6 empty / truncated / future guests.dat) | scripted clients + the stdin console vs REAL dedicated hosts | 66/66 |
| test_guest_g6_real.py (new: refused real client with the readable message, not hung; home land == town refused on the client) | REAL host + REAL clients | 14/14 |
| test_guest_g3_real.py / test_guest_g4_real.py / test_guest_g2_real.py / test_guest_real_client.py (cleanup fix + new client code; the G6.0 check: no `save\mp` left afterwards) | REAL host + REAL client(s) | 43/43, 36/36, 34/34, 43/43 |
| test_guest_protocol.py / test_guest_persist.py (the re-mint code touched the guest table) | scripted vs REAL host | 174/174, 51/51 |
| test_dedicated_runtime.py / test_dedicated_cli.py (the console gained commands) | REAL dedicated host / argv | 64/64, 34/34 |
| test_guest_storage.py (native, one pin updated) / test_guest_g2_unit.py | native unit | 95 checks 0 failed, 116 checks 0 failed |
| test_guest_src / g1_src / g2_src / g3_src / g4_src / g5_src, test_dedicated_src, test_observer_src, test_hostcfg_src, test_txn_src, wire_baseline | source audits | 81/81, 55/55, 44/44, 35/35, 27/27, 19/19, 87/87, 65/65, 21/21, 111/111, 18/18 |

Pins updated (each explained, nothing weakened): test_guest_src (s_guest_rec[] / guests.dat write sites now include the operator removal function), test_guest_g1_src (pc_guest_arrive has 10 failure paths instead of 9),
test_guest_g4_src and test_dedicated_src (pc_net_game.h is the HEAD header plus the ONE pinned declaration instead of byte-identical), test_guest_g5_src (its "vs baseline" diffs now measure baseline..G5 commit,
so a later milestone cannot break them), test_hostcfg_src (pc_net_notice_draw's guard is split in two and draws the join message first), test_guest_storage (the backup function removes only its own partial copy),
test_txn_src (the journal clear has one more guest-table call site). Pre-existing, NOT touched by G6 and failing before it: test_d3_record_src "P hook ... pc_save_write_gci" (the dedicated milestone wrapped
`pc_save_write_authoritative`), test_identity_validation_src S2 / S9 / S11 (stale since the G4 cap refusal / observer blocks).

Limitations (G6):

* The on-screen drawing of the join message and of the title message is NOT visually verified (only the log / stderr output and process state of real clients are); the message is word wrapped to 44 characters x 8 rows.
* A refused guest cannot be told WHY beyond the frozen reasons: the text lists the possible causes (SERVER_FULL covers cap, table full, in use, token missing / wrong).
* The recovery of a lost token is operator-mediated, in memory only (10 minutes, one use, cancelled by a host restart) and trusts whoever claims the key first during the window.
* Windows `stop` / Ctrl+C were verified through the console `stop` (graceful path); a hard kill keeps the last durable guests.dat (<= 60 s old, or the early save on a dirty disconnect).

## Guest arrival: train / sync / world edge

Phase T (design: investigation of branch `multiplayer` at 018344c). Commit: b67775d. Protocol version unchanged (8), no wire message added.

| Step | What | Status |
|---|---|---|
| T1 | `[TRAIN]` diagnostics: `src/game/m_train_control.c` logs (PC_LOG GENERAL, i.e. `-debug`) every CHANGE of action / control / last_control / coming_flag / signal / title demo with `start_timer` vs `now` (RTC seconds), plus the title-demo-1 edge | done, no behaviour change |
| T2 | Title-demo train leak fix: on the edge "title demo 1 seen -> anything else" `mTRC_move` calls `mTRC_init` once, keeping `train_coming_flag == 3` (ride-off arrival); the pre-game player-select / train draw types are skipped (vanilla Start flow untouched) | done, verified by real host + client logs |
| T5 | Borderless edge clamp: in `Player_actor_BGcheck_common_type2` the vanilla `mCoBG_UniqueWallCheck` clamp runs only when the new position is in an INVALID acre (`mFI_BlockCheck`, floor-divided so negative coordinates are invalid); the borderless acre-change wade is refused into an invalid acre | done, native + source audit only |
| T3 | Puppet rows (`pc/src/pc_remote_player.c`): main index DEMO_STANDING_TRAIN is a table row `standing_train` with the existing `PC_ROWF_HIDE_BODY` flag (puppet hidden while the guest rides the train; released by the HIDE mechanism's clears: teleport snap, silent peer, scene change, any other state). DEMO_WALK is drawn as WALK1 (walk tempo) through the pure `pcarr_demo_walk_plays_walk`, so a demo-walking guest no longer slides in idle. DEMO_GETOFF_TRAIN deliberately stays a fallback (see below) | done; native + source audit + the real puppet row-table test; look NOT verified |
| T4 | Local arrival train on the other processes: when a puppet ENTERS (or is first seen in) DEMO_STANDING_TRAIN in the town, `mTRC_pc_remote_arrival` (TARGET_PC, `src/game/m_train_control.c`) sets `train_coming_flag = 3` exactly like the arriving client's ride-off demo (`aROD_first_set`); the vanilla `mTRC_schedule` case 3 -> `mTRC_demo_init` then drives the train in, stops, waits about 20 s of RTC time and leaves, on that process too | done; native guard unit + source audit + REAL host / resident / guest runs |
| T6 | guest departure guard | NOT done (no evidence it is needed: the guest's own train departed in every real run) |

Root cause of "the train never departs" (confirmed in a real run): the first title screen is title demo START1, whose train is parked by `mTRC_mati_init` (action 5, control 1 == last 1). The bootstraps (`--bootstrap-resident`, the host observer) bind a player from the still-running title scene; `mSDI_StartInitAfter -> mTRC_init` resets the train, but the title scene keeps ticking during the outgoing wipe and re-parks it. In the town the train never leaves (WAIT_STOPPED needs control != last or control == 0). The log of a bootstrapped process shows `title_demo=1 action=5 control=1 last_control=1` followed by the T2 re-init line and `action=0 control=0`.
The interactive Start flow and a real guest arrival (ride-off demo sets coming_flag 3 on its first town frame) are not affected by the leak; the generic edge keeps coming_flag 3 anyway.

Real-run result (bin_fixture4, host resident 0 + client resident 2): one re-init per process, the parked title state never reappears, and the client log shows the hourly train lifecycle `0, 1, 2, 3, 4, 5, 6, 7, 8, 0` (spawn at hh:14:50, stop, depart about 5 minutes later, departed). Note: a resident in the first-job / HRA events (fixture residents 0 and 1) never gets the hourly train (vanilla `mEv_CheckArbeit`), so the test picks a free resident.

Limitations: the edge fires once per title exit and is also taken when the title cycles START1 -> START2 (a harmless reset). The train lifecycle after the re-init follows the hourly schedule (spawn at hh:14:50), so a short test may not observe a full arrival/departure. Visual checks (train, edge feel at the map corners and at the station) were NOT done: no human / UI automation. T5 changes single-player borderless behaviour at the map edges (intended); diagonal and corner crossings need a manual pass. Host-side MOVE bounds are not checked (a puppet out of bounds means its owner really is).

Tests: `test_guest_train.py` (native unit of `pc/include/pc_arrival_logic.h`), `test_guest_train_src.py` (source audit pinned to baseline 018344c), `test_guest_train_real.py` (REAL host + client processes, `[TRAIN]` log assertions).

### T3 / T4: arrival presentation on the other processes

Commit hash: 98c61c8. Baseline f660cc7. Protocol version unchanged (8), NO wire message / flag added: everything is derived from the MOVE `action_state` main index, the positions and the PLAYER_SCENE already on the wire; `pc_net_game.c/.h`, `pc_net.c/.h` are byte-identical to the baseline.

What the code does (verified against the decomp, not only the design):

* The guest's ride-off demo (`ac_ride_off_demo_move.c_inc`) puts the player in DEMO_STANDING_TRAIN (forced to the caboose every frame), sets `train_coming_flag = 3` on its first town frame; `mTRC_schedule` case 3 consumes it and `mTRC_demo_init` starts the train at x 2037 with `train_start_timer = now - 290 s`; it leaves 20 s after stopping. T4 reproduces exactly that request, nothing else (no passenger is forced: `train1->arg0/arg1` are set only by the ride-off demo / station master).
* Hook: `pc_remote_player_arrival_train_poll` (TARGET_PC) in the puppet's per-move row bookkeeping. A per-puppet latch (in the per-actor visual struct) allows ONE evaluation per contiguous standing period, only once the puppet's announced scene is the town this process shows (a late SCENE packet does not lose the arrival); the latch is cleared when the state ends and by actor re-creation. The call goes only through `mTRC_pc_remote_arrival`, which evaluates the pure guard `pcarr_remote_arrival_train_decide` (`pc/include/pc_remote_arrival_logic.h`).
* Guard (refusals are logged as `[TRAIN] remote arrival of player N: NOT calling a local train (guard reason R ...)`, reasons: 1 latched, 2 puppet scene, 3 local scene, 4 no player, 5 title demo, 6 player-select / train interior draw type, 7 LOCAL train demo, 8 coming_flag pending, 9 train busy): this process is in the town (FG), has a player actor, no title demo, not in the pre-game draw types, the local player is NOT in a train main index (STANDING / GETOFF / GETON / GETON_WAIT) and no RIDE_OFF_DEMO / INTRO_DEMO actor exists, `coming_flag == 0`, `train_action == NONE`. Never for the local player (the puppet is a remote one and the local demo guard also refuses).
* The observer / dedicated host runs the same code (no window needed; `mTRC_move` already runs there). It is not excluded: a real dedicated host started the train.
* Puppet rows: DEMO_STANDING_TRAIN -> hidden (`PC_ROWF_HIDE_BODY`, reuses the HIDE row's clears); DEMO_WALK -> WALK1 with the walk tempo. The table differs from the baseline in exactly these rows plus the GETOFF comment.

Corrections to the design (verified in code):

* DEMO_GETOFF_TRAIN is NOT given an OUTTRAIN1 row. The vanilla state plays OUTTRAIN1 with `cKF_SkeletonInfo_R_AnimationMove_ct_base` root motion (`m_player_main_demo_getoff_train.c_inc`); puppets never run AnimationMove (`animation_enabled == 0`), so the clip's own root translation would be drawn on top of the already synced (root-moved) position. This is the same reason every other `ct_base` state is an explicit fallback in the table. The puppet steps off the train with the idle clip and the synced position (as before). Doing it right needs an AnimationMove emulation for puppets (set the base shape trs + the animation flags for the row's duration) which cannot be validated without seeing it; left as a follow-up.
* A resident that enters the town through the interactive Start flow ALSO arrives with the ride-off demo (`ac_npc_p_sel2_talk.c_inc` sets RIDE_OFF_DEMO for the normal game start), so its puppet also shows DEMO_STANDING_TRAIN. The wire cannot tell a guest from such a resident, and the train is correct for both (they both arrive by train), so the hook is not restricted to guests. Bootstrapped residents (`--bootstrap-resident`) and the observer do not play the ride-off demo.
* A host whose own resident is in the first-job event spawns the vanilla INTRO_DEMO (its own train arrival); the guard then refuses with reason 7 (LOCAL_DEMO) - observed in the real run with fixture resident 0 as host.

Known limitations (honest):

* The other process's train runs about 0.2-2 s behind the guest's (READY handshake + 100 ms interpolation delay); the puppet is hidden meanwhile, but nothing keeps the two trains in lockstep (the offset at the stop is a few units).
* If the guest becomes READY only after it got off the train, observers see GETOFF / WALK first: no train is called (the latch only starts on STANDING_TRAIN).
* The train on the other process is its OWN local state (not synced) and can race an hourly train: while a train is arriving / stopped / departing the call is refused (reason 9), so a guest arriving during the hourly train gets no second train there; the puppet is hidden for the length of its standing state in any case.
* After a teleport snap or a silent-peer gap during the standing state the hide row is released (same rule as HIDE), so the puppet becomes visible in idle until the state changes.
* Not seen by a person: whether the hidden puppet / train timing looks right, the step-off with the idle clip, the demo-walk tempo near the goal (WALK1 at the minimum tempo while the guest stands at the goal). The station master / welcome speech of the guest is local to the guest.

Tests (exact results below): native guard unit `test_guest_arrival.py` (`guest_arrival_selftest.c` over `pc/include/pc_remote_arrival_logic.h`: 33/33, every guard branch, order, latch, sequences); source audit `test_guest_arrival_sync_src.py` (43/43: vanilla train / player / ride-off decomp byte-identical to f660cc7, pc_remote_player.c table differs only in the three rows, hook TARGET_PC guarded and calling only through the guard, no wire change); REAL host + resident + guest processes `test_guest_arrival_real.py` (resident host: 17/17, `--host-mode dedicated`: 19/19). The pin `test_guest_train_src.py` was updated strictly: its "no pc_remote_player.c touched" wire check now only forbids pc_net* / protocol files (34/34).
Real-run evidence: the resident client logged exactly one `starting the local arrival train (coming_flag=3)` for the guest and the action sequence 2, 3, 4, 5, 6, 7, 8, 0 (arriving, stopped, waiting, departing, departed); the dedicated host logged the same; no second train for the same puppet (one start line, one 0 -> non-zero transition per process); with resident 0 as host the guard refused once with LOCAL_DEMO.
Re-run of the directly affected existing tests on the new exe (bin_fixture4): `test_guest_train.py` 33/33 (native), `test_guest_train_src.py` 34/34 (source audit), `test_guest_train_real.py` 12/12 (REAL), `test_move_action_state_wire.py` 38/38 (REAL host + scripted clients; its row table parser picked up the new `standing_train` row with no pin change), `test_guest_g3_real.py` 43/43 (REAL), `test_guest_g4_real.py` 36/36 (REAL); the other pc_remote_player-related source audits pass (g3 35/35, g4 27/27, g5 19/19, dedicated 87/87, observer 65/65, hostcfg 21/21, events 24/24). Pre-existing failures NOT caused by this change (files untouched here: pc_m_card.c / pc_net_game.c / the guest-arrival pins of earlier commits): `test_boot_exec_gate_src.py` 8/10 (A4 / A7 player-actor allowlists), `test_d3_record_src.py` 133/134 (P hook), `test_identity_validation_src.py` 93/96 (S2 / S9 / S11).


### Train arrival on residents (fix)

Symptom: resident clients did not see the train when a guest arrived (19 of 24 logged remote-arrival lines were guard reason 7, LOCAL_DEMO).

Root cause: `mTRC_pc_remote_arrival` treated the mere existence of an INTRO_DEMO actor as "the local player is arriving". Vanilla keeps that actor alive for the whole first-job event (`m_actor.c`, `ac_intro_demo.c`; deleted when the job finishes, `ac_intro_demo_move.c_inc`), so any resident still in the first job was refused. The decision was also latched once per standing period, so it was never retried after the condition cleared.

Change (wire protocol unchanged, version 8):

* `src/game/m_train_control.c`: the INTRO_DEMO actor counts as a local arrival only while `mEv_CheckFirstIntro()` is true (cleared in `aID_retire_rcn_guide_wait`), through the pure `pcarr_local_intro_arriving`. `mTRC_pc_remote_arrival` now returns the guard reason (0 = started) and logs a refusal only when (peer, reason) changes.
* `pc/include/pc_remote_arrival_logic.h`: reasons 7 (LOCAL_DEMO), 8 (COMING_FLAG), 9 (BUSY) are transient (`pcarr_remote_reason_is_transient`); `pcarr_remote_standing_settle` releases the latch for them. OK and reasons 1-6 still consume it (once per standing period).
* `pc/src/pc_remote_player.c` (`pc_remote_player_arrival_train_poll`): passes the returned reason to `pcarr_remote_standing_settle`, so the puppet re-polls every frame while it stands in DEMO_STANDING_TRAIN.
* `mTRC_trainSet` diagnostics (`-debug`): one `[TRAIN] actor spawned` or `[TRAIN] actor not in area (... player block x,z = ..)` line per train.

Limitations: the train ACTORS only exist while the train is inside `aTRC_area_check` of the local player (near the station acre; vanilla behaviour, unchanged), so a resident far from the station runs the train state machine but sees nothing (the new `not in area` line says so). A transient refusal that outlasts the guest's standing period (a few seconds) is lost. The visual result was NOT verified by eye or screenshot.

Tests: `guest_arrival_selftest.c` via `test_guest_arrival.py` (47/47: transient-vs-latching cases, intro-only-while-FirstIntro, BUSY-then-free calls once, structural refusals not retried); `test_guest_arrival_sync_src.py` pins updated (40/43; the 3 failures are pre-existing wire/classifier checks against the T-phase baseline because `pc_net_game.c` changed in later G phases, not by this fix). No real host+resident+guest run was made for this fix.

## Guest profiles (multiple guests on one PC)

Problem: `--connect HOST --guest` always used `save/mp/guest.ini` + `save/mp/guest_token.dat`, so several clients started on one PC were all the SAME guest.

Usage: `AnimalCrossing.exe --connect HOST --guest-profile NAME` (implies `--guest`; may also be given together with `--guest`). CLIENT only, requires `--connect`, same exclusivity and
refusals as `--guest` (exit 2 + a usage line); a missing, empty, invalid or repeated NAME is refused with exit 2 and a diagnostic naming the rule. One process = one profile.

Files (in `save/mp`, next to the unchanged default):

| profile | profile file | client token file |
|---|---|---|
| none (`--guest` alone, and the title-menu item without `--guest-profile`) | `guest.ini` | `guest_token.dat` (exactly as before, byte-identical behaviour) |
| `--guest-profile NAME` | `guest_<name>.ini` | `guest_token_<name>.dat` (each profile has its OWN token, so each is its own guest on every host) |

`<name>` is the profile name folded to lower case: `Alice` and `alice` are ONE profile (one file, one identity); the typed case is kept only as the default display name of a new profile.

Rules:

* NAME = 1..16 characters of `[A-Za-z0-9-]`; it must not start with `-` (an option such as `--guest` can never be swallowed as a value); no dots, spaces, path separators, `..`, drive
  colons; not a Windows device name (`CON PRN AUX NUL COM0..COM9 LPT0..LPT9`, any case).
* A missing profile file is created on first use (atomic write, own CSPRNG `player_id` / `land_id`, home town `GuestVil`, gender / face derived from the ids as before). An existing or
  corrupt file is never overwritten (exit 2 naming the bad key, as for guest.ini). The home land name stays `GuestVil`: the host key is the full PersonalID, equal land names are harmless.
* Default display name of a new profile = the profile name cut to 8 characters; if that is not a valid game name, is `SERVER` (any case) or is `Guest` (the default profile's own name),
  the name is `Guest` + 2 hex digits of the player_id. The host refuses a NEW guest whose name equals another guest's name in the town, so the names of the profiles of one PC differ.
* Uniqueness: before a NEW profile is created the sibling profiles of the folder (`guest.ini`, `guest_*.ini`) are scanned read-only (an unreadable or invalid one is skipped with a
  stderr warning, never touched) and the ids are re-drawn (at most 32 draws) until neither the display name (any case) nor the full identity (name, home town, player_id, land_id) equals a
  sibling's; a name collision first falls back to the `GuestXX` name. If no unique identity is found the creation fails (exit 2) and nothing is written.
* ONE source of truth: `pc_main.c` selects the profile once (`pc_guest_profile_select`); `--guest`, the title-menu item and the client token file all derive their paths from that selection
  (`pc_guest_token_path()`, `pc_guest_profile_selected_path()`).
* Title menu: drawing the menu only READS the selected profile (label `Join as Guest (NAME)`, or `Join as Guest (new profile)` when the file does not exist); the profile is created only when
  the player actually joins (title item or `--guest`). Before this change merely drawing the menu of any `--connect` client created `save/mp/guest.ini`.
* No protocol bump, no wire change.

Limitations: the same profile must not run in two processes at once (no lock, nothing is refused); the title-menu item uses the profile chosen on the command line (there is no in-game profile
picker); the title-menu label and a join from the title menu are not visually verified; the profile identity is permanent exactly like guest.ini (deleting a profile file makes a NEW guest).

Tests: native `test_guest_profiles_unit.py` (101 native checks), CLI `test_guest_profiles_cli.py` (101), source audit `test_guest_profiles_src.py` (30), REAL `test_guest_profiles_real.py`
(40: three real guests from one cwd, kill / restart one, host restart). Commit: 7c95a59

## First-run guest creation

`AnimalCrossing.exe --connect HOST:PORT --guest-profile NAME` now has two modes:

- **`save/mp/guest_<name>.ini` exists**: unchanged. The profile is loaded and validated, the guest arrives at the station (RIDE_OFF_DEMO) as before. A corrupt file is still never touched (exit 2).
- **the ini does not exist**: nothing is auto-created. The client draws the permanent ids (and a placeholder display name, unique among the sibling profiles) **in memory** (`pc_guest_profile_prepare_new`) and arms a "creation pending" state. The guest is bound exactly as in `pc_guest_arrive` (placeholder name, `mSDI_StartDataInitGuest`, player_no 4) but the door is the vanilla Rover entry `SCENE_START_DEMO2`, north, (120, 340), no RIDE_OFF_DEMO. The player plays the REAL vanilla Rover scene (`ac_npc_guide2*`: name entry, gender, face questions). When the scene ends, `aNG2_scene_change_wait_init` calls `pc_guest_creation_finish(play)` instead of the vanilla body (TARGET_PC only).

The finish: validates name / gender / face, writes the ini **create-only** (tmp file + `MoveFileExA` without `MOVEFILE_REPLACE_EXISTING`, `link()` on POSIX; an existing file is never replaced), applies the identity-hash starter shirt (`pc_guest_starter_shirt`, the same one `pc_guest_build_fresh_record` uses), keeps the face assignment and the two BGM calls, and goes to the station (`SCENE_FG` 1979,760, RIDE_OFF_DEMO, circle wipe). It does NOT run `mEv_SetFirstJob`, `mEv_SetFirstIntro`, the random shirt, the weather decision, `mGH_animal_return_init` or the submenu lock, does not rebuild the record, and never arms `pc_save_ready` (a guest process writes no save).

Plain `--guest` and `--bootstrap-guest` keep their behaviour (`--guest` auto-creates `guest.ini`). `--guest-profile` still implies `--guest`. The title menu label only reads the profile (never creates). Choosing "Join as Guest" with a named profile that has no file uses the same creation path; with the default profile (no `--guest-profile`) the legacy auto-create remains.

### Persistence and failure rules

- Written once, at the end of the Rover scene, to `save/mp/guest_<name>.ini` (lower-cased profile name). Nothing is written before that, so an interrupted creation (closed window, crash) simply replays on the next launch.
- If the write fails (or the file appeared meanwhile) the process exits with status 2 and a message; an existing file is never replaced.
- If `save/mp/guest_token_<name>.dat` exists WITHOUT the ini, creation is refused with a message (exit 2 / title message); the token file is never deleted.
- The typed name must be storable in the ini (`pc_guest_profile_name_from_game`: A-Z a-z 0-9 blank . ' -, no leading blank, not SERVER). Otherwise the Rover scene answers with its normal "that name is taken" message and asks again (`aNG2_check_pname`; the existing "same name as a resident" rule is unchanged). `aNG2_getP_other_pl_name` returns before its out-of-bounds second loop for player_no >= PLAYER_NUM.
- Network: the client does not send its guest claim (IDENTITY_EXT / IDENTITY) while a creation is pending (`pc_guest_creation_active()` gate in `pcnetgame_client_tick`), so the host only ever sees the final identity. No wire or protocol change.

### Test-only hook

`--guest-creation-test NAME,GENDER,FACE` (requires `--guest-profile`; inert when the profile exists) types those answers into the Rover scene without UI after it has run for 120 frames and calls the very same finish. Used for process tests; it is not a gameplay option.

### Tests run for this feature

- Real client process (disposable copy of the fixture dir under the session scratchpad, no host): new profile `Roger` + hook `Zed,1,3` loaded the Rover scene, wrote `guest_roger.ini` (`name = Zed`, `gender = 1`, `face = 3`, ids as drawn at start, no tmp file), then moved to the station scene without a crash; the copied town save GCI stayed byte-identical.
- Relaunch with the same profile: log `loaded guest profile ... name 'Zed'`, no creation line, ini byte-identical (md5), straight to the station arrival with the identity-derived shirt.
- `test_guest_profiles_src.py` (37 checks, new group G audits: finish never touches `mEv_*` / `pc_save_*` / the record builder, create-only write helper, arm/read order in `pc_main.c` and the title join, actor hooks) and `test_guest_profiles_unit.py` (123 checks incl. new `first-run:` group: prepare_new writes nothing, create_exclusive create-only, orphan token refused and not deleted, name round trip).

### Limitations

- The Rover UI itself (name keyboard, gender and face questions) was not exercised visually; only the scene load / actor run (120+ frames at player_no 4) and the finish path were verified through the hook.
- START_DEMO2 actors with player_no 4 (mPr_FOREIGNER) are unverified beyond "the scene ran without crashing"; other Rover-scene code that indexes `private_data[player_no]` was not audited.
- Name clashes with OTHER guests of a host are only known at first contact (the host refuses a NEW guest whose name equals another guest's; nothing is checked while creating).
- The network handshake waits during creation; a host that times out an idle connection would drop the client before the Rover scene ends.
- A name the profile file cannot represent shows the Rover scene's "name already used" message, which is not an exact explanation.
- `test_guest_profiles_real.py` still expects `--guest-profile` to auto-create its ini files; it needs pre-seeded profile files (or the hook) to run unchanged and was not updated or run.

## Building interactions (doors, knock, enter/exit)

What a remote puppet does when its owner knocks, opens a door, walks in and comes out again. No protocol change: the MOVE `action_state` (vanilla main index + entry counter) and the existing scene event drive everything; the only sender change is a VALUE: `facing_angle` carries `shape_info.rotation.y` for KNOCK_DOOR and DOOR (as it already did for TURN_DASH), because `AnimationMove_base` turns the visual facing toward the door while `world.angle.y` holds the state's fixed target angle.

### Behaviour (pc/src/pc_remote_player.c)
- KNOCK_DOOR (100): row `knock_door`, KNOCK1 on the body layer + the carried item pose, STOP at 0.5. Flag ROOT_PIN.
- DOOR (4): row `door`, OPEN1 on both layers (table default; INTO_S1 for shop-style buildings, see 'Door class'), STOP at 0.5. Flags ROOT_PIN | HIDE_AT_END: when the clip ends the puppet is hidden (`hidden_door`) until the scene event arrives, a new scene presence is consumed, or 600 frames pass. Not cleared by the 90 frame silent-peer gap (an owner inside a building keeps sending).
- OUTDOOR (5): row `outdoor`, GO_OUT_O1 on both layers, STOP at 0.5 from frame 1 (table default; `row_start` swaps it per door class, see 'Door class' below). Flag SCENE_ENTRY and NO root pin: vanilla has no `ct_base` there, `world.position` stays put and the clip's own root translation walks the body out. (The old PCFB comment claiming `AnimationMove` root motion was wrong and was replaced.) It starts from the scene-change clear (and from first sight on a fresh actor) and is not released by that clear.
- ROOT_PIN reproduces vanilla `ct_base` TRANS_XZ|ROT_Y with `Set_base_shape_trs(0,1000,0,0,0,0x4000)`: the draw of joint 0 then uses the base translation (0) instead of the clip root, so the clip is not added on top of the synced position (no double root motion). `AnimationMove_base` is never run on a puppet; the pin is dropped on row release and on any rebind.
- Scene gate: `dw` draws a puppet only while `pc_remote_player_scene_is_local_shown` holds: its announced scene is the town this process shows (`pc_remote_player_scene_is_local_field`), OR it and the local player are in the very same shared interior (see 'Shared interiors' below). An owner in a DIFFERENT scene (whose MOVEs carry foreign coordinates) is never drawn as an idle ghost. Effects, sounds, collision, the arrival-train poll and the cosmetic building doors keep the field-only rule.
- Return: `on_scene` records `scene_accept_frame` and arms `snap_req`. The puppet stays hidden until a MOVE received at/after that frame exists, then every older sample is dropped so it is placed at the first post-scene sample (no slide through stale interior positions, no interpolation from old samples; escapes: 60 frames without any MOVE, or the frame clock restarting). That placement frame never sets `pos_snapped` (which would release the row).
- Collision: the puppet's pipe is held (silent) while a KNOCK_DOOR / DOOR / OUTDOOR row runs, while hidden and while a scene event is pending.
- Remote animation only writes the puppet's own `PCRemotePlayerVisual` / slot; nothing touches the local player's state.

### Vanilla trace (include/m_player.h, src/game/m_player_main_*.c_inc)
knock_door: Base1(KNOCK1) + Base2 STOP 0.5, ct_base ROT_Y|TRANS_XZ, then request door. door: type 0 -> OPEN1 else INTO_S1 (shops), ct_base flags 5 (TRANS_XZ|ROT_Y), STOP 0.5, then the scene change. outdoor: GO_OUT_O1 (GO_OUT_S1 for the start demo), start frame 25 for type 0 else 1, no ct_base, then RETURN_OUTDOOR (idle fallback row).

### Limitations
- The building door animation IS played for hinged-door buildings (see 'Cosmetic building doors' below); door_type 1 buildings (Nook shops, museum, police, ...) have no building door clip to trigger but the puppet now plays the shop-style body clips (INTO_S1 / GO_OUT_S1). Island cottages are not covered (the island is not the shared town).
- The door type is not on the wire: it is inferred (see 'Door class'). When neither the previous scene nor a door point within 60 units identifies the building, the table clips are kept (OPEN1 / GO_OUT_O1 from frame 1). The 60 unit radius and the point offsets are read from the actor sources but unverified against real puppet positions.
- OUTDOOR from a hinged building starts GO_OUT_O1 at frame 25 like vanilla type 0; the jump from the synced exit position to the clip root at frame 25 (a small pop) is unverified.
- The clip root end positions of OPEN1 / GO_OUT_O1 vs the owner's synced positions are unverified; a small pop at the end of the exit clip is possible.
- Ordering of MOVE vs SCENE is not guaranteed. MOVEs received before the scene event are dropped on purpose; an OUTDOOR state that began before the event is only caught if it is still the current state when the event is consumed.
- Interiors are shared only between players in the same interior instance (see 'Shared interiors'); a player outside never sees one inside and vice versa.
- The knock sound is not reproduced.
- Nothing was verified visually. Every statement above is source-level or hook-driven; no screenshot or two-screen play test was done.

### Door class (hinged vs shop-style clips)
Vanilla: `Player_actor_setup_main_Door` plays OPEN1 for door type 0 (hinged buildings: houses, my house, post office, needlework shop via m_demo.c) and INTO_S1 otherwise (`mPlib_request_main_door_type1(..., TRUE)`: Nook shops, conveni, super, depart, museum, police box, Crazy Redd's tent, buggy, kamakura, lighthouse, tent). The exit comes from the exit `door_data.extra_data` (m_scene.c:412 -> m_player_main_dma.c_inc:37-42): 2 (hinged) -> GO_OUT_O1 from frame 25, 3 (every type-1 building) -> GO_OUT_S1 from frame 1, 1 -> GO_OUT_O1 from frame 1.
`pc_remote_player_door_pick(self, exit_row, &best)` returns PC_DOOR_HINGED / PC_DOOR_SHOP / PC_DOOR_UNKNOWN once per DOOR/OUTDOOR row instance (reusing the `door_cosm_time` latch; the class is kept in `visual.door_cls`). It needs the puppet's announced scene to be the shown town, then takes the nearest building door point (`pc_remote_player_door_point`, entry or exit point, hinged and type-1 buildings, ITEM actor list) within 60 units. For OUTDOOR the interior the owner just left decides the class when known (`slot->prev_scene_id`, saved in `on_scene` before the overwrite when the scene id changes; `pc_remote_player_door_class_of_scene` maps by scene id: shops, depart floors, Redd's tent, museum rooms, police box, kamakura, buggy, lighthouse, tent -> SHOP; player rooms, NPC house, post office, needlework shop, cottages -> HINGED; NEEDLEWORK is KIND_SHOP in `pc_net_game_scene_kind` but hinged, hence the own map); the geometry then only chooses the building, and an unknown previous scene falls back to the nearest door point of either class. `row_start` then overrides before the body clips are bound: DOOR+SHOP -> INTO_S1 on both layers (still ROOT_PIN, vanilla also runs it under `ct_base`); OUTDOOR+SHOP -> GO_OUT_S1 from frame 1; OUTDOOR+HINGED -> GO_OUT_O1 from frame 25; UNKNOWN or a missing clip keeps the table row. The aHUS/aMHS/aPOFF/aNW wrappers are called only when the class is HINGED (and the existing gates pass). Type-1 door points (entry / exit, relative to the building position unless noted): shop (-50,+50)/(-68.29,+68.29); conveni (-25,+82.5)/(-42,+98.57); super and depart (-45,+102.5)/(-62,+118.57); museum (0,+100)/home+(0,+120); police box (+50,+50)/home+(+60,+60); Redd's tent (BRSHOP) and buggy (0,+64)/(0,+100); kamakura and tent (0,+68)/(0,+86); lighthouse (0,-60)/(0,-70).

### Shared interiors (players see each other inside the same room)
Previously `dw` hid every puppet that was not in the town field. The wire already suffices: PLAYER_SCENE carries `scene_id` and `owner` (`house_owner_name` for player rooms and the NPC house, else 0; shops, post office, police box, museum rooms, depart floors, needlework, kamakura, buggy, lighthouse and tent are one instance per town), and MOVE is sent from any live play_main scene and relayed by the host regardless of scene. `pc_remote_player_scene_is_local_shown(slot, play)` is the field rule OR (slot scene valid, kind SHOP / POST_OFFICE / POLICE / MUSEUM / PLAYER_HOUSE / VILLAGER_HOUSE / OTHER_INTERIOR, not SCENE_COTTAGE_MY / SCENE_COTTAGE_NPC, `pc_net_game_get_local_scene` succeeds, and local scene id == `play->scene_id` == slot scene id with equal owner). It is used ONLY in `dw`. Collision, effects, sounds (the field footstep path), the arrival-train poll and the cosmetic doors keep `pc_remote_player_scene_is_local_field`, so inside an interior the puppet has no collision, footsteps or effects. Island cottages are excluded (owner is forced to 0 and the island is not shared). In interior mode `dw` additionally hides a puppet that has sent no MOVE for 90 frames (the owner is in an unannounced scene such as the save screen, so the announced interior is stale).
Transitions use the existing machinery: every scene event arms `snap_req` (hidden until the first fresh MOVE, older samples dropped); a local scene change re-creates the puppet actor at the newest sample and keeps a pending `snap_req`; the owner's own DOOR row in the town (owner enters while the local player is already inside) runs on a hidden puppet and its `hidden_door` is cleared by the scene clear. `row_post` no longer sets `hidden_door` when the owner's announced scene is already an interior, so a DOOR clip that ends after the scene event cannot keep a puppet invisible in an interior both players are in.
Limitations / unverified: interior lighting and draw order of a puppet (it uses the generic actor draw of the scene) were not checked on screen; MOVE vs SCENE ordering is not guaranteed (a MOVE with stale coordinates right before the scene event is dropped, one right after may still carry the old scene's coordinates for a frame); the puppet's Y comes from the MOVE sample and no interior collision or floor attribute is evaluated; the TURN_DASH skid sound is not scene-gated and can play in an interior; the 90 frame stale rule also hides a puppet whose owner is merely paused. Manual checks: two instances in the same shop / player room / NPC house / museum room: both visible, walking, the other one entering and leaving; a third instance outside sees nothing; two players in different player rooms (different owner) do not see each other; cottages never show.

### Cosmetic building doors (puppet enters / leaves a building)
Root cause of the closed door: a vanilla hinged door only animates when the LOCAL player enters (the building requests mDemo DOOR and its wait proc waits for the local PLAYER_ACTOR `get_door_label_proc` to return the building), when the local player exits (`ac_structure.c` `aSTR_check_door_data` sets request_type 4/5 from `door_data`), or when an NPC walks in/out (`aNPC_request_house` sets request_type 1/2). A puppet is not the PLAYER_ACTOR, so nothing ever reached the building.

Mechanism (TARGET_PC only, no protocol change): `pc_remote_player_row_start` (pc_remote_player.c) runs `pc_remote_player_door_pick` and then `pc_remote_player_cosmetic_door` once per DOOR (enter) / OUTDOOR (exit) row instance (latch `door_cosm_time`, no retry); the wrapper dispatch happens only for class HINGED. It runs only when the slot is not waiting for a scene snapshot, the puppet's announced scene is the shown town, the local player is not in OUTDOOR and the structure control has no pending exit door (`str_door_name == EMPTY_NO`). The pick walks the ITEM actor list, computes each building's entry point (my_house `arg0_f/arg1_f`; house world+(0,40); post office / needlework shop world + 40 * direction 5) or exit point (house home+(0,60); my_house world+(+-48.29,48.29); shops as entry) and picks the nearest within 60 units. The per-building wrappers `aHUS_/aMHS_/aPOFF_/aNW_pc_cosmetic_door(ACTOR*, int exit)` return 0 unless the building is idle (action_proc is the wait proc, request_type 0, no mDemo DOOR/DOOR2/SPEAK, not the local player's door label); so several puppets at one door cannot corrupt it (the first wins). They only write the building's own fields:
- ac_house: request_type 1 (in) / 2 (out) + `ACTOR_STATE_NO_MOVE_WHILE_CULLED`, exactly the NPC path (the wait proc plays it; open_door restores the flags).
- ac_my_house: request_type 5 (in) / 4 (out) with arg2_f 2 / 1, then `aMHS_setup_animation(0.5)` + OPEN_DOOR directly (the wait proc would need the local OUTDOOR state). arg2_f 0 (player 'in' clip) and 3 (save scene) change the scene and are never used; `aMHS_actor_move` recomputes arg2_f from request_type through drtbl (4->1, 5->2), which stays in the safe set. Entering therefore plays the 'out' clip (the 'in' clip is the scene-changing one).
- post office / needlework shop: request_type 2 (in) / 4 (out) + `setup_animation(0.5)` + OPEN_DOOR directly (routing 1/2 through the wait proc gets stuck); request_type 3 (scene change) is never used.
No goto_*scene, mDemo_End or mDemo_Request is reachable from the wrappers (source-audited).

Isolation findings (interior): draw, effects and collision were already gated by `pc_remote_player_scene_is_local_field`, so an owner inside a building is not shown. Two gaps were looked at:
1. FIXED: when the LOCAL player changed scene while a remote scene event (e.g. an exit from a building) still awaited its first fresh MOVE, the actor re-creation in `pc_remote_player_poll` cleared `snap_req`, so the puppet could be placed at a stale pre-event sample. It now keeps `snap_req`, empties the ring and re-bases `scene_accept_frame` on the new frame clock.
2. NOT changed (documented): MOVE is sent in any live scene but `pcnetgame_scene_tick` never announces non-announceable scenes (START_DEMO*, EVENT_ANNOUNCEMENT, FIELD_TOOL*), so peers keep the last announced scene and could show foreign coordinates as a town ghost. Skipping MOVE there would only freeze the ghost at the last town position (the announced scene still reads as the town) and risks the guest arrival / creation and title flows, so it was left alone. Known limit (a) of the scene detector.

Exceptions / limitations: door_type 1 buildings (Nook shops, museum, police box, ...) have no building door clip (the puppet plays INTO_S1 / GO_OUT_S1 itself); island cottages are not covered; the knock sound (KNOCK_DOOR) triggers nothing; entering a my_house plays the 'out' clip; while the cosmetic clip runs (about 100 frames at speed 0.5) the building's request_type is non-zero, so a LOCAL entry or NPC use of that door is blocked for that time. The nearest-door pick is geometric (60 units) and untested against real puppet positions.

Tests: `pc/tools/net_spike/test_building_doors_src.py` (SOURCE AUDIT: four wrappers exist, TARGET_PC guarded, prototypes, no scene-changing call, guards present, my_house arg2_f set, dispatcher gates, door_pick / door_point (type-1 offsets cross-checked against the actor sources) / door_class_of_scene, row_start hook and clip override, prev_scene_id in on_scene, snap_req fix) and `test_building_interactions_src.py` (also pins the shown predicate, its dw-only use, the stale-gap hide and the interior-aware hidden_door). `test_move_action_state_wire.py --only s3` refuses to run from the live build dir (needs a disposable NET_SPIKE_GAME_BIN copy) and was not run for this change.
Manual verification still needed (two instances, nothing was seen on screen): a puppet entering and leaving each of NPC house, own/other player house, post office, needlework shop shows the door opening and closing once with sane timing and the right door picked; two puppets at one door; a local player entering a building right after a puppet; the exit position/timing of the clip; no stuck door (request_type stays non-zero) after a local scene change mid-animation.

### Tests
- `pc/tools/net_spike/test_building_interactions_src.py` (SOURCE AUDIT only): flags, row shapes, root pin apply/clear, hidden_door set/timeout/clears, scene gate, snap_req wait and trim, collision hold, sender facing value.
- `pc/tools/net_spike/test_move_action_state_wire.py --only s3` (REAL host process + scripted client, HOOK-DRIVEN, no visual check): the parsed row table still covers all 121 indexes (now 48 filled rows incl. knock_door / door / outdoor) and every index, including 4, 5 and 100, logs the expected row without crashing the host.

### Puppet idle speed and shop NPC (investigation)

* Remote idle played at 2x: the puppet's animation fallback rebind (`pc_remote_player.c`, "desired_anim_idx changed") bound frame 0 / speed 1.0, but vanilla wait (`m_player_main_wait.c_inc`, `InitAnimation_Base1`) is frame 1.0 / speed 0.5; WAIT1 is excluded from the tempo retune, so after every walk->stop and every action row (door, outdoor, tool, pickup) idle ran at twice the vanilla rate. Walk/run/dash/RUN_SLIP overwrite the speed afterwards, so they were unaffected. Fix: bind 1.0 / 0.5. The door clip itself already used the vanilla 0.5; the abrupt start (vanilla door uses morph -9, the puppet morph 0) is NOT changed. Timebase check: `cKF_FrameControl_play` scales by the same frame-time for local and puppet, and the puppet advances once per `mv`, so no per-packet / double advance was found. Verified by build + source reading only, not visually.
* Tom Nook following only the local player is NOT a sync bug: `ac_npc_shop_master*` / `ac_npc_shop_common.c` target only `GET_PLAYER_ACTOR`; puppets are not PLAYER actors, the shop scene is a per-process interior, Nook is not in `Save animals[]` so N2 villager sync never carries it. Each process sees its own Nook. Deliberately left unsynchronised (making shop NPCs host-authoritative would break buy/sell/camera demos). A cosmetic follow-up would be a TARGET_PC target picker over same-interior puppets in `aNSC_set_zone_data` / `aNSC_decide_next_move_act`.

### Interior puppet collision (shared interiors)

Change (`pc_remote_player_collide_eval`, `pc/src/pc_remote_player.c`; no wire / save change): the existing puppet pipe (it only pushes the LOCAL player; the local player's own wall / furniture collision then corrects the result) is now also armed when the puppet and the local player are in the very same shared interior (`pc_remote_player_scene_is_local_shown`: same scene id + same owner, not a cottage). Different houses, rooms, floors or scenes never match. PLAYER_HOUSE scenes stay DISARMED while furniture is not synchronized. In an interior the pipe is held while the puppet's last MOVE is older than 90 frames (same stale rule as dw) and while the local player is in a door / outdoor / knock / intro / return / demo main state. Outdoor behaviour is unchanged. No environmental collision was added to puppets: the owner already collided with the same geometry locally. Tests: `test_interior_collision_src.py` (source audit), `test_building_interactions_src.py` (one pin updated: the shown predicate may now be used by dw AND collide_eval). Not verified visually (shove feel in a shop, door-mat case).

### Furniture synchronization: Stage 1 (host-authoritative, `--house-sync`; default ON only for a `--dedicated` host)

Stage 1 synchronizes the FURNITURE (and wall / floor / music box) of player houses. Collision of a visitor with the owner's furniture is Stage 2 and is NOT part of this change (the interior puppet pipe stays disarmed for player-house scenes). The feature is OFF unless the HOST is started with `--house-sync` or with `--dedicated` (a dedicated host turns it ON by default, `--no-house-sync` disables it; a plain `--host` stays off, like `--authoritative-wildlife`; the host announces it in HOST_CONFIG byte 1 bit 0 and a client follows the host; a client that never sees the bit gates nothing).

Why a pair: a pickup moves an item between the room and a pocket in ONE frame (`Player_actor_putin_furniture` -> `aMR_Furniture2ItemBag`), a placement does not (`mTG_drop_furniture` clears the pocket, the item sits in `rsv_ftr` for 46 frames), drawer contents live in `ftr_actor->items[]` and reach the save only when the room is torn down (`aMR_KeepItem2Fg` in `aMR_AllFurnitureDestruct`), and a pocket transaction commit writes the client's pocket pre-image into the host record. House and pockets therefore agree only OUTSIDE the room, and they are committed as ONE pair or not at all. (Read from code; none of it reproduced in a real room.)

Wire (v8 unreleased, extended in place, NO version bump; ids 59..61, all RELIABLE):
* `HOUSE_BEGIN` (59, 36 B, both directions): kind 1 OWNER_COMMIT (client -> host: house image `||` the 0x2440 B record = 16004 B = 17 chunks; `house_seq` / `host_session` = the canon the edit was built on, `rec_epoch` / `rec_rev` = the record base) or kind 2 CANON_PUSH (host -> client: image only = 7 chunks, `house_seq` = the resulting canonical seq). FNV-1a32 of the whole payload.
* `HOUSE_CHUNK` (60, 1012 B, the RECORD_CHUNK layout), `HOUSE_ACK` (61, 24 B, host -> owner): status 0 APPLIED, 1 STALE, 2 BAD_DIGEST, 3 BAD_SHAPE, 4 INVALID_CELL (detail = floor*1024 + layer*256 + cell; `0x4000 | floor<<4 | 1 wall / 2 floor` for wall_floor; `0x8000 | record field` for a record field), 5 CONSERVATION, 6 NOT_OWNER, 7 BUSY, 8 RATE_LIMITED. A misdirected kind (a client's CANON_PUSH / HOUSE_ACK, a host's OWNER_COMMIT) is dropped.
* House image (0x1A44 B) = big-endian `mHm_hs_c` bytes `[0x0000, 0x1A30)` (header, floors[3]) + `[0x2678, 0x268C)` (goki, music_box); mailbox (host-fed by MAILBOX_LETTER) and haniwa are not in it. The OWNER may write ONLY floors[*] (4 layers incl. ftr_switch / haniwa_step, wall_floor, tempo_beat, floor_bit_info) and music_box; everything else is host-owned. `pc_save_bswap_home()` is the public wrapper of `swap_mHm_hs`.

Host (`HOUSE COMMON + HOST` block of `pc_net_game.c`): canonical copy = the host's own `Save homes[h]` plus a BE image + `seq` per house (`s_hh[]`), re-read once per second (never while the host's own player is in that house, `pcnetgame_local_house_unsafe`); a changed digest bumps `seq` and is pushed to every READY non-guest peer (host-originated changes such as turnip spoilage, upgrades, the host's own edits are therefore never undone: an older owner commit is STALE). OWNER_COMMIT handler (reassembly like the record upload, a transfer older than 5 s is discarded): (1) READY + bound + world ready + record SYNCED + no push in flight else BUSY; (2) a guest slot -> NOT_OWNER before any `homes[]` index; (3) the house comes from the sender's bound PersonalID (`BEGIN.house` is only checked); (4) digest, then the record path's rate limit; (5) BUSY while the host's player is in / entering / leaving that house; (6) base (host_session, house seq, record epoch / rev) else STALE; (7) the record through `pcnetgame_rec_validate_fields`; (8) every changed cell must be EMPTY / RSV_FE1F / a legal furniture or item, structural ids can neither be written nor removed, floors the house lacks must be byte-identical, wall / floor indexes in range; (9) CONSERVATION over house (3 floors x 4 layers + wall_floor + music box) + the 15 pockets + the gift of every used letter: every item class exactly equal old vs new, except wallpaper / carpet / disc classes which may only decrease (furniture -> rotation-free item class via `mRmTp_FtrItemNo2Item1ItemNo`, my-design furniture skipped, all four turnip ids one class); the first 4 differing ids are logged; (10) commit in ONE call: `pcnetgame_rec_merge_into_save`, record rev + 1, only the owner-writable bytes copied into `Save homes[h]`, seq + 1, ACK APPLIED, CANON_PUSH to every other peer. Any rejection except BUSY / RATE_LIMITED / NOT_OWNER changes nothing and rolls the owner back to the last known good pair (a FULL record push, un-paced, + a CANON_PUSH of the house). Persistence: nothing new -- house and record mirror live in the host save and are written together by the normal host save.

Client (`HOUSE CLIENT` block): `canon[h]` (last state known to agree with the host) / `stash[h]` (newest push not yet written) per house, own house DIRTY = FNV of its owner-writable bytes != canon. A push is digest-checked, must be newer than the last one of that (session, house) and must carry the local owner of that house; it is stashed and written into the IN-MEMORY save (never persisted, never the mailbox / haniwa) only when `pcnetgame_local_house_unsafe(h)` is false (no GAME_PLAY, any fade / wipe, or a room scene of house h) -- a visitor inside a room of a house that is not its own gets it through the live rebuild of Stage 1b when the room is quiescent, else on its next entry. While the own house is dirty / its commit is in flight / its first push is outstanding / the player is inside it: the plain record upload (`pcnetgame_crec_upload_deferred`), pocket transactions (`pcnetgame_txn_begin_blocked`) and the adoption of a FULL record push (`pcnetgame_crec_adopt_blocker`, counted as scene-lifecycle time by the adoption watchdog) are held. The pair is sent from `pcnetgame_crec_tick` before the plain upload as soon as the player is outside the house and the record is SYNCED with nothing pending; on APPLIED the record base and canon move together. BUSY / RATE_LIMITED retry after 2 / 3 s. Quit flush: a dirty house outside the room is committed first; if the player quit inside (or the house was never synced) the plain record flush is SKIPPED so the pockets of an unsent edit are never uploaded alone. canon / stash survive a reconnect of the same local player.

Deviations from the design note (each chosen for the SAFER behaviour):
1. A push that arrives while the own house is dirty is NOT adopted over the edits (the design said "adopt, discard local edits"): the pockets would keep the picked-up item while the room got it back = a duplicate. It is stashed; the owner's commit is then STALE, the host rejects both and rolls the pair back, and the FULL record adoption restores the stash (else canon) into the save in the same call (`pcnetgame_house_client_on_full_adopt`). A race with a host-originated change therefore costs the edit, never an item.
2. After a refused commit the next FULL record push must replace the client-owned ranges even when it carries the lineage point the client already holds (otherwise `pcnetgame_crec_try_adopt` would take host-owned ranges only and the pocket edit would survive the rollback): `s_hcl.rollback` / `pcnetgame_house_client_force_full_adopt`.
3. `RSV_FE1F` (the multi-tile furniture marker) MAY change in a commit (the design said reserved ids must equal the host's, which would forbid moving a multi-tile furniture); every other non-furniture / non-item id is structural and immutable.
4. House rejections bypass the 2 s FULL-push pacing (`rec_last_stale_push_ms` is cleared): the shared commit attempt window already bounds them.
5. Guests are never pushed a house and never own one; a client that does not receive a usable push of its OWN house within 20 s disables the gates for the session (loud log) instead of holding record uploads hostage.
6. All four turnip ids are one item class (a 10 -> 100 swap passes conservation; the wallet / bank are not validated against gains by this feature either).

Known limits: the first-join MIGRATE of a resident (host rev 0) imports the CLIENT record while the HOST house wins, so furniture moved offline before the first join can duplicate once; since Stage 1b a quiescent owner commits from INSIDE the room (a crash inside the room loses at most the edits since the last quiescent commit, never an item), and a visitor inside the room sees a live rebuild when it is safe (otherwise on re-entry); the host's own player inside a house still defers everything (see Stage 1b); the dirty test runs on the BE image of the whole home every 200 ms (cheap, not profiled); entering and leaving a room without changes may still produce a (no-op) commit if the room teardown rewrites the layers differently (the host accepts it: equal multiset, record rev + 1); the design's "poll 30 frames while in the room" was first dropped (nothing can be read from the save while the room is live) and is now implemented differently: Stage 1b snapshots the live room and commits when it has been quiescent for 30 frames.

Tests: `pc/tools/net_spike/test_house_sync_protocol.py` (REAL host process `--house-sync` + scripted FakeClients; run in a disposable copy of `bin_fixture4`: HOST_CONFIG bit, initial pushes vs the GCI, pickup / placement / rotation pairs APPLIED, the other client's push, owner not echoed, duplication -> CONSERVATION with rollback pair, wrong house -> BAD_SHAPE, stale house base after a host-originated change (`--house-test-host-edit`), stale record base, BAD_DIGEST, INVALID_CELL, guest NOT_OWNER, BUSY (`--house-test-host-in-house`), partial 16/17 + disconnect, reconnect pushed the committed seq, graceful stop and the GCI holds house + pockets as one pair) and `test_house_sync_src.py` (SOURCE AUDIT: ids / sizes / constants, gates, ten-step order, every client save write behind `pcnetgame_local_house_unsafe`, guest before `homes[]`, defaults). The older source audits that pinned the literal highest id 58 now pin 61 (`wire_baseline.EXPECTED_MAX_MSG_ID`); `wire_baseline` pins the three new structs and lists the two local structs host-only.

NOT tested by any process test: the real CLIENT half (dirty test, unsafe-scene gates against a live room, stash writer, commit trigger, quit flush) and the real conservation of drawer contents / mannequins / music boxes / wallpaper exchanges. Manual verification still needed (two real instances, host with `--house-sync`): owner picks up and places furniture (incl. a multi-tile one and one with items in a drawer), leaves, the commit log line appears and a second client entering afterwards sees it; wallpaper / carpet change and music box insert / eject; picking up a my-design mannequin / umbrella; quitting while inside the room; host kill / restart with a dirty client; the host's player inside the owner's house (BUSY then retry); turnip spoilage in a house; an owner standing in its house while the host edits it; a visitor standing in the house when the owner commits (stash applied after it leaves); reconnect with a pending edit; `--dedicated` host; basement / upper floor houses; a conservation false positive (any legal vanilla action that makes the host answer CONSERVATION is a bug in the class normalisation).

#### Furniture synchronization: Stage 1b (live room: in-room owner commits, live apply for a visitor)

Correction of the Stage 1 text above: the live room does NOT work on a copy. `mFM_SetFgUtPtoHomeInfo` and `fg2_p` (`m_field_make.c`) alias `Save_Get(homes[h]).floors[f]`; `aMR_GetLayerTopFg` returns pointers into the save. What is NOT in the save while the room is live: `aMR_MakeItemDataInFurniture` moves the contents of every storage furniture (drawers, wardrobes, the disc of a music player) out of the upper layers into `ftr_actor->items[]` and leaves those cells EMPTY, and `aMR_ClearSwitchSaveData` zeroes `ftr_switch`; `My_Room_Actor_dt` writes everything back (`aMR_SaveSwitchData`, `aMR_KeepItem2Fg`). The save is therefore not readable as a house while a scene of that house is live (`pcnetgame_local_house_unsafe` is unchanged and still gates the stash writer, the FULL adoption, the record upload and the pocket transactions). Everything below is TARGET_PC and only active while furniture sync is in effect (`pc_net_game_house_sync_active()`); single-player and an unsynchronized session are unchanged. No wire change.

Owner, live commit (client): `aMR_pc_export_home(out, house, &floor)` (`ac_my_room.c`) builds "what the teardown would write at this instant" on a COPY of the save's house: for every used actor the logic of `aMR_KeepItem2Fg` (contents back into the upper layers at the actor's origin cell), `ftr_switch` like `aMR_SaveSwitchData`, `haniwa_step` like `aMR_SaveHaniwaStepData`; `tempo_beat` stays the save's (the audio state is not read). It refuses unless the field's layer pointers alias the save. The live room is never modified. Without this merge a commit would omit the drawer contents and the host would answer CONSERVATION (edits lost, nothing duplicated), so it is mandatory. A commit is only built while the room is QUIESCENT: game side (`pcnetgame_hcl_live_room_house`): GAME_PLAY running, no fade / wipe, a player-house room scene, the OWN room (`Common field_type == PLAYER_ROOM`), submenu fully idle, no demo, message window hidden, the player in WAIT / WALK / RUN / DASH with no pending main-index request; room side (`aMR_pc_room_quiet`): `state == 0`, no demo, `msg_type` / `requested_msg_type` NONE, no forced-open demo, no emulator request, no bgm reservation, no throw lock, no pickup / picking-up / leaf, no parent furniture, no reserved furniture (`rsv_ftr[]`), every used actor in `aFTR_STATE_STOP` with `demo_status == 0`, wallpaper / carpet changers idle. All of it must hold continuously for 30 game frames (about 0.5 s) and again in the very call that builds the snapshot. Dirty = the digest of the snapshot's owner-writable bytes (with `haniwa_step` and `tempo_beat` masked: they drift on their own) differs from the canon image's; minimum gap 2 s as before; the snapshot, the record and the pair are built in the same call. Gate changes: `pcnetgame_house_client_commit_tick` no longer stops at "unsafe" but at "unsafe AND NOT in-room-commit-ok"; the plain record upload, the pocket transactions, the FULL adoption and the stash writer stay blocked inside the room (the records still only travel as a pair). On APPLIED the canon moves to the snapshot, exactly like an outside commit. After any STALE / refusal (or a failed pre-check) no further in-room commit is tried until the owner leaves the room; the existing rollback at exit is unchanged. Client pre-check: at room entry the class counts of (house, pockets) are captured as a baseline, only when house and record are settled and equal to what the host holds (not dirty, SYNCED, nothing pending, acked record digest equal); before each in-room send the host's own conservation rule (`pcnetgame_house_conserved`) is applied to (snapshot + pockets) vs the baseline, a mismatch sends nothing and stops in-room commits for the visit; the baseline moves to the committed pair on APPLIED. If the baseline could not be captured, in-room commits are not armed and the edits travel when the owner leaves, as in Stage 1.

Visitor, live apply (client): a stashed push for a house that is NOT the local player's own is applied while the visitor stands in that room when the room has been quiescent for 30 frames (same terms, owner-agnostic). If the live floor's four layers already equal the new ones, every other part is written and the live floor's bytes are left alone (the drawer contents are in the actors). Otherwise `aMR_pc_live_reload(game, cb, ctx)` rebuilds the actor set in place: (1) a copy of the destroy half of `aMR_AllFurnitureDestruct` WITHOUT `aMR_KeepItem2Fg` (collision registry, music / radio, weight, `dt_proc`; `used_list` cleared) -- the write-back must not happen, otherwise the old drawer contents would be written into the new layers = duplicates; (2) `aMR_InitFurnitureWork / Table / ExistTable / BankTable / InitHaniwaOnTable`; (3) the callback writes the pushed house into the save (the owner of the image is checked BEFORE the destroy); (4) the tail of `My_Room_Actor_ct`: furniture actors of layers 0 and 1, parent furniture reset, `aMR_MakeItemDataInFurniture`, `aMR_DeleteMusicWhichMusicBoxDontHave`, bgm reset, `aMR_ClearSwitchSaveData`, `aMR_OneMDFurnitureSwitchOn`. Heap, banks, camera and the player are untouched. Deferred (the push stays stashed and is applied on exit / re-entry as before) while anything of the quiescence is false, or when the new layout puts furniture on a cell within one unit of the visitor. The visitor's local copy of another player's house is never committed anywhere and a client never persists the save, so a failure of this path can cost the visitor a glitch or a crash, never an item of the host or of the owner. Never for the visitor's own house (a push for the own house stays stashed and is written after the room is gone, as before). The host's own player inside a house is unchanged: the host answers an OWNER_COMMIT for that house BUSY (this reply now comes BEFORE the rate-limit accounting -- a latent bug: `rec_win_count++` ran first, so an owner retrying against a host standing in the house could reach RATE_LIMITED and a peer drop), and the host's own live edits in a house it stands in are not pushed to visitors until it leaves (the host would need a snapshot export of its own `cur`; left as a limitation).

Tests: `test_house_sync_src.py` (source audit, extended: destroy-without-write-back precedes the save write, never for the own house, the quiescence terms, the export works on a copy, BUSY precedes the rate accounting, in-room commit only under sync, the pre-check precedes the send) and `test_house_sync_protocol.py` (P1, P2, re-run once on the new executable: nothing regressed in the host half). Fidelity hook: `--house-test-fidelity` (TEST-ONLY, default off) makes `My_Room_Actor_dt` export before the teardown and log `[NET][HOUSE][TEST-ONLY] fidelity house H floor F: MATCH / MISMATCH` afterwards (cells, `ftr_switch`, `wall_floor`; `haniwa_step` reported separately because the teardown does not write it). Not run: a room cannot be entered without UI input, and no process test drives one, so the merge has NOT been validated against a real room.

UNVERIFIED (read from code only, never run in a real room): that the pickup / placement / drawer / disc actions all end in the quiescent states listed (the client pre-check and the host's conservation are the net: a miss costs the in-room commit, not an item); `parent_ftr` / `fit_ftr_table` state after a rebuild that follows a move; the redraw of layer-0 ITEM1 items (`mFI` draws them from the grid) after the layers were replaced; goki / gyroid actors that reference furniture ids across a rebuild; stale `bed_ftr_actor_idx` / `demo_ftrID` / `emu_ftrID` after a rebuild; a one-frame hitch and a bgm blip (stop + restart of a playing music player) at a rebuild; the push-out of the visitor next to newly registered collision (only the 3x3 cells around the visitor are checked); wallpaper / carpet are NOT refreshed by a rebuild (the indoor actor keeps its textures, they appear on re-entry); the host's own player inside a house (see above); whether a commit built right after a pickup finished (30 frames later) can still meet a host that standing in the house answers BUSY for a long time (retries every 2 s, harmless).

Manual tests required (two real instances, host with `--house-sync`, disposable save copies): owner places / picks up / moves furniture incl. a multi-tile one and a dresser with items inside while a second client stands in the same room, with and without `--house-test-fidelity` on the owner (MATCH expected at every exit); owner stays inside several minutes and the host shows the in-room OWNER_COMMIT APPLIED lines; leaving and re-entering shows the same room; owner changes wallpaper / carpet and inserts / ejects a disc inside the room; the visitor walks, then stands next to the spot where the owner places a piece (deferred, applied after it moves) and sits / lies in bed during an owner commit (stash applied after); a STALE inside the room (host edit during the visit) stops in-room commits and the rollback at exit restores the host's state; quit while inside the room; host standing in the owner's house (BUSY then retry, no peer drop).

### Player-house collision (Stage 2)

`pc_remote_player_collide_eval` now also arms the puppet pipe in PLAYER_HOUSE interiors (same scene id + owner, same stale / door / demo guards as the other shared interiors) but ONLY while furniture sync is in effect for the session: `pc_net_game_house_sync_active()` (host: `--house-sync`; client: the host announced it in HOST_CONFIG and the client was not disabled). Without sync each process builds the room from its own save, so the pipe stays disarmed there (puppet may stand in a piece that only exists on the owner's side). Shops / museum / other interiors are unchanged. No wire or save change. Tests: `test_interior_collision_src.py` (7/7) and `test_building_interactions_src.py`, source audits only; house shove feel not verified visually. The Stage 1 client half (dirty test, unsafe-scene gates against a live room) has no real-process test, so house collision is only as trustworthy as that.

## Host admin item tools (dedicated console: `finditem`, `iteminfo`, `items`, `give`)

Host-only, typed into the `--host --dedicated` console (`help` lists them). They run on the game thread (the stdin reader only queues lines). No wire change: no new message, no protocol bump.

### Syntax

| Command | What it does |
|---|---|
| `finditem <text> [page]` | case-insensitive substring search over the item names. A trailing all-digit word after at least one other word is the page (`finditem dresser 2`); a single word is always the search text. Text up to 40 characters. |
| `iteminfo <id>` | name, category and whether the item can be given (and why not). `<id>` = `0x2203` (hex) or decimal `8707`, range 0..0xFFFF; anything else (`0xZZ`, `-5`, `0x`, 99999999, a number that is not a pocket item) is rejected with `Invalid item ID`. |
| `items <furniture\|tools\|clothing\|wallpaper\|carpet\|miscellaneous\|all> [page]` | lists a category, 15 per page. |
| `give <player> <id> [qty]` | puts `qty` copies (1..15, default 1) of the item in the player's FREE pockets. `<player>` = a resident or guest name (case-insensitive, spaces allowed), `peer <N>` (a connected player, see `players`) or a `"quoted name"`. |

### Output

```
ITEM SEARCH: "dresser" - 16 results
0x1034 - Jingle dresser - Furniture
...
Page 1/1
ITEM SEARCH: "zzz" - no results
ITEMS (furniture) - 1266 results         (then 15 lines, then "Page 1/85 - next: items furniture 2"; a page past the end says it does not exist)
ITEM INFO: 0x2200 (8704)
  name: net / category: Tools / giveable: yes (...)
GIVE: Angelica received item 0x2200 (net).             ("... (axe) x3." for qty 3; a note line when a rotated furniture id was masked)
GIVE FAILED: Angelica's inventory is full. No items were given.
GIVE FAILED: Unknown player 'Nobody'.
GIVE FAILED: Invalid item ID 0x2100 (money bags are not given by the item tools). No items were given.
```

Names are the game's own (lower case, as the game prints them). They are 16 game-font codes, NOT ASCII: the console folds them through a 256-entry table (accented letters lose the accent, symbols become `?`). Entries whose name table slot is blank/"unknown" show `(unknown name)` and are not giveable. A list line ends with `[not giveable]` when the item exists but `give` refuses it.

### What can be given

Anything a pocket can legally hold (the same rule the host applies to uploaded records: `pcnetgame_is_pocket_legal_item`) EXCEPT: money bags, tickets (the month is encoded in the id), my-design umbrellas / mannequins (they need a player design) and ids the game has no name for. Furniture ids carry the rotation in the low 2 bits: a rotated id is masked to the unrotated piece and the console says so. `qty` = number of pocket slots to fill (pockets have no stack counts); filled slots get the NORMAL item condition. Paper stacks use different ids for 1..4 sheets (the names repeat).

### Failure behaviour (all-or-nothing)

`give` either fills all `qty` slots or changes nothing and says why; there is never a partial gift. Refused (with a `GIVE FAILED: ...` line): unknown / ambiguous player (use `peer <N>`), the host's own resident, an invalid or non-giveable item, a bad quantity, fewer than `qty` free pockets, a record that has never synced with this host (rev 0: the first join would adopt the client's record and the gift would vanish), a connected player that is not idle (record sync / house upload in progress) or not outside in the town (e.g. inside a house: keeps the owner out of the house-sync conservation window), guests when `guests.dat` is untrusted, and an offline guest whose `guests.dat` write fails (rolled back).

### How it is delivered

Pockets belong to the client, so the host writes its mirror record through the same sanctioned writer the transactions use, bumps the record lineage (`rev`, `last_pocket_rev`: a client transaction built on the old image becomes STALE_IMAGE) and, for a connected player, restarts a PUSH_FULL record push (the client adopts any push newer than its base). An offline resident / guest receives it at the next join (the bumped rev makes the host push its record). Residents are saved with the next (early) host save; offline guests immediately through `guests.dat`.

Known costs: when the client adopts the push it discards its own unsynced client-owned edits since the last accepted upload (about 2 s, logged `[discarded local client-owned edits]`): a loss, never a duplicate. A client that cannot adopt for 10 s is dropped by the host, like for every push. A stale record upload / commit after a give is answered STALE_BASE / STALE_IMAGE and the gift is kept. House-sync conservation is not changed: a house OWNER_COMMIT whose record rev is older than the gift is STALE (rolled back on the client); the "in town and no open house upload" gates narrow that window.

### Limitations

- The categories are INCOMPLETE. Only furniture, tools, clothing, wallpaper and carpet map cleanly onto the game's tables. Fish, insects, fruit, plants, paper, music, diaries, lucky bags, turnips, "etc" (fossils, presents, spirits, signs...) and everything else are only in `miscellaneous` / `all` (the category column then shows the table, e.g. `Fish`). Sub-kinds the game does not store in a table (gyroids, fossil models, mannequins, tool sub-kinds) cannot be filtered. Search with `finditem` instead.
- Furniture forms of fish / insects / tools / clothing resolve to the inventory item's name (that is what the game's name function does).
- Name resolution: residents and the ACTIVE guests of the current host town (guests of other towns are inactive); a name shared by several entries is refused as ambiguous. Non-ASCII characters of a player name print as `?` and cannot be typed; use `peer <N>` for those.
- Nothing here changes bells (wallet, bank, loan), teleports a player or touches houses.

Tests: `pc/tools/net_spike/test_admin_items_protocol.py` (REAL dedicated host process on the disposable `bin_fixture4` copy + a scripted FakeClient resident + the stdin console) and `test_admin_items_src.py` (source audit).

### Live furniture sync: stale `picking_up_flag` fix (manual report)

Manual two-client test: a placed dresser did not appear live for a visitor, and a picked-up dresser stayed visible for the visitor. Root cause found by code tracing (not by a real-room run): vanilla sets `pickup_info.picking_up_flag` when a pickup's shrink animation ends (`ac_my_room_move.c_inc` ~2551) and never clears it (it is only read inside the pickup states), but `aMR_pc_room_quiet` tested it, so after the FIRST furniture pickup of a visit the owner's room was never "quiet" again: no in-room OWNER_COMMIT, hence no CANON_PUSH for any later edit (a ghost on the visitor, and a placement that only travelled on exit). Fix: `aMR_pc_room_quiet` no longer tests `picking_up_flag` (`pickup_flag` already covers the whole pickup). The visitor's rebuild chain (compare, write order, destroy-without-write-back, anchor-cell actor creation incl. RSV_FE1F fillers and layer 1) was traced and found correct; the draw loop iterates `used_list`, so a rebuild without the dresser cannot leave a ghost. Second possible delay: the visitor's "new furniture within one cell of me" deferral (retried every tick, applies when the visitor steps away); its log now has its own counter (1 line per 32) instead of the 16-lines-per-session cap. Not changed: a switch-only change (lamp toggle) is not shown live. Tests: `test_house_sync_src.py` 64/64 (new check that `picking_up_flag` is absent from the quiet test). Real-room behaviour NOT verified; to separate causes in a manual run look for `OWNER_COMMIT ... APPLIED` in A's log and `live apply of house ... deferred` in B's.

## Dedicated host defaults

A dedicated server is started with the one normal command:

```
AnimalCrossing.exe --host 7777 --dedicated
```

For `--host P --dedicated` (and only then) these services default to ON, without any further flag:

* **Furniture / house sync** (`g_pc_house_sync`, HOST_CONFIG byte 1 bit 0). Override: `--no-house-sync` (`--house-sync` is still accepted and means the same as the default).
* **Town serving, AUTO = ON / sanitized** (`town_serve = auto`): `--town-fetch` clients get a SANITIZED copy of the town (other residents' pockets / mail / diary / designs / villager letters blanked, the same image as `--town-serve on`). Override: `--town-serve off|on|full`, or `settings.ini` `[Network]` `town_serve = auto|off|on|full` (also `0|1|2`); the CLI wins over settings.ini, and an explicit settings.ini value wins over auto. A fresh game-written settings.ini contains `town_serve = auto`.
* **Personal (diary) sync** stays `auto` and follows town serving, so it is ON as well (HOST_CONFIG byte 1 bit 1); `--town-serve off` therefore also turns it off unless `--personal-sync on` is given.

A non-dedicated `--host` keeps every one of these OFF (unchanged).

**Privacy note.** The town that is served by default is the sanitized one: nothing a resident keeps private leaves the host. Resident names / PersonalIDs are still in it, so anybody who fetches the town could claim a resident name while `resident_tokens` is off. **`resident_tokens` is still recommended**: `AnimalCrossing.exe --host 7777 --dedicated --resident-tokens tofu` (or `settings.ini` `resident_tokens = tofu`).

**Startup log line**, printed once at launch of a dedicated host:

```
[PC] dedicated host services: house sync ON (default for --dedicated; --no-house-sync disables), town serve ON (sanitized) (default for --dedicated; --town-serve off disables)
```

The line is printed after settings.ini is loaded, so it reflects a `town_serve` set only in the file (this was a cosmetic defect found by the first test run and fixed).

Test: `pc/tools/net_spike/test_dedicated_defaults_real.py` (REAL dedicated hosts + FakeClients + one REAL guest client on disposable `bin_fixture4_dfl` / `bin_fixture4_dflc` copies: defaults, `--town-serve off`, `--no-house-sync`, settings.ini off / auto / fresh file, non-dedicated unchanged).

## Client auto-reconnect

Root cause: when the host link died (5 s silence, retransmit budget, DISCONNECT) the client only reset its session state, freed transport slot 0 and stayed in role CLIENT with a closed slot: nothing ever sent a new HELLO, so a host restart (or a network drop) left the client permanently inert until the process was restarted.

Lifecycle used (NO `pc_net_game_shutdown()`, NO wire change, protocol version unchanged): the socket and role stay; only the transport slot is restarted. `pc_net_client_restart()` (pc_net.c) re-arms slot 0 as PENDING toward the saved host address with a fresh nonce on the SAME local UDP port, so a host that still holds the stale slot sees "same address, new nonce" and replaces it at once. Cancelling an attempt = `pc_net_disconnect(PC_NET_INVALID_PEER)` (sends DISCONNECT, frees slot 0, queues no event).

State machine (`s_rc` in pc_net_game.c, client only, set up by `pc_net_game_start_client`): IDLE -> (link lost) WAIT (backoff 1, 2, 3, 3, 3 ... s (was 1, 2, 4, 8, 15 ... until the dev-restart UX change)) -> ATTEMPT (`pcnetgame_client_begin_attempt()` + `pc_net_client_restart()`, 4 s to become transport-connected, then cancelled and back to WAIT) -> after CONNECTED there is NO game-level timeout (the host may park the claim or wait for its save); a disconnect during HANDSHAKE counts as a failed attempt with backoff -> READY -> IDLE ("Reconnected" shown 3 s). The backoff index resets once the record reaches SYNCED (or, as a safety for guests whose record machine may not reach SYNCED, after 20 s of continuous READY). Ticked in `pc_net_game_poll` right after the event loop; an explicit `pc_net_game_shutdown()` zeroes the state (log `intentional shutdown -- no reconnect`).

Deliberate deviation from the spec (safer): the machine is armed only after the client has been READY at least once in the process. Before the first READY nothing changes (join messages, `pc_net_game_shutdown()` on refusals, no retry), so the version-mismatch / identity-validation / guest join paths are untouched, and a host that was never joined (legacy transport, wrong build) never causes an endless retry loop.

Permanent (no retry): host REJECT (any reason), IDENTITY_ACK protocol/town mismatch, guest-token mismatch, local town changed under a READY link, 3 consecutive ADOPT_FAILED link closes. Before the first READY these still call `pc_net_game_shutdown()` (byte-identical). After a READY the client does NOT shut down (ROLE_NONE mid-game would lift the client "no save" rules): `pcnetgame_client_refused()` logs, closes the link, resets the session state, destroys the puppets, state GAVE_UP, role stays CLIENT, persistent notice. Temporary: transport timeout, DISCONNECT, retransmit budget, close during HANDSHAKE, attempt-window timeout, and the 60 s unresolved-transaction cap (now `pc_net_disconnect` + `pcnetgame_client_on_link_lost("txn unresolved")` instead of a shutdown).

Kept across a reconnect (never reset): the local save / Now_Private, `s_crec_last` (record lineage: the RECORD HELLO carries have_last=1 and the host wins), `s_hcp` house canon/stash, txn nonce/seq, catch id/outcome, wildlife generation, clock offset, last appearance, `s_client_gtk` guest token, `s_local_world_latched`. Reset by the existing session reset: handshake, guest claim flags, pending pickup/drop/bury/field/catch, `s_ctxn` (cleared, never replayed: pockets are never mutated before APPLIED), mail / town-service / mailbox state, world seqs, snapshot, NPC rings/scenes, the record machine, the house session (`s_hcl.known=0` until the client's own CANON_PUSH arrives). `s_client_wildlife_mode` returns to unknown until HOST_CONFIG arrives. Remote players: on link loss `pc_remote_player_shutdown()` replaces `on_disconnect(HOST)` (a restarted host's MOVE `sender_frame` restarts low and a surviving slot would reject samples as stale; relayed ids may change); puppets come back through the normal on_ready / relay paths (static slot table, no re-init needed; same destroy path as on_disconnect).

House / in-room: nothing is auto-committed at disconnect. After a reconnect `s_hcl.known=0` holds uploads / txns / adoption until the CANON_PUSH; in a room the first room tracking sees `entry` with known=0, so in-room commits are not armed and edits go out at exit against the old canon base; a newer host copy gives STALE + the existing rollback. Taken from the design; not exercised by a real run.

UI: `pc_net_game_client_reconnect_status()` -> `pc_net_notice_draw`: "Connection lost - Reconnecting... (attempt N)", "Reconnected" (3 s), "Disconnected from host (refused) - restart to rejoin" (persistent). It wins over the join message and the generic "Not connected" notice; the "No answer ... still trying" and "closed before you could join" messages are suppressed once armed.

Log lines (prefix `[NET][RECONNECT] client:`): `host link lost (cause=...) -- retry N in MS ms`, `attempt N: HELLO to ip:port`, `attempt N timed out`, `reconnect successful (attempt N, offline MS ms) -- identity re-established (resident|guest: resident R | guest slot S)` (guests: logged when the IDENTITY_TOKEN arrives, since that carries the slot), `permanent refusal (reason) -- giving up, staying offline`, `intentional shutdown -- no reconnect`. No per-frame logging.

Limitations: the cause of a transport loss is generic (the transport does not say why); a REJECT such as SERVER_FULL after a READY is treated as permanent even if it might have been transient (a host normally absorbs a stale slot through the nonce-restart path); GAVE_UP needs a process restart; the on-screen notices were not visually verified.

Tests actually run (disposable copy `pc/build64/bin_fixture4_reconnect`, ONE execution of `pc/tools/net_spike/test_client_reconnect_real.py`): 18/18. T1 resident: host killed, `host link lost`, 3 attempts with delays 1000/2000/4000 ms, client alive, host restarted on the same port + save -> `reconnect successful ... resident 1`, `HELLO sent (have_last=1`, exactly one host puppet created after success, no CANNOT JOIN / violation. T2: host restarted with `--bootstrap-resident 1` -> REJECT -> `permanent refusal (host REJECT)`, no further attempt for 30 s, client still running. NOT run: the guest variant, test_reconnect_state.py, test_g7_disconnected_client_gates.py, test_version_mismatch.py, test_identity_validation.py, the full suites, any in-room / house reconnect, a UI screenshot. Manual tests to do: join as resident and as guest, kill and restart the host mid-play (watch the notice, puppets, inventory, an open house), cut the network for 3 s and for 30 s, restart the host with a different town (expect the refused notice), quit the client during a reconnect.

## Characters and town memberships

Status: M1 (character store) + M2 (membership lookup, per-town tokens) are implemented; M3 (servers.ini + `--server`) and M4 (Play Online title menu; connects in this process since "Play Online without relaunch", it was a relaunch before) are implemented; M2b is not (see "Remaining").

M-C (phase 2): Play Online now connects with the `--town-fetch` flow (first as a relaunch, now in-process: see "Play Online without relaunch"), resolves `membership.ini` after the fetch (resident -> bound by PersonalID, guest / none -> guest arrival or first-run creation), writes `membership.ini` + servers.ini `last_town` at READY, and shows Retry / Use saved copy / Quit boxes on fetch failures (M-J renamed the middle button: it is NOT offline play). Details and limits: docs/multiplayer-phase2-design.md, "M-C".

M-J (phase 2): a resident membership whose character is no longer a resident of the fetched town (removed or town reset) now ends with a Quit box and exit 3 instead of a silent title screen (the membership file is kept); the character list marks `(resident)` for the selected server's last known town; the old "Play offline" button is "Use saved copy" (it still connects in the background); small layout fixes of the Play Online screens after a screenshot pass. Hostnames in servers.ini are still not supported (IPv4 literals only; see design doc, "M-J").

**Player-owned vs town-owned.** A *character* is the player-owned portable seed of a guest: `name`, `gender`, `face`, `home_town`, `player_id`, `land_id` (+ a local-only `uuid`).
Everything else is **town-owned per membership** and host-authoritative: pockets, item conditions, wallet, loan, bank, lotto, equipment, mail, quests, catalog, museum, maps,
calendar, events/flags, house, mailbox, villager memories/friendship (keyed by the character's PID). Ambiguous items are town-owned for now: cloth/shirt (seeded from the
identity hash, then changes in play), Able designs, birthday, animal memory/remail, sunburn. An imported resident's name may contain non-ASCII game font codes, so the
raw `name_bytes` (hex) are stored next to the display name.

**Identity rules.** No wire change. The wire identity is the existing guest *home PersonalID* (name, home town "GuestVil", CSPRNG `player_id` + `land_id`, sent in
IDENTITY_EXT, immutable). The `uuid` (128-bit CSPRNG) exists only on the player's PC (directory name / registry); it is never sent and never derived from an IP or a
guest slot. Each membership has its own `town_pid`: for a guest membership it equals the home PID; for a resident membership it will be that resident's vanilla
PersonalID (recorded only; resident authentication is M2b).

**Storage** (`save/mp`, atomic writes, a corrupt file is never overwritten, moved or deleted):

    characters/<uuid32hex>/character.ini                   uuid, name, name_bytes, home_town, home_town_bytes, player_id, land_id, gender, face, created, [legacy_profile]
    characters/<uuid>/towns/<townkey>/membership.ini       role=resident|guest, town_pid (hex, 20 B), last_server (UI hint only)
    characters/<uuid>/towns/<townkey>/token.dat            the existing PCMpGtk file format holding exactly ONE entry
    characters.ini                                         default=<uuid> (last used)

The registry is the directory scan (no index to corrupt). `townkey = <host land name 16 hex>_<land_id 4 hex>_<terrain_hash 8 hex>`: the host *town* identity, known before
connecting because LAND_MISMATCH forces the local town to equal the host's. It is not the server address (an address can host another town tomorrow); `last_server` is
only a hint. One token file per (character, town) avoids the defect of the legacy `guest_token*.dat`: `pc_mp_gtoken_put` silently recycles the oldest of its 4 slots
when a 5th town is visited.

**Migration is an adapter, never a move.** `guest.ini`, `guest_<name>.ini` and `guest_token*.dat` keep working byte for byte. They are listed as LEGACY characters
(`--characters`). `--character-import-profile NAME` (NAME `""`/`default` = guest.ini) creates `characters/<uuid>` with the same identity, copies the matching token entries
to `towns/<key>/token.dat` (+ `membership.ini`), records `legacy_profile`, and refuses if it already exists or the legacy token file is unreadable; the legacy files are only
read. After an import `--guest-profile NAME` (or plain `--guest` for the default) resolves to the character; otherwise to the legacy file; otherwise first-run creation as
before. Token lookup: store file first, then a read-only fallback to the legacy token file (copied forward on a hit).

**CLI.** `--character NAME|UUIDPREFIX` (implies `--guest`; client only, needs `--connect`, not with `--guest-profile`; an unknown NAME starts the Rover first-run creation
which writes `characters/<uuid>/character.ini`), `--characters` (list, exit 0), `--character-import-profile NAME` (import, exit 0). Resolution order for `--character`:
imported `legacy_profile`, exact name (ambiguous => refused), uuid prefix (>= 4 hex), legacy profile file.

**Membership lookup** (`pc_mp_membership.h`, pure): `(host town, PID)` is NONE / RESIDENT / GUEST / AMBIGUOUS; the same PID can be a resident of town A and a guest of town B.
The guest table (`guests.dat`) is already keyed by (host town, home PID). The dedicated console command `members` lists residents and guests (kind, slot, confirmed).
Host admission (`pcnetgame_host_process_identity`) is deliberately not rewired.

**Session.** `pc_session.h` holds the minimal connect session (role, host, port, join kind, selected character + storage LEGACY|STORE). For a STORE character the token file is
chosen per host town (`pc_session_select_town` installs it as the `pc_guest_token_path()` override); for LEGACY nothing changes.

**Remaining (after M3 / M4):** M2b resident token authentication; guest -> resident promotion (blocked by town transfer); per-server town save dir; hostname resolution; in-process role start (see below).
Known gaps of M1/M2: the title-menu "Join as Guest" item refuses a STORE session (use the CLI or Play Online); a few creation log lines still print the legacy profile
path for a STORE character; `membership.ini` is written on import only (no reader yet).

### M3: saved servers (`save/mp/servers.ini`) and the connect descriptor

A *server profile* is only a destination; characters are NOT tied to servers (`last_character` / `last_town` are UI hints). Format (repeated sections, `#` / `;` comments; `pc_settings.c`
ignores sections so `pc_servers.c` has its own parser):

    [server]
    name = Friends
    address = 192.168.1.20
    port = 7777
    last_character = Roger
    last_town = Foo

Rules: name 1..32 printable ASCII without `[ ] = " \` and without leading/trailing space (unique, case-insensitive); `address` = an IPv4 dotted-quad literal ONLY (`pc_net.c` uses `inet_pton`, there is
no hostname resolution, so a hostname is refused at save time instead of failing later); port 1..65535 (default 7777); at most 32 entries. Writes are atomic (tmp + replace). A file the parser cannot
read (unknown section / key, bad value, duplicate name, missing name / address, > 32 entries, > 64 KiB) is CORRUPT: it is reported, every write is refused, and it is never overwritten, moved or deleted.

CLI (`pc_main.c`, `pc_session_apply_server()` fills `pc_session()->host / port / server_name`):

* `--server NAME` - connect to the saved server (fills `--connect HOST:PORT`, role CLIENT). Exit 2 + usage for an unknown NAME, a corrupt servers.ini, or when combined with `--connect`, `--host`,
  `--dedicated` or `--host-observer`. It implies `--guest`: with `--character NAME|UUID` (or `--guest-profile NAME`) that character plays, ALONE it connects as the default guest profile exactly like
  `--connect HOST:PORT --guest` (least surprising: it never invents a character). With `--character` the server's `last_character` hint is updated at start.
* `--servers` (list, exit 0), `--server-add NAME HOST[:PORT]`, `--server-delete NAME` (one-shot, exit 0, or 2 on a refusal). Legacy `--connect HOST:PORT [--guest|--guest-profile|--character]` is unchanged.
* `last_town` is NOT updated (it would need the READY handshake in `pc_net_game.c`; skipped to keep that file untouched).

### M4: Play Online (title menu)

Title menu item "Play Online" (`ac_animal_logo.c`, `pc_play_online_menu.c`), shown only when no network role is active (not `--connect`, not `--host` / `--dedicated`; a client keeps "Join as Guest").
Flow: Server list (saved servers, "Add server", "Back") -> per server Connect / Edit / Delete (with Keep/Delete confirm) / Back -> Character list (local store characters + legacy guest
profiles not yet imported + "New character" (asks for a name) + Back; the server's `last_character` is preselected) -> Connect. Navigation is pad/keyboard like the settings menu. Add / Edit / New character use a
text-entry mode: `pc_main.c` routes `SDL_TEXTINPUT` / `SDL_KEYDOWN` / controller A,B to the menu while it is active (Enter = next / OK, Esc or pad B = cancel), the pad driver stands down meanwhile (same
pattern as the keybinding capture). Add = name -> address -> port, validated field by field with the same rules as `--server-add`.

**Superseded decision (kept for the history): relaunch, not in-process start.** The first M4 cut assumed the role is fixed before init and RELAUNCHED the executable on Connect; the role reads turned out to
be call-time reads, so Connect now runs in-process (next section). The relaunch machinery (`pc_relaunch.c`: `CreateProcess`, exe from `GetModuleFileName`, same working directory so `save/` resolves identically) is still used by the promoted-guest relaunch and for argument validation. The original design relaunched with
`--connect HOST:PORT --character UUID` (store character; a typed new name starts the Rover creation), `--guest-profile NAME` (legacy-only profile) or `--guest` (the default `guest.ini`), then sets
`g_pc_running = 0` so the title process exits cleanly. Arguments are validated (IPv4 literal, port, name `[A-Za-z0-9-]`) so no quoting/injection is possible. On non-Windows the function only prints the
command and does not quit. Other command-line options of the title process (e.g. `--fullscreen`) are NOT forwarded; settings come from `settings.ini`.

Verification: the menu code is compile-checked and source-audited only (no UI automation, never seen on screen); the data layer (`pc_servers.c`, relaunch command line) and the CLI are covered by
`pc/tools/net_spike/test_servers.py` (native selftest + temp-cwd CLI checks).

Still missing: M2b resident token authentication; guest -> resident promotion (needs town transfer); a per-server town save dir (there is one `save/card_a` today, so switching to a server
with another town still needs the matching town GCI copied there); hostname / DNS resolution (IPv4 literals only); starting the role in-process (needs a re-init path audited for every role-dependent
module); `last_town` hint; controller-only add/edit of servers (needs a keyboard; use `--server-add`).

### Play Online without relaunch

**Before.** Connect called `pc_relaunch_connect` (a new process with `--connect H:P (--character U | --guest-profile N | --guest) --town-fetch --online-ui`) and quit the title process. **Now** Connect files a request
(`pc_main_play_online_request`) and `pc_main_play_online_poll()` (`pc_vi.c`, every frame, right after the dedicated console poll, before the bootstrap polls) runs the same steps IN the title process:

1. guards: network role NONE, a live `play_main` scene without a running wipe, the Play Online menu open (the menu is inert from the request on);
2. snapshot for rollback: `pc_session()` contents, the guest spec / selected guest profile, the CLI-style globals, `pc_save_loaded` / `pc_save_ready`;
3. session + character exactly like the CLI (`pc_main_prepare_store_character`; a legacy-only profile / the default guest goes through a copy of the `--guest` legacy block, `pc_po_prepare_legacy_guest`);
4. the pre-boot town fetch (`pc_net_game_town_prefetch`, its own connection, up to ~30 s) with the same boxes: Retry loops, Use saved copy continues, Quit or "no usable town" CANCELS;
5. membership resolve (a stale membership cancels with a menu message instead of `exit(3)`) and `pc_save_validate_gci_file` on the town that is about to be loaded;
6. `pc_net_game_start_client` (the role becomes CLIENT);
7. COMMIT: `pc_save_ready = 0`, then `pc_save_loaded = pc_save_check_and_load()` (the role is CLIENT before a sanitized cache is read, which `pc_save_read_gci` would otherwise refuse with exit 3);
8. the play scene fades back to the title with the vanilla `FADE_TYPE_OUT_RETURN_TITLE` (`m_play.c`: trademark -> `common_data_reinit` -> `pc_save_reload` of the fetched town): the new title is the state a relaunched
   `--connect --town-fetch` process reaches at its first title frame (menu shows "Join as Guest");
9. only after `gamePT->exec` has left `play_main` the one-shot bootstrap polls are armed (`g_pc_bootstrap_guest` / `g_pc_bootstrap_resident_pid_set`), so they fire on the NEW title over the fetched town and the guest
   arrival / resident bind run unchanged. The log has `[PC] play-online: in-process connect pid=<pid> (no relaunch)`.

**Why it is safe.** Every role read happens at call time (save writers, field / wildlife authority, the local-world latch are re-evaluated per frame); `pc_net_game_shutdown` already returns the role to NONE and net
init / shutdown already run twice per process in the `--town-fetch` flow; the fetch peer is closed after TOWN_DONE before the game peer connects (the host sees two transport connections, and the game peer is never
disconnected). The town dir is only switched by the prefetch itself (`pc_card_set_town_dir`), never on top of another one.

**Rollback.** Any failure before the fade (bad character, cancelled box, no usable town, stale membership, invalid GCI, client start failure, load failure) restores the session, the guest selection and spec, all globals,
the town dir (`pc_card_reset_town_dir_for_test`), shuts the client down if it was started, disarms a first-run creation, and - only when the RAM save had already been replaced - re-reads the single-player town from
`save/card_a`; then the menu shows the reason. One deliberate side effect stays: the "last used character" hint (`save/mp/characters.ini`) is written before the fetch, exactly like the CLI.

**Limits.** (a) The fetch BLOCKS the frame loop (the window title shows the progress; `SDL_PumpEvents` keeps Windows from flagging it unresponsive); follow-up: a non-blocking fetch state machine. (b) An arrival failure
after the fade still `exit(2)` like the CLI path. (c) The promoted-guest relaunch (`pc_main_relaunch_poll`, guest -> resident) STAYS a relaunch (follow-up: the same in-process path with a fresh fetch).
(d) The old title scene keeps running on the freshly loaded town RAM for the ~2 s fade. (e) Leaving a town and reconnecting elsewhere in the same process is not supported (the menu is only offered at role NONE).

**Test aids (test-only, env vars).** `AC_DISPLAY_NAME=<text>`: the window opens on the display whose SDL name, Windows monitor friendly name or EDID vendor (SAM = "samsung") contains the text (case-insensitive);
every display is logged (`[PC] display N: ... bounds=...`); no or several matches exit with code 4 before any window exists. `AC_MASTER_VOLUME=<0..100>`: integer percent applied at the audio output
(`sample * vol / 100`, the same scale as `master_volume`; 1 = 1 %), never written to `settings.ini`, no Windows volume is touched.

**Test.** `pc/tools/net_spike/test_play_online_inprocess_real.py` (disposable `bin_fixture4_ipc_*` copies, dedicated host, windowed client driven with key events on the Samsung display, `AC_RELAUNCH_DRYRUN=1` as a
tripwire): S1 same PID and StartTime before / after Connect, exactly one client process, no `RELAUNCH` in the log, the in-process line carries the PID, the host saw two transport connections and no disconnect of the game
peer, the client reached READY and the guest arrived at the station; S2 (host stopped, `AC_TOWN_NO_MSGBOX=1`): the fetch finds no town, the connect is rolled back, the process stays alive and the menu accepts a new request.

### Town cache and town transfer (phase 2, M-A / M-B)

A client no longer has to copy the host's town save by hand. Started with `--town-fetch` it downloads the host's town into a per-town cache and plays that; the legacy
`--connect` without the flag is unchanged.

**Flags.**

* `--town-fetch` (client only, requires `--connect` or `--server`, exit 2 otherwise): fetch the town before the game boots (progress in the window title; errors in an SDL message box).
* `--town-serve off|on|full` (host only, exit 2 otherwise) and `settings.ini` `[Network]` `town_serve = 0|1|2` (the words `off` / `on` / `full` work there too): whether and how this host serves its town. **Default AUTO (`town_serve = auto`, `-1`): ON/sanitized for a `--dedicated` host, OFF for a plain `--host`**; an explicit `--town-serve` or settings.ini value always wins. `on` (= 1) serves a SANITIZED copy (M-G, below); `full` (= 2) serves the whole saved file as before.
* `--town-dir DIR` (hidden, client only, requires `--connect`, not together with `--town-fetch`): use `DIR` (a town directory) as the Card-A parent: the save is read from `DIR/card_a`.
  For tests and the fallback; `save/card_a` and the legacy `save/DobutsunomoriP_MURA.gci` are never moved, renamed or written, and the legacy migration is skipped.

**Cache layout** (`pc/src/pc_town_cache.c`, pure module):

```
save/mp/towns/<townkey>/card_a/DobutsunomoriP_MURA.gci   the cached town (what the loader reads)
save/mp/towns/<townkey>/incoming/town.part               a download in progress (never inside card_a: the loader renames an orphaned .tmp and scans GAF*.gci)
save/mp/towns/<townkey>/origin.ini                       server name, address, port, last fetch time
```

`<townkey>` is `land_name` (16 hex) `_` `land_id` (4 hex) `_` `terrain_hash` (8 hex), the same formatting as the character store. A server is mapped to its town by scanning
`towns/*/origin.ini` (`servers.ini` is not changed: its parser is strict). The cache is shared by all characters; clients only write it through a fetch.

**Fetch flow** (`pc_net_game_town_prefetch`, called from `pc_main.c` after `pc_platform_init` and before `boot_main`): a separate single-purpose connection (own pump, no game state),
messages `TOWN_FETCH_REQ` 62 / `TOWN_INFO` 63 / `TOWN_CHUNK` 64 / `TOWN_DONE` 65 (protocol v8 extended in place, no version bump). The client sends what it already has (crc32, size, town);
the host answers `UP_TO_DATE` (no stream) or `STREAM`: 468 in-order chunks of 1000 bytes with a `u32` offset. The stream goes to `incoming/town.part`; at the end the client checks
size and CRC32, validates the file as a GCI of the announced town (`pc_save_validate_gci_file`: exact size, `GAF`, land name / id / terrain hash equal to `TOWN_INFO`) and installs it
with `MoveFileExA(REPLACE_EXISTING | WRITE_THROUGH)`. A partial, corrupt or mismatching download never replaces a valid cache. Every connect refetches when the file differs.

Fallback ladder (no `TOWN_INFO` within 3 s = old host, or the fetch failed / was refused / is busy): (1) the cached town of this address:port found through `origin.ini` (offline-capable),
(2) the legacy `save/card_a`, (3) an SDL message box and exit code 3 (a wrong town is never booted).

**Host side.** The host serves its last durably saved GCI, read into a snapshot when the request arrives. Only a connection that is still in the handshake and has not sent `IDENTITY` /
`IDENTITY_EXT` can fetch; on such a fetch connection `IDENTITY` is ignored (it can never bind an identity). At most 2 concurrent streams and 10 requests per address per minute (BUSY
otherwise), chunks paced by the reliable backlog, the connection is closed after `TOWN_DONE` or 30 s. Statuses: 0 STREAM, 1 UP_TO_DATE, 2 UNAVAILABLE, 3 BUSY, 4 REFUSED (town_serve off, or
IDENTITY already sent), 5 BAD_REQUEST.

**Privacy.** The saved GCI contains every resident's pockets, mail, diary and designs. `town_serve = full` serves it UNSANITIZED (friends / a supervised LAN only); `town_serve = on`
serves a SANITIZED image instead (next section). That `on` is now the sanitized mode is the only change of meaning of an existing setting: a settings.ini that already says `town_serve = 1`
now serves the sanitized copy.

#### Sanitized town transfer and the record-import guard (M-G)

With `town_serve = on` the host builds a TRANSFER image from its last saved GCI (`pc/src/pc_town_sanitize.c`, a pure module) and serves that, never the file itself: the host's own
file is only read, the image is cached per (save generation, CRC32 of the file), the CRC32 / UP_TO_DATE comparison and the stream all use the sanitized bytes, and the output is
validated again as a GCI of the same town before it is sent (otherwise the host answers UNAVAILABLE). `TOWN_INFO` carries `flags` (the old reserved field; bit0 = SANITIZED; old
clients ignore it). The game still loads the image: the loader reads only the main Save and the three ARAM blocks and checks no checksum; the sanitizer recomputes the ones it
touches anyway.

What the transfer image holds, in the order of the file:

* CARDDir, comment / banner / icon: kept, plus the 8 byte marker `ACMPSAN1` at file offset 0x70 (zero in the normal writer). The ARAM blocks (mail, original designs, diary) are replaced
  by what a fresh PC save holds (block order normalised to the Dolphin order with the land id at +2, checksums recomputed); the rest of the Others area is zero.
* Every `private_data` record (all four slots, the requester's own included, its real record arrives through PUSH_FULL): only the PersonalID / gender / face / reset_count, exists /
  hint_count / shirt, reset_code and state_flags are kept; every other byte is a cleared record (pockets, wallet, bank, quests, inventory mail, designs, museum, calendar ...).
* Every house mailbox: ten cleared letters; the gyroid's bells: zero. The post office's stored mail: cleared, its counters zero. Every villager's saved letter (the villagers and the
  islander): attached present zero, text blanked; the memory itself (who, friendship, letter flags) stays.
* The Save checksum is recomputed and the backup Save is a copy of the sanitized main Save. Everything else is kept byte for byte (the table is exported as data by
  `pc_town_sanitize_layout()`; the unit test checks that it tiles the file).

**What stays visible (residual leakage, by design of this iteration).** Resident names, PersonalIDs, faces and shirts; the houses (furniture, dresser / storage contents, the gyroid
items and message, music box, house palette); the villager memory headers (which player knows which villager, friendship values, hp_mail), event flags, the museum, fish and insect
records outside the private records, needlework, the cottage, the noticeboard, the whole town map. The real protection against someone who uses a leaked name is `--resident-tokens
tofu` (or `required`): the host logs a warning when `town_serve` is on and `resident_tokens` is off. Residents' mail reaches other admitted peers no longer through the friendship
snapshot (a villager's saved letter text is sent only to the peer whose PersonalID the memory belongs to); other live broadcasts (for example MAIL_DELIVERED) are unchanged.
**Regression:** a client's own diary, letter storage and design storage were never synced; with a sanitized cache they are now EMPTY in the client's copy (they used to be whatever its
local copy held). A follow-up has to sync them; the host keeps them.

**A client whose cache came from a sanitized image can never upload a blank record.** Three layers:

1. The client: `pc_save_read_gci` sets `g_pc_save_sanitized` when it loads an image with the marker. RECORD_HELLO then carries flag `0x02 NO_MIGRATE` if the local player is a resident
   (guests are unaffected), and if a MIGRATE_REQUEST arrives anyway the client logs it and never uploads (the host's MIGRATE timeout closes the link).
2. The host: at rev 0 for a RESIDENT it takes "host wins" instead of "client wins once" when any of these holds: the HELLO says NO_MIGRATE, `town_serve` is `on` (an old client cannot
   say it), or `members.dat` holds a promotion entry for that PersonalID (the crash window of a guest promotion). It seeds rev 1 with the promotion's lineage code, logs
   `HOST WINS (<reason>)` and sends the PUSH_FULL; no MIGRATE_REQUEST. A host that does not know the flag refuses it (BAD_SHAPE detail 6) and the client stays unsynced, which is safe.
3. The process: a HOST or single-player process that loads an image with the marker refuses it (message box, exit code 3), and the writer never writes while the flag is set, so a
   cache can never be hosted, played as a town or saved over anything.

**Consequence to know:** while `town_serve = on` the legacy "client wins once" import of an offline copy is off for residents. To import such a copy, start the host once with
`town_serve` off (or `full`), let the resident join, then switch back.

Tests (M-G): `pc/tools/net_spike/test_town_sanitize_unit.py` (native: table tiling, template equality, independent checksums, idempotence, refusals; source audit),
`test_town_sanitize_real.py` (real host + FakeClient + a real client with an empty save dir under `--resident-tokens tofu`). `test_town_transfer_protocol.py` P1 and
`test_town_fetch_real.py` R1 now start the host with `--town-serve full` (they compare the streamed bytes with the host's file).

**Limitations.** No adopt-on-match of an existing `save/card_a` into `towns/` (copy it by hand into `towns/<key>/card_a`); the transfer is about 60 s stale at worst (the READY snapshot
and the record / house / service syncs bring the session up to date); Play Online does not pass `--town-fetch` yet (M-C); NES game saves still live in `save/card_a` (`famicom.cpp`);
a fetch refetches the whole file whenever it differs (about 467 KB); the Save_t checksum is not verified (the loader does not either; the transport checks size + CRC32); a host that
never saved yet answers UNAVAILABLE.
Tests: `pc/tools/net_spike/test_town_cache_unit.py` (native unit + audits + `--town-dir` real process), `test_town_transfer_protocol.py` (scripted client against a real host),
`test_town_fetch_real.py` (real client with an empty save dir).

### Personal data sync (diary only; phase 2 follow-up of M-G, milestones P0 + P1)

Why: with `town_serve = on` (M-G) the sanitized image blanks the three ARAM blocks (letter storage, design storage, diary) for everyone, so a client's own diary used to be EMPTY in its copy. This feature gives each resident client back ITS OWN diary, authenticated by the resident binding. Only the diary is implemented.

**What the code says (verified, and why only the diary).** `include/m_card.h` / `pc/src/pc_town_sanitize.h`: the letter storage (`mCD_keep_mail_c`, 0xBAC0 B) and the design storage (`mCD_keep_original_c`, 0xCCA0 B) are ONE block per TOWN shared by every resident (post office / Able Sisters), not per character; only `mCD_keep_diary_c` (0xBA20 B: checksum at +0, `entries[4][12]` of 992 B at +2) is per resident slot. One slot = 12 months x 992 B = 0x2E80 B at `2 + slot * 0x2E80`. The D3 record (`Private_c`, 0x2440 B) already carries the pockets, the 10 inventory letters, the 8 own designs and the calendar and is the client-owned range set, but none of the three ARAM blocks is part of it. **Letter and design storage are deliberately NOT synced**: they are town-shared rather than character-owned, a per-resident pairing with the record would be needed (a storage letter may carry a gift item, so a one-sided write is a possible duplication path), and designs belong to the Able Sisters' shared stock. Deferred (P2, needs its own conservation design). The ARAM blocks are in memory (`l_aram_block_p_table`, `pc_m_card.c`) and the NORMAL host save serialises them into the GCI, which is also how the diary persists.

**Option.** `--personal-sync on|off` (host only, exit 2 otherwise) overrides `settings.ini` `[Network]` `personal_sync = auto|on|off`; the default `auto` is ON only while the town is served (`town_serve` is not `off`), because that is the case where clients hold blank diaries, otherwise OFF. It is announced in HOST_CONFIG byte 1 bit 1 (bit 0 = house sync, they are independent); the validator accepts both bits. A client without the bit never sends and never adopts (an old client would reject a blob with bit 1: v8 is unreleased, same situation as bit 0).

**Wire (NO new id, NO struct change).** Two more KINDS on `HOUSE_BEGIN` / `HOUSE_CHUNK` / `HOUSE_ACK` (59..61): kind 3 `PDATA_COMMIT` (client -> host) and kind 4 `PDATA_PUSH` (host -> client). `house` = block (0 = diary), `rsv` = page (diary: the slot), `house_seq` = page revision, `host_session` as for houses, `rec_epoch` / `rec_rev` / `rsv2` = 0, payload = one diary slot = 12 chunks, digest FNV-1a32. ACK statuses are reused (APPLIED / STALE / BAD_DIGEST / BAD_SHAPE / INVALID_CELL / NOT_OWNER / BUSY / RATE_LIMITED); `house_seq` = the page revision, `epoch` = block, `rev` = slot, INVALID_CELL `detail` = the offset of the first refused byte. Commits share `hup_last_xfer`, the single reassembly buffer and (on the client) the xfer counter of the house commits: one transfer at a time, the other kind is answered BUSY. The host routes by KIND before the `--house-sync` gate, so this works without `--house-sync`.

**Authorization (the key safety point).** The slot is ALWAYS the authenticated binding's resident index (`pcnetgame_rec_gate`, i.e. `bound_resident_idx` re-checked against the saved PersonalID); only a RESIDENT binding can write that slot. A guest gets NOT_OWNER before any index exists; the message's page field must merely EQUAL the bound slot (BAD_SHAPE detail 5 otherwise: it never selects anything), the block must be 0 (detail 4); a client cannot name another slot, block or town record. Host order: READY + bound + feature on + one transfer at a time; guest NOT_OWNER; shape; at completion world ready / no push in flight (BUSY), digest, BUSY while the host's OWN player has any submenu open (the diary overlay copies the WHOLE block at open and writes it back whole, so the block must not be patched meanwhile; a process outside GAME_PLAY, e.g. a dedicated host, is never busy), the shared record rate limit (deviation: BUSY precedes the rate accounting, exactly as the house path does it, so a retry against a busy host is not counted), base (host_session, page rev) else STALE, validation, ONE put into the live block, page rev + 1, the resident's save marked dirty (the next normal host save writes the GCI; a peer leaving with the marker set asks for the early save), APPLIED. Any refusal except BUSY / RATE_LIMITED / NOT_OWNER leaves everything untouched and owes the canonical slot back (a push). Page revisions live in memory per host session: after a host restart an old base is STALE, which is safe.

**Validation.** Exactly 0x2E80 B; the checksum / landid are host-owned (never in the payload, never written). Refused: byte 0x7F (CHAR_CONTROL_CODE) and byte 0x80 (CHAR_MESSAGE_TAG), the control family of `m_font.c`, anywhere in the text. Verified against `m_editor_ovl.c`: the keyboard tables contain neither, and the ornament exchange table maps both to themselves, so the vanilla editor cannot produce them. (The 0x80 refusal goes one byte beyond the brief on the same evidence.) Everything else the editor can produce, including the control-looking low codes of the ornament page, is accepted.

**Push.** After READY and record SYNCED the host pushes ONLY the bound resident's OWN slot (stricter than vanilla, where the whole block is one file); a 1 s digest poll of each resident slot bumps the page rev on a host-side change (the owner playing on the host) and pushes it. The client's other three slots stay blank. Interaction with M-G: the sanitized image keeps blank ARAM blocks for everyone; each resident client gets its own slot back through this push (the image is fetched before IDENTITY, so it cannot be per requester).

**Client.** Keeps `canon` (page base + digest of the slot) and a stash. A push is adopted (read-modify-write of its slot in the local in-memory block, never persisted) only while no submenu is open and no commit is in flight. A commit is sent by a 500 ms poll when the slot digest differs from canon, no submenu is open (the diary closed: the vanilla overlay has written its copy back; no game code was touched), a canon exists (a client never uploads before the host's slot was received, so a blank sanitized diary can never overwrite the real one) and the link is READY. Conflict policy is HOST WINS: a push whose content differs from canon while the local slot holds unsent edits, STALE, and every refusal replace the local slot with the host's copy (logged loudly); a push that only moves the base (host restart, same content) keeps the local edits and they commit on the new base. Guests and clients without the bit send nothing. A client that quits right after closing the diary may lose that last edit (no quit flush for the diary).

**hp_mail (sanitizer).** NOT zeroed: `mNpc_SendHPMail` (`src/game/m_npc.c`) READS `Animal_c.hp_mail[4]` (0x900, 4 x 0x1C B: `receive_time` + 20 byte password) every day and acts on a non-zero `receive_time`; the other users are the writer `mNpc_ReceiveHPMail` and the GBA island converters. By the brief's rule (a reader exists) the sanitizer is unchanged and the residue stays listed among the visible leakage above. A zeroed block would be semantically "nothing pending" (exactly what `mNpc_ClearHPMail` writes), so it can be switched on later if the leak matters more than a pending password mail of a client's copy.

**Tests.** `pc/tools/net_spike/test_pdata_protocol.py` (REAL host + FakeClients on a disposable `bin_fixture4_pdata`: P1 `--personal-sync on` without `--house-sync`: HOST_CONFIG byte 1 == 0x02, each client pushed its own slot only (12 chunks, bytes == the GCI slot), APPLIED, other client untouched, page field naming another slot -> BAD_SHAPE + canonical push back, unknown block, STALE (old rev / foreign session), BAD_DIGEST, 0x7F / 0x80 -> INVALID_CELL, a guest gets no push and NOT_OWNER, graceful stop: the GCI holds the committed slot and every other slot is byte-identical; P2 no flag = off (bit absent, commit dropped); P3 `--town-serve on --house-sync`: AUTO on, byte 1 == 0x03, house pushes unaffected) and `test_pdata_src.py` (source audit: no new ids / structs, routing before the house gate, slot from the binding only, guest before any index, handler order, validation constants, client gates, defaults, the editor tables, the hp_mail decision). Not tested by any process test: the real CLIENT half (adopt with no menu, the commit after the diary closes) and the host's own-menu BUSY branch (no process test opens a menu); manual check: write a diary entry on a client, close the diary, watch `[NET][PDATA] client: PDATA_COMMIT ... APPLIED`, restart the client with a fresh `--town-fetch` cache and see the entry come back; same for an entry written by the host's own resident (rev bump + push).

**Limits.** Letter / design storage are not synced (above). One slot is 11.9 KB, sent in 12 reliable chunks on every change of the slot (a diary edit is rare). An old client that never learned kind 4 drops the BEGIN and logs a few chunk violations (no write, no disconnect). The Windows exit path does not flush an unsent diary edit. Host-side diary edits made while a client edit is pending are resolved host-wins.

### Resident credentials and admission (phase 2, M-D / M-E)

PersonalIDs are public, so a resident could be claimed by anyone who knows the name and ids. Optional **resident credentials** give each resident the same trust-on-first-use token a
guest has. They are **off by default**: with `resident_tokens = off` nothing below happens and every existing flow is unchanged.

**Policy** (`settings.ini` `[Network]` `resident_tokens = off|tofu|required`, host override `--resident-tokens off|tofu|required`, host only, exit 2 otherwise; default `off`).

* `off`: the `IDENTITY_EXT` RESIDENT flag is parsed and ignored, nothing is minted or checked, `members.dat` is never read or written.
* `tofu`: a resident slot that has a credential accepts only the matching token (KNOWN); a missing or wrong token is refused with REJECT reason 5. A slot with no credential and a RESIDENT claim
  gets a token minted at its first claim (NEW). A **legacy client** (sends no `IDENTITY_EXT`) is admitted **only while the slot has no credential** (the host logs
  `resident N has no credential (legacy client)`; nothing is minted); once a credential exists a legacy client is refused.
* `required`: like `tofu`, but a slot without a credential is refused until the operator arms it (`resident-arm`); a legacy client can never be armed in (it cannot receive a token).
* A client that presents a token for a resident the host has **no** credential for (the host table was reset, or it is another host) is refused and nothing is minted over it (the client must
  delete its stored token): minting a different token would make the real client refuse the host and leave an orphan credential locking the slot.
* `members.dat` **UNTRUSTED** (it existed but no generation is readable, or only preserved `*.corrupt-*` files remain): under `required` every resident is refused; under `tofu` residents are admitted
  legacy-style (no credential check, no mint, nothing written). The bad file is moved to `members.dat.corrupt-<timestamp>`, never deleted.

**Admission order** (M-D, `pcnetgame_host_admission_decide`, behaviour-identical by default): has_save / town checks, classify (the existing `pcnetgame_host_classify_identity`, NOT replaced), AMBIGUOUS
refused, UNKNOWN without a GUEST claim = NO_SAVE, UNKNOWN with a GUEST claim = `pcnetgame_host_guest_check`, RESIDENT with a GUEST claim refused, RESIDENT = own-resident check, then the
credential check. The credential check is **before** the duplicate-park step, so an impostor can never evict a live peer. Afterwards (in `process_identity`, unchanged): duplicate park, guest cap /
reserve, mint (guest or resident), ACK, IDENTITY_TOKEN. **M-H update: membership is now the decider.** `pc_mp_membership_resolve` (pure, `pc/src/pc_mp_membership.c`) classifies the claim; `pcnetgame_host_admission_resolve_view` feeds it the host state. Its comparison `pc_mp_pid_equal_vanilla` mirrors `mPr_CheckCmpPersonalID` (equal player id and land id, both names non-blank = not all 0x20, and equal), and a resident counts only if its land id passes `mLd_CHECK_LAND_ID`, it is not the null PersonalID and `exists` is set. Results: RESIDENT(slot) when exactly one resident matches; AMBIGUOUS when several match, or one matches while a guest entry of this town has the same PID (guest aliasing); GUEST (slot or new) for a guest EXT with no resident match; STALE when no resident matches, the EXT is no guest claim and members.dat (only read when resident_tokens is not off and the file is trusted) holds a credential or handoff of this town for that PID; otherwise NONE. A malformed EXT counts as no EXT. `admission_decide` switches on that result (RESIDENT: guest-claim refusal, own-resident, credential check; GUEST: `guest_check`; NONE/STALE without a guest claim: the NO_SAVE text, STALE preceded by a `claim is STALE` log line; AMBIGUOUS: the ambiguous refusal). The old classifier still runs as a cross-check: if it disagrees with the resolver in any way the host logs `ADMISSION EQUIV differs (...)` and refuses (ambiguous text if either side says ambiguous, else the NO_SAVE text) - never admits. Known differences from the old code, all refusals: guest aliasing (old: RESIDENT, now AMBIGUOUS) and STALE (same refusal, one extra log line). The wire answers and every refusal text are unchanged, including REJECT 5 for an old client without a RESIDENT EXT under `resident_tokens=required`. Test: `test_membership_equiv.py` (native table against a C copy of the old classifier).
New host option `allow_new_guests = 0|1` (`settings.ini` `[Network]`, `--allow-new-guests 0|1`, default 1): with 0 a NEW guest key is refused as SERVER_FULL before anything is minted or created;
known guests (key + token, and the operator re-mint) still return.

**Wire** (protocol v8 extended in place, no version bump): `IDENTITY_EXT` flag `0x02` RESIDENT (exactly one of GUEST / RESIDENT; `home_*` = the resident's PersonalID, must equal the IDENTITY's
name / player id / land name / land id; token as for guests). Reply `IDENTITY_TOKEN` flags `0x04` RESIDENT + `0x01` NEW or `0x02` KNOWN, `guest_slot` = the resident index, `table_size` = 4. New
REJECT reason **5 RESIDENT_CREDENTIAL** (8-byte form); a new client shows "resident credential missing or wrong: ask the operator for resident-reset/arm", an old client shows "unknown reason 5: update the
game". Reasons 1..4 are unchanged. A new client sends the RESIDENT claim whenever it plays a resident (an old host drops the unknown flag as malformed and admits the legacy way; a new host with
`off` ignores it).

**Files.** Host `save/mp/members.dat` (`pc/src/pc_mp_members.c`, same storage pattern as `guests.dat`: CRC32, tmp, `.bak1`/`.bak2` rotation, atomic replace, UNTRUSTED). 16 entries of **80 bytes**
(the design text said 72, but its own field list adds up to 80): present, confirmed, res_slot, kind (1 RESIDENT_TOKEN, 2 PROMOTION_HANDOFF for M-F), town key, resident PID (BE), token, age, aux pid.
It is keyed by (host town, resident PID), not by the slot index, and is written synchronously at mint **before** the token is sent (a failed ACK / token send rolls the mint back). `confirmed` is set
when the client presents the token once. Client: a store character keeps the resident token in `characters/<uuid>/towns/<townkey>/token.dat` (same PCMpGtk format, `home_pid` = the resident PID) and
`membership.ini` `role=resident`; a legacy / CLI resident uses `save/mp/resident_token.dat`. A different token than the one the client presented = the client refuses the host (as for guests).

**Console** (`--dedicated`; order of checks like `guest-remove`: world ready, not UNTRUSTED, selector, refused while the resident is connected, the `confirm` word, backup `members.dat.bak-<timestamp>`):
`residents` (slot, name, credential yes/no, confirmed, armed, connected; never a token), `resident-reset <slot|name> confirm` (backs `members.dat` up, deletes the credential; the player must also delete
its stored token), `resident-arm <slot|name> confirm` (only under `required`; allows exactly ONE mint for a slot without a credential; memory only, 10 minutes, consumed by the mint, cancelled by a restart).

**Compatibility.** Old host: drops the RESIDENT flag, admits the legacy way, no token, the client plays without a credential. Old client: sends no `IDENTITY_EXT`; admitted under `tofu` only while its slot
has no credential, refused under `required` and once a credential exists (it sees "unknown reason 5").

**Limitations.** Not internet-grade: the token is a bearer token over the same unencrypted UDP transport as the guest tokens (a captured packet can be replayed), and trust on first use means whoever claims a
resident first wins it; `resident-arm` / `tofu` do not prove who the claimant is. The credential protects the slot, not the data: the town save is still served unsanitized when `town_serve` is on. Nothing here
changes the town save; Play Online passing a resident membership is M-C (done); guest -> resident promotion is M-F (see "Guest -> resident promotion"); the real client half is verified by a real-client run (see "Real-client round trip" below) as well as source audit and a scripted host test.
Tests: `pc/tools/net_spike/test_members_unit.py` (native storage unit), `test_resident_credentials_protocol.py` (scripted clients against real dedicated hosts: off / tofu / restart / required + arm /
corrupt UNTRUSTED / allow_new_guests).

**Real-client round trip (verified).** `pc/tools/net_spike/test_resident_credentials_real.py` (62 checks, passes) drives a REAL dedicated host (`--town-serve on --resident-tokens tofu`, later `required`) and REAL client game processes (store characters, `--town-fetch --online-ui --character`, membership.ini role=resident for fixture resident 1, empty save dir) on disposable `bin_fixture4_rcred` / `bin_fixture4_rcredc` copies. Exercised: first claim mints NEW (one members.dat entry); the client stores a one-entry PCMpGtk token.dat equal to the minted token under exactly one town key dir; the token text never appears in host/client logs or `residents`; abrupt host kill (TerminateProcess) + host restart on the same port/save/members.dat: the SAME client process auto-reconnects ("identity re-established ... resident 1"), the host logs KNOWN (not MINTED), the entry count stays 1 and `residents` shows credential=yes confirmed=yes connected=yes; a different valid-format token in token.dat and a missing token.dat are both refused REJECT 5 (client logs "resident credential missing or wrong", never READY, token.dat untouched); `resident-reset 1 confirm` then the same client re-mints a different token; under `required` with a fresh members.dat: refused until `resident-arm 1 confirm`, exactly one mint, arm consumed, then a client without a token is refused again; a second character (copy of the town, same resident PersonalID, no token) is refused REJECT 5 under tofu and under required while the real resident stays connected (no eviction, no mint). Not covered: the legacy CLI client path (`--bootstrap-resident` without a store character, `save/mp/resident_token.dat`), a token filed under another town key (equivalent to a missing token by construction), a graceful host stop (only the abrupt kill), and the in-game refusal UI (only the log text). No product bug was found by this test; the only defects were in the test's first draft (the fetch-only prep launch had made a guest claim; the shared town cache logs UP_TO_DATE instead of installed).

**Edge cases (verified, real processes).** `pc/tools/net_spike/test_resident_edge_real.py` (46 checks, passes) uses a REAL dedicated host (`--resident-tokens tofu`) and REAL clients on disposable fixture copies: (A) graceful host `stop` (exit 0) + restart, the store resident auto-reconnects, host KNOWN not MINTED, members.dat still one entry, `residents`/`players` show resident 1 once; (B) a valid token.dat under another town-key directory only: REJECT 5 'presented NO token', no mint, members.dat byte-identical, the restored correct token still admits KNOWN; (C) legacy `--bootstrap-resident 1` (no character): NEW mint, token saved in save/mp/resident_token.dat (PCMpGtk, keyed by resident PID), a second run presents it (KNOWN); (D) hard host kill + restart with the legacy client connected: auto reconnect KNOWN, file byte-identical; (E) a resident_token.dat holding another town's token: REJECT 5, host state and the real file untouched.

### Guest -> resident promotion (phase 2, M-F, reduced scope: host side + wire handoff)

A dedicated host operator can turn a GUEST into a RESIDENT of the host town: `promote <guest slot|name> <resident slot 0-3|auto> <house 0-3|auto> confirm`. Without `confirm` nothing changes and
the command says what it would do. It is refused (nothing changed) unless: the process is a host with a ready world; `guests.dat` and `members.dat` are trusted; the selector names ONE guest of
this town; the guest is OFFLINE and has synced a record; there is a free resident slot (`mPr_CheckPrivate != TRUE`) and a free house (null ownerID) (a full town of 4 residents cannot promote);
no resident already has the guest's name; `members.dat` has room for 2 entries. `guests.dat`, `members.dat` and `records.dat` are backed up first (`<file>.bak-<timestamp>`).

What is created (pc_m_card.c `pc_mp_promote_create`, game thread, the vanilla routines): `mPr_ClearPrivateInfo` + `mPr_InitPrivateInfo` into the free slot (this town's land, a new unique 0xF0xx player
id, vanilla defaults), then the guest's name, gender, the shirt it wears, pockets / item conditions / wallet, bank account and its Able Sisters designs are copied over, and the face when no other
resident wears it (otherwise the vanilla unique face stays; logged). `inventory.loan` = the house price (what vanilla's house selection writes). `mHS_set_use(slot, house)` assigns the house
(a vanilla delete clears a house, so a null-owner house is a default house) and `mEv_ClearPersonalEventFlag(slot)` gives the slot the veteran event state: the Nook intro and first-job are NOT started.
Then, in this order: the records lineage of the new resident is seeded at rev 1 (the host pushes its record, it never MIGRATEs), `members.dat` gets a resident credential for the new PID (always minted,
whatever `resident_tokens` says: with `off` the entry exists but is unused) plus a PROMOTION_HANDOFF entry (aux = the guest's home PID, token = the guest's own GUEST token), the town is saved
(`records.dat` follows from the save hook), and LAST the guest entry is removed from `guests.dat`. A failure before the save is durable rolls the game state, the lineage and `members.dat` back.
Crash safety: a handoff whose resident PID is not in the saved town is ignored (log `INVALID`), so the guest entry keeps working; a handoff with the resident present is valid even if the guest entry
removal did not happen.

Wire (v8 extended in place, no version bump): `RESIDENT_HANDOFF` id 66 (host -> client, 48 B: type, flags, res_slot, rsv, town PID[20], token[16], rsv[8]) is sent to the promoted guest when it connects
with its GUEST claim and its guest token, followed by `REJECT` reason 6 `PROMOTED` (8-byte form). An old client drops 66 and shows "unknown reason 6: update the game"; a new client shows "This character was
promoted to a resident of this town: restart to join as a resident." Client (cheap part): a STORE character gets `characters/<uuid>/towns/<townkey>/token.dat` replaced by the single resident token and
`membership.ini` set to `role=resident` + the new town PID; the next Play Online connect re-fetches the town (it now contains the resident) and binds by PID (M-C). A legacy guest profile is not rewritten
(message only). The handoff entry is deleted at the first confirmed resident login with the matching token (`members.dat` `confirmed`; under `resident_tokens=off` there is no confirm step, the handoff stays
and the guest keeps getting it until the entry is reset).

**M-I additions (complete promotion).**
* *Automatic relaunch.* After the handoff handler has written token.dat AND membership.ini of a STORE character it arms a flag; the following REJECT 6 turns it into a one-shot request. `pc_main_relaunch_poll()` (pc_main.c, polled from `VIWaitForRetrace` in pc_vi.c) acts on it only in a process started with `--town-fetch`: it runs `pc_relaunch_connect(host, port, PC_RELAUNCH_CHARACTER, uuid)` (the usual validated command line: `--connect H:P --character "<uuid>" --town-fetch --online-ui` plus the forwarded whitelist) and clears `g_pc_running` only once CreateProcess succeeded; on failure the join message says so. A CLI client without `--town-fetch` (it plays the local card_a town, which lacks the resident) and a legacy guest profile only show the message. The new process fetches the town (now holding the resident), resolves role=resident, binds the slot by PID and sends the RESIDENT claim with the handed-over token; a resident process never sends a guest claim, so no loop is possible. Test hook: `AC_RELAUNCH_DRYRUN=1` logs the command line (`[PC] RELAUNCH DRYRUN: ...`) and neither starts anything nor quits.
* *Re-fetched town vs. the guest arrival (found by the real-client test).* After the re-fetch the local town contains a resident with the guest's own name, and the guest arrival refused that clash locally, so the claim (and therefore the handoff) never went out. Now the arrival is allowed when the character already holds a GUEST token of that town (`pc_net_game_client_holds_guest_token`); the host decides (handoff + REJECT 6, or its normal refusal). Without a token the refusal is unchanged.
* *Nook intro / first job.* Not run, deliberately: vanilla sets the first-intro/first-job flags at the Nook scene change, and the intro demo's house pick, loan and first-job event flags are host-owned Save state with no client -> host path, so running them on a client would split the brain. The promoted resident is a veteran (flags cleared by `mEv_ClearPersonalEventFlag`). Added instead: the birthday copied from the guest record (only a plausible date), and the four catalog bits the intro demo sets right after `mHS_set_use` (verified in `ac_intro_demo_move.c_inc` `aID_retire_rcn_guide_wait`: the worn shirt, `FTR_START(FTR_SUM_CASSE01)`, the house's carpet and wallpaper) through `mPr_SetItemCollectBit` (it writes `Common now_private`, which is pointed at the new resident for those calls only). Not given: the first-job quest, no Nook letter.
* *Villager memories.* In `pc_mp_promote_create`, for the 15 villagers and the islander, every memory whose `memory_player_id` is the guest's home PID gets the new PID; for town villagers the land (name/id) becomes the host town and `saved_town_tune` the town melody (what `mNpc_SetAnimalLastTalk` writes for a resident). The islander's `memuni` is the furniture-bitfield member of the union, so only its player id is re-keyed. The rollback snapshot now also holds `animals[]` and the island animal (~39 KB), so a failed save puts them back. Since the lifecycle hardening (below) the contest quests, the record's letters and more are migrated as well; fish records and host-held mail never existed for a guest. Clients see the memories with the next FRIENDSHIP snapshot.
* *Crash safety (verified in code).* A full town is refused before anything changes. A crash before the GCI is durable leaves a members.dat whose resident is not in the saved town: the handoff is ignored (`INVALID`), the guest keeps working; those orphan RESIDENT_TOKEN / PROMOTION_HANDOFF entries show as stale in `residents` and are now pruned by the next promote (`pcnetgame_members_prune_orphans`, written with the step-3 members.dat commit and counted as free room in the pre-check; entries whose resident is present, other towns and other kinds are never touched). A crash between the GCI write and records.dat is covered by the existing "promote entry => host wins" rule in the rev-0 HELLO path (`pcnetgame_rec_has_promote_entry`). The handoff can be delivered any number of times until the first confirmed resident login.

**Remaining limits.** (Stale since the lifecycle hardening: the guest record's letters, equipment, quests and bits now migrate, fish records / host-held mail never existed for a guest; see "Guest -> resident lifecycle hardening".) The first-job quest and Nook intro are not given; the house is whatever default house the free house holds (contents left behind by a removed owner are kept); a resident_tokens=off host never confirms, so the handoff stays and the guest keeps being told. A legacy guest profile and a CLI client without `--town-fetch` are not rewritten / relaunched. Not verified: the in-game look of the new house / resident while playing, the rollback of the memories on a forced save failure (source-audited order only, no failure hook), and the `AC_RELAUNCH_DRYRUN` path covers the command line only: the relaunched client was started by the test from that logged command line (not by the game's own CreateProcess; that branch is source-audited).
Tests: `pc/tools/net_spike/test_promotion_protocol.py` (scripted clients against real dedicated hosts on `bin_fixture4_promo`, a copy with resident slot 3 and house 3 cleared; M-I: re-keyed memories, birthday, catalog bits, source audits) and `pc/tools/net_spike/test_promotion_relaunch_real.py` (REAL host + REAL store-character client on disposable copies: guest -> promote -> dry-run relaunch line -> the relaunched resident client reaches READY and the handoff is confirmed).

**Promoted resident gameplay check.** One real run (disposable `bin_fixture4_promo_rc` host with `--dedicated --town-serve on --resident-tokens tofu --house-sync`, `bin_fixture4_promo_rcc` client, store character Roger: guest -> `promote Roger auto auto confirm` -> dry-run relaunch line) and then the logged command line (`--connect 127.0.0.1:P --character <uuid> --town-fetch --online-ui`) started as a normal WINDOWED client, driven with synthetic key events (`keybd_event` scan codes, sent only after the game window was confirmed foreground) and screenshots of ONLY the game window client rectangle. SEEN on screen: (1) the player in the host town as a resident standing at the door step of house 3 (no title / Rover / Nook intro scenes; log: `resident PersonalID matches slot 3`, `playing a resident`, READY, `resident token verified by the host (resident 3)`), in the guest's look (white cap, striped shirt, face 3; the name was not drawn on screen, it is in the promote log: `Roger`, player id 0xF04D); (2) normal movement (walk S and back N, right along the step; screenshots differ as expected); (3) no intro / first-job: no cutscene or dialogue appeared and the client log has no INTRO_DEMO / first-job line; (4) the house door opened with the A key (vanilla needs A while facing the door, not just walking into it) and the interior was entered: a valid default room (metal floor, desk with a diary, radio, lights off), no crash; leaving it returned to the door; (5) host process stopped and restarted (the stop was a normal `stop`): the open client showed `Connection lost - Reconnecting... (attempt 5)` in the room, reconnected after 5 attempts / ~38 s with `identity re-established (resident: resident 3)`, the host logged `credential verified (token sent: KNOWN)`, and the player could walk out of the house afterwards. NOT seen / NOT verified: (6) villager memories: the guest had talked to nobody, so the promote logged `0 villager memories re-keyed`; nothing was re-keyed, no FRIENDSHIP line appeared, no villager was approached or talked to, and the host GCI could not be inspected after the final shutdown (the game processes were ended by the harness with `taskkill`, not a graceful `stop`; and file mtimes under the fixture dirs did not advance in this environment, so on-disk contents were not checked). Not seen: the appearance beyond the character model (name tag / gender text), a second resident, the interior after a house edit, villager greeting dialogue. Observation, not a bug: at the first frames of the resident arrival the client logs `entered own house 3: in-room commits NOT armed` while it is in the town scene; `pcnetgame_local_house_unsafe` also returns 1 while a fade / wipe is running, so the line is a fade artefact and the in-room tracking resets when the fade ends. No code was changed.

## Guest-first town: paid house purchase (host + rejoin)

Goal: a player can join any town as a GUEST first and later BUY a house from inside the game; the host then makes that guest a RESIDENT. This block is the host transaction, the client
request seam, the in-process rejoin and the test hook. The Nook dialogue (the "buy a house" offer) comes AFTER it and only calls the client seam below. Nothing here changes the
console `promote` behaviour, Play Online, the dedicated defaults, resident credentials, house sync or the reconnect.

**Flow.** Guest client -> `TXN_COMMIT` kind **14** `HOUSE_PURCHASE` (no new message id) -> the host runs ONE synchronous handler (`pcnetgame_handle_host_house_purchase_txn`, no yield):
READY gate -> binding gate (a RESIDENT sender is refused: PRECOND) -> guests.dat / members.dat trusted, record SYNCED, no push in flight -> shape -> journal (fence / replay / CONFLICT, same
rules as the other one-phase kinds) -> base epoch / rev (STALE_IMAGE) -> pre-image validation -> price -> house index range -> the guest MIRROR wallet can pay (the client's `pre_wallet` is
never trusted) -> the mirror wallet is debited IN MEMORY -> the shared core `pcnetgame_promote_exec(PROMOTE_ONLINE | PROMOTE_PAID)` -> `TXN_RESULT` APPLIED (post wallet) ->
`RESIDENT_HANDOFF` (66) -> `REJECT` 6 `PROMOTED` -> the peer is closed. All three messages ride the same RELIABLE ORDERED channel, so the client always sees them in this order.

**Price.** Vanilla: 1,000 Bells down payment + a 17,400 Bells loan (`mPlayer_DEBT0`). Here the guest pays everything at once: `PC_NETGAME_HOUSE_PRICE_DIRECT = 1000 + mPlayer_DEBT0 = 18,400`
(static-asserted, fits `tag.aux_item`). Only the WALLET counts (money bags in the pockets are left alone, unlike the Nook shop). The new resident starts with `loan = 0`
(`pc_mp_promote_set_paid` is a one-shot flag consumed by `pc_mp_promote_create`; the console `promote` keeps `loan = mPlayer_DEBT0`). The wallet carried into the resident is the already
debited mirror wallet.

**Message fields.** `tag.dest = NONE`, `slot = 0`, `item = 0`, `flags = 0`, `aux_item` = the price the client expects (!= the host constant -> `PRICE_MISMATCH` 21), `aux_cond` = the requested
house 0..3 or `0xFF` = auto; `base_epoch / base_rev / pre_*` = the usual pre-image (validated, then ignored for the money: the host decides from its mirror).

**Atomicity.** The handler and the core run in ONE call on the game thread (the host save runs only at frame boundaries). The core is the former body of `pc_net_game_dedicated_promote`
(the console command is now a thin wrapper, output / order of checks unchanged): token mint -> `pc_mp_promote_create` (vanilla Clear + Init + `mHS_set_use` + veteran event state + catalog
bits + re-keyed villager memories) -> lineage rev 1 -> members.dat (RESIDENT_TOKEN + PROMOTION_HANDOFF) -> `pc_save_write_authoritative` (the GCI) -> the guests.dat entry removed LAST.
ANY failure before the durable save rolls the game state, the lineage and members.dat back (existing code) AND the handler restores the pre-debit mirror wallet: the guest gets
`TXN_RESULT REJECTED`, wallet unchanged, no resident, no house. A second guest asking for the last slot in the same tick runs after the first returns and gets `NO_RESIDENCE`. A lost
`TXN_RESULT` after a durable purchase is recovered by the guest's next connect: its guest claim + token hit the persisted PROMOTION_HANDOFF (66 + REJECT 6 again, same as the console path).
The guests.dat backup / members.dat backup / records.dat backup of the console path are written for every purchase too.

**Reason codes** (appended to `PC_NETGAME_TXN_REASON_*`, 1..27 unchanged): `NO_FUNDS` 19 (reused: wallet < price), `PRICE_MISMATCH` 21 (reused), `STALE_IMAGE` 10 / `BAD_SHAPE` 11 / `BAD_IMAGE` 8 /
`NOT_SYNCED` 4 / `BUSY` 12 (reused), `PRECOND` 9 (a resident sender), new: `NO_RESIDENCE` **28** (no free resident slot, no free house, or no room for the 2 members.dat entries),
`INVALID_HOUSE` **29** (the requested house is not free / out of range), `NAME_TAKEN` **30** (a resident already has the guest's name). `net_spike_lib.py`, `wire_baseline.py` pin them.

**Client seam (M2)** (`pc_net_game.h`; the dialogue calls only these):
* `int pc_net_game_house_purchase_price(void)` -> 18400.
* `int pc_net_game_house_purchase_precheck(int house_or_auto)` -> 0 = may be tried, else a local reason: 9 PRECOND (not a READY guest client), 19 NO_FUNDS (local wallet < price), 28 NO_RESIDENCE
  (no free resident slot / free house in the LOCAL town copy), 29 INVALID_HOUSE (out of range / not free). `house_or_auto` = 0..3 or -1.
* `int pc_net_game_ts_begin_house_purchase(int house_or_auto)` -> 1 started, 0 refused (the local reason is in `pc_net_game_ts_last_reject_reason()`), -1 busy (retry next frame).
* `int pc_net_game_ts_poll(void)` -> `PC_NETGAME_TS_OP_PENDING / APPLIED / REJECTED` (consumed once); after REJECTED `pc_net_game_ts_last_reject_reason()` = the HOST reason (19 / 21 / 28 / 29 / 30 / 10 ...; 0 = link loss).
* On APPLIED the client debits the price from its local wallet (delta if it moved), logs `house purchase APPLIED`, and for a store character writes `nook_intro = bought` into
  `characters/<uuid>/towns/<townkey>/membership.ini`. `pc_character_membership_write` now PRESERVES every key it does not own (it used to rewrite the file); `pc_character_membership_get_key /
  _set_key` read / write one extra key. The handoff then flips `role` to `resident` and keeps `nook_intro`.

**In-process rejoin (M3).** REJECT 6 used to request a PROCESS relaunch (`pc_main_relaunch_poll`). Now, for a `--town-fetch` / Play Online client (a store character), `pc_main_relaunch_poll` calls
`pc_main_play_online_rejoin`: `pc_net_game_shutdown()` (role NONE), then the NORMAL Play Online connect (`pc_po_connect`) runs for the same character with a REJOIN flag: the town is
re-fetched (it now holds the resident), the membership says `resident`, the slot is bound by PersonalID and the RESIDENT claim carries the handed-over token. Differences to a menu-started
connect: no Play Online menu is open (that guard is waived), the start is a LIVE play scene (field / building: it waits for an idle scene, no wipe), and the fetched town is NOT loaded into
RAM under the old scene (only `pc_save_ready` is cleared, so no save writer runs) - the title's `trademark_init -> pc_save_reload` reads it after the existing fade to
the title (`FADE_TYPE_OUT_RETURN_TITLE`). Because a field scene counts as "world ready" (a title does not), `pc_net_game_client_world_hold(1)` keeps the new client session from building an identity
claim out of the OLD scene (the stale guest would be sent and refused); it is released when that scene was left, and the claim then comes from the new town as a RESIDENT. A rejoin that fails shows a title notice and fades to the title; it NEVER reloads save/card_a into the old live session. Fallbacks (documented, not
silent): if the rejoin cannot even be requested the old process relaunch (`pc_relaunch_connect`) runs; `AC_RELAUNCH_DRYRUN=1` keeps the old dry-run path (used by
`test_promotion_relaunch_real.py`); a CLI / legacy-profile client (no store character) keeps the old message path.

**Test hook.** `--house-buy-test H|auto` (client only, hidden, loud `[NET][HOUSE][TEST-ONLY]` logs, refused with exit 2 for any other role): once the record is SYNCED the hook raises the
LOCAL wallet to 20000 when it is below the price (its one local write), waits for the D3 upload, then calls `pc_net_game_ts_begin_house_purchase()` / `pc_net_game_ts_poll()`; fires once.

**Limits.** At most 4 houses / 4 residents (the vanilla table; slot / house selection lives inside `pcnetgame_promote_exec`, so a future >4 houses only changes that function). No Nook intro,
no first-job quest, no loan to repay (the price is paid up front). The guest record's letters and the other record state migrate like in the console promotion (see the lifecycle hardening section; fish records / host-held mail never existed for a guest). Money bags do not pay.
A host with `resident_tokens=off` hands off the same way (the handoff entry then stays until the operator resets it, as for the console path). The rejoin keeps the old scene alive for ~2 s on its OLD
RAM while it fades out (nothing is written).

Tests: `pc/tools/net_spike/test_house_purchase_protocol.py` (FakeClients vs real dedicated hosts on `bin_fixture4_hbuy`: NO_FUNDS / PRICE_MISMATCH / STALE_IMAGE / INVALID_HOUSE / BAD_SHAPE with the
files byte-identical, two guests racing for the last slot -> exactly one APPLIED, RESULT -> 66 -> REJECT 6 order, GCI loan 0 / wallet / house owner, guests.dat / members.dat / records.dat, the town
FULL -> NO_RESIDENCE, handoff again on reconnect, resident claim KNOWN, a resident sender refused) and `test_house_purchase_real.py` (one real store-character guest client with `--house-buy-test auto`
-> APPLIED -> the SAME pid re-joins in-process as resident 3 and reaches READY).

## Guest Nook dialogue (house offer)

Goal (milestone M4 of "Guest-first town"): a GUEST (`player_no >= PLAYER_NUM`, role CLIENT, READY link) who talks to Tom Nook is offered a house in Nook's own message window; accepting runs the
paid purchase of the section above (`pc_net_game_ts_begin_house_purchase` / `pc_net_game_ts_poll`). Residents, the host, solo play and every other Nook talk are byte-identical (every entry point is
guarded by `pc_net_game_house_offer_available()`: CLIENT role + READY link + the local player is a guest; all shop variants except Timmy / Tommy, i.e. `!aNSC_MAMEDANUKI`).

**Flow** (state machine `aNSC_pc_house_proc` in `src/actor/npc/ac_npc_shop_common.c`, installed as the `proc` of an existing action so that animation / camera / demo handling stay vanilla):
1. FIRST talk (membership.ini key `nook_intro` absent): `aNSC_start_wait` gets a new wait type `aNSC_WAIT_TYPE_PC_HOUSE` (only reachable for a guest): Nook greets and introduces himself (message
   `PC_NOOK_MSG_INTRO`, pages: greeting / "no house yet" / "would you like to buy a house here in town, hm?") and the Yes. / No. choice opens. The action is `aNSC_ACTION_CHECK_COL_CHG_OR_MAKE_BASEMENT`
   (the vanilla greeting-with-a-question action), then `proc` is switched to the house proc.
2. No. (or the B button = last choice): `nook_intro = declined` (`pc_net_game_nook_intro_set`, the membership writer preserves unknown keys; no store character -> kept in RAM for the session),
   "I see, I see. No pressure at all! ... just ask me under Other things", the conversation ends: the guest is NOT an employee (no mEv_* call: every first-job / intro check is `< PLAYER_NUM` already),
   no chores, no house, no slot, no loan.
3. Later talks (declined, or the first prompt was skipped, never `bought`): the top menu "Other things" row (`aNSC_request_Q_answer_wait`) shows the guest message `PC_NOOK_MSG_OTHER` = the vanilla
   text and choices of message 0x3E07 with a 5th-row insert "Buy a house." before "Umm, hang on!" (`SETSELSTR5`); choice 3 of `aNSC_request_Q_answer_wait2` starts the same flow as Yes.
4. Yes. / "Buy a house.": `pc_net_game_house_purchase_precheck(-1)`: 19 NO_FUNDS -> "Oh dear, you don't have enough Bells for that. A house costs 18,400 Bells." (still a guest, nothing charged, `declined`);
   28 / 29 -> "there are no lots available in this town" (`PC_NOOK_MSG_NOLOT`); 9 / anything else -> "something went wrong with the paperwork, nothing was charged".
   Otherwise `PC_NOOK_MSG_PICK`: price line, then the choice among the FREE houses of the local town copy (`pc_net_game_house_free_list`: `homes[h].ownerID` null): "House N." for each (1..4, `N = index + 1`),
   "Any house." when 2 or more are free, "Never mind..." (last = the B-button choice). The pick is re-checked with `precheck(house)`; `PC_NOOK_MSG_CONFIRM` ("House N, then! That comes to 18,400 Bells, payable
   right now, hm? Shall we make it official?" Yes./No.).
5. Yes.: `pc_net_game_ts_begin_house_purchase(house)` (-1 busy = retried every frame; 0 = refused locally, reason in `pc_net_game_ts_last_reject_reason()`), then `pc_net_game_ts_poll()` every frame while the
   question text stays on screen (the vanilla shop buy pattern). REJECTED = the host reason 19 / 21 / 28 / 29 / 30 / 10 / 0 (link loss) mapped to the NOFUNDS / NOLOT / FAILED rows (since the lifecycle hardening: 30 NAME_TAKEN has its own row, and a link loss after the request was sent shows the LINKLOST row instead of "nothing was charged"). APPLIED =
   `PC_NOOK_MSG_THANKS` ("Splendid, splendid! The house is yours, <name>! I'm filing the paperwork right now. Thank you very much!"). The client wrote `nook_intro = bought` itself.
6. The dialogue never waits for the session end. Because the host closes the session within a frame or two of the APPLIED result, `pc_net_game_ts_poll()` can already report the link loss (REJECTED, reason 0)
   when the dialogue polls one frame later: `pc_net_game_house_purchase_applied()` (sticky flag set by `pcnetgame_txn_apply_house`, cleared by the next begin) is the truth for APPLIED. One small coupling the other way
   round: the in-process rejoin (`pc_main_play_online_poll`, PO_REQUESTED) asks `pc_nook_house_rejoin_hold()` once per frame and WAITS while the congratulation row is being read (set when the purchase begins,
   cleared when the conversation ended or the purchase failed; since the lifecycle hardening it is a HEARTBEAT: the shop proc touches it every frame and it expires 30 frames after the last touch, with a hard cap of ~15 s after APPLIED; the old fixed 2400-frame hold is gone), otherwise the rejoin fade (a fraction of a second) hides the row (observed in the first visual run).

**Text mechanism: generated messages, not an overlay.** The port has no message override system; the only table is the ARAM message resource read by `mMsg_LoadMsgData`. This milestone adds the
smallest possible one: message ids `>= MSG_MAX (0x3F91)` and choice string ids `>= mChoice_SELECT_STR_NUM (607)` are generated at load time by `pc/src/pc_nook_house.c` in the game's own byte format
(`pc_nook_msg_build` / `pc_nook_sel_build`), hooked in exactly four places: `mMsg_LoadMsgData` (m_msg_main.c_inc), the two `index < MSG_MAX` bounds (`mMsg_ChangeMsgData`, `mMsg_request_main_index_fromNormal`
in m_msg_normal.c_inc) and `mChoice_Load_ChoseStringFromRom` (m_choice_main.c_inc). The text therefore renders in Nook's own window with his name tag, voice and the normal choice window (verified on screen).
The pre-PC ids 0..0x3F90 are untouched (the table has no free id: every id 0..0x3F90 has data, which is why the ids live above the table).
* Format learned from the real rows (extracted with the same `tools/msg_tool.py` tables): text bytes are ASCII for letters / digits / space / `! ' , - . ? :`; `\n` = byte 205; `7F 03 nn` = PAUSE; `7F 04` = wait for A;
  `7F 02` = clear; `7F 1A` = player name; a question ends with `SETSELSTR<n> id id ..` (`7F 16` = 2 choices, `7F 17` 3, `7F 18` 4, `7F 79` 5, `7F 7A` 6; ids are 2-byte big-endian into the choice string table),
  `7F 5E` (SELNOB: B = last choice), `7F 04`, `7F 0D` (OPENCHOICE), `7F 19` (FORCENEXT), `7F 09 09 00 01` (demo order 9 = 1: "answered", read by the actor) and `7F 01` (MSGCONTINUE); a closing row ends with `7F 00`.
  The actor reads `mChoice_Get_ChoseNum` once `mDemo_Get_OrderValue(TYPE_4, 9)` is set and `mMsg_Check_MainNormalContinue`, then selects the next message with `aNSC_Set_continue_msg_num` (same as the vanilla `0x1092` row).
* Charset: only letters, digits, space and `! ' , - . ? :` (the font has no arbitrary punctuation, `/` draws a music note); `pc_nook_house_selftest()` checks every byte (run from the source audit).
* Dynamic parts (price from `pc_net_game_house_purchase_price()`, the free house list, the chosen house) are built into the bytes when the message loads; the actor sets them with `pc_nook_house_set_pick` /
  `pc_nook_house_set_confirm` right before it selects the message.
* **Adding a message:** add an id to `enum` in `include/pc_nook_house.h` (and raise `PC_NOOK_MSG_COUNT`), add a `case` in `pc_nook_msg_build`, select it with `aNSC_Set_continue_msg_num(msg_p, shop_common,
  pc_nook_msg_id(PC_NOOK_MSG_X))` (or `mDemo_Set_msg_num` for a greeting). Choice strings: a new id after `PC_NOOK_SEL_ANY_HOUSE` (16 characters max) and a `case` in `pc_nook_sel_build`.
  No other file changes. Every row that asks something must end with the `nk_choice` tail, every other row with `7F 00`.

**Insertion points** (all `TARGET_PC`): `ac_npc_shop_common.c`: the block `aNSC_pc_hs_*` / `aNSC_pc_house_proc` (before the vanilla `aNSC_start_wait`), `aNSC_start_wait` (wait type), `aNSC_request_Q_answer_wait` (guest "Other things"
row), `aNSC_request_Q_answer_wait2` (choice 3). `pc_net_game.c`: `pc_net_game_house_offer_available`, `_free_list`, `_nook_intro_get/_set`, `_house_purchase_applied` (declared in `pc_nook_house.h`, NOT in
`pc_net_game.h`: that header is included by almost every translation unit, touching it rebuilds the whole tree).

**Test hook.** `--nook-test SPEC` (client only, hidden, loud `[NET][HOUSE][TEST-ONLY]` logs, refused with exit 2 for any other role; `pcnetgame_run_nook_test_hook`): SPEC = `wallet=N` (sets the LOCAL wallet once; the
normal D3 upload carries it to the host mirror and the hook waits for a clean record) and / or `warp` (a scene change into Nook's shop through the vanilla `goto_other_scene`, once the guest is idle in the town: the
walk from the station was not attempted). Rig: `pc/tools/net_spike/nook_visual_rig.py` (real dedicated host + one real store-character guest on disposable `bin_fixture4_nk_<tag>` copies, display pinned with
`AC_DISPLAY_NAME=samsung`, `AC_MASTER_VOLUME=1`) and `nook_visual_ctl.py` (synthetic scancode keys, only after the game window is the foreground window; captures only the client rectangle of the game window).
No automated assertion drives the dialogue: it is a manual / screenshot-driven tier.

**What was seen on screen** (Samsung monitor, game volume 1 %; screenshots of the game window only):
Three real runs (disposable `bin_fixture4_nk_s1 / s2 / s3` host + client copies; a real dedicated host and one real store-character guest client; the guest was walked into the shop by the test warp, NOT from the
station on foot; every dialogue key was a synthetic scancode, every screenshot the game window only). SEEN = a screenshot of the game window; LOG = client / host log or file; nothing else is claimed.
* **S1 (decline)**, wallet 0: SEEN Nook's name tag + the greeting pages ("Welcome, welcome! I'm Tom Nook, the owner of this shop, hm?" / "I see you're new in town, and you have no house of your own yet." /
  "Would you like to buy a house here in town, hm?") then the Yes. / No. choice window; No. -> "I see, I see. No pressure at all!" / "...just ask me under Other things, hm?", the conversation closed and the guest
  walked freely. SEEN: talking to Nook again opens the NORMAL shop greeting and menu (no second intro); "Other things" shows "Turnip Prices? / Hear code / Say code / Buy a house. / Umm, hang on!" (5 rows);
  "Buy a house." with 0 Bells -> "Oh dear, you don't have enough Bells for that. A house costs 18,400 Bells." / "Do come back when you can afford it, hm?" (this doubles as the cannot-afford scenario; no money was
  created or taken). LOG: `nook dialogue: the guest declined the first offer`, `membership.ini nook_intro = declined`, `precheck=19 -> row 5`; no first-job / intro / mEv line. Host after the run: `guests` = 1 guest
  (bound), `residents` = slots 0..2 only (no resident created); membership.ini `role = guest`, `nook_intro = declined`.
* **S2 (accept, first run)**, wallet 20000 (test hook, uploaded to the host mirror): SEEN the same greeting, Yes. -> "Wonderful! A house costs 18,400 Bells, paid all at once, hm?" / "Which house would you like?"
  with a choice window "House 4. / Never mind..." (only house 3 was free in the fixture, so "Any house." is not offered) -> "House 4, then! That comes to 18,400 Bells, payable right now, hm?" / "Shall we make it
  official?" Yes./No. -> Yes. LOG: APPLIED, `wallet 20000 -> 1600`, `membership.ini nook_intro = bought`, `M3: re-joining IN-PROCESS`, same PID, READY a second time as resident 3. HOST: `BOUGHT a house: resident slot 3,
  house 3, price 18400, wallet 20000 -> 1600, loan 0`; `residents` slot 3 "Buyer" credential=yes confirmed=yes connected=yes; `guests`: none stored; membership.ini `role = resident`, `nook_intro = bought`.
  SEEN after the rejoin: the player standing in the town in front of its new house (green roof, mailbox). This run exposed two defects that were FIXED before S3: the dialogue polled one frame after the host had
  already closed the session (-> link-loss REJECTED -> the FAILED row, invisible) and the rejoin fade started before the congratulation row could be read (black screen after Yes.).
* **S3 (accept, after the fixes)**: same flow; SEEN after Yes.: Nook's congratulation row "Splendid, splendid! The house is yours, Buyer!" (the player's name through the name code; page 2 "I'm filing the
  paperwork right now. Thank you very much!" read with A), then the fade to the title logo scene of the SAME process (PID unchanged), then the player in the town as the resident next to its house. Also SEEN in that
  frame: the pre-existing refusal notice of REJECT 6 ("Cannot join the host: This character was promoted to a resident of this town: restart to join as a resident. (reason 6)") drawn over the shop during the
  hold, a cosmetic leftover of the M2 / M3 notice (it is not suppressed by this milestone). LOG / HOST of S3: `nook dialogue: purchase APPLIED -> congratulation row`, the rejoin waited until the row was closed, same PID 215996; host `BOUGHT a house: resident slot 3, house 3, price 18400, wallet 20000 -> 1600, loan 0`, `residents` slot 3 "Buyer" credential=yes confirmed=yes connected=yes, `guests`: none stored, membership.ini `role = resident` + `nook_intro = bought`. The GCI file itself (private_data slot, house owner) was NOT re-parsed in these runs (that is covered by `test_house_purchase_protocol.py`); only the host log / console output were read.

**Limits.** The house is chosen by NUMBER ("House 1..4"), not by a position on a map. "Any house." appears only when 2 or more are free. The offer needs a READY link (a guest talking to Nook while the link is
down gets the normal shop). A client without a store character (legacy / CLI guest) keeps `declined` in RAM only. The congratulation row is shown, but the rejoin that follows fades out right after it;
Timmy / Tommy (the mamedanuki variant of the shop code) do not make the offer. Source audits re-pinned for this change: `test_guest_src.py` (the `Save_Get(homes[...])` list gained the loop-bounded
`pc_net_game_house_free_list`). `test_shop_src.py` (2 checks: `W` reason names / `C` TS CLIENT block contains no `inventory`) and `test_d3_record_src.py` (check `P`) were already failing for the earlier house-purchase milestones, not for this one.


## Guest -> resident lifecycle hardening

Patch on top of the promotion (M-F), the paid purchase and the Nook dialogue. Protocol version and wire ids are unchanged (max id 66); `pc_net_game.h` is byte-identical to before (the allocator prototypes live in `pc/include/pc_residence.h`).

### Corrections to the earlier statements

* **Fish records are NOT lost.** `Save_t.fishRecord[5]` is written only by client actors (`mEv_fishRecord_set` from the fishing / angler code) and there is no network path for it; a guest's CATCH stores no catcher PID, so a guest's catches never carry the guest PID on the host. The only PID-keyed fish entries that can exist are written by a resident. `pc_mp_promote_migrate` still re-keys an entry whose pid equals the guest key (defensive, a no-op in practice; the entries are in the rollback snapshot). Observed in the S2 test: a fish record seeded in the GCI for a non-resident PID never reached the migration (`fish_records=0` in the `[PC] migrate:` line): the game's own `mFR_delete_npc_record` purges records of non-residents, so the fish re-key is source-audited only.
* **Mail "held for the guest" does NOT exist on the host.** No host letter can be addressed to a guest (`MAIL_SEND` needs a house on the host). The letters a guest owns are the ones in its own record's inventory (`mail[10]`), which DO migrate now (below).
* What the earlier text called "lost" and really was lost until now: the record's inventory letters `mail[10]` (@0x04E0) + `saved_mail_header` (@0x04A6), the equipment in hand (@0x04A4), the delivery / errand quests, the collection bit fields, the maps, destiny, hint_count, the background texture, sunburn, remail, animal_memory, and `animals[a].contest_quest.player_id` (set by `mQst_SetReceiveLetter`) for the 15 villagers and the islander.

### What migrates (`pc_mp_promote_migrate(gkey, grec, priv)`, `pc_m_card.c`)

Called inside `pc_mp_promote_create` right after `pc_residence_assign` (the house is set), BEFORE the durable save, so it is atomic with the promotion and covered by `pc_mp_promote_rollback` (the snapshot now also holds `fishRecord[5]`; `priv`, `animals[]` and the island animal were already in it). `pc_pid_rekey(PersonalID_c*, gkey, npid)` copies the WHOLE PersonalID (name, land_name, player_id, land_id) when the entry equals the guest key.

| carried | rule |
|---|---|
| villager memories (15 + islander) | as before: new PID; town villagers also get the host land + melody; the islander's union is left alone |
| `contest_quest.player_id` of the 15 villagers + the islander | re-keyed |
| fishRecord entries with the guest pid | re-keyed (defensive) |
| `mail[]` used letters only | compacted to the front; recipient / sender PID == guest key -> the new PID; a present that is not pocket-legal is cleared; `saved_mail_header` copied |
| `equipment` | only when `pc_net_game_pocket_legal_item` |
| collection bit fields (aircheck, furniture, wall, carpet, paper, music) | OR-ed into the bits the intro already set |
| `maps`, `backgound_texture` (a shirt or empty), `hint_count`, `destiny` (valid type), `sunburn` (valid rank), `remail`, `animal_memory` | copied |
| deliveries / errands | quest type delivery / errand only, first-job kinds and `errand_type == FIRST_JOB` are NOT carried |

NOT carried: `state_flags`, `museum_record` (host-owned), `reset_code`, `ecard_letter_data`, `calendar`, the tortimer trophies. The host log prints one line `[PC] migrate: memories=N contest_quests=N fish_records=N letters=N equipment=N quests=N`. The promotion also resets the host's `s_remail_day[slot]` (a stale day of a former occupant of the slot would skip the new resident's remail pass; it is restored on the rollback paths).

### Durability gate (B3a)

`pc_save_write_gci_to()` returns TRUE WITHOUT WRITING when the save is not ready or is a sanitized transfer image; `promote_exec` used to treat that as durable and then removed the guest entry. New `pc_save_can_be_durable()` / `pc_save_write_authoritative_durable()` (FALSE unless the GCI was really written). `pcnetgame_promote_exec` (console AND the kind-14 purchase) refuses up front with `BUSY` when the save cannot be made durable (nothing changed, the guest entry and the wallet intact; the purchase handler restores the mirror wallet) and step 4 uses the durable variant.

### Purchase applied flag (B3b) and the link-lost row (B2)

`pcnetgame_txn_apply_house` sets the sticky "purchase applied" flag BEFORE its post-image consistency check, and `pc_net_game_house_purchase_applied()` is also true once a RESIDENT_HANDOFF (66) for the claimed town arrived in this process, so the dialogue never shows FAILED after a purchase that succeeded. New Nook rows: `PC_NOOK_MSG_LINKLOST` (reason 0 after the request was sent: the outcome is unknown, the text does not say "nothing was charged") and `PC_NOOK_MSG_NAMETAKEN` (reason 30 was shown as "no lots available"). A request refused locally (nothing sent) still uses the FAILED row.

### Stale guest entry (B4b / 4c)

The save hook writes `guests.dat` right after the GCI with the guest still present, so a crash between the GCI and step 5, or a failed step-5 write, left a STALE guest entry; after the first confirmed resident login the handoff was deleted and an authenticated returning guest would be admitted with the old pockets / bank (duplicated) in one of the 8 never-evicted slots. Fixes: (1) `pcnetgame_resident_confirm` removes the `guests.dat` entry whose key == the handoff's `aux_pid` FIRST (backup like `guest-remove`; when that fails the handoff is KEPT) and then deletes the handoff; (2) a load-time sweep (`pcnetgame_promoted_guest_sweep`, once per process at world ready) removes the guest entry of every handoff of this town whose resident exists in the loaded GCI and whose RESIDENT_TOKEN entry exists. Never while `guests.dat` / `members.dat` is UNTRUSTED. With `resident_tokens=off` `members.dat` is never loaded, the handoff is never confirmed and the sweep does not run: such a host needs `guest-remove` after a crashed promotion (documented, not changed).

### Client token file (B8)

The handoff handler used to REPLACE `token.dat` by the single resident entry before writing `membership.ini` (a crash between the two left a guest membership with no guest token). Now `token.dat` is loaded, gains the resident entry and keeps the guest entry; `membership.ini` is written next; the guest entry is dropped (`pcnetgame_client_drop_guest_entries`) only after the first KNOWN resident login ("resident token verified by the host"). A client that dies between the two files simply claims again with the guest token, gets 66 + REJECT 6 again and finishes. Test hook: env `AC_PROMOTE_CLIENT_CRASH` makes the client exit exactly between the two writes.

### Last-house race (B12)

The handlers are synchronous (`pc_save_write_authoritative` is plain file I/O, no pumping), so two kind-14 requests in one tick run one after the other. The invariants added to the permutation check (`pc_residence_check`, run before the snapshot and again by `pc_residence_assign`): the slot `mHS_get_pl_no(house)` maps to is the new slot or a free slot, and every live resident r owns `homes[arrange(r)]`; a failure is a refusal (nothing changed), the purchase answers REJECTED.

### Test hooks (production cleanup, C)

Hook flags and env switches that inject state exist only when the build is configured with `-DPC_TEST_HOOKS=ON` (compile definition `PC_NET_TEST_HOOKS`, the default of the development build so the suites keep working; **release builds must configure `-DPC_TEST_HOOKS=OFF`**) and need the runtime env `AC_TEST_HOOKS=1`: a hook flag without it is refused with a stderr message and exit 2, a hook env switch without it is ignored with a stderr note. Covered: `--house-buy-test` and `--nook-test` (they raise the LOCAL wallet), `--d3-test-wallet-add[-late]`, `--house-test-host-edit`, `--mail-test-*`, `--txn-fault` (`pcnetgame_txn_fault_fire`), the new `--promote-fault fail_save|crash_after_members|crash_after_gci|no_durable` (host only: the first fails the authoritative save once, the crash modes `_exit` right after the members.dat write / the GCI save, `no_durable` reports the save as not durable), `AC_RELAUNCH_DRYRUN`, `AC_TOWN_NO_MSGBOX`, `AC_PROMOTE_CLIENT_CRASH`. `--help` lists them on one `Test builds only` line. `net_spike_lib.py` puts `AC_TEST_HOOKS=1` into every game process it launches; the tests that launch the game themselves set it explicitly. `test_hook_guard_src.py` audits that every hook sits inside the guard.

Other test-only flags (`--pickup-test-seed`, `--world-test-force`, `--ts-test-*`, `--shop-test-*`, `--scene-test-*`, `--collide-test-*`, `--force-*`, `--txn-test-*`) were not touched: they do not raise money or bypass the host transaction rules in the way the guarded ones do. They are still compiled in.

### Rejoin hold heartbeat

The fixed ~40 s rejoin hold is replaced by a heartbeat: the Nook shop proc calls `pc_nook_house_rejoin_hold_touch()` every frame while in BEGIN / PENDING / END; the hold expires 500 ms (30 frames at 60 fps) after the last touch, with a hard cap of 15 s after APPLIED (`pc_nook_house_rejoin_hold_set(2)`). Both limits are WALL CLOCK (`SDL_GetTicks`): the first version counted polls, but `pc_main_play_online_poll` runs from `VIWaitForRetrace`, many times per game frame, so its 900 "frames" were gone ~4 s after APPLIED, in the middle of the congratulation row (seen in the S1 run 2 log: `901 polls since APPLIED`). Log lines: `rejoin hold set to N`, `rejoin hold EXPIRED (heartbeat)`, `nook dialogue: the conversation ended -> the rejoin hold is released`. The wait in `pc_main_play_online_poll` is unchanged (it polls `pc_nook_house_rejoin_hold()`).

### Residence allocator (D, behaviour-identical)

`pc_residence.h`: `PC_RESIDENCE_SLOTS = PLAYER_NUM`, `PC_RESIDENCE_HOUSES = mHS_HOUSE_NUM`, `_Static_assert(== 4)` (house_arrangement packs 2 bits per player). Host pair in `pc_net_game.c`: `pc_residence_find_free(want_slot, want_house, &slot, &house, &reason)` (the console / purchase rules, auto prefers `mHS_get_arrange_idx(slot)`), `pc_residence_assign(slot, house)` (invariants + `mHS_set_use` + `mEv_ClearPersonalEventFlag`); client twin `pc_residence_local_free(...)` used by `pc_net_game_house_purchase_precheck` and `pc_net_game_house_free_list`. The literals of the new code (`aux_cond <= 3u`, `house_or_auto > 3`, `& 3` / `< 4` in `pc_nook_house.c`, `aNSC_pc_hs_free[4]`, `house >= PLAYER_NUM` in `pc_mp_promote_create`, `promote_parse_index`, the `promote` usage text) derive from them.

**What more than 4 houses would still need (not implemented):** `Save_t` arrays `private_data` / `homes` / `keep_house_size` / `mother_mail`; the GCI layout / version and the byte swapping; the 2-bit `house_arrangement` and the vanilla `mHS_*` (`house_no < PLAYER_NUM`); house acre / actor placement; `PC_NETGAME_HOUSE_NUM` house sync; record and resident table sizes; the sanitized transfer image; the museum 4-bit donor nibbles; per-player event bits; `return_animal.talk_bit`; post office quotas / address lookup; Nook's choice paging beyond 6 choices (4 houses + Any + Never mind already fill it).

### Sync after a promotion (E): read, one limitation documented

Verified by reading: the host's `pcnetgame_host_reject_and_close` for the promoted peer calls `pc_remote_player_on_disconnect` and relays a CLEARED scene to the other READY clients, so the old guest puppet disappears like after any disconnect (or by the liveness timeout); the new resident's APPEARANCE at its READY replaces the entry by player id. NOT done: other clients' local copy of the town keeps the stale `private_data[slot]` and `house_arrangement` of the new resident until they re-fetch (`homes[h]` is pushed only with `--house-sync` and never to guests). A roster refresh would need a new push channel (no existing service carries the resident table; a new wire id is avoided on purpose: 15 tests pin the id range), so it is not hacked in here.

### Tests

* `test_guest_lifecycle_protocol.py` (S2 + S3; run 3 of 3: 74/77, the three failures all about the seeded fish record, which the game purges before the migration, see above; the checks were then relaxed to "never left keyed to the guest" and NOT re-run (the run cap was used); everything else of S2 / S3 passed: byte-identical rollback files, rollback visible in the GCI, the migration bytes, `no_durable` BUSY, the race, `crash_after_gci` with the sweep, `crash_after_members` with the orphan prune),  scripted FakeClients against REAL dedicated hosts on the disposable `bin_fixture4_lcyc`): S2 migration byte checks (mail re-keyed byte for byte, equipment, bits, quests, first-job errand and state_flags NOT carried, memory / contest / islander contest / fish re-keyed, controls untouched), `fail_save` rollback (GCI / guests.dat / members.dat byte-identical, rollback visible in the next GCI, the retry applies), `no_durable` refusal; S3 last-house race (+ a byte-identical resend of the winner), `crash_after_gci` (sweep, 66, KNOWN, replayed old token refused), `crash_after_members` (handoff invalid, guest admitted with the pre-debit wallet, orphans pruned). The first upload of a guest must be a FRESH character, so the S2 content enters through the stored `guests.dat` record (like the purchase test).
* `test_guest_lifecycle_real.py` (S1, real host + real store-character clients on the Samsung monitor, 1 % volume, keys = SendInput scancodes synchronised on the test-build log line `[CHOICE][TEST-ONLY] choice window is NORMAL`, screenshots of the game window only): 41/41 in its last run (3 runs). SEEN in screenshots: the intro offer with Yes. / No. and 0 Bells in the funds box; after No. Nook's "I see, I see. No pressure at all!"; after the relaunch (wallet 20,000) the NORMAL top menu (I'd like to sell / See my catalog / Other things / Never mind...: no second intro offer); `Other things` with the extra row (Turnip Prices? / Hear code / Say code / Buy a house. / Umm, hang on!); the house choice (House 3. / House 4. / Any house. / Never mind...); "Shall we make it official?" Yes. / No.; the congratulation row (funds 1,600 Bells) whose second page was still being typed, with NO fade, 3.5 s after APPLIED (the heartbeat held it; in that run the row was not closed by the test, so the hold ended at its 15 s cap: `EXPIRED ... 15004 ms since APPLIED`; the release by the end of the conversation is NOT seen); then the player standing next to its new house in the town; after the host restart the same picture (the resident reconnected by itself, `identity re-established (resident: resident 2 | guest slot 0)`, host `credential verified (token sent: KNOWN)`). LOG / FILES: token.dat held guest + resident until the first KNOWN login and only the resident after (`1 obsolete guest token entry ... dropped`), host: members.dat = the confirmed credential only, guests.dat empty. Part B (`--house-buy-test auto` + env `AC_PROMOTE_CLIENT_CRASH`): the client exited with code 96 between the two files (token.dat both entries, membership.ini still `role = guest`); the relaunch got the handoff again, rejoined in-process, KNOWN, one entry. No screenshots were taken of Part B.
* `test_hook_guard_src.py` (new, 20/20), `test_guest_src.py` 83/83, `test_shop_src.py` 64/64, `test_ts_src.py` 72/72, `test_d3_record_src.py` 136/136 (re-pinned), `test_mail_src.py` / `test_mail2_src.py` (help-line pin).
* One-entry token pins: `test_promotion_relaunch_real.py` now asserts BOTH entries after the handoff and ONLY the resident entry after the first KNOWN login (`test_resident_credentials_real.py` / `test_resident_edge_real.py` concern plain residents and are unchanged).

### Limits

Not run: the full suites, `test_house_purchase_real.py` / `test_promotion_relaunch_real.py` (only their environment plumbing and pins were changed), wildlife. The mail / contest / fish / memory effects were checked in the saved GCI bytes, not in a running game. Mail sent by other residents to the new resident's NEW PID is the vanilla path, untested here.

## Known limitations

* A guest needs a copy of the HOST's town save: either fetched with `--town-fetch` (the host must serve it, `--town-serve on`; see "Town cache and town transfer") or copied by hand
  into `save/card_a/DobutsunomoriP_MURA.gci`. The town identity (land name, id, terrain hash) must match the host's or the join is refused (LAND_MISMATCH / no matching save).
  A freshly generated town is not acceptable.
* The empty-economy rule only constrains a NEW guest's first upload. Afterwards the client-owned ranges (pockets, wallet, bank, ...) are uploaded after a legality check only,
  exactly as for residents; a modified client can still edit its own inventory by later uploads (pre-existing trust model).
* A guest's `player_id` / `land_id` / `name` / `home_town` cannot be changed without becoming a different guest; guests.dat holds 8 entries for ALL towns, confirmed entries are
  never evicted; the dedicated console tools `guests` / `guest-remove` / `guest-reset-token` (G6.2) are the only way to free a slot or recover a lost token.
* The guest token is a bearer secret over unencrypted UDP (fine for LAN / friends).
* `--guest` / `--bootstrap-guest` still fire on the first title-demo frame (now after a save reload and without the gateway flag); the RIDE_OFF_DEMO / station-master arrival and the
  title-menu "Join as Guest" click are not visually verified / not exercised by a process test (G3).
* The guest profile name charset is ASCII on purpose (`A-Za-z0-9 .'-`); no in-game name editor yet.
* The home land of a guest may by chance equal the host town's land id (probability 1 / 65534); since G6.3 the CLIENT refuses to arrive with a message telling the user to edit `land_id` in guest.ini (a different
  land_id is a NEW guest); there is still no automatic re-draw (the ids are permanent).

## Deferred

* Town transfer from the host (so a guest needs no local copy), in-game profile creation UI (vanilla name editor), changing the look of an existing guest, villager -> guest
  letters, museum / mail / bank for guests, a real-client gameplay run for guests (needs UI automation), the deferred player_no 4 indexed uses listed under G5.0, an additive REJECT_INFO wire message (deliberately NOT implemented in G6: the frozen reject reasons are mapped to text locally), a soak test with 8 real clients (G4 follow-up),
  encrypted / challenge-response token handshake, a persisted operator recovery flag.

### Own-room ground pickup (client, own player house)

* **Bug**: a network CLIENT could not pick up layer-0 ground items (e.g. cherries it had dropped) inside its OWN player-house room: the pickup animation played and nothing happened.
* **Cause**: `Player_actor_setup_main_Pickup` (`src/game/m_player_main_pickup.c_inc`) replaced the vanilla `Player_actor_putin_item` (pocket grant + `mFI_SetFG_common(EMPTY_NO)`, which in a room aliases `Save homes[h].floors[f].layer_main[0]`) by `pc_net_game_request_pickup()` for every client. That function refuses before sending: with house sync on, `pcnetgame_txn_begin_blocked()` is true the whole time the owner is in the room (`pcnetgame_house_client_txn_blocked`); with sync off, the town-only rule (`!pcfa_scene_is_town()`) applies. The drop worked because `mTG_room_put_proc` has no network seam. The host was never affected (`pc_net_game_field_tile_reserved` is 0 indoors).
* **Fix**: new predicate `pc_net_game_client_own_room_local()` (client + resident with a house + player-house room scene + room number == own house + `field_type == PLAYER_ROOM`; guests and visitors excluded). When it holds, the pickup seam takes the vanilla branch (the local mutation, like the drop). Full pockets (`slot_idx < 0`) block the pickup (item stays on the floor, no exchange flow); the PC_ENHANCEMENTS money-bag-to-wallet shortcut is skipped so a bag enters a pocket (house conservation counts pockets and mail gifts, not the wallet). The house-sync refusal in `pc_net_game_request_pickup` is now logged (rate limited, 1 in 32).
* **Safety**: the pocket and the room cell change in the same frame; the in-room snapshot is only taken in WAIT/WALK when quiet, so every committed (snapshot + pockets) pair is one consistent moment; no pocket transaction runs in the room; plain record uploads are held while inside (`pcnetgame_house_client_upload_deferred`); a STALE -> rollback -> FULL adoption is deferred until exit restores house and pockets together, so a reconnect cannot resurrect the item; with sync off the house is owner-local exactly like the room drop. There is NO host-side room-floor transaction: the host validates the (house, pockets) pair at OWNER_COMMIT (conservation). No wire change.
* **Limitations**: remote observers see the pickup pose in a shared interior but not the flying item / pickup effect (no event for room pickups on the wire; the sounds and flying item remain gated to the town field); visitors inside ANOTHER player's house still cannot pick up (a local pickup there would create the item); the pickup with full pockets only plays the animation.
* **Manual tests (not run)**: client drops cherries in its own room, picks them up (pocket gets them, cell empties, no duplicate after leaving / reconnecting); full pockets leave the item; a money bag lands in a pocket; with `--house-sync` and without; a visitor in another house still refuses. Protocol half: `test_house_sync_protocol.py --only P3`; source audit: `test_house_sync_src.py`.

### House state across disconnect/reconnect (client, own player house)

* **Bug**: a house-sync client standing inside its OWN house lost the link and could keep editing (ground items, furniture, wallpaper, drawers), all local only. After the reconnect the room still showed the stale local state until the player left. Worse, when the host PROCESS kept running (a network blip of >= 5 s) the new HELLO was a continuation (`HAVE_LAST` with the same host session / epoch / rev): no FULL push, the CANON_PUSH equalled canon ("local edits kept, to be committed"), and at the exit `commit_tick` sent the pair. Both bases matched and a room <-> pocket move passes CONSERVATION, so the offline edits became AUTHORITATIVE on the host.
* **Offline taint** (`s_hcp.offline_taint`, TARGET_PC, house sync only): set in `pcnetgame_client_on_link_lost` BEFORE the session reset when house sync was on and the player owns a house. It survives the reset (like canon / stash). While it is set: `pcnetgame_crec_send_hello` OMITS `HAVE_LAST`, so the host (`slot->rev != 0`, no continuation) always answers with a FULL record push (`host_only = 0`) even inside the same host session; `pcnetgame_house_client_commit_tick` and the quit flush REFUSE (nothing offline is ever committed or flushed); the edit lock is on. Cleared ONLY by: a FULL record adoption that put house + pockets back to the host's pair (`pcnetgame_house_client_on_full_adopt` outside the house, or the own-room reconcile finish inside it), a new local player (owner stamp change) or a host that does not announce house sync. A failed restore keeps the taint. `s_hcp.sync_last` (set by `pcnetgame_house_client_set_hostcfg`, survives the reset and an owner change) records that the last host announced house sync.
* **Edit lock** (`pc_net_game_client_room_edit_locked()`): CLIENT + `sync_last` + `pc_net_game_client_own_room_local()` and (link not READY, or offline taint, or - unless the session disabled house sync - the own house unknown / the record not SYNCED). Gates refuse BEFORE touching any state: `Player_actor_CheckAndRequest_main_pickup_all` (PLAYER_ROOM: return FALSE, no animation, deliberately not the refuse demo which says "pockets full"; belt-and-braces `host_pickup_blocked` in `Player_actor_setup_main_Pickup`), `mTG_room_put_proc` (the game's own refusal row `mWR_WARNING_PUT_ITEM`, pocket unchanged), the push / pull / rotate A-button grab in `ac_my_room_move.c_inc` (before `mPlib_request_main_hold_type1`), wallpaper / carpet (`mTG_nw_carpet_proc`, `mTG_nw_cover_proc`, `mTG_carpet_proc`, `mTG_cover_proc`) and the drawer / music-box put-in (`mTG_putin_proc`), and the drawer / music-box choices in `ac_my_room_msg_ctrl.c_inc` (`aMR_PcEditLocked`: put in, take out and the MD switch are treated as "cancel"; listening to the music box is not gated). DEVIATION from the spec: the wallpaper / carpet gates are in the CALLERS in `m_tag_ovl.c`, not in the reserve procs of `ac_my_indoor.c`: those return `EMPTY_NO` as "old item", and the caller stores that into the pocket, so refusing there would ERASE the pocket item. Message: `pc_net_game_room_edit_denied()` arms a ~3 s line drawn by `pc_net_notice_draw` ("Not connected - house changes are disabled" while the link is down, "Leave the house to resync" once it is back but the reconcile is waiting); it is naturally rate-limited (one line, re-armed by each refusal).
* **In-room reconcile** (taint only; otherwise a FULL push inside the own room is deferred until the exit, as before): `pcnetgame_house_client_adopt_blocker` lets a staged FULL push through while the owner is inside only when `pcnetgame_hcl_reconcile_ready(h)` holds: sync `known`, no commit in flight, `live_room_house(1) == h`, the room quiet >= 30 frames, a usable target (the stash, else canon), the target's `wall_floor` equals the live indoor actor's `wall_num` / `floor_num` (`aMI_pc_wall_floor_matches`; stale values would later mint the wrong item in `aMI_GetWallFloorItem`; else defer) and no new furniture within one cell of the player (the visitor rule; else defer). In `pcnetgame_crec_try_adopt` (`!host_only`, inside the own room) the room is rebuilt FIRST (`aMR_pc_live_reload`, whose callback writes the image into the save; actors are destroyed without write-back, so offline drawer edits are dropped, which is correct), and only then `pcnetgame_crec_apply_staged` adopts the pockets, in the same call / frame; `pcnetgame_house_client_reconcile_finish` then sets canon from the image, clears stash and taint, `rollback`, `room_stopped`, and re-captures the in-room baseline from the image plus `Now_Private`. If the live floor already equals the target layers the rebuild is skipped (live floor bytes kept) but the adoption still happens. If the reload returns 0 the push stays staged (the shared deferral: ADOPT_DEFERRED acks, lifecycle-time watchdog) and the exit adoption path (`on_full_adopt`: stash, else canon) restores the same state consistently. Defer, never force.
* **Limitations / unverified**: the in-place rebuild of the OWN room was never run (no real room test was possible): layer-0 ITEM1 redraw, stale `bed_ftr_actor_idx` / `demo_ftrID` / `emu_ftrID` of the player actor, gyroid / cockroach state and a bgm blip are unverified (the visitor rebuild shares them). Drawer / music-box contents edited offline are dropped. A host with record rev 0 (lost records.dat) answers MIGRATE_REQUEST instead of a FULL push: the taint then stays until the next login (edit lock and commit refusal keep protecting the host; the plain record upload is held while the taint is set). The lock also covers the first seconds after a (re)connect until the CANON_PUSH and the record arrive. Edits made in the ~5 s window before the client DETECTS the lost link are local only; the taint is set when the loss is detected and the reconnect restores the host's pair (the edit is reverted, not committed).
* **Tests**: source audit `pc/tools/net_spike/test_house_sync_src.py` (section O: HELLO without HAVE_LAST under taint, host answers PUSH_FULL, commit_tick / quit flush refuse, lock gates at every anchor, reconcile ordering, wall_floor equality, own-house reload only inside try_adopt); protocol test (a HELLO without `have_last` in the same host session gets PUSH_FULL) in `test_house_sync_protocol.py`. No real-client run.
* **Manual tests (not run)**: (1) kill the host while inside the own house, then try pickup / drop / push / pull / wallpaper / carpet / drawer: refusal or no-op with the notice; (2) restart the host: the room reconciles in place with position and camera kept and no duplicates after leaving; (3) cut the network for ~6 s and edit inside the detection window: the edit is reverted, not committed (no `OWNER_COMMIT ... APPLIED` in the host log for it); (4) stand on a removed piece's cell: the reconcile is deferred ("Leave the house to resync") until you step away or leave; (5) `--house-test-fidelity` still reports MATCH at the exit.

## Release readiness summary (G1 .. G6)

What a guest can do today (evidence tier in brackets):

* Start a client (`--connect host:port --guest`, or the title item "Join as Guest (NAME)") with a generated or hand-edited `save/mp/guest.ini`; the arrival path re-reads the town save, builds a FRESH character,
  binds it as the foreigner without the visitor gateway, and walks the vanilla train arrival at the station [real: G3 / G4 arrival and adoption; the cutscene and the title click are NOT visually verified].
* Be admitted by the host as a guest (never a resident), receive a host-minted token at first contact, return with it and get the SAME record back (also after a host restart, after an abrupt guest kill, with several
  guests at once up to the `max_guests` cap) [scripted + real: G1, G4, G6].
* Play with the guest record owned by the host: pickup / drop / bury / dig / shop buy and sell / police / catch / villager talk and letters to villagers are accepted and journalled; museum donation, mail to players,
  mailbox, house entry, catalog ordering are refused with the game's own refusal rows [scripted + source audit: G5; no real-client gameplay run].
* Get a readable reason when the join fails (server full / guest limit, wrong town, protocol version, token problems, host unreachable) on screen and in the log [real: the cap refusal and the home-land refusal; the
  other texts by source audit; drawing not visually verified: G6.1].

What a host operator can do: run `--host --dedicated` and use `guests`, `guest-remove <guest> confirm`, `guest-reset-token <guest> confirm` (always backed up, refused while the guest is connected), set
`--max-guests N` / `max_guests`, and rely on a corrupt guests.dat producing UNTRUSTED mode (guests refused, residents unaffected, the bad file preserved) instead of a crash or silent re-issue of tokens [scripted: G6].

Known limitations that materially affect play (all documented above and in "Known limitations"):

1. A guest needs a MANUALLY COPIED copy of the host's town save (`DobutsunomoriP_MURA.gci` in `save/card_a`); there is no town transfer. A wrong or missing copy is now explained on screen but not fixed.
2. The guest token is a bearer secret over unencrypted UDP: anyone who can see the traffic can be that guest (fine for LAN / friends, not for the open internet).
3. The guest table holds 8 entries for ALL host towns; confirmed entries are never evicted. The operator tools (G6.2) are the only way to free a slot or recover a lost token; recovery trusts whoever claims the key first
   while it is armed.
4. The arrival cutscene (train, station master), the title-menu "Join as Guest" click and the new on-screen messages were never seen by a person or a screenshot; they are verified by logs, process state and source audit only.
5. No real-client gameplay run beyond arrival / reconnect (no UI automation): the G5 guards and the guest activity table are scripted / source audited.
6. At most 3 real client processes were ever run together; 4..8 guests and 8 peers are scripted without puppets / rendering, so the memory and frame cost of 8 real puppets is unmeasured (default cap 4).
7. A guest never owns a house, mailbox or museum donor slot; villager letters to guests, bank / loan UI and the birthday calendar are deferred.
8. Guest identity (name, home town, ids) is permanent: changing it makes a new guest, and the old character stays on the host until the operator removes it.

Verdict: ready for a supervised friends-and-LAN test of arrival, reconnection, capacity and failure handling; NOT ready to be advertised as a public / internet feature until limitations 1, 2 and 4 are addressed.

## Hostname server addresses + client ping counter (release prep)

- **Hostnames.** A saved server / `--connect` address is an IPv4 literal or a DNS hostname (labels of `[A-Za-z0-9-]`, 1..63 characters in total, no IPv6, a name of only digits and dots must be a valid IPv4 literal). `servers.ini` stores it verbatim. `pc_net_client_connect` resolves it once per connect with `getaddrinfo(AF_INET)` (IPv4 literal fast path unchanged); the town prefetch uses the same call. Nothing else changed, the UDP wire protocol is untouched. A host IP change while the game runs is only noticed at the next connect (auto-reconnect reuses the address resolved at connect).
- **Ping counter.** `settings.ini` `show_ping = 0|1` (default 0), `F4` toggles it for the session. It reads the RTT the reliable layer already keeps (`srtt`, DATA -> ACK samples, `pc_net_client_rtt_ms`), refreshed once a second, shown top-right only while a client is READY; "Ping: --" until a sample exists or when the newest is older than 30 s. It sends no packets and never feeds heartbeat/timeout/reconnect logic. Samples only occur when the client sends reliable data, so a long idle stretch shows "--".
