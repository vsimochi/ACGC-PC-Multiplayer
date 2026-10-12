# Master Sprint 2 - progress tracker (final state of this run)

Baseline: HEAD 1c67a1ae9324c96e6549567e8261075efd492a1f, branch multiplayer, everything uncommitted (62 tracked files modified, ~59 untracked files: tests, modules, docs).
States: NOT_STARTED / DIAGNOSING / IMPLEMENTING / NEEDS_INTEGRATION / NEEDS_TESTING / BLOCKED / DONE.
Labels: RUNTIME VERIFIED (real processes) / PROTOCOL/HARNESS VERIFIED (real host + protocol FakeClients) / SOURCE/MODEL TESTED / BUILT, NOT RUNTIME-TESTED / NOT TESTED.
Message ids: 1..78 contiguous; 79 BALL_STATE, 80 BALL_KICK_REQUEST, 81 BEE_SWARM, 83 DRESSER_STATE, 84 DRESSER_QUERY (82 unused). Protocol version still 8. TXN kind 18 = DRESSER, reason 37 = DRESSER_DISABLED. PC_NOOK_MSG_COUNT 35.
Dev build helper: E:\CodeStuff\AnimalCrossingMultiplayer\build_dev.sh (serialized; prints real ld errors). Release (PC_TEST_HOOKS=OFF) dir: E:\CodeStuff\AnimalCrossingMultiplayer\stab_release_build.

| ID | Item | State | Verification | Notes |
|---|---|---|---|---|
| B | Outdoor/observer host shop renewal blocks purchases | DONE | PROTOCOL/HARNESS (observer host: bug reproduced, 1 reload, buy APPLIED, 2 renewals, peers stay; 22/22) | pc_host_renewshop_poll in pc_m_card.c; real --dedicated not run (same observer path) |
| H | Fee once / Work not counted / boost / visitor flag | DONE | SOURCE/MODEL (23/23) | |
| C | Ball sync (79/80) | DONE | RUNTIME (real host + real client: kick accepted, ball moves, proxy converges, one ball, restart rejoin, old-host fallback 7/7); one intermittent 16/18 on a final-exe run, 18/18 on the immediate rerun (unexplained, not reproduced) | scoop/axe on proxy not forwarded; standing push not replicated; dribble = kicks <= 2/s |
| D | Dresser storage (kind 18, msgs 83/84, sidecar, UI, 'More storage' incl. empty/1/2-item dressers, refusal line) | DONE; default ON (AC_MP_DRESSER_STORAGE=0 off) | RUNTIME (real client round trip 9/9; UI-driven put/take 13/13; leave-commit APPLIED 18/18); PROTOCOL 32/32 incl. restart replay; message patch + refusal line SOURCE/MODEL only | visuals NOT TESTED; in-room edit commit while still in room not exercised; host's own dresser stays vanilla |
| E | Nook first-purchase map line | DONE | RUNTIME (flag logic + real message bytes); on-screen render NOT TESTED | ticket rows skip the line |
| F | Submenu world ticking | DONE (stage 2) | RUNTIME on host: 606 NPC_MOVE with walking, villagers moved 188/123 units, actors/banks/pockets identical; client side FakeClient only | refuse/defer paths not hit; no NPC-NPC collision during menu; wildlife/indoor not covered |
| G1 | Museum fish/insect live refresh | DONE | RUNTIME (14/14) | |
| G2 | Able Sisters live refresh | DONE | RUNTIME host-in-shop (11/11) | client-in-shop not run |
| G3 | Fossil exhibit live rebuild | DONE | RUNTIME host by log (13/13) | visual render and client-in-room NOT TESTED |
| A1 | Host Esc pause + conversation suppression + recovery | DONE | RUNTIME host (22/22; clients are FakeClients) | |
| A2 | Villager push + passability | DONE | RUNTIME host (22/22) | real second client NOT TESTED |
| A3 | Bee sync | DONE | RUNTIME (24/24) | observer's own scene change NOT TESTED |
| A4 | FLY_ROD watchdog + sea/kaseki | DONE | RUNTIME (watchdog 240/60/30 fps; sea cast release ignored 7/7). FOUND+FIXED a real bug: watchdog never freed the player | |
| A5 | Dig + tree-drop | DONE | RUNTIME (dig applied + rejected, tree drop 23/23). FOUND+FIXED: TREE_DROP accepted item 0xFFFF | |
| A6 | Preview sync (78) | DONE | RUNTIME (new real client in shop) | old real client in a shop NOT POSSIBLE (v0.1.1 exe refuses --nook-test): old-dispatch safety by source + real old client in town + old host + FakeClients |
| A7a | Client Work reward overflow apply | DONE | RUNTIME (24/24, both apply branches, conservation exact) | |
| A7b | Restock exit | PARTIAL | RUNTIME for 'greeting open: held, dismissed: shown out'; 'idle player shown out' NOT TESTED (test-setup loop) | |
| I | Protocol hardening | DONE | ids unique (1..78, 79-81, 83-84), exact-size static asserts present, version 8, old dispatch ignores unknown ids (source + real old client/host runs), malformed 77/78 rejected on a real host (15/15) | |
| J | Stale test cleanup | DONE (26 -> pass), 4 later hook-related pins being re-fixed | SOURCE/MODEL | |
