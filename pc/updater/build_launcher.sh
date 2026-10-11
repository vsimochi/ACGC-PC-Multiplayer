#!/bin/sh
# Builds ACLauncher.exe (64-bit, static, no console window). Run from MSYS2 UCRT64 (gcc + zlib): sh build_launcher.sh [outdir]
set -e
OUT="${1:-$(dirname "$0")/out}"
mkdir -p "$OUT"
gcc -std=gnu11 -O2 -Wall -Wextra -Wno-unused-parameter -mwindows -municode -static -o "$OUT/ACLauncher.exe" "$(dirname "$0")/ac_launcher.c" -lwinhttp -lbcrypt -lcomctl32 -lshell32 -luser32 -lz
echo "built $OUT/ACLauncher.exe"
