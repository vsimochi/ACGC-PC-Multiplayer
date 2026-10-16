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
| `test status` / `test help` | whether hooks are enabled / what they gate | no |
| `tp ...` | **UNSUPPORTED** on the server | - |
| `bell ...` | **UNSUPPORTED** on the server | - |

Examples: `where Yubel`, `where peer 0`, `event status halloween`, `wildlife spawn fish seabass 2794 1404`, `wildlife despawn 3`.

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

Tests: `pc/tools/net_spike/test_devconsole_real.py` (real dedicated host process; hooks disabled and enabled).
