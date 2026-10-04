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
(40: three real guests from one cwd, kill / restart one, host restart). Commit: (filled by orchestrator)

## Known limitations

* A guest needs a manually copied copy of the HOST's town save (`save/card_a/DobutsunomoriP_MURA.gci`): there is no town transfer, the town identity (land name, id, terrain
  hash) must match the host's or the join is refused (LAND_MISMATCH / no matching save). A freshly generated town is not acceptable.
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
