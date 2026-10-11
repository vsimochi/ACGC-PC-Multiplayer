# Launcher and auto-updater (`ACLauncher.exe`)

Status: implemented and tested **offline** (local HTTP fixtures). **No official GitHub Release exists yet** (`/releases/latest` of `vsimochi/ACGC-PC-Multiplayer` answers 404 when checked), so nothing can be updated from GitHub until a maintainer publishes a package as described below.

## For players

**Start the game with `ACLauncher.exe`** (not `AnimalCrossing.exe`). It checks GitHub for the latest *stable* release, then starts the game. Command-line options you give the launcher (e.g. `--host 7788`, `--connect ...`) are passed to the game unchanged.

- No newer version: the game starts.
- Newer version: a dialog shows the installed and the latest version. **Yes** = download, verify and install it, then start the new version. **No** = start the version you have, nothing is changed (you are asked again the next time you start the launcher).
- The check fails (offline, GitHub error or rate limit, a release without a valid package): a message says why and the installed game starts. A failed check is never treated as "up to date".
- An update fails at any point (download interrupted, checksum mismatch, bad package, disk full, a file locked): everything is rolled back and your installed version starts. The launcher refuses to update while `AnimalCrossing.exe` of this folder is running.
- Install folder: any normal writable folder (no administrator rights needed). Do not install into a Git checkout or build folder: there the launcher never updates anything.

