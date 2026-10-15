# Animal Crossing Multiplayer — Beta 0.2.1

Hotfix for Beta 0.2.0: remote guests could get stuck at the train tracks after joining. This is an unofficial fan project, not affiliated with or endorsed by Nintendo, and it is still a beta: keep backups of your `save\` and `servers\` folders before playing and before every update.

## What you need
Windows 10/11 (64-bit), a GPU/driver with OpenGL 3.3 core profile, and **your own legally obtained disc image** of *Animal Crossing* (USA, GAFE01_00) in the `rom\` folder. The packages contain no game data and nothing downloads any.

## Installing and updating
- **Existing installation:** start `ACLauncher.exe`; it offers 0.2.1 and installs it after you say yes (SHA-256 checked, previous program files kept in `.update\backup-<version>`, rolled back on any problem). Only `AnimalCrossing.exe`, `SDL2.dll`, `shaders\`, `licenses\` and the notes change; `rom\`, `save\`, `servers\`, `settings.ini`, `server_config.ini` and logs are never touched.
- **Server owners: update the server folder too.** With `check_updates = 1` in `server_config.ini`, `Start Server.bat` offers the update when the server starts (answer **Y**); or start `ACLauncher.exe` in that folder once.
- **New install:** unzip `ACGC-PC-Multiplayer-0.2.1-win64-first-install.zip` into a writable folder, put your disc image in `rom\`, start `ACLauncher.exe`.
- **Nothing updates the launcher or the server starter.** They did not change since 0.1.1, so a 0.1.1 or 0.2.0 installation needs nothing else.
- **One-time step for existing 0.1.0 installations** (only for the server's startup update check; the Dedicated server console improvements, the startup update check and "Host and play on the same PC" came with 0.1.1): open the first-install zip and copy only `ACLauncher.exe`, `Start Server.bat`, `server_starter.ps1` and `SERVER_SETUP.txt` over yours. **Never copy `server_config.ini`, `settings.ini`, `rom\`, `save\` or `servers\`.**

## What changed in 0.2.1
- **Fixed: remote guests stuck at the train tracks after joining.** A joining player's arrival scene waited for the host's player list and world snapshot without any time limit; if they arrived late, the player stayed at the tracks. The wait is now bounded: the arrival starts at most 4.5 seconds after the connection is ready and 15 seconds after joining at the latest, and another player the game does not know yet holds a joiner for at most 3 seconds.
- **The host spaces out what it sends to a joining player** (town pages, villager outfits, mailbox letters) until that player's world snapshot is complete, so the join is not slowed down by them.
- **New diagnostics for bug reports:** set `client_log = 1` in the `[Debug]` section of `settings.ini` (or start with the environment variable `AC_CLIENT_LOG=1`) and the game writes its log, including a `[JOIN]` trace of every join step, to `client_log.txt` next to `AnimalCrossing.exe` (rotated at 4 MB). It is off by default and never used by a dedicated server (its log stays in `servers\<id>\logs`).
- **Honest note:** the exact cause of the stuck arrival could not be reproduced in our tests, not even with added network delay and packet loss. The fix therefore bounds the wait and adds the diagnostics above. If you still get stuck, please enable `client_log`, try again, and send `client_log.txt` together with the server's log (`servers\<id>\logs\server.log`).
- **Your items are safe:** a guest's items and Bells are stored on the server and were not lost. A stuck guest only saw an empty bag until its record arrived.

## Playing with other versions
The network protocol and messages are unchanged from 0.2.0: 0.2.1 and 0.2.0 games work together. The host part of the fix helps every player who joins a 0.2.1 host; the client part helps the player running 0.2.1, so friends who join should update too.

## Known limitations and what was not verified
Unchanged from 0.2.0 (see the 0.2.0 release notes): most features are verified by automated tests, not by a person playing; event NPCs are still not shared; island / Kapp'n, fishing tournament and bug-off are missing; gyroid desync is unresolved; the executables are unsigned, so Windows SmartScreen / antivirus may warn, and the checksum detects corruption, not tampering. The 0.2.1 fix was checked by automated tests with real game processes; it was not verified by a person joining over the internet.

**Legal status (not cleared, unchanged):** the executable is built from a public decompilation and contains compiled code and data tables derived from the original game and from SDK headers carrying SGI/Nintendo copyright notices; supplying your own disc image does not settle the licensing of that compiled code. Read `licenses\THIRD_PARTY_NOTICES.txt` and `licenses\THIRD_PARTY_NOTICES.md`; nothing here is legal advice.

Release notes of 0.2.0: https://github.com/vsimochi/ACGC-PC-Multiplayer/blob/v0.2.1/docs/release-notes-0.2.0.md . Project: https://github.com/vsimochi/ACGC-PC-Multiplayer
