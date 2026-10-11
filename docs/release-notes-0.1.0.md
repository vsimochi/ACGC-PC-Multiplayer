# Animal Crossing Multiplayer — Beta 0.1.0

**First public beta.** Expect bugs, desyncs and rough edges; keep backups of your `save\` folder. This is an unofficial fan project, not affiliated with or endorsed by Nintendo.

## What you need
- Windows 10/11, 64-bit, and a GPU/driver with **OpenGL 3.3 core profile**.
- **Your own legally obtained disc image** of *Animal Crossing* (USA, GAFE01_00; `.iso`, `.gcm` or `.ciso`). It is **not included and never downloaded**. Put it in the `rom\` folder. Only the USA Rev 0 image (GAFE01_00) is supported.

## Install
1. Download `ACGC-PC-Multiplayer-0.1.0-win64-first-install.zip` and unzip it into a normal writable folder (not a Git checkout, not Program Files).
2. Copy your disc image into `rom\`.
3. Start **`ACLauncher.exe`** (not `AnimalCrossing.exe`) — it checks GitHub for newer releases and asks before updating. Read `INSTALL.txt` and `UPDATING.txt`. (`ACGC-PC-Multiplayer-0.1.0-win64.zip` is the update package the launcher downloads; you do not need it for a new install.)

## Playing together
- **Host:** `ACLauncher.exe --host` (UDP port 7777, or `--host <port>`). **Join:** title screen -> *Play Online* -> add the host's address and port -> choose or create a character; or `ACLauncher.exe --connect <ip>[:port]`.
- Residents and guests, host-authoritative world (items, digging, planting, shops/Nook, Gulliver and K.K. Slider claims, fish and bug sync), persistent identities and guest profiles.
- **Network:** the host must allow inbound UDP on its port (Windows Firewall, and a router port forward over the internet; the game does not open ports itself). Everyone must run the same release. Defaults: 4 guests / 8 peers.
- **Saves:** the town save belongs to the host. **Back up `save\` before playing and before every update.** Only play with people you trust.

## Dedicated server (new in this release)
- **Easy start (Windows):** edit `server_config.ini` (every setting is commented: port, peers, guests, new-guest policy, resident tokens, town serving, personal sync, interest management, guest memory, debug categories, update check), then double-click **`Start Server.bat`**. It validates the whole file first, tells you exactly what is wrong, writes the two settings that have no command-line switch into `settings.ini` without touching anything else there, and starts the server. `SERVER_SETUP.txt` covers the first start, stopping (`stop`), Windows Firewall, router forwarding and how friends connect. These files are in the **first-install bundle only** (updates never touch `server_config.ini`).
- **First start asks questions** in the server's console window (use an existing town or generate a new one; the town name). They are never answered automatically.
- **Console:** the server opens its own console window with a proper line editor (your half-typed command is never corrupted by log output), consistent `HH:MM:SS LEVEL SUBSYSTEM message` lines, and clear player messages: `A client is connecting to the server...`, `<name> arrived into town`, `<name> left town`. Detailed peer/network lines remain in `servers\default\logs\server.log` and appear on the console with `-debugnetwork` / `--verbose`. Commands: `docs\console-commands.md`.

## Fixes in this release
- **First-time character creation** no longer gets disconnected during Rover's long train introduction (the client sends a small keepalive while it cannot identify itself yet; the host's 60 s identity deadline is extended up to 30 minutes since connecting).
- **Rover** no longer shows `iiiiiiiiii` where he names another traveller when the town has no other resident; he says "A traveler" instead.
- **Porter** (station master) no longer offers a trip to another village that this version does not support; he explains that the station serves this town and online visits are arranged by connecting to a host.

## Known limitations and unverified behaviour
- **Not verified on this release by a person:** a real-ROM playthrough, a clean-machine install/first run through the starter, real public (internet) multiplayer, and the on-screen look of the console (colours/fonts) and of the Porter / Rover text boxes. Automated checks ran against fixtures with a disc image on the developer's machine: scripted host/client processes, a real server console window driven through the console API, and package/installation tests with a stub game; the real Rover cutscene was tested with a transport-level client, not a real character creation.
- **Gyroid duplication / desync (stacked gyroids outside houses) is unresolved**: it could not be reproduced and no fix was made.
- Event NPCs other than Gulliver and K.K. (Redd, Saharah, Joan, …) are **not shared**: two players can buy the same unique stock.
- No teleport command; the console cannot start/stop events, set bells or resync. Unsupported: ants, bees, ghosts, fishing tournaments / bug-off, island / Kapp'n sync, text chat, mailbox / museum for guests.
- The dedicated console's player lines appear when a client sends an identity (or keepalive) and when it is standing in the town; a client from an older build that stays silent through Rover shows "connecting" only when it identifies. A town-fetch download is not reported as a player.
- Server settings are read once at start (no reload); the starter does not restart a crashed server and does not install a service.
- The updater checks the latest stable GitHub release, verifies the package SHA-256 and installs with backup and rollback. The checksum detects corruption, **not tampering** (there is no signature), and its dialogs were not exercised on screen. Windows SmartScreen / antivirus may warn: the executables are **unsigned**.
- Some diagnostic environment variables (`PC_*_DIAG`, `PC_FORCE_TALK_*`) are still read in this build; they do nothing unless set deliberately. The test-hook mechanism is compiled out of this build.
- **Legal status (not cleared):** this is an unofficial fan project. The executable is built from a public decompilation of the game and contains compiled code and data tables derived from the original game and from SDK headers carrying SGI/Nintendo copyright notices; supplying your own disc image does not by itself settle the licensing of that compiled code. Several provenance questions are open. Read `licenses\THIRD_PARTY_NOTICES.txt` and `licenses\THIRD_PARTY_NOTICES.md`; nothing here is legal advice. The packages contain no disc image and no intentionally bundled extracted game data; they use a generic icon created for this project.

Full status: https://github.com/vsimochi/ACGC-PC-Multiplayer/blob/v0.1.0/TODO.md . Source: https://github.com/vsimochi/ACGC-PC-Multiplayer
