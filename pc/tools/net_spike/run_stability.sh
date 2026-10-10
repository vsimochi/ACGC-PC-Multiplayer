#!/bin/bash
# Repeated real-game acceptance runs against ONE frozen executable (Samsung display, volume 1). One line per step in stab_summary.txt, full output in stab_<name>.txt.
# Every step is bounded by `timeout` (a stalled test is stopped, and only the game processes of the test fixture directory are killed -- never another game); the fixture is refreshed ONCE.
# Usage: bash run_stability.sh [proxy_reps] [sim_reps]     (defaults 5 and 3)
export PATH=/c/msys64/ucrt64/bin:$PATH AC_DISPLAY_NAME=samsung AC_MASTER_VOLUME=1
cd "$(dirname "$0")"
PROXY_REPS=${1:-5}
SIM_REPS=${2:-3}
export NET_SPIKE_GAME_BIN="E:\\CodeStuff\\AnimalCrossingMultiplayer\\ACGC-PC-Multiplayer\\pc\\build64\\bin_fixture4_wplay"
: > stab_summary.txt
kill_fixture_games() {
  powershell -NoProfile -Command "Get-Process AnimalCrossing -ErrorAction SilentlyContinue | Where-Object { \$_.Path -like '*bin_fixture4_wplay*' } | Stop-Process -Force" >/dev/null 2>&1
}
step() { # name, timeout-seconds, command...
  name=$1; tmo=$2; shift 2
  kill_fixture_games
  echo "$(date +%T) START $name" >> stab_summary.txt
  timeout "$tmo" "$@" > "stab_$name.txt" 2>&1
  rc=$?
  kill_fixture_games
  echo "$(date +%T) DONE  $name :: rc=$rc$( [ $rc = 124 ] && echo ' (TIMEOUT, stopped)' ) :: $(grep -E 'checks passed' stab_$name.txt | tail -1) :: FAIL lines: $(grep -cE '^FAIL' stab_$name.txt) :: $(grep -m1 -E 'PlayerStuck|Traceback' stab_$name.txt)" >> stab_summary.txt
}
echo "exe sha256: $(sha256sum ../../build64/bin/AnimalCrossing.exe | cut -c1-16)  fixture exe: $(sha256sum ../../build64/bin_fixture4_wplay/AnimalCrossing.exe 2>/dev/null | cut -c1-16)" >> stab_summary.txt
python -c "import test_wildlife_proxy_capacity_real" >/dev/null 2>&1   # refresh the fixture once with the frozen exe
echo "fixture exe after refresh: $(sha256sum ../../build64/bin_fixture4_wplay/AnimalCrossing.exe | cut -c1-16)" >> stab_summary.txt
step lifecycle 1800 python -u test_wildlife_lifecycle_real.py
step indoor_spawn 900 python -u test_wildlife_indoor_spawn_real.py
step rod_type 600 python -u test_wildlife_rod_type_real.py
step golden_rod 1200 python -u test_wildlife_golden_rod_real.py
step host_indoors 1200 python -u test_wildlife_host_indoors_real.py
step snapshot_race 900 python -u test_wildlife_snapshot_race_real.py
step bobber_order 600 python -u test_wildlife_bobber_order_real.py
step protocol 1800 python -u run_wildlife_protocol_tests.py
for n in $(seq 1 "$PROXY_REPS"); do step "proxy_life_$n" 1500 python -u test_wildlife_proxy_lifecycle_real.py --parts rep,conc,dc; done
for n in $(seq 1 "$SIM_REPS"); do step "sim_full_$n" 2700 python -u test_wildlife_sim_real.py; done
echo "$(date +%T) ALL DONE" >> stab_summary.txt
