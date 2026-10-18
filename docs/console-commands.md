# Console command reference

There are two command channels; the game has no in-game text console.

1. **Dedicated-server console** (`AnimalCrossing.exe --host <port> --dedicated`, commands typed on its stdin / console window; implemented in `pc/src/pc_dedicated.c`). Documented below.
2. **Test autopilot** (test-hook builds, `AC_TEST_HOOKS=1`, `AC_TEST_AUTOPILOT=<file>`): one command line per `ac_auto_<label>.txt` for a *running game process* (`pos`, `walk`, `tp X Z`, `hspawn`, `give`, `place`, ...; see `pc/src/pc_net_game.c` and `pc/tools/net_spike/wplay_lib.py`). `tp X Z` there is a raw positioning aid for fixtures, not a safe teleport.

Syntax: `command [subcommand] [arguments]`, case-insensitive command word. Failures print `... FAILED: reason` / `... REFUSED: reason` / `... UNSUPPORTED: reason`; nothing is claimed that did not happen. `help` lists everything.

| Command | Effect | Hooks needed |
|---|---|---|
| `help` | all commands | no |
| `status`, `players`, `guests`, `members`, `residents`, `save`, `stop`, guest/resident admin | existing server commands (see `help`) | no |
| `finditem <text>`, `iteminfo <id>`, `items <category>`, `give <player> <id> [qty]` | existing item tools (real item tables, free pockets only, all-or-nothing) | no |
| `where` / `where <name>` / `where peer N` | last position a connected player reported: x y z and acre (x/640, z/640) | no (read-only) |
| `net status` / `net peers` | role, port, peers/ready, transport counters / per-peer table | no |
| `net resync` | **UNSUPPORTED**: no general resync API exists; each system resyncs itself on connect | - |
| `time get` | game date, time, weekday | no |
| `day` / `day status` | day rollover (docs/multiplayer-guest-roadmap.md "Day rollover on a long-running host"): ON/OFF (`AC_MP_DAY_ROLL=0`), game date, last new-day init, last / next growth, state (idle / DUE / DEFERRED: why, how long / ROLLING), rolls this run + the last result | no (read-only) |
| `day roll now` | a DUE roll skips the wait for players in "soft" buildings (museum, post office, police, other interiors); a shop / villager house / joining player still holds it; nothing due -> says so | no |
| `day advance [hhmm]` | **TEST-ONLY**: moves the game clock to tomorrow hh:mm:05 (default 00:00) so the next frames run a real roll | **yes** |
| `event list` / `event status [name]` | the game's event schedule for today (`scheduled-today`, `active-now`, RUN/ACTIVE/ERROR status) for the 119 event types | no |
| `event start|stop|reset <name>` | **REFUSED** with the reason (see below) | - |
| `wildlife list` / `wildlife info <id>` | the host's authoritative wildlife records | no |
| `wildlife spawn fish <species> <x> <z>` | one authoritative fish through the host's own injection path (`pcwld_test_host_inject`); species `seabass snapper coelacanth jellyfish knifejaw whale salmon2` or a number; coordinates must be inside the town | **yes** |
| `wildlife despawn <id>` | releases the record (clients despawn it) | **yes** |
| `wildlife spawn bug`, `wildlife sync` | **UNSUPPORTED** (no host bug-injection path; sync is automatic) | - |
| `npc` | villagers (docs/multiplayer-guest-roadmap.md "Villager authority range and stale fallback"): per slot who simulates it (`host actor` / `relay peer N` = a client runs it and the host relays it / `no actor`), the last NPC_MOVE age, the conversation hold (peers, age, keepalive age, capped peers), at home / outside, position + acre (`[list]` = the host's villager list, no live sample) | no (read-only) |
| `npc free <slot>` | releases a stuck conversation hold of that villager now; the holder's keepalives are refused until its END | no |
| `test status` / `test help` | whether hooks are enabled / what they gate | no |
| `ops` | the operator list (`servers\<id>\ops.ini`): kind (resident / guest), name, `(online)` | no (read-only) |
| `op <player>` / `deop <player>` | make / unmake an OPERATOR (like Minecraft ops). `<player>` = the name of an online player, of a resident of the save or of a guest of this town (case-insensitive), or `peer N`. The identity stored is the authenticated binding key (name + town + player id + land id; a guest: its guest key), never the name alone; a name that matches two identities is REFUSED as ambiguous (use `peer N`). An online player's rights change at once. Operators may pick up / rotate placed furniture (more admin features will use the same check). `deop` also accepts a name only the list still knows. | no |
| `furniture list [acre]` | furniture PLACED outdoors (docs/multiplayer-guest-roadmap.md "Outdoor furniture (C1) and the operator list"): index, tile (town units), acre, item, rotation S/E/N/W, placer kind + name, time; the safety ceilings and the Place / leaf-drop switches | no (read-only) |
| `furniture remove <x> <z> [delete]` / `furniture remove #<index> [delete]` | removes ONE placed piece: the tile becomes empty and the item goes to the town's lost and found (police box), or is deleted with `delete`. A storage piece whose outdoor storage holds items is REFUSED (take them out first); with `delete` the piece goes and its storage is kept as an orphan (the next piece of that id placed outdoors gets it back). `furniture list` shows `[storage: N item(s)]` | no |
| `furniture clear acre <n> [delete]` / `furniture clear player <name> [delete]` | removes every placed piece of an acre (0..29 = az * 5 + ax) / of a placer (by the name stored at placement) | no |
| `roommates [list]` | ROOMMATES (docs/multiplayer-guest-roadmap.md "Roommates / move-in"): every relation of `roommates.ini` (servers/<id>/ or save/mp/): state pending / roommate / denied / removed, guest, house holder (+ its house), time, waiting notices | no (read-only) |
| `roommates remove <guest name> confirm` / `roommates clear <guest name> confirm` | demote that guest (it is told at its next join, shown out of the house if inside) / delete every relation of that guest (no notice). Without `confirm` it says what it would do | no |
| `roommates owner <x> <z> <resident slot 0-3\|name>` | sets the house holder of the gyroid PLACED on that tile (town units, see `furniture list`); the resident must have a house | no |
| `say <text>` | a server announcement: every connected player sees it in the chat log as `Server: <text>` (up to 60 characters of the game font; other characters become `?`); logged `[CHAT] [server]: <text>` | no |
| `mutes` / `mute <player>` / `unmute <player>` | the chat mute list (`servers\<id>\mutes.ini`, the same identity keys and name / `peer N` resolution as `op`; ambiguous names are refused). A muted player's lines reach nobody (they get "You are muted on this server."); an online player is told at once. `unmute` also accepts a name only the list still knows | no |
| `chat [on\|off]` | the text chat: state + counters (relayed, dropped for rate / muted / malformed / off), or switch it for this session (every client is told at once; `settings.ini` `[Network] chat = 0\|1` sets the start value). Every line players send is logged as `[CHAT] name: text` (console + `server.log`) | no |
| `tp ...` | **UNSUPPORTED** on the server | - |
| `bell ...` | **UNSUPPORTED** on the server | - |

Examples: `where Yubel`, `where peer 0`, `event status halloween`, `wildlife spawn fish seabass 2794 1404`, `wildlife despawn 3`, `op Bella`, `op peer 2`, `ops`, `furniture list 12`, `furniture remove 41 57`, `furniture clear player Bella delete`.

A listen host (no `--dedicated`) has no console: its own player is implicitly an operator, and `ops.ini` lives in `save\mp\` (edit it while the host is stopped: `op = resident|guest <40 hex digits> <name>`).

## The console window

Started by hand (double-click, `Start Server.bat`, a shell) the server opens its **own console window** and takes it over, so text you are typing is never torn by log output:
- A `> ` prompt line with a line editor: type, Backspace / Delete, Left / Right / Home / End, Esc or Ctrl+U clears the line, Enter runs it (exactly once). Log lines and notices are printed above the prompt; your half-typed command and cursor are redrawn unchanged. A line longer than the window scrolls sideways.
- Server notices are formatted `HH:MM:SS LEVEL  SUBSYSTEM  message` (LEVEL is INFO / WARN / ERROR; colour is added only as a second signal and `NO_COLOR` turns it off). Command responses are printed as they are.
- Player messages: `A client is connecting to the server...`, `<name> arrived into town` (printed when the player is standing in the town, not when the connection or identity handshake completes: a first-time character is still in Rover's train introduction then) and `<name> left town`. The low-level peer lines (`peer N connected (transport; awaiting identity)`, `READY`, `disconnected`) are written to `servers\<id>\logs\server.log` and shown on the console with `-debugnetwork` / `--verbose`.
- With `-debug...` / `--verbose` the game's own output is routed through the same locked writer. If the console cannot be switched into this mode a notice says so and the plain line input is used.
- Redirected stdin/stdout (tests, services, `> log`) are not changed: plain lines, `[DEDICATED] ...` prefixes, commands read line by line from stdin.

## Why some commands are not offered
- **`tp`**: the dedicated server has no local player, and a remote player's position is owned by that client (the host only receives it); moving it from the host would be overwritten by the client's next move sample. A safe local teleport also has to go through scene/acre transitions; the autopilot's `tp X Z` only overwrites coordinates and is for fixtures.
- **`event start|stop|reset`**: the only forced-init entry is `mEv_init_force`, which re-rolls the whole day's schedule and renewal (changes ordinary scheduling, can duplicate reward state); a status flag alone would not create the event's actors. Shared event-NPC claims (Redd, Saharah, Joan, ...) remain future work and are not touched.
- **`bell`**: bells are per-player save data and there is no host-side admin API; `give` covers items.
- **`net resync`**: see above.

Tests: `pc/tools/net_spike/test_devconsole_real.py` (real dedicated host process; hooks disabled and enabled); `test_ofurn_console_protocol.py` (`ops` / `op` / `deop` / `furniture` against a real dedicated host with FakeClients).
