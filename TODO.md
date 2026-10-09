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

## Latest test evidence (last updated after commit `84c0a9e` + wildlife stabilization work)

| Test | Result | Level |
|---|---|---|
| Clean rebuild (`cmake --build build64 --clean-first`) | 0 errors, ~4 min | compiled |
| `test_wildlife_sim_real.py --only s1,f1,f2,f3,r2` | **44/44** | real-game automated |
| `test_wildlife_proxy_lifecycle_real.py --parts rep,conc,dc` (proxy alloc/release, 3 concurrent anglers x2, kill with fish tied, replacement) | **36/36** | real-game automated |
| `test_wildlife_lifecycle_real.py` (L1 cull→re-materialize, L2 catch frees entries, L3 B-7 release + re-roll, L4 B-9 reset, L5 B-3 deferred spawn, L6 kaseki sea bass / red snapper) | **33/33** | real-game automated |
| `test_wildlife_snapshot_race_real.py` (spawn/despawn inside a late joiner's snapshot walk) | **22/22** | real-game automated |
| `test_wildlife_bobber_order_real.py` (reordered `BOBBER_STATE`, host + fake peer) | **9/9** | harness (real host, fake peer) |
| `test_wildlife_sim_real.py` (full acceptance suite) | **70/71 — OUTSTANDING FAILURE, not a completed suite** (B3: host net-catch of a flying bug; failed identically on two runs) | real-game automated |
| `test_hostcfg_src.py` | 19/21 (A1, A3 fail; both also fail at the previous HEAD) | harness (source-grep) |
| `test_capacity_phase567.py` | 40 pass / 3 fail (all "link … [ASan+UBSan]": no ASan link on this toolchain) | harness |
| `test_catch_disconnect_exchange.py` | 21/22 (F1 `pc_save_write_authoritative`, unrelated to wildlife) | harness (source-grep) |

None of the above is manual gameplay verification.

---

## 1. Immediate priorities

- [ ] **Manual multiplayer fishing smoke test — BLOCKED: manual verification required**
  - Steps: (1) host a game; (2) join with a second game instance; (3) cast fishing rods on both clients; (4) catch fish and check the remote presentation and disappearance; (5) fish the ocean, including sea bass and red snapper; (6) disconnect a player while a bobber is active; (7) check the remaining session keeps working.
  - Expected: each angler's fish bites only that angler's bobber; the other player sees the fish swim, then vanish when it is caught; exactly one catch/item per fish; a disconnect mid-bite frees the bobber and the fish and nobody else is affected; casting again afterwards works for everyone.
  - Results / issues found: _(empty — nobody has played this yet. Do not tick this because automated tests pass.)_
- [ ] **Investigate the 70/71 wildlife acceptance result (B3, flying-bug catch) — IN PROGRESS**
  - Observed (real-game automated, twice): the host's own net-catch of bug 7 ends `net gone GONE`; the host actor of the bug dies about a second after every re-creation until B-7 releases the record. The bug was at ~140 height (a flier), species 18/15.
  - Unknown: why the re-created actor dies (net swing scares/destroys it? insect lifetime? cull?). Earlier runs before B-7 had the same class of B3 failures with fliers, so it is probably pre-existing, but that is **not proven**.
  - Next: log the insect's destruct reason on the host, then decide whether it is an autopilot limitation (net cannot reach a high flier) or a real host-actor bug.
- [ ] Run the older wildlife protocol tests that were skipped (`test_wildlife_catch.py`, `test_wildlife_bug_catch.py`, `test_wildlife_catch_realgrant.py`, `test_wildlife_catch_exchange_gate.py`, `test_wildlife_spawn_wire.py`, `test_wildlife_flag_gate_bugfix.py`) — harness. `test_wildlife_flag_gate_bugfix.py` scenarios B/C were retired earlier because a client now follows the host's mode.
- [x] ~~Back up the current wildlife/multiplayer work to `origin/multiplayer`~~ — done in the commit "chore: add project TODO and back up wildlife fixes" (this file is part of it).
- [ ] Hand-check the build that was left running on the Samsung display (the window was started plain, with `AC_DISPLAY_NAME=samsung AC_MASTER_VOLUME=1`; Windows reported a single active 1920x1080 display at that moment). — BLOCKED: manual verification required

---

## 2. Wildlife and fishing synchronization

Done (evidence in brackets; none manually verified):

- [x] ~~Bobber proxy released when its fish is destroyed (84c0a9e)~~ [real-game automated: proxy lifecycle 36/36]
- [x] ~~B-1: host 10-minute record TTL removed (attendance release stays)~~ [real-game automated; source review that every record ends by catch / 12 s unattended release / reset]
- [x] ~~B-2: client presentation entries follow the host (`despawned` flag), culled fish re-materialize, caught fish free their entries~~ [real-game automated: lifecycle L1/L2]
- [x] ~~B-3: spawn the client cannot show (pool full / indoors / loading) is deferred and retried~~ [real-game automated for the pool-full case (L5); the indoors case is NOT tested — see below]
- [x] ~~B-4: spawn/despawn during a late joiner's wildlife snapshot~~ [real-game automated: snapshot race 22/22; smaller fix than a final table diff, same invariant held]
- [x] ~~B-5: `PcWldBobber b` zero-initialised~~ [compiled + source review]
- [x] ~~B-7: a record that cannot keep a host actor (5 consecutive failed re-creations) is released and despawned~~ [real-game automated: L3]
- [x] ~~B-8: `BOBBER_STATE.seq` (repurposed always-0 reserved field) drops reordered packets~~ [harness: 9/9 incl. a control]
- [x] ~~B-9: session/generation reset retires stamped fish; a stamp-less fish catch/exchange is denied on an authoritative client~~ [real-game automated: L4 for the reset; the denial branch is source review + compiled only]
- [x] ~~B-10: host judges a remote angler's bobber with that angler's rod type~~ [compiled + source review only — see rod-type task]
- [x] ~~B-11: kaseki-program fish (sea bass, red snapper, salmon, coelacanth, jellyfish, knifejaw, whale) get the proxy/driven/event hooks~~ [real-game automated: sea bass and red snapper in L6]
- [x] ~~B-13: `aGYO_actor_dt` releases proxies that still have a fish tied~~ [compiled + source review only]

Open:

- [ ] Test deferred wildlife spawning when a client is **indoors** — BLOCKED: missing fixture/tooling (needs an autopilot command that enters a house and a spawn arriving meanwhile; `AC_TEST_ROOM_ENTER` exists but is villager-house driven).
- [ ] Improve automated whale-fishing coverage — BLOCKED: missing fixture/tooling (the autopilot finds no casting spot for a whale; it swims too far from the beach).
- [ ] Verify individual rod types, including the **golden rod** (B-10): needs a golden rod in a fixture resident's bag and an autopilot equip step; compare bite timing/search radius host-side. — BLOCKED: missing fixture/tooling
- [ ] Runtime-test fish proxy cleanup during **controller teardown** (host leaves the town scene with a fish tied; B-13) — BLOCKED: missing fixture/tooling (a host scene-change autopilot command).
- [ ] Runtime-test the B-9 denial branch (a leftover unstamped fish catch is denied, not granted) and a late packet of an old generation.
- [ ] **B-6**: wildlife when the host is inside a building — all host wildlife work (state, spawns, catch validation) is gated on the host's own scene being the town; clients keep "zombie" fish and every catch is rejected. Decide: document + require a dedicated/observer host, or defer catches and queue triggers. IN PROGRESS (investigation only)
- [ ] **B-12**: `action` / `engaged` are sent in `WILDLIFE_STATE` but no client reads them. Remove, or use them (observers could show a hooked fish).
- [ ] Investigate the unexplained fish-position offset: a constant ~6 unit z difference between host and an observer copy in early samples of one earlier log. Not reproduced since; may be easing residue.
- [ ] Observers do not see the hook / pull-in / lift of another player's fish (the angler's engaged fish follows local vanilla code; the host target is ignored while engaged). Design limitation — decide whether to present it.
- [ ] W-04: attendance radius (700) vs host cull radius (600): a creature 600–700 away flickers; B-7 now releases it after 5 failed re-creations, the mismatch itself is not removed.
- [ ] W-05: host and each client have only 2 fish / 8 bug actors for all players (vanilla pools). A third fish record never gets a host actor and is released by B-7. Design limit; consider a bigger host-side pool.
- [ ] W-06: ant records are inserted but never materialized and block other bug spawns in their acre.
- [ ] W-07: a stamped (driven) fish on a client can never bite, even when the host stopped sending state.
- [ ] W-09/W-10: a lost `BOBBER_EVENT` / lost "bobber gone" can leave the host fish tied until the next bobber stage change; events are reliable but have no resend.
- [ ] W-14: `WILDLIFE_STATE.seq` is ignored on the client (reordered datagram can overwrite a newer target).
- [ ] W-15: catch reach check is generous (fish 800, bug 1000 units) and the species/tool/bite is not verified host-side (modified client could claim any creature it saw).
- [ ] W-17: a bug net-catch with no entity stamp falls through to a vanilla grant (the fish path now denies; the bug path was not changed).
- [ ] Client RNG is still consumed by driven wildlife (`mv_proc` runs on every client); harmless unless something needs RNG parity.

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

- [ ] Investigate **host MOVE relay cost** (F-07 / N-15): each incoming MOVE builds interest views and sends to every other peer — O(N²) per sample (~1.3M views/s at 254 peers). Cache per-peer views per poll; profile with 64–100 bot peers. (source review only)
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
- [ ] The test-only autopilot / hooks (`killfish`, `resetstamps`, `pspawn`, `pdespawn`, `hspawn`, `give`, snapshot-race hook, `[PROXY-DIAG]`) have grown inside `pc_net_game.c` (~37k lines). **Speculative:** move them into their own translation unit.
- [ ] `pcwld_test_set_ttl_override_frames` and `--diag-bug-ttl-lookup` are now no-ops (no TTL exists); remove them when convenient.
- [ ] Logs written next to the net_spike tests are now git-ignored; consider writing them under a temp/`logs/` directory instead.
- [ ] Villager memory holds 7 residents per villager, so 20+ guests thrash friendship/mail memory.
- [ ] **Speculative:** a host-migration or dedicated-observer mode that keeps simulating wildlife while the host player is indoors (would also resolve B-6).
