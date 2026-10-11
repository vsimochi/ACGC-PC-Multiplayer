# Animal Crossing PC Port

A native PC port of Animal Crossing (GameCube) built on top of the [ac-decomp](https://github.com/ACreTeam/ac-decomp) decompilation project.

The game's original C code runs natively on x86, with a custom translation layer replacing the GameCube's GX graphics API with OpenGL 3.3.

This repository does not contain any game assets or assembly whatsoever. An existing copy of the game is required.

Supported versions: GAFE01_00: Rev 0 (USA)

> **This is an unofficial, non-commercial fan project. It is not made, endorsed, sponsored or approved by Nintendo.** "Animal Crossing" and related names belong to their owners. You must supply your own legally obtained game disc image; nothing in this project grants any right to the original game. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for what is and is not established about the licensing of the code in this repository (some provenance questions are still open).

**This repository is a multiplayer fork** (host / client and dedicated-server multiplayer, wildlife synchronization, a developer console, a GitHub-based updater) of the original PC port [flyngmt/ACGC-PC-Port](https://github.com/flyngmt/ACGC-PC-Port). The original port's own README, FAQ answers and licence below were written by its author; where they describe the original port (for example "online play is not planned" in the FAQ) they do not describe this fork.

## Quick Start (Windows ZIP, Beta 0.1.0)

Requirements: Windows 10/11 64-bit, an OpenGL 3.3 capable GPU, and **your own disc image of Animal Crossing (GameCube, USA, Rev 0 = GAFE01_00)** as `.iso`, `.gcm` or `.ciso`. Other regions/revisions are not supported.

1. Download `ACGC-PC-Multiplayer-<version>-win64-first-install.zip` from this repository's Releases page (when a release has been published) and unzip it into a normal writable folder (not Program Files, not a Git checkout).
2. Copy your disc image into the `rom/` folder. The ZIP contains no disc image and no extracted game data.
3. Start **`ACLauncher.exe`** (it checks GitHub for newer releases, asks before changing anything, then starts the game). `INSTALL.txt` and `UPDATING.txt` in the folder explain updates and recovery.

**Back up your `save/` folder before playing and before every update.** This is a beta: multiplayer can desync and a bug could damage a save.

The game reads all assets directly from the disc image at startup. No extraction or preprocessing step is needed. The original port's own pre-built releases are at [flyngmt/ACGC-PC-Port releases](https://github.com/flyngmt/ACGC-PC-Port/releases).

## Building from Source

Only needed if you want to modify the code. Otherwise, use the [pre-built release](https://github.com/flyngmt/ACGC-PC-Port/releases) above.

### Requirements

- **MSYS2** (https://www.msys2.org/)
- **Animal Crossing (USA) disc image** (ISO, GCM, or CISO format)

### MSYS2 Packages

Open **MSYS2 MINGW32** from your Start menu and install:

```bash
pacman -S mingw-w64-i686-gcc mingw-w64-i686-cmake mingw-w64-i686-SDL2 mingw-w64-i686-make
```

### Build Steps

1. Clone the repository:
   ```bash
   git clone https://github.com/flyngmt/ACGC-PC-Port.git
   cd ACGC-PC-Port
   ```

2. Build (from **MSYS2 MINGW32** shell):
   ```bash
   ./build_pc.sh
   ```

3. Place your disc image in the `rom/` folder:
   ```
   pc/build32/bin/rom/YourGame.ciso
   ```

4. Run:
   ```bash
   pc/build32/bin/AnimalCrossing.exe
   ```

## Controls

Keyboard bindings are customizable via `keybindings.ini` (next to the executable). Mouse buttons (Mouse1/Mouse2/Mouse3) can also be assigned.

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

### Gamepad

SDL2 game controllers are supported with automatic hotplug detection. Button mapping follows the standard GameCube layout.

## Command Line Options

| Flag | Description |
|------|-------------|
| `--verbose` | Enable diagnostic logging |
| `--no-framelimit` | Disable frame limiter (unlocked FPS) |
| `--model-viewer [index]` | Launch debug model viewer (structures, NPCs, fish) |
| `--time HOUR` | Override in-game hour (0-23) |

## Settings

Graphics settings are stored in `settings.ini` and can be edited manually or through the in-game options menu:

- Resolution (up to 4K)
- Fullscreen toggle
- VSync
- MSAA (anti-aliasing)
- Texture Loading/Caching (No need to enable if you aren't using a texture pack)

## Texture Packs

Custom textures can be placed in `texture_pack/`. Dolphin-compatible format (XXHash64, DDS).

I highly recommend the following texture pack from the talented artists of Animal Crossing community.

[HD Texture Pack](https://forums.dolphin-emu.org/Thread-animal-crossing-hd-texture-pack-version-23-feb-22nd-2026)

## Save Data

Save files are stored in `save/` using the standard GCI format, compatible with Dolphin emulator saves. Place a Dolphin GCI export in the save directory to import an existing save.

## Credits

This project would not be possible without the work of the [ACreTeam](https://github.com/ACreTeam) decompilation team. Their complete C decompilation of Animal Crossing is the foundation this port is built on.

Also built on: the original PC port by FlyingMeta (flyngmt) and its contributors (including Cuyler36 and oprel; see the original repository's history), [fixNES](https://github.com/FIX94/fixNES) by FIX94, SDL2, and glad. Details and licences: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## AI Notice

AI tools such as Claude were used in this project. In the original port this covered the PC port code only; in this fork, AI assistance was used for a large part of the multiplayer, networking, console, updater, test and documentation work (commits by "Claude" and co-authored commits show it). The human maintainers review and are responsible for what is published. Ownership of AI-assisted output has not been legally assessed.

## Legal and Notices

- [LICENSE](LICENSE): the inherited licence file of the original port (unchanged). It says the upstream decompilation code is under CC0 and the PC-port layer is MIT. **CC0 only waives the rights of the person who applied it; it does not and cannot clear rights that Nintendo or other third parties may hold in the game or in material derived from it.**
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md): per-component provenance, licences, and the list of unresolved questions (inherited SGI/Nintendo-marked SDK headers, reconstructed Nintendo libraries and data tables, contributor licensing, icon and hash-function provenance).
- [docs/provenance/](docs/provenance/): the file-level inventories behind those statements.
- Nothing here is legal advice, and the project is not described as "cleared" or "fully licensed".

## FAQ

See [FAQ](FAQ.md) for more info.