**First install:** unzip `ACGC-PC-Multiplayer-<version>-win64-first-install.zip`, put your own disc image (`GAFE01_00`, USA) into `rom\`, start `ACLauncher.exe`. The game assets are never part of a release and never downloaded. The first-install bundle also contains `INSTALL.txt`, `UPDATING.txt` (a short user version of this page) and `docs\updater.md` / `docs\console-commands.md`; the update package carries `UPDATING.txt` but not `docs\` (the launcher's managed-file list has no `docs\` entry, so adding it needs a launcher change), so those two documents are only refreshed by a new first-install bundle or on GitHub.

**Files the updater replaces (the explicit managed list):** `AnimalCrossing.exe`, `SDL2.dll`, `shaders\*`, `licenses\*`, `version.txt`, `LICENSE`, `RELEASE_NOTES.txt`, `UPDATING.txt`. A package containing anything else is rejected as a whole.
**Never touched:** `rom\` (your disc image), `save\`, `servers\`, `settings.ini`, `keybindings.ini`, `texture_pack\`, `shader_cache.bin`, game logs, `updater.ini`, `ACLauncher.exe` itself and every other file in the folder. (`ACLauncher.exe` is not updated by packages; a new launcher comes with a new first-install bundle.)

**Recovery:** the previous program files are kept in `.update\backup-<oldversion>\` (the newest one only). To go back manually: close the game, copy the files from that folder over the folder above it. `updater.log` in the install folder records every launch (version check, decision, result, errors); it is truncated above 256 KB.

**Settings** (`updater.ini` next to the launcher, see `pc/updater/updater.ini.sample`): `check_updates=0` disables the check, `allow_prerelease=1` also offers prereleases, `show_check_errors=0` hides check-failure messages, `repository=owner/name` selects the release source. Environment `AC_UPDATER_DISABLE=1` or the switch `--updater-skip` skip the check once.

## How it decides and what it trusts

1. `version.txt` (one line, `MAJOR.MINOR.PATCH[-prerelease]`) identifies the installed version. No/invalid `version.txt` (a developer's `bin` folder) = not a packaged installation: no check, no update. A folder inside a Git working tree or CMake build tree is also never updated.
2. `GET https://api.github.com/repos/<owner>/<repo>/releases/latest` (stable only; drafts and prereleases are not "latest"). With `allow_prerelease=1`: `.../releases?per_page=15`, highest version wins. Versions are compared as semantic versions (numeric fields, prerelease < release).
3. The release must contain exactly `ACGC-PC-Multiplayer-<version>-win64.zip` and `ACGC-PC-Multiplayer-<version>-win64.zip.sha256`, whose `browser_download_url` must be exactly `https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>` for the configured repository. Anything else (other repository, other name, malformed JSON, tag that is not a version) is refused. Redirects must end on a GitHub host.
4. The package is downloaded to `.update\`, size-limited (asset size, max 512 MB), compared to the SHA-256 in the `.sha256` file (and to GitHub's own `digest` field when present) **before it is opened as an archive**. Nothing downloaded is executed before the installation is complete; nothing from release notes is ever used.
5. The zip is validated: only allow-listed names, no `..`/absolute/drive/backslash paths, no symlinks/reparse points, no encryption/zip64, no duplicates (case-insensitive), size/CRC of every entry checked while extracting into `.update\stage`, `version.txt` must match the release.
6. Files are swapped in one by one (old ones moved to the backup), verified by size, and rolled back on any failure.

**What the checksum means:** the SHA-256 detects corruption and a package that does not match what the release lists. It is **not a signature**: someone who can edit the GitHub release can replace both files. The security rests on HTTPS to GitHub, the fixed repository and URL shape, and on who can publish releases to it (protect that account). Code signing of the executables is not implemented.

## For maintainers: publishing a release

1. Build a RELEASE configuration of the game (test hooks OFF): `cmake -S pc -B <dir> -G "MinGW Makefiles" -DPC_TEST_HOOKS=OFF -DPC_ALLOW_64BIT=ON` then `cmake --build <dir> -j`, and the launcher: `sh pc/updater/build_launcher.sh <outdir>` (MSYS2 UCRT64, gcc + zlib). Never ship a development build (it contains test hooks).
2. Package: `sh pc/updater/prepare_release.sh 0.1.0 <dir>/bin <outdir>/ACLauncher.exe <staging>` (needs `docs/release-notes-0.1.0.md`). It assembles a clean payload (AnimalCrossing.exe, SDL2.dll, shaders, LICENSE, RELEASE_NOTES.txt, licenses/) and calls `make_release_package.py build`, which writes
   - `ACGC-PC-Multiplayer-1.1.0-win64.zip` and `ACGC-PC-Multiplayer-1.1.0-win64.zip.sha256` (`<hex>  <file name>`): **the updater assets**,
   - `ACGC-PC-Multiplayer-1.1.0-win64-first-install.zip`: the bundle for new users (launcher + program files + `updater.ini` + `INSTALL.txt` + `rom\` note; not used by the updater).
   It only packs the managed files; ROMs, saves, configs, logs and source are never included.
3. Verify locally: `python pc/updater/make_release_package.py verify dist/ACGC-PC-Multiplayer-1.1.0-win64.zip` (re-hashes and applies the launcher's safety rules). Optionally run `python pc/updater/test_updater.py`.
4. Publish: tag `v1.1.0` (the tag must equal `v` + the version), create a GitHub Release from it (not a draft; tick "pre-release" exactly when the version has a `-suffix`), attach the `.zip` and the `.zip.sha256` (and the first-install zip). Example: `gh release create v1.1.0 dist/ACGC-PC-Multiplayer-1.1.0-win64.zip dist/ACGC-PC-Multiplayer-1.1.0-win64.zip.sha256 dist/ACGC-PC-Multiplayer-1.1.0-win64-first-install.zip --title "1.1.0" --notes-file notes.md`.
5. **Channel rule (Beta 0.1.0):** the updater's stable channel is GitHub's non-pre-release `/releases/latest`, and the launcher additionally requires that the tag's suffix and GitHub's pre-release flag agree. So "Beta 0.1.0" is published as a normal (NOT pre-release) release with tag `v0.1.0`; "Beta" lives in the title and notes. Ticking "pre-release" on `v0.1.0` would hide it from `/releases/latest` (players see "no official release") AND be rejected by the tag/flag check even with `allow_prerelease=1`; a real pre-release needs a suffixed version/tag such as `0.2.0-beta.1` / `v0.2.0-beta.1` (covered by `test_updater.py` checks 14b-14f).
6. Prereleases are never offered unless the player sets `allow_prerelease=1`. A release without a valid package/checksum pair is reported as a failed check and nobody is updated.

Never attach disc images, saves or Nintendo assets to a release.

## Tests

`python pc/updater/test_updater.py` runs the real `ACLauncher.exe` against a local HTTP server and fake game executables (no game, display or audio): up to date, No, Yes (full install, backup, launch of the new version), interrupted download, checksum mismatch, malformed metadata/JSON/tags/other repository/odd asset names, unsafe archives (`..`, absolute, drive, backslash, symlink, duplicates, writes to `settings.ini`/`rom\`/`save\`, oversized entry), rollback with a locked file, running game, offline/403/rate limit/500/404, Git working tree, unversioned install, settings, prerelease handling, missing game executable. `python pc/updater/test_release_package.py --dist <staging>/dist --version 0.1.0` checks the REAL release artifacts (checksum, tamper detection, file list, installation through the launcher, and a first-run of the first-install bundle that reaches the game's "Missing ROM" box). Not covered: the progress window / dialogs (test mode runs without UI; not manually verified), real GitHub TLS/redirects (no release exists to download), disk-full, antivirus interference.

## Headless modes (used by the dedicated-server starter)

`ACLauncher.exe --updater-check-only --updater-result <file>` and `ACLauncher.exe --updater-install-now --updater-result <file>` run the same check and the same installer as the normal flow, but show no window and never start the game. They exist so that `Start Server.bat` (`pc\server\server_starter.ps1`) can ask its "Update now? [Y/N]" question on its own console and still use this one verified implementation.
- `--updater-check-only`: reads `version.txt`, asks GitHub for the latest STABLE release (same rules as always, prereleases refused), changes nothing. Result `STATUS=` `UPTODATE` / `AVAILABLE` / `CHECKFAILED` / `UNSUPPORTED` (no `version.txt`, development tree) / `DISABLED` (`AC_UPDATER_DISABLE=1`), plus `INSTALLED=`, `LATEST=`, `MESSAGE=` lines in the result file. Exit code 0 ok, 3 check failed, 5 not applicable.
- `--updater-install-now`: the CALLER has the user's consent. It re-checks, then runs the normal installer (download of `ACGC-PC-Multiplayer-<version>-win64.zip` and its `.sha256`, SHA-256 and package validation, staging, backup of the old program files, swap, rollback on any problem; it refuses while the game of this folder is running). Result `UPDATED` (exit 0) or `UPDATEFAILED` (exit 4, the installed version is kept).
- `updater.ini` still supplies `repository` and `allow_prerelease`; its `check_updates` does not apply to these modes (the caller decided to ask).
- A launcher that has these modes contains the text `ACLAUNCHER-CAPS:headless-update-v1`; the starter looks for it and never sends these switches to an older launcher (which would ignore them and run its normal flow).
- **Delivery / migration.** An update package may contain only the managed files (`AnimalCrossing.exe`, `SDL2.dll`, `version.txt`, `LICENSE`, `RELEASE_NOTES.txt`, `UPDATING.txt`, `shaders/*`, `licenses/*`); a package with anything else (`ACLauncher.exe`, `Start Server.bat`, `server_config.ini`, ...) is refused as a whole by every launcher, old or new. So the new launcher and starter reach users only through the first-install package: an installation made with 0.1.0 needs the one-time step described in `UPDATING.txt` (copy `ACLauncher.exe`, `Start Server.bat`, `server_starter.ps1`, `SERVER_SETUP.txt` from the new first-install zip; never `server_config.ini`). The 0.1.0 launcher keeps working with the new program files in the meantime. There is deliberately no hidden self-update path (a second place that replaces executables).
- Tests: `pc\updater\test_updater_headless.py` (the launcher modes against the local scripted server) and `pc\server\test_server_starter_update.py` (the starter flow).
