#!/bin/bash
# sequential real-game wildlife regression (Samsung display, volume 1); one summary line per step in wild_regress_summary.txt
export PATH=/c/msys64/ucrt64/bin:$PATH AC_DISPLAY_NAME=samsung AC_MASTER_VOLUME=1
cd "$(dirname "$0")"
: > wild_regress_summary.txt
step() { # name, command...
  name=$1; shift
  echo "$(date +%T) START $name" >> wild_regress_summary.txt
  taskkill //F //IM AnimalCrossing.exe >/dev/null 2>&1
  "$@" > "regress_$name.txt" 2>&1
  echo "$(date +%T) DONE  $name :: $(grep -E 'checks passed' regress_$name.txt | tail -1) :: FAILS: $(grep -cE '^FAIL' regress_$name.txt)" >> wild_regress_summary.txt
}
export NET_SPIKE_GAME_BIN="E:\CodeStuff\AnimalCrossingMultiplayer\ACGC-PC-Multiplayer\pc\build64\bin_fixture4_wplay"
python -c "import test_wildlife_proxy_capacity_real" >/dev/null 2>&1   # refresh the fixture with the freshly built exe
step sim_subset python -u test_wildlife_sim_real.py --only s1,f1,f2,f3,r2
step proxy_life python -u test_wildlife_proxy_lifecycle_real.py --parts rep,conc,dc
step lifecycle python -u test_wildlife_lifecycle_real.py
step bobber_order python -u test_wildlife_bobber_order_real.py
step snapshot_race python -u test_wildlife_snapshot_race_real.py
step sim_full python -u test_wildlife_sim_real.py
echo "$(date +%T) ALL DONE" >> wild_regress_summary.txt
