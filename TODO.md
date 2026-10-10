# ACGC PC Multiplayer — master TODO

Persistent checklist for the multiplayer port (`multiplayer` branch). Update it whenever a task changes state, with the evidence that justifies the change.

**How to read it**

- `- [ ]` open · `- [x] ~~done~~` finished (with an evidence note).
- Statuses: **IN PROGRESS** · **BLOCKED: manual verification required** · **BLOCKED: missing fixture/tooling**.
- Evidence level is always stated. These are *different* things and none implies the next:
  1. **source review** — read the code, nothing ran;
  2. **compiled** — the project builds;
  3. **harness** — automated test against a real host with fake/scripted peers, or source-grep tests;
  4. **real-game automated** — real `AnimalCrossing.exe` host/client processes driven by the test-only autopilot (`AC_TEST_HOOKS=1`, `PC_NET_TEST_HOOKS` build), on the Samsung display at volume 1 (`AC_DISPLAY_NAME=samsung`, `AC_MASTER_VOLUME=1`);
  5. **manual** — a person actually played it.
- Source references use the audit IDs (B-n = wildlife audit, W-n/N-n/F-n/WS-n/ID-n = the whole-codebase audit).

---

## Validation tiers (wildlife / fishing sync)

**ROUTINE tier — hard budget 18 min (20 with the summary), `bash pc/tools/net_spike/run_routine.sh`** (Samsung display, volume 1). Incremental build, then the steps below in risk order. A step is only started when its measured duration still fits in the remaining budget (otherwise it is reported `NOT RUN (budget)`), each step's `timeout` is capped by the remaining budget (a timeout keeps its log), nothing is retried and no assertion is skipped. Output: `routine_summary.txt`, `routine_<step>.txt`.

| Step | Measured | Why it is in the routine |
|---|---|---|
| build (`cmake --build build64`) | ~6 s incremental, ~4 min clean | always |
| `test_wildlife_golden_rod_real.py` | ~2.6 min | the real equipment → host path |
| `test_wildlife_host_indoors_real.py` | ~4 min (stay shortened to 100 s) | host-indoors hold + reconcile |
| `run_wildlife_protocol_tests.py` (6 protocol tests, ants included) | ~4.3 min | catch / spawn wire / exchange protocol |
| `test_wildlife_sim_real.py --only s1,b3,r1` | ~6 min | acceptance cases that exercise the insect lifecycle fixes |
| `test_wildlife_rod_type_real.py`, `test_wildlife_bobber_order_real.py` | ~0.5 min each | wire-level rod / ordering |
| `test_wildlife_indoor_spawn_real.py`, lifecycle `--only l7`, snapshot race | ~2 min each | only if budget remains |

**EXTENDED tier — milestones / before a release, 1–2.5 h, `bash pc/tools/net_spike/run_stability.sh 5 3`**: the whole routine set plus the full lifecycle suite (39 checks, ~6 min), 5 × `test_wildlife_proxy_lifecycle_real.py` (~8 min each), 3 × the full `test_wildlife_sim_real.py` (~13–15 min each), all against ONE frozen executable (the script prints the exe and fixture hashes). Not for everyday work.

## Latest test evidence (sprint 2, 2026-10-09; frozen executable `a8f512f175451e42`)

Extended-tier run on the frozen executable (interrupted by the 20-minute instruction after `proxy_life_2`; output kept in `pc/tools/net_spike/stab_chain2/`): lifecycle **39/39**, indoor-spawn **15/15**, rod-type **15/15**, golden-rod (real client) **14/14**, host-indoors **14/14**, snapshot-race **22/22**, bobber-order **9/9**, protocol suite (catch 25/25, bug_catch **71/71** incl. ants, realgrant 15/15, exchange_gate 17/17, flag_gate 5/5, spawn_wire 11/11), `proxy_life` runs 1 and 2 **36/36 each** (`proxy_life_3` interrupted by me, not a failure; `proxy_life_4/5` and `sim_full_1..3` not run on this exe).

ROUTINE run on the same executable: elapsed **1015 s (16 min 55 s) including the incremental build** (budget 1080 s). Build rc 0, 0 errors. golden_rod **14/14** (171 s); host_indoors **14/14** (252 s); protocol suite **all six pass**, 246 s (catch 25/25, bug_catch 71/71, catch_realgrant 15/15, exchange_gate 17/17, flag_gate 5/5, spawn_wire 11/11); `test_wildlife_sim_real.py --only s1,b3,r1` **15/15** (162 s); rod_type **15/15**; bobber_order **9/9**; indoor_spawn **15/15** (123 s). **NOT RUN (budget)**: lifecycle `--only l7` and snapshot_race (they passed 39/39 and 22/22 earlier on the same executable, in the extended run). **Never run on this executable**: the full `test_wildlife_sim_real.py`, `proxy_life` repeats 3-5 (repeats 1-2 passed).

