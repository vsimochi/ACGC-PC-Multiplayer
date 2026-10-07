# Multiplayer scalability (capacity phases 5, 6, 7)

Phases 1-4 made the transport (`max_peers`, up to 254 wire ids), the guest store (one `.gst` file per guest) and guest admission (`max_guests` 1..254) dynamic. Phases 5-7 remove what was left
between "the host admitted a guest" and "everybody sees and serves that guest". Wire peer ids stay `uint8_t`; the host's own id stays 8; `0xFF` is "nobody"; nothing in the protocol changed
(no new message, no version bump).

## The bug that started it (root cause)

`pc_remote_player.c` kept a static table of **9 puppet slots** (player ids 0..7 plus the host's wire id 8) and rejected every other id at the door (`pc_remote_player_get_slot` returned NULL).
The 9th client of a host gets wire id **9** (8 is the host's): it received the roster and the movement of ids 0..7 and the host (all below 9), so it saw everybody; but every other process
(and the host) refused to create a puppet for id 9 - appearance, scene and movement for it were dropped. Admission, transport and persistence were fine.

## Phase 5 - interest management, roster deltas, reliable pressure

| piece | where | what |
|---|---|---|
| roster book | `pc_roster.c` | per destination: entries still owed and departures still owed. A join marks the joiner for every READY peer and every READY subject (and the host) for the joiner; a change marks one subject; a leave owes a CLEARED to the others. |
| roster pump | `pcnetgame_roster_pump` (pc_net_game.c) | per READY peer and frame: at most 4 messages per kind, only while its reliable window is at most half full; a bit is cleared only when `pc_net_send` accepted the message. A full window delays that peer, never anyone else. Departures go before entries (ids are reused). |
| refresh | `pc_roster_refresh_step` | the old "full roster to every peer every 3 s" (N x N reliable messages) is one subject per period (N messages). |
| MOVE relay | `pc_interest.c` | the host relays a sample at a rate that depends on the receiver's place: NEAR (unknown, same interior, same town field within 1 acre) every sample; MID (<= 3 acres) every 2nd; FAR every 4th; APART (another scene) every 20th. Acres are the game's own unit (640 world units). A scene change boosts the sender to full rate for 40 samples. Nobody is ever cut to zero; unknown scenes / positions fail open. `settings.ini [Network] interest_management = 0` restores the old full relay. |
| transport | `pc_net.c` | **unchanged**. Reliable per-peer windows, sequence / SACK / ACK, the (unused by the game) all-or-nothing reliable broadcast contract are as before. |

## Phase 6 - dynamic puppets, actors, collision

* Puppet slots are a pool (`pc_puppet_pool.c`): allocated on demand for any wire id 0..254, zeroed and 32-byte aligned (the appearance buffers are DMA targets), freed when the player leaves (disconnect on the host, the host's CLEARED notice on a client, shutdown). Readers never allocate.
* Three resources are kept apart: slot **state** (this pool, memory only), the puppet **actor** (the scene's actor pool: `mAc_MAX_ACTORS` = 200, shared with villagers, items and effects - puppets are created only while at least `PC_PUPPET_ACTOR_RESERVE` = 48 actors stay free; a puppet that does not fit stays pending and is counted), and the shared **collision** table (`Cl_COLLIDER_NUM` = 50: only puppets within 150 units of the local player register; a refused `setOC` is counted).
* The per-puppet effect id (`item_name`) was `0xFFE0 + id` (room for 17 ids); it is `0xFEE0 + id` (0xFEC3..0xFFDF is unused by `m_name_table.h`). Actor names for ids above 8 are `0xFA00 | id` (below 8 unchanged).
* Still true: only puppets that are **visible** matter to the player; a scene mismatch hides a puppet but its actor is still created (the door / arrival logic uses it).

## Phase 7 - remaining shared guest state

| structure | class | decision |
|---|---|---|
| K.K. once-per-concert song (`foreigner_bitfield`, one bit for every foreigner) | C | host keeps the claim **per guest identity** (home PersonalID) and day (`pc_dayclaims.c`); residents keep their vanilla bits |
| Nook Work Mode job table (`s_work[64]`, one record per character ever) | B | dynamic `PCKeyTab` keyed by PersonalID, bounded by a quarter of `guest_memory_mb`; `work_jobs.dat` v3 (record count) with a v2 (64 records) reader; a promoted guest's record is re-keyed by remove + add |
| kabu peddler `spoken_pids[TOTAL_PLAYER_NUM]`, `shine_pos` / `stone_pos`, the gateway event bits (`TOTAL_PLAYER_NUM` = 4 residents + 1 foreigner) | A | per-process save state of each CLIENT (its own single foreigner slot); the host never indexes them with a guest. Unchanged |
| villager memories (`Anmmem_c.memories[7]` per villager) | D | content-addressed by PersonalID already (stable identity); 7 is vanilla's own "who remembers me" mechanic |
| guest record slots, guest tables, transaction journals | B | already dynamic since phase 3 |
| mailbox host state, mail replies (`s_mbox_host[PLAYER_NUM]`, `s_remail_day[PLAYER_NUM]`), houses (`PC_NETGAME_HOUSE_NUM` 4), resident credentials | A | residents only: guests have no house, no mailbox, no resident credential |
| event NPC table (`PC_EVNPC_MAX` 8), tree-cut slots, money rocks, field-action queue (client side), per-address token issuance (3 / 60 s), town-transfer backlog and rate | D | gameplay or abuse limits, not guest counts |

## Phase 7 runtime verification (real game processes)

`tools/net_spike/test_kk_work_guests_real.py` (K.K. 17 checks, Work Mode 40) and `test_kk_work_promotion_real.py` (13) run a REAL host with REAL guest / resident client processes on a disposable fixture
(TEST-ONLY hooks: `AC_TEST_KK_CONCERT`, `AC_TEST_KK_DAY_FILE`, `AC_TEST_EVNPC_CLAIM`, `AC_TEST_WORK_ENTER` / `AC_TEST_WORK_KEEP`). Seen in the host logs: a guest that took its song, left, and came back
under another wire id is refused (`EVNPC_DONE`); another guest that was handed the freed wire id is served; the shared foreigner bit stays 0 through every guest claim; the next day claims again; five
guests with different PersonalIDs hold independent work records; a reconnect or a host restart finds the same record; a version 2 file (64 rows) is read and rewritten as version 3 and the 65th and
the 265th characters work; promotion re-keys the work record (same bytes, new key) and moves the claim to the new resident slot's vanilla bit (a promoted guest cannot take a second song of the same concert).
Not driven (GUI): the K.K. / Nook dialogue on screen; the claim and work requests are the exact calls the dialogue seams make.
Harness limits seen (kept, class D): `max_guests` (default 4), `max_peers`, 3 new guest tokens per address per 60 s.

## What is NOT claimed

Phases 5 and 6 were later run in the real game (the 9th guest works); Phase 7 is covered by the section above. The native tests are model / logic tests of the pure modules plus the real transport over loopback for Phase 4.
A crowd is still bounded by: the wire ids (254 peers), `max_peers`, `max_guests`, `guest_memory_mb`, the scene's 200 actors, 50 colliders, the renderer's cost of many animated puppets, and the
remaining O(N^2) of the host's per-peer relays (MOVE is thinned, appearance / scene are deltas; reliable gameplay broadcasts are unchanged).
