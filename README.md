# Animal Crossing PC Port (Multiplayer)

A native PC port of Animal Crossing (GameCube) built on top of the [ac-decomp](https://github.com/ACreTeam/ac-decomp) decompilation project, with online multiplayer.

The game's original C code runs natively on x86, with a custom translation layer replacing the GameCube's GX graphics API with OpenGL 3.3.

This repository does not contain any game assets or assembly whatsoever. An existing copy of the game is required.

Supported versions: GAFE01_00: Rev 0 (USA)

> **This is an unofficial, non-commercial fan project. It is not made, endorsed, sponsored or approved by Nintendo.** "Animal Crossing" and related names belong to their owners. You must supply your own legally obtained game disc image. See [THIRD_PARTY_NOTICES.md](https://github.com/vsimochi/ACGC-PC-Multiplayer/blob/multiplayer/THIRD_PARTY_NOTICES.md) for the licensing status of the code in this repository.

This is a multiplayer fork of the original PC port by FlyingMeta, [flyngmt/ACGC-PC-Port](https://github.com/flyngmt/ACGC-PC-Port). The multiplayer code lives on the [`multiplayer`](https://github.com/vsimochi/ACGC-PC-Multiplayer/tree/multiplayer) branch, and the releases are built from it.

## Quick Start (Pre-built Release)

Pre-built releases are on the [Releases](https://github.com/vsimochi/ACGC-PC-Multiplayer/releases) page. No build tools required.

1. Download `ACGC-PC-Multiplayer-<version>-win64-first-install.zip` and unzip it into a normal writable folder
2. Place your disc image (`.iso`, `.gcm` or `.ciso`) in the `rom/` folder
3. Run `ACLauncher.exe`

The launcher checks GitHub for new versions and asks before updating. Your `save/`, `servers/` and settings are never touched by an update, but back up `save/` before playing and before every update. This is a beta.

The game reads all assets directly from the disc image at startup. No extraction or preprocessing step is needed.

## Playing Together

- **Host:** run `ACLauncher.exe --host` (UDP port 7777, or `--host <port>`).
- **Join:** on the title screen choose *Play Online* and add the host's address and port, or run `ACLauncher.exe --connect <ip>[:port]`.
- **Dedicated server:** edit `server_config.ini`, then double-click `Start Server.bat`. The server opens its own console (type `help`).
- The host needs to allow inbound UDP on its port (firewall and, over the internet, a router port forward). Everyone should run the same version.
- Players join as **residents** (their own house and character) or as **guests** (no house; they can buy a house from Nook).

What the multiplayer includes: a host-run world (items, digging, planting, shops, events), other players shown walking, fishing, sitting and more, shared villagers and wildlife, chat, emotes, dresser storage, outdoor furniture, guest mail and museum donations, and a server console with operator and moderation commands. See the release notes for what each version added.

## Building from Source

Only needed if you want to modify the code. Otherwise, use the pre-built release above.

### Requirements

- **MSYS2** (https://www.msys2.org/)
- **Animal Crossing (USA) disc image** (ISO, GCM, or CISO format)

### MSYS2 Packages

Open **MSYS2 UCRT64** from your Start menu and install:

```bash
pacman -S mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-cmake mingw-w64-ucrt-x86_64-SDL2 mingw-w64-ucrt-x86_64-make
```

### Build Steps

1. Clone the repository:
   ```bash
   git clone -b multiplayer https://github.com/vsimochi/ACGC-PC-Multiplayer.git
   cd ACGC-PC-Multiplayer
   ```

2. Build (from the **MSYS2 UCRT64** shell):
   ```bash
   cmake -S pc -B pc/build64 -G "MinGW Makefiles" -DPC_ALLOW_64BIT=ON -DPC_TEST_HOOKS=OFF -DPC_CONSOLE=OFF
   cmake --build pc/build64 -j
   ```

3. Place your disc image in the `rom/` folder:
   ```
   pc/build64/bin/rom/YourGame.ciso
   ```

4. Run:
   ```bash
   pc/build64/bin/AnimalCrossing.exe
   ```

More details are in [pc/DOCUMENTATION.md](https://github.com/vsimochi/ACGC-PC-Multiplayer/blob/multiplayer/pc/DOCUMENTATION.md).

## Controls

Keyboard bindings are customizable in the game (Esc menu > Settings > Controls > Keybindings) or via `keybindings.ini` next to the executable. Mouse buttons (Mouse1/Mouse2/Mouse3) can also be assigned.

### Keyboard (defaults)

| Key | Action |
|-----|--------|
| WASD | Move (left stick) |
| Arrow Keys | Camera (C-stick) |
| Space | A button |
| Left Shift | B button |
| Enter | Start |
| X | X button |
| Y | Y button |
| Q / E | L / R triggers |
| Z | Z trigger |
| I / J / K / L | D-pad (up/left/down/right) |
| T | Chat: type a line, Enter sends it, Esc cancels |
| G (hold) | Tool wheel: point with the mouse, release to equip |
| F (hold) | Emote wheel: point with the mouse, release to play |

### Gamepad

SDL2 game controllers are supported with automatic hotplug detection. Button mapping follows the standard GameCube layout. Hold **LB** for the tool wheel and **D-pad Up** for the emote wheel (the right stick picks, release confirms).

## Command Line Options

| Flag | Description |
|------|-------------|
| `--host [port]` | Host a game (default port 7777) |
| `--connect <ip>[:port]` | Join a host directly |
| `--dedicated` | Run a dedicated server (see `Start Server.bat`) |
| `--verbose` | Enable diagnostic logging |
| `--no-framelimit` | Disable frame limiter (unlocked FPS) |
| `--time HOUR` | Override in-game hour (0-23) |

## Settings

Graphics settings are stored in `settings.ini` and can be edited manually or through the in-game options menu:

- Resolution (up to 4K)
- Fullscreen toggle
- VSync
- MSAA (anti-aliasing)
- Texture Loading/Caching (No need to enable if you aren't using a texture pack)

Multiplayer options (chat, emotes, and so on) are in the same file. To write a log file for bug reports, set `client_log = 1` under `[Debug]`.

## Texture Packs

Custom textures can be placed in `texture_pack/`. Dolphin-compatible format (XXHash64, DDS).

I highly recommend the following texture pack from the talented artists of Animal Crossing community.

[HD Texture Pack](https://forums.dolphin-emu.org/Thread-animal-crossing-hd-texture-pack-version-23-feb-22nd-2026)

## Save Data

Save files are stored in `save/` using the standard GCI format, compatible with Dolphin emulator saves. Place a Dolphin GCI export in the save directory to import an existing save. The town save belongs to the host; a dedicated server keeps its own in `servers/`.

## Credits

This project would not be possible without the work of the [ACreTeam](https://github.com/ACreTeam) decompilation team. Their complete C decompilation of Animal Crossing is the foundation this port is built on.

Also built on: the original PC port by FlyingMeta (flyngmt) and its contributors, [fixNES](https://github.com/FIX94/fixNES) by FIX94, SDL2, and glad. Details and licences: [THIRD_PARTY_NOTICES.md](https://github.com/vsimochi/ACGC-PC-Multiplayer/blob/multiplayer/THIRD_PARTY_NOTICES.md).

## AI Notice

AI tools such as Claude were used in this project. In the original port this covered the PC port code only; in this fork, AI assistance was used for a large part of the multiplayer, networking, console, updater, test and documentation work. The human maintainers review and are responsible for what is published.

## Legal and Notices

This is an unofficial fan project. [LICENSE](LICENSE) is the inherited licence file of the original port. It does not and cannot clear rights that Nintendo or other third parties may hold in the game. Per-component provenance and open questions are in [THIRD_PARTY_NOTICES.md](https://github.com/vsimochi/ACGC-PC-Multiplayer/blob/multiplayer/THIRD_PARTY_NOTICES.md). Nothing here is legal advice.

## FAQ

See [FAQ](FAQ.md) for more info.