Earlier extended chain on the previous executable (`26f9c069`, `stab_chain1/`): `proxy_life` 5/5 × 36/36, `sim_full` 65/65, 66/69, 62/65 (R1 bug race lost to a naturally ended bug, both players refused — harness, since fixed), lifecycle 35/39 (L6: the autopilot left the player inside the catch notice, see the stall investigation) — that is why the executable was changed and everything re-run.

None of the above is manual gameplay verification.

---

## 1. Immediate priorities

- [ ] **Manual multiplayer fishing smoke test — BLOCKED: manual verification required**
  - Steps: (1) host a game; (2) join with a second game instance; (3) cast fishing rods on both clients; (4) catch fish and check the remote presentation and disappearance; (5) fish the ocean, including sea bass and red snapper; (6) disconnect a player while a bobber is active; (7) check the remaining session keeps working.
  - Expected: each angler's fish bites only that angler's bobber; the other player sees the fish swim, then vanish when it is caught; exactly one catch/item per fish; a disconnect mid-bite frees the bobber and the fish and nobody else is affected; casting again afterwards works for everyone.
  - Results / issues found: _(empty — nobody has played this yet. Do not tick this because automated tests pass.)_
- [x] ~~**Investigate the 70/71 wildlife acceptance result (B3, host net-catch of a bug)**~~ — root cause found and fixed (uncommitted), regression test added.
  - Evidence (real-game automated; reproduced in isolation with `--only s1,b3`; logs `pc/tools/net_spike/ws1_b3_iso1*.txt/.log`, `ws1_b3_iso2*`): **not** a "flying bug" (heights of 120–200 are ordinary terrain + flight height; the autopilot nets such bugs). The bugs the host had picked were **ended by their own species program** (`insect_flags.destruct`: a butterfly that left its flowers, a dragonfly away from water, ...). In vanilla such an insect is simply gone. `pcwld_host_ensure_actors()` instead re-created the actor "where it was"; the new actor's spawn check failed the same way, it died again, and after 5 consecutive failures (B-7) the record was released. For ~6 s the table advertised a bug nobody could catch, and the harness (which picks from the table) chose it. `pcwld_insect_diag()` (`AC_TEST_INSECT_DIAG=1`) logs why an insect is destroyed (1 fade, 2 destruct flag, 3/4 cull).
  - Fix: `pcwld_insect_ended_naturally()` (called where `aINS_cull_check` consumes `destruct`, `src/actor/ac_insect_move.c_inc`) marks the host record abandoned, so `pcwld_host_collect_abandoned()` releases and despawns it at once.
  - Regression test: `test_wildlife_lifecycle_real.py` **L7** (new autopilot command `endbug ENT` flags the host's bug as ended). It FAILED on the unfixed build (no release within 4 s, 1 re-creation) and passes on the fixed build (7/7 on four runs; its last check, "the acre rolls a new bug afterwards", is RNG-limited: it failed once with 10 acre entries, now 25).
  - Acceptance: `--only s1,b3` 17/17; full suite 61/61 in the final run.
  - Harness changes made while verifying (none hides a product failure): `net_catch` re-rolls fresh bugs for up to 3 rounds when no catchable bug is left (only the last round records a failure; checks after a catch always record); S1 no longer demands 20 samples from a creature the host *released* during the window (it still requires one survivor); `ensure_creature(free_others=True)` / `surviving_fish()` release other fish records first and choose a fish still in the table 9 s later (the 2-fish actor pool, W-05, otherwise gives the chosen fish no host actor).
  - **Known flakiness of the full suite**: three consecutive full runs *before* those harness changes failed (65/68, 56/64, 50/57) while the isolated parts always passed. Causes seen: bug catches on bugs that end during the swing or sit too high; fish records without a host actor (the pool of 2, or a fish > ~600 units from the host player: re-created 5x, then released); accumulated state across steps. The last full run passed 61/61, but treat the suite as order sensitive until W-04/W-05 are removed. Also seen once: S1 "entity 1 has a trace in every process (0 samples)" (an insect destroyed repeatedly by its own program) — covered by the S1 change.
- [x] ~~Run the older wildlife protocol tests~~ (WS3: all six run; runner `pc/tools/net_spike/run_wildlife_protocol_tests.py`; summaries `proto_summary*.txt`, per-test output `proto_<name>.txt`, earlier outputs/logs in `ws3_before/` and `ws3_keep_run1/`). What failed and why:
  - **Production behaviour change, stale test premise**: the tests predate record release. They seed records with a scripted peer that leaves again, boot a second process for 30–60 s and expect the records to still exist. Since B-1 (attendance release after 12 s), B-7 (abandon after 5 failed re-creations) and the WS1 fix the host releases such records. With the real lifetime (`--no-keep`, `proto_summary_nokeep.txt`): catch 21/25, bug_catch 47/61, catch_realgrant 11/15, exchange_gate 7/8, flag_gate 5/5, spawn_wire 9/11. Fix: TEST-ONLY env `AC_TEST_WILDLIFE_KEEP_RECORDS=1` (needs `AC_TEST_HOOKS=1`; `pcwld_test_keep_records()`) = the host never releases a record by itself; the runner sets it by default. The record lifetime itself is covered by the lifecycle tests. (A stale fixture exe silently hid this at first; the runner now calls `make_fixture()`.)
  - **Genuine small production defect, fixed**: spawn_wire W3 (entity ids pairwise unique): a trigger rolled a spawn, broadcast it to every peer **and then replayed the same record again** to the triggering peer. Now only records that existed before the roll are replayed (`pcnetgame_wildlife_replay_acre_to_peer(peer, recs, n, ...)`).
  - **Stale assertion, intended behaviour verified**: spawn_wire W4 "re-trigger produced no duplicate-kind spawn": re-entering an acre intentionally replays its live records (documented in the code). The check now asserts no NEW same-kind record (no unseen entity_id) and reports how many records were replayed.
  - **Ant fixture, solved (sprint 2)**: ants spawn only ON_CANDY / ON_TRASH (`ac_set_ovl_insect.c`; no rain/snow), the fixture town has neither. New test-only autopilot command `place ITEM_HEX UT_X UT_Z` writes a ground item through the field-authority API (`pcfa_set_tile`; tile units = world/40, 16 per acre); the ant test puts `ITM_FOOD_CANDY` (0x2806) in ONE otherwise unused acre (5,3) just before TEST ANT and triggers only that acre (candy in every acre made the roll produce *only* ants and starved the ordinary-bug tests). Production spawn rules are untouched. Deterministic: 4 of 4 repeat runs produced 1 ant on the first trigger (`ant-seed-0`), then the guard checks pass (catch of an ant rejected, ant stays live); bug_catch 71/71 in the final runs. **RNG-dependent seeds** (a burst can roll no bug / no fish): the seeders repeat the same stimulus up to 3-6 passes; one roll can announce a whole swarm of bugs in one acre, so the race tests now release every surplus bug record with the test-only `hdespawn` and keep exactly one target.
  - Final protocol runs: catch 25/25, bug_catch 71/71, catch_realgrant 15/15, exchange_gate 17/17, flag_gate 5/5 (scenarios B/C retired earlier), spawn_wire 11/11 (routine run and the earlier extended run on the frozen executable). The runner also refreshes the fixture exe (a stale copy once hid a change).
- [x] ~~Intermittent `proxy_life` stall~~ — root cause found: **autopilot/fixture, not a game deadlock** (sprint 2). Evidence: `main_index` 56 = `mPlayer_INDEX_NOTICE_ROD` (the catch-notice dialogue); the stuck client had just received the host's *accepted* verdict. `catch fish` finished the moment the host's verdict arrived during the hook (`stage < 6` early-finish), leaving the player inside the notice dialogue, where `walk` can never move it; the next steps (`cross()` in `ensure_creature`) then retried for hours. Reproduced with the new diagnostics in lifecycle L6 (`ok catch accepted by the host (player NOT free: main_index 56)`, followed by 4 failures that all vanished after the fix). Fix: the early finish now enters stage 7 (settle: press A only while a rod dialogue is up, finish when the player is free) when the rod is out. Diagnostics added: `pos` reports `main_index` and `free`; the catch finisher says when the player is NOT free; `Player.walk()` checks the state on failure, tries `mash` once and otherwise raises `PlayerStuck` with the last autopilot lines (so a stuck test stops with a diagnosis, not a silent stall); every step of `run_stability.sh` / `run_routine.sh` is bounded by `timeout` and kills only fixture-directory games. Results: proxy_life passed 5/5 (36/36 each) on the pre-fix executable (the stall did not recur there, so that is not evidence for the fix) and 2/2 on the frozen post-fix executable; lifecycle went from 35/39 (pre-fix, L6) to 39/39 (post-fix). The stall's frequency is low, so these few runs cannot prove it is gone. Remaining uncertainty: the original stall was seen once in REP cast 2 and the same mechanism is inferred, not proven for that exact occurrence.
- [x] ~~Back up the current wildlife/multiplayer work to `origin/multiplayer`~~ — done in the commit "chore: add project TODO and back up wildlife fixes" (this file is part of it).
- [ ] Hand-check the build that was left running on the Samsung display (the window was started plain, with `AC_DISPLAY_NAME=samsung AC_MASTER_VOLUME=1`; Windows reported a single active 1920x1080 display at that moment). — BLOCKED: manual verification required

---

## 2. Wildlife and fishing synchronization

Done (evidence in brackets; none manually verified):

- [x] ~~Bobber proxy released when its fish is destroyed (84c0a9e)~~ [real-game automated: proxy lifecycle 36/36]
- [x] ~~B-1: host 10-minute record TTL removed (attendance release stays)~~ [real-game automated; source review that every record ends by catch / 12 s unattended release / reset]
- [x] ~~B-2: client presentation entries follow the host (`despawned` flag), culled fish re-materialize, caught fish free their entries~~ [real-game automated: lifecycle L1/L2]
- [x] ~~B-3: spawn the client cannot show (pool full / indoors / loading) is deferred and retried~~ [real-game automated: pool-full case (L5) and the indoors case (`test_wildlife_indoor_spawn_real.py` 15/15)]
- [x] ~~B-4: spawn/despawn during a late joiner's wildlife snapshot~~ [real-game automated: snapshot race 22/22; smaller fix than a final table diff, same invariant held]
- [x] ~~B-5: `PcWldBobber b` zero-initialised~~ [compiled + source review]
- [x] ~~B-7: a record that cannot keep a host actor (5 consecutive failed re-creations) is released and despawned~~ [real-game automated: L3]
- [x] ~~B-8: `BOBBER_STATE.seq` (repurposed always-0 reserved field) drops reordered packets~~ [harness: 9/9 incl. a control]
- [x] ~~B-9: session/generation reset retires stamped fish; a stamp-less fish catch/exchange is denied on an authoritative client~~ [real-game automated: L4 for the reset; the denial branch is source review + compiled only]
- [x] ~~B-10: host judges a remote angler's bobber with that angler's rod type~~ [harness: `test_wildlife_rod_type_real.py` 15/15 (wire → proxy → fish AI); a real client equipping the golden rod is NOT covered, see Open]
- [x] ~~B-11: kaseki-program fish (sea bass, red snapper, salmon, coelacanth, jellyfish, knifejaw, whale) get the proxy/driven/event hooks~~ [real-game automated: sea bass and red snapper in L6]
- [x] ~~B-13: `aGYO_actor_dt` releases proxies that still have a fish tied~~ [compiled + source review only]
- [x] ~~WS1: a species-ended insect is released at once instead of being re-created 5 times (`pcwld_insect_ended_naturally`)~~ [real-game automated: lifecycle L7 (fails before / passes after), `--only s1,b3` 17/17]
- [x] ~~Acre replay no longer re-sends the spawn this very trigger just broadcast (duplicate `WILDLIFE_SPAWN` to the triggering peer)~~ [harness: spawn_wire 11/11]

Open:

- [x] ~~Test deferred wildlife spawning when a client is **indoors**~~ (WS2-A, real-game automated, `test_wildlife_indoor_spawn_real.py` 15/15): the client walks into a villager's house with `AC_TEST_ROOM_ENTER` (83 s inside); the host injects fish meanwhile. Verified: the announcement is received and kept deferred (`entity N deferred`), nothing is presented in the interior, no fish actor exists indoors, a fish the host releases while it is deferred (`hdespawn`) is forgotten and never appears, and back in the field the other fish materialize exactly once (one actor, one stamp, deferred count 0, pool 2). No product defect found.
  - Not covered: **session reset while a fish is deferred indoors** — the autopilot is dormant while a process is indoors (commands queue until it is back in the field), so `resetstamps` cannot be issued indoors; by source review `pcwld_presentation_reset()` clears `s_deferred`. A **host session restart** while deferred is handled by the snapshot-begin generation check (source review only).
  - Test-fixture notes: the host's fish actors are culled by distance to the HOST player (a fish ~670 units away is re-created 5x and released), so the host must stand near the injected fish; the pool holds only 2 fish.
- [ ] Improve automated whale-fishing coverage — BLOCKED: missing fixture/tooling (the autopilot finds no casting spot for a whale; it swims too far from the beach).
- [x] ~~Rod types (B-10), automated part~~ (WS2-B, `test_wildlife_rod_type_real.py` 15/15, real host + fake peers). Path traced: client `pcwld_client_collect_bobber()` sets `rod_type = (Now_Private->equipment == ITM_GOLDEN_ROD)` → `BOBBER_STATE.rod_type` (wire byte 8) → host `pcwld_host_set_bobber()` stores `PcWldProxy.rod_type` (any non-zero byte = golden) → `aGYO_get_uki_type_for()` / `aGKK_get_uki_type_for()` ask `pcwld_proxy_rod_type()` for a proxy bobber, the local rod for the host's own bobber. Verified with `[PROXY-DIAG]` lines: wire 0 → normal, 1 → golden, 0 → back to normal, 7 → golden, 255 stays golden, a legacy sender that never sets the byte = 0 = normal; in each case the fish AI actually **judged the bobber with the stored rod** (a host fish was injected next to the bobber so that the AI examines it). The two-peers-at-once check is weak (only one peer ever sent a golden rod; the other stayed normal and was never examined by a fish), so per-peer separation rests on the per-proxy `rod_type` field (source review).
  - Note: in `ac_gyo_test.c` / `ac_gyo_kaseki.c` the two rods differ in `aGYO_search_angle` and `aGYO_bite_time`, not in `aGYO_search_area`; the test proves *which table is used* (diag), it does not measure bite timing.
  - Design note: an invalid non-zero value counts as golden; a modified client can claim a golden rod and widen the search angle of its own bobber only (same trust level as the other unchecked client claims, W-15).
- [x] ~~Rod types, **real golden rod** on a real client~~ (sprint 2, real-game automated, `test_wildlife_golden_rod_real.py` 14/14). New autopilot commands: `equip HEX` and `catch fish ENT golden` cycle the equipped tool with the game's **own D-pad tool cycle** until `Now_Private->equipment` is the wanted item (nothing writes `equipment`; the golden rod is put in a pocket with the existing test-only `give 223C`, exactly the item a player owns). A real client then walks, aims, casts, waits for the bite and hooks. Verified: G1 client golden rod while the **host has the normal rod equipped** → client log shows `equipment 0x223C`, host `[PROXY-DIAG]` stored wire value 1 = golden and its fish AI **judged the bobber with the golden rod**; G2 client normal rod while the **host has the golden rod equipped** → host stored no golden rod and judged **normal**; both casts reached the host fish AI (bobber events / verdict). So the host evaluates the *angler's* rod, not its own, end to end on a real client. Missing / default / invalid values: the real client can only produce 0 or 1; 0, 1, 7, 255 and a legacy sender are covered by `test_wildlife_rod_type_real.py` (fake peer). **Not covered:** bite-time / search-angle differences themselves (the evidence is which rod table the host AI used), a golden rod obtained through a shop/present, and a rod change between two casts of one bobber.
- [ ] Runtime-test fish proxy cleanup during **controller teardown** (host leaves the town scene with a fish tied; B-13) — BLOCKED: missing fixture/tooling (a host scene-change autopilot command).
- [ ] Runtime-test the B-9 denial branch (a leftover unstamped fish catch is denied, not granted) and a late packet of an old generation.
- [x] ~~**B-6**: wildlife when the host is inside a building~~ — decision made and the low-risk part implemented (sprint 2; real-game automated `test_wildlife_host_indoors_real.py` 14/14; with the change switched off (`AC_TEST_NO_INDOOR_HOLD=1`) check H3 fails: the fish drifted 42 units in 20 s).
  - Code analysis: all host wildlife (actor pools of fish/insects, clip pointers, `pcwld_host_collect_state`, `pcwld_host_spawn_trigger`, `pcwld_host_ensure_actors`, catch validation) lives inside the host *player's* town scene; `aGYO_actor_dt` tears the pools down on scene change. A host indoors therefore **cannot** simulate wildlife — continuing authority independent of the room would need the actors to exist (a dedicated/observer host) — so "suspend and resume" is the only design the current architecture supports, and the records (authoritative table) deliberately survive.
  - Measured with a real host in a house (72–143 s): `WILDLIFE_STATE` carries 0 creatures; a client acre crossing (spawn trigger) rolls nothing (suspended, not queued); a client that **joins while the host is indoors still gets the snapshot** with the records; on return the state flows again, the fish under test is in the table once, and the client has exactly one actor for it. Fishing: bites and the catch decision come from the host's AI (a driven fish cannot bite without it, W-07), so no catch request is produced while the host is indoors; a bug net-catch request would be refused (`pcnetgame_validate_catch` bails on `!pcfa_scene_is_town()`, source review — the runtime refusal was not observed).
  - Defect found and fixed: after the 2.5 s stale-target timeout every client let its **local vanilla AI** run the fish ("the host went quiet"), so each client saw a different fish moving (42–47 units in 20 s). Now `pcwld_find_target()` keeps the last host target while the host is connected but announced a non-town scene (`pc_net_game_host_out_of_town()`, no protocol change) → the fish is held (0 units moved). A host that disconnects still frees the local AI. Test-only `AC_TEST_NO_INDOOR_HOLD=1` switches the hold off (the "before" run).
  - Not done, decision for a person: (a) document that a host should stay outdoors while others fish — preferred for now; (b) a dedicated/observer host that keeps simulating (also resolves B-6 completely; large); (c) an explicit "wildlife suspended" flag in `WILDLIFE_STATE` so clients can show it (protocol change). Not tested: host in a shop/museum/island scene (same code path by `pcfa_scene_is_town()`); a late joiner's far fish stays culled until its player comes near (existing B-2 behaviour).
- [x] ~~**B-12**: `action` / `engaged` of `WILDLIFE_STATE`~~ — audited (source review): `pcwld_host_collect_state()` fills them (`action` = the creature's vanilla action number, `engaged` = 0xFF nobody / 0xFE the host's own bobber / the peer id), `pcnetgame` copies them through the wire message, and **no client code reads either** (`pcwld_client_set_state` / `pcwld_ease_actor` / `pcwld_drive_*` use x, y, z, angle only; the engaged check in `pcwld_drive_fish` uses the local fish's own `linked_actor`). Deliberately **not removed**: dropping them changes the wire layout of an unreliable 5 Hz message (2 bytes per entity) for no functional gain. Left as reserved informational fields; a future "observers show another player's hooked fish" feature would be the user (see the observer-presentation item below).
- [x] ~~Investigate the unexplained fish-position offset (~6 units z)~~ — not a defect, no change (WS4, source review + measurement). Coordinate spaces: the host sends absolute world x/y/z of the fish actor (`aGYO_pc_entity_state`, `world.position`), the client eases its own actor's `world.position` toward that target (`pcwld_ease_actor`, k = 0.2 per frame, snap above 200 units): same space, no conversion and no constant offset anywhere in the code (grepped). Measurement from the S1 traces of the full run (host vs observer, same entity, ~1 s samples, 185 pairs): **mean signed z difference −0.2 units**, mean distance 2.5 units, 38.7 units max (an outlier, probably an event or a mis-aligned sample); S1 itself requires max < 90 and mean < 35. So a *systematic* offset does not exist; a transient ~6 units is what easing lag + a 5 Hz/network delay + sampling the two logs a few frames apart produce for a swimming fish. Remaining uncertainty: the original sample was one log; if it was a *constant* 6 units for the whole life of a fish it was something else (not reproduced in the logs analysed this session).
- [ ] Observers do not see the hook / pull-in / lift of another player's fish (the angler's engaged fish follows local vanilla code; the host target is ignored while engaged). Design limitation — decide whether to present it.
- [ ] W-04: attendance radius (700) vs host cull radius (600): a creature 600–700 away flickers; B-7 now releases it after 5 failed re-creations, the mismatch itself is not removed. **Seen again this session** (indoor/rod/catch tests): a fish ~670 units from the host player is re-created 5x and released although a client stands next to it, because the host's fish actors are culled by distance to the HOST player. A remote angler far from the host player therefore gets no stable fish. Real-play impact is not measured.
- [ ] W-05: host and each client have only 2 fish / 8 bug actors for all players (vanilla pools). A third fish record never gets a host actor and is released by B-7. Design limit; consider a bigger host-side pool.
- [ ] W-06: ant records are inserted but never materialized and block other bug spawns in their acre.
- [ ] W-07: a stamped (driven) fish on a client can never bite, even when the host stopped sending state.
- [ ] W-09/W-10: a lost `BOBBER_EVENT` / lost "bobber gone" can leave the host fish tied until the next bobber stage change; events are reliable but have no resend.
- [ ] W-14: `WILDLIFE_STATE.seq` is ignored on the client (reordered datagram can overwrite a newer target).
- [ ] W-15: catch reach check is generous (fish 800, bug 1000 units) and the species/tool/bite is not verified host-side (modified client could claim any creature it saw).
- [ ] W-17: a bug net-catch with no entity stamp falls through to a vanilla grant (the fish path now denies; the bug path was not changed).
- [ ] Client RNG is still consumed by driven wildlife (`mv_proc` runs on every client); harmless unless something needs RNG parity.
- [ ] **Autopilot is dormant indoors**: a process inside a house does not run its autopilot commands (they queue until it is back in the field). Limits tests of anything a process does indoors (reset, catch attempts); drive indoor scenarios from the host and read the client log. — BLOCKED: missing fixture/tooling
- [ ] Full-suite stability of `test_wildlife_sim_real.py`: **no full run exists on the frozen executable** (only `--only s1,b3,r1` 15/15). Earlier executable: 65/65, 66/69 and 62/65 (harness races: R1 lost to a naturally ended bug, since fixed). Causes of earlier failures, all harness: bugs ending during the R1 race (now repeated with a fresh bug), the 2-fish pool (`free_others`), S1 samples of released creatures. Do not read one pass as stability; the extended tier (`run_stability.sh`) is for this.
- [ ] Legacy protocol tests depend on `AC_TEST_WILDLIFE_KEEP_RECORDS=1` (record release disabled); convert them to keep a scripted peer attending, so they run against the real lifetime. (The ant fixture is solved: see the protocol item in section 1.)
- [ ] `test_wildlife_proxy_capacity_real.py` was only run with 3 guests (12/12); the 10-guest run (>8 concurrent proxies) and the 8-simultaneous-angler boundary were not run this session.
- [ ] Not re-run this session: `test_hostcfg_src.py`, `test_capacity_phase567.py`, `test_catch_disconnect_exchange.py` (known failures listed in the evidence table).

---

## 3. Multiplayer correctness and regression testing

- [ ] Wire the self-tests into CMake/ctest (23 native `*_selftest.c` + the pure-Python source tests); today nothing runs from `ctest` and `run_game_tests.py` only covers fake-peer tests.
- [ ] Real-process (not fake peer) tests that are still missing: two real clients racing the same pickup/shop/bury; kill a real client mid-transaction (shop, catch exchange, mail); a 9+-client real run (9th guest was only verified manually); host crash mid-save recovery.
- [ ] Packet fuzzing / malformed-packet test against a real host (none exists).
- [ ] Fix or retire stale tests: `test_hostcfg_src.py` (docstring still says default OFF; checks A1/A3 fail), `test_catch_disconnect_exchange.py` F1, tests that still pass the now-redundant `--authoritative-wildlife`, ASan link steps in `test_capacity_phase567.py`.
- [ ] Many source-grep tests assert exact C text and break on harmless renames; keep them labelled "static" and prefer behaviour tests.
- [ ] **F-01 / N-01/02/14**: unchecked `pc_net_send` results; state is marked "sent" before the send (NPC lease, event NPC, notice board pages, NPC_STATE, friendship, mail, work state). A full reliable window loses the update for good. Add a retry/resync wrapper. (source review only)
- [ ] **WS-01**: day rollover on a long-running (dedicated) host: weather, villager move-in/out, daily mail and field growth only run at boot / scene entry. (source review only)
- [ ] **F1 (seams)**: the host cannot chop/shake/hit rocks on the island (host-local seams bail on `!pcfa_scene_is_town()` after skipping vanilla); **F2**: clients on the island are swallowed instead of falling back to vanilla (inconsistent with weeds/flowers).
- [ ] **F3**: event-NPC claims fall back to a local vanilla grant when a client's link is not READY.
- [ ] **F4**: a client's perfect snowman reward mail goes to a local post office and is lost; BUILD is not retried.
- [ ] **F5 / F6**: dig bonus amounts and the golden-shovel roll are client-chosen; wallet/pocket pre-images are trusted by shape only; **F7**: friendship deltas unclamped, mail request fire-and-forget.
- [ ] **ID-F4/F6/F10, WS-02/03**: Work Mode and K.K. persistence: non-atomic save (`remove` then `rename`), table keyed by PersonalID only (leaks between towns on a non-dedicated host), failed K.K. claim mark still grants, no daily cap on Work Mode rewards, guest claims memory-only.
- [ ] **N-05**: no handshake timeout (a peer that never sends IDENTITY holds a slot); **N-07/08**: stale unreliable DATA/MOVE can create a ghost puppet.
- [ ] **F-03**: many `--force-*` / `--*-test-*` command-line flags and `PC_NET_FAULT_*` env switches are live in every build (release builds must configure `-DPC_TEST_HOOKS=OFF`; several flags are not behind it).
- [ ] **F-02**: clamp hostile-host villager arrival/snapshot/departure values before they hit the live save.
- [ ] **ID-F1 (proxy table size)**: the 8 bobber-proxy slots are 8 *concurrent* anglers. The "8 historical players" audit claim was wrong; the caught-fish leak was fixed (84c0a9e). The 8-simultaneous-angler boundary itself was never exercised — BLOCKED: missing fixture/tooling (FakeClient peers bind as residents; guests need a rod).

---

## 4. Networking and performance

- [x] ~~**Host MOVE relay cost** (F-07 / N-15)~~ — measured (sprint 2), **decision: no optimisation**. (It is the *player* MOVE relay `pcnetgame_handle_host_move`, not wildlife.) Instrumentation: opt-in `AC_TEST_MOVE_PROFILE=1` prints every 5 s moves, destination iterations, relayed/thinned and where the time went. Baseline in a **real host** with 2 and 3 scripted peers (25 s at 20 Hz each, `bench_move_relay_real.py`; the fixture has only 3 non-host residents, so ≤ 3 peers): a handler call costs **6.4 µs (2 peers) / 12.0 µs (3 peers)**, of which `pc_net_send` is **5.9 µs per relayed datagram** (≈ 92–96 %), the lookup + tier + copy + the fixed per-MOVE work only **0.45–0.55 µs per move**; CPU 0.03–0.07 % of a core; near and spread layouts equal (nothing was far enough to be thinned). Lookup/decision cost at 8…254 peers with the real `pc_interest.c` + puppet pool (`bench_move_relay_model.c`, native): **≈ 8 ns per destination iteration, 0.4 ms per 20 Hz round (≈ 1 % of a core) at 254 peers** (results in `bench_model_baseline.txt`). Conclusion: view caching would save about 1 % of a core at the 254-peer limit; the O(N²) cost that matters is the **sends** (inferred from the measured 5.9 µs: all-near relay of N peers costs N·(N−1)·20 sends/s → roughly 46 peers all in one acre would already use 25 % of a core, interest tiers cut that for distant peers). Reducing it needs batching several samples per datagram or sending from one aggregated per-tick packet — a protocol change, not justified without a real many-peer measurement. Remaining unknowns: real-transport cost with > 3 real peers (no harness), kernel/driver effects at high packet rates.
- [ ] N-04: the unreliable event ring holds 80 entries per poll regardless of peer capacity; drops are only counted on the dedicated server.
- [ ] N-10/N-18: keepalive is frame-coupled with a 5 s timeout; `getaddrinfo` blocks the main thread on connect.
- [ ] F-06 / N-06: no per-session token or MAC after HELLO (spoofable DISCONNECT/DATA).
- [ ] `WILDLIFE_STATE` goes to every READY peer at 5 Hz with no interest filtering (~1–4 KB/s per peer); `BOBBER_STATE` 10 Hz.
- [ ] WS-04: every roster slot gets a real puppet actor even when the player is in another scene; 150 players would starve the 200-actor pool.
- [ ] ID-F15: guest store rewrites every guest on each save; admission scans are O(guests).
- [ ] x64 hardening (audit G): `ARStartDMA` address guessing, `ARAlloc` wrap/zero-failure ambiguity, GBI odd-pointer token ring, `Clip_c` size assert.

---

## 5. Manual verification required

All items below are **BLOCKED: manual verification required** until a person plays them.

- [ ] Manual multiplayer fishing smoke test (see section 1 for the steps and expected results).
- [ ] Remote fish presentation looks right to a human: positions match between players, no popping, a caught fish disappears from the observer's water.
- [ ] Ocean fishing by two players (sea bass, red snapper, salmon) feels vanilla.
- [ ] Golden rod fishing in multiplayer.
- [ ] Late-join while another player is fishing.
- [ ] K.K. Slider and Nook Work Mode with several guests (documented as manually verified earlier; no automated gameplay test).
- [ ] 9th and further guests (verified manually earlier for the 9th; larger counts not tried).

---

## 6. Future features and technical debt

Grounded in the audit (`docs/multiplayer-scalability.md`, `docs/multiplayer-guest-roadmap.md`) unless marked **speculative**.

- [ ] Unsupported wildlife: ants, bees, latent bugs/ghost (hitodama), fishing tournaments / bug-off, released creatures.
- [ ] Event NPC host claims beyond Gulliver's gift and K.K.'s song (Redd, Saharah, Joan, Artist, Designer, Gypsy, Wisp, Kapp'n …): two clients can buy the same unique stock today.
- [ ] Island / Kapp'n sync, house upgrades, guest mailbox and museum donation, villager dialogue, puppet emotes, text chat, balloons, custom umbrellas/pinwheels on puppets (all listed ABSENT/PARTIAL in the audit).
- [ ] Guests have no starter bag; a guest-with-rod fixture would unlock the proxy-capacity test.
- [ ] The test-only autopilot / hooks (`killfish`, `resetstamps`, `pspawn`, `pdespawn`, `hspawn`, `hdespawn`, `endbug`, `give`, snapshot-race hook, `[PROXY-DIAG]` incl. the rod lines, `AC_TEST_INSECT_DIAG`, `AC_TEST_WILDLIFE_KEEP_RECORDS`) have grown inside `pc_net_game.c` (~37k lines). **Speculative:** move them into their own translation unit.
- [ ] `pcwld_test_set_ttl_override_frames` and `--diag-bug-ttl-lookup` are now no-ops (no TTL exists); remove them when convenient.
- [ ] Logs written next to the net_spike tests are now git-ignored; consider writing them under a temp/`logs/` directory instead.
- [ ] Villager memory holds 7 residents per villager, so 20+ guests thrash friendship/mail memory.
- [ ] **Speculative:** a host-migration or dedicated-observer mode that keeps simulating wildlife while the host player is indoors (would also resolve B-6).
