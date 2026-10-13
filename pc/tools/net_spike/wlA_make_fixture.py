"""wlA_make_fixture.py - makes the disposable fixture that wlA_run.py named (W.make_fixture with the pc\build64_w3 exe), for tests that expect it to exist."""
import wplay_lib as W
W.make_fixture()
print("fixture ready:", W.BIN)
