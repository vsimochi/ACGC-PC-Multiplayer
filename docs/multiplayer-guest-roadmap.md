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

### Furniture synchronization: Stage 1 (host-authoritative, opt-in `--house-sync`)

Stage 1 synchronizes the FURNITURE (and wall / floor / music box) of player houses. Collision of a visitor with the owner's furniture is Stage 2 and is NOT part of this change (the interior puppet pipe stays disarmed for player-house scenes). The feature is OFF unless the HOST is started with `--house-sync` (default off, like `--authoritative-wildlife`; the host announces it in HOST_CONFIG byte 1 bit 0 and a client follows the host; a client that never sees the bit gates nothing). To make it the default change `g_pc_house_sync` in `pc/src/pc_main.c`.

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

Status: M1 (character store) + M2 (membership lookup, per-town tokens) are implemented; M3 (servers.ini + `--server`) and M4 (Play Online title menu, relaunch) are implemented; M2b is not (see "Remaining").

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

**Decision: relaunch, not in-process start.** Starting the network client inside the running title process would change the role after startup, which the role audit (role is fixed before init) does not
allow. Connect therefore RELAUNCHES the same executable (`pc_relaunch.c`: `CreateProcess`, exe from `GetModuleFileName`, same working directory so `save/` resolves identically) with
`--connect HOST:PORT --character UUID` (store character; a typed new name starts the Rover creation), `--guest-profile NAME` (legacy-only profile) or `--guest` (the default `guest.ini`), then sets
`g_pc_running = 0` so the title process exits cleanly. Arguments are validated (IPv4 literal, port, name `[A-Za-z0-9-]`) so no quoting/injection is possible. On non-Windows the function only prints the
command and does not quit. Other command-line options of the title process (e.g. `--fullscreen`) are NOT forwarded; settings come from `settings.ini`.

Verification: the menu code is compile-checked and source-audited only (no UI automation, never seen on screen); the data layer (`pc_servers.c`, relaunch command line) and the CLI are covered by
`pc/tools/net_spike/test_servers.py` (native selftest + temp-cwd CLI checks).

Still missing: M2b resident token authentication; guest -> resident promotion (needs town transfer); a per-server town save dir (there is one `save/card_a` today, so switching to a server
with another town still needs the matching town GCI copied there); hostname / DNS resolution (IPv4 literals only); starting the role in-process (needs a re-init path audited for every role-dependent
module); `last_town` hint; controller-only add/edit of servers (needs a keyboard; use `--server-add`).

### Town cache and town transfer (phase 2, M-A / M-B)

A client no longer has to copy the host's town save by hand. Started with `--town-fetch` it downloads the host's town into a per-town cache and plays that; the legacy
`--connect` without the flag is unchanged.

**Flags.**

* `--town-fetch` (client only, requires `--connect` or `--server`, exit 2 otherwise): fetch the town before the game boots (progress in the window title; errors in an SDL message box).
* `--town-serve on|off` (host only, exit 2 otherwise) and `settings.ini` `[Network]` `town_serve = 0|1`: whether this host serves its town. **Default OFF.**
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

**Privacy.** The GCI contains every resident's pockets, mail, diary and designs and is served UNSANITIZED. That is why `town_serve` is off by default: turn it on only for
friends / a supervised LAN. Sanitizing other residents' ranges (M-B2) is deferred; it needs a rule so a sanitized record can never be migrated back.

**Limitations.** No adopt-on-match of an existing `save/card_a` into `towns/` (copy it by hand into `towns/<key>/card_a`); the transfer is about 60 s stale at worst (the READY snapshot
and the record / house / service syncs bring the session up to date); Play Online does not pass `--town-fetch` yet (M-C); NES game saves still live in `save/card_a` (`famicom.cpp`);
a fetch refetches the whole file whenever it differs (about 467 KB); the Save_t checksum is not verified (the loader does not either; the transport checks size + CRC32); a host that
never saved yet answers UNAVAILABLE.
Tests: `pc/tools/net_spike/test_town_cache_unit.py` (native unit + audits + `--town-dir` real process), `test_town_transfer_protocol.py` (scripted client against a real host),
`test_town_fetch_real.py` (real client with an empty save dir).

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
reserve, mint (guest or resident), ACK, IDENTITY_TOKEN. `pc_mp_membership_lookup` is only a logged cross-check (a `CROSS-CHECK differs` line when it disagrees); it is not the decider.
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
changes the town save; promotion (M-F) and Play Online passing a resident membership (M-C) are not implemented; the real client half was verified by source audit and a scripted host test, not yet by a real
resident client run.
Tests: `pc/tools/net_spike/test_members_unit.py` (native storage unit), `test_resident_credentials_protocol.py` (scripted clients against real dedicated hosts: off / tofu / restart / required + arm /
corrupt UNTRUSTED / allow_new_guests).

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
