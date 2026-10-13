#!/usr/bin/env python3
"""wlA_spawn_wire_runner.py - test_wildlife_spawn_wire.py needs an already-running host (run_wildlife_protocol_tests.py starts one on a fixed port 7831):
the same, on a port of worker A's range, on the fixture wlA_run.py chose. Usage: python wlA_run.py proto wlA_spawn_wire_runner.py [--port 14260]"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402
import wplay_lib as W  # noqa: E402

port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 14260
W.make_fixture()
host = L.HostProcess(port=port, extra_args=["--bootstrap-resident", "0", "-debug"], log_path=os.path.join(HERE, "wlA_logs", "wlA_spawn_wire_host.log"), bin_dir=W.BIN).start()
rc = 1
try:
    if host.wait_listening(60) and host.boot_to_field(timeout=90, slot=0):
        L.resolve_host_town("127.0.0.1", port)
        rc = subprocess.run([sys.executable, "-u", os.path.join(HERE, "test_wildlife_spawn_wire.py"), "127.0.0.1", str(port)], cwd=HERE, timeout=1200).returncode
    else:
        print("FAIL - host did not reach the field")
finally:
    host.stop()
sys.exit(rc)
