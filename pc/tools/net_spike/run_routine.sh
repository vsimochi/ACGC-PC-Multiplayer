#!/bin/bash
# ROUTINE validation, hard budget 18 minutes (leaves 2 of the 20 for the summary), Samsung display, volume 1. One line per step in routine_summary.txt, full output in routine_<step>.txt.
# Steps run in RISK order. A step is only STARTED when its estimated duration still fits in the remaining budget (otherwise "NOT RUN (budget)"), and its `timeout` is capped by the remaining
# budget, so the total can never exceed the budget; a timed-out step keeps its log. Nothing is retried, no assertion is skipped: the tests are the same ones as in the extended tier.
# Estimates are the measured durations of the last extended run (see TODO.md "Validation tiers"). Usage: bash run_routine.sh [budget_seconds=1080]
export PATH=/c/msys64/ucrt64/bin:$PATH AC_DISPLAY_NAME=samsung AC_MASTER_VOLUME=1
cd "$(dirname "$0")"
BUDGET=${1:-1080}
T0=$(date +%s)
export NET_SPIKE_GAME_BIN="E:\\CodeStuff\\AnimalCrossingMultiplayer\\ACGC-PC-Multiplayer\\pc\\build64\\bin_fixture4_wplay"
: > routine_summary.txt
elapsed() { echo $(( $(date +%s) - T0 )); }
log() { echo "$(date +%T) [+$(elapsed)s] $*" >> routine_summary.txt; }
kill_fixture_games() {
  powershell -NoProfile -Command "Get-Process AnimalCrossing -ErrorAction SilentlyContinue | Where-Object { \$_.Path -like '*bin_fixture4_wplay*' } | Stop-Process -Force" >/dev/null 2>&1
}
step() { # name, estimated seconds, command...
  name=$1; est=$2; shift 2
  left=$(( BUDGET - $(elapsed) ))
  if [ "$left" -lt "$est" ]; then log "NOT RUN (budget: $left s left, ~$est s needed) $name"; return; fi
  kill_fixture_games
  log "START $name (est $est s, timeout $left s)"
  timeout "$left" "$@" > "routine_$name.txt" 2>&1
  rc=$?
  kill_fixture_games
  log "DONE  $name :: rc=$rc$( [ $rc = 124 ] && echo ' (TIMEOUT: stopped, log kept)' ) :: $(grep -E 'checks passed' routine_$name.txt | tail -1) :: FAIL lines: $(grep -cE '^FAIL' routine_$name.txt) :: $(grep -m1 -E 'PlayerStuck|Traceback' routine_$name.txt)"
}
log "budget $BUDGET s"
log "build (incremental)"
( cd ../.. && cmake --build build64 -j8 > routine_build.txt 2>&1 ); log "build rc=$? $(grep -cE '\berror\b' ../../routine_build.txt) error line(s); exe $(sha256sum ../../build64/bin/AnimalCrossing.exe | cut -c1-16)"
python -c "import test_wildlife_proxy_capacity_real" >/dev/null 2>&1   # refresh the fixture once with this exe
log "fixture exe $(sha256sum ../../build64/bin_fixture4_wplay/AnimalCrossing.exe | cut -c1-16)"
step golden_rod        170 python -u test_wildlife_golden_rod_real.py
step host_indoors      270 python -u test_wildlife_host_indoors_real.py
step protocol          270 python -u run_wildlife_protocol_tests.py
step sim_acceptance    360 python -u test_wildlife_sim_real.py --only s1,b3,r1
step rod_type           40 python -u test_wildlife_rod_type_real.py
step bobber_order       40 python -u test_wildlife_bobber_order_real.py
step indoor_spawn      130 python -u test_wildlife_indoor_spawn_real.py
step lifecycle_l7      130 python -u test_wildlife_lifecycle_real.py --only l7
step snapshot_race     130 python -u test_wildlife_snapshot_race_real.py
log "ALL DONE, elapsed $(elapsed) s"
