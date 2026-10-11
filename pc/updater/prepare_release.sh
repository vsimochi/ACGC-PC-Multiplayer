#!/bin/sh
# prepare_release.sh <version> <exe-build-bin-dir> <launcher-exe> <outdir>   (MSYS2 UCRT64; see docs/updater.md)
# Assembles the clean payload (program files + user docs + licenses + release notes, NOTHING else) and builds the update zip, its .sha256 and the first-install bundle.
# License files that come from the toolchain are copied from the MSYS2 packages that built the binaries; the others live in pc/updater/licenses and pc/lib (see licenses/SOURCES.txt).
set -e
VER="$1"; BIN="$2"; LAUNCHER="$3"; OUT="$4"
[ -n "$VER" ] && [ -f "$BIN/AnimalCrossing.exe" ] && [ -f "$LAUNCHER" ] && [ -n "$OUT" ] || { echo "usage: prepare_release.sh <version> <bin-dir> <ACLauncher.exe> <outdir>"; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"
PAY="$OUT/payload"; rm -rf "$PAY"; mkdir -p "$PAY/shaders" "$PAY/licenses"
cp "$BIN/AnimalCrossing.exe" "$BIN/SDL2.dll" "$PAY/"
cp "$BIN"/shaders/default.vert "$BIN"/shaders/default.frag "$PAY/shaders/"
cp "$ROOT/LICENSE" "$PAY/LICENSE"
cp "$ROOT/docs/release-notes-$VER.md" "$PAY/RELEASE_NOTES.txt"
cp "$HERE/UPDATING.txt" "$PAY/UPDATING.txt"
M=/c/msys64/ucrt64/share/licenses
cp "$HERE/THIRD_PARTY_NOTICES.txt" "$PAY/licenses/"
cp "$HERE/licenses/Apache-2.0.txt" "$HERE/licenses/Khronos-khrplatform-NOTICE.txt" "$HERE/licenses/GPL-3.0-gcc-COPYING3.txt" "$HERE/licenses/SOURCES.txt" "$PAY/licenses/"
cp "$HERE/licenses/xxHash-LICENSE.txt" "$ROOT/THIRD_PARTY_NOTICES.md" "$PAY/licenses/"
cp "$M/SDL2/LICENSE.txt" "$PAY/licenses/SDL2-LICENSE.txt"
cp "$M/zlib/LICENSE" "$PAY/licenses/zlib-LICENSE.txt"
cp "$M/winpthreads/COPYING" "$PAY/licenses/winpthreads-COPYING.txt"
cp "$M/gcc/COPYING.RUNTIME" "$PAY/licenses/gcc-COPYING.RUNTIME.txt"
cp "$ROOT/pc/lib/fixnes/LICENSE" "$PAY/licenses/fixnes-LICENSE.txt"
python "$HERE/make_release_package.py" build --version "$VER" --build-dir "$PAY" --launcher "$LAUNCHER" --out "$OUT/dist"
