# Animal Crossing Multiplayer — Beta 0.1.1

Update to Beta 0.1.0. This is an unofficial fan project, not affiliated with or endorsed by Nintendo, and it is still a beta: keep backups of your `save\` and `servers\` folders before playing and before every update.

## What you need
Windows 10/11 (64-bit), a GPU/driver with OpenGL 3.3 core profile, and **your own legally obtained disc image** of *Animal Crossing* (USA, GAFE01_00) in the `rom\` folder. The packages contain no game data and nothing downloads any.

## Installing and updating
- **New install:** unzip `ACGC-PC-Multiplayer-0.1.1-win64-first-install.zip` into a writable folder, put your disc image in `rom\`, start `ACLauncher.exe`.
- **Existing installation:** `ACLauncher.exe` offers this release and installs it after you say yes (SHA-256 checked, previous program files kept in `.update\backup-<version>`, rolled back on any problem). `ACGC-PC-Multiplayer-0.1.1-win64.zip` is the package the launcher downloads; you do not need it yourself.
- **What an update touches:** only `AnimalCrossing.exe`, `SDL2.dll`, `shaders\`, `licenses\` and the notes. Your `server_config.ini`, `settings.ini`, `keybindings.ini`, `rom\`, `save\`, `servers\` and logs are never changed.
- **Nothing updates the launcher or the server starter.** `ACLauncher.exe`, `Start Server.bat`, `server_starter.ps1` and `SERVER_SETUP.txt` come only with the first-install zip.

### One-time step for existing 0.1.0 installations (needed for the new server startup update check)
The 0.1.0 launcher cannot be replaced by the updater and does not know the new check. To get the new launcher and starter once: open `ACGC-PC-Multiplayer-0.1.1-win64-first-install.zip` (do not extract it over your folder) and copy **only** `ACLauncher.exe`, `Start Server.bat`, `server_starter.ps1` and `SERVER_SETUP.txt` over the ones in your folder. **Never copy `server_config.ini`, `settings.ini`, `rom\`, `save\` or `servers\`.** Until you do this, nothing breaks: the 0.1.0 launcher and starter keep working with the 0.1.1 program files, and the new starter (if you copy only it) says exactly this and skips the check.

## What changed
- **Dedicated server console.** The answers to commands (`help`, `status`, errors, …) now use the same time / level / subsystem format as the server notices and wrap to the window width instead of being cut at the edge; narrow windows get a shorter prefix and no text is lost. Your half-typed command survives log lines and window resizes. Redirected output and `server.log` are unchanged.
- **Dedicated server startup update check.** With `check_updates = 1` in `server_config.ini` (the default for new installs; set 0 to turn it off, or start once with `-SkipUpdateCheck`), `Start Server.bat` asks GitHub for a newer stable release before the server starts. If there is one it shows the installed and available versions and asks `Would you like to download and install the update now? [Y/N]`. Only **Y** installs it, through the launcher's own verified updater; N, an unclear answer, no console to ask on, no internet or any error starts the server you have. It never installs while your game of that folder is running and never closes anything. `-UpdateAnswer yes|no` answers on the command line.
- **Host and play on the same PC.** `Start Server.bat` no longer refuses to start because *your own game* of the same folder is running; it refuses only a second dedicated server (two would write the same `servers\default` town). The server never writes `save\`.
- Rover's "iiiiiiiiii" name, the first-time-character disconnect and Porter's text were already fixed in 0.1.0.

## Known limitations and what was not verified
- **Not verified by a person:** a real-ROM playthrough, a clean-machine install, real public (internet) multiplayer, the on-screen look of the console window, and a person answering the update question. The automated checks drove a real server console window and a real client of the same folder through the console API and fixtures, ran the updater against a local scripted GitHub-like server, and ran the real update path once against the published 0.1.0 release in a temporary folder. 0.1.1 itself has not been published yet, so its own update path against the live release has not been exercised.
- If the game of the folder is a process the updater cannot inspect (for example one started as administrator), the starter refuses to update; the launcher itself can then only rely on its rollback.
- The checksum detects corruption, **not tampering**; there is no signature and the executables are unsigned, so Windows SmartScreen / antivirus may warn.
- Gyroid desync, unshared event NPCs (Redd, Saharah, Joan, …), no teleport command, no ants / bees / ghosts / fishing tournament / island sync, and the other 0.1.0 limitations stand.
- Some diagnostic environment variables (`PC_*_DIAG`, `PC_FORCE_TALK_*`) are still read; they do nothing unless set. The test-hook mechanism is compiled out.
- **Legal status (not cleared):** the executable is built from a public decompilation and contains compiled code and data tables derived from the original game and from SDK headers carrying SGI/Nintendo copyright notices; supplying your own disc image does not settle the licensing of that compiled code. Read `licenses\THIRD_PARTY_NOTICES.txt` and `licenses\THIRD_PARTY_NOTICES.md`; nothing here is legal advice.

Project: https://github.com/vsimochi/ACGC-PC-Multiplayer
