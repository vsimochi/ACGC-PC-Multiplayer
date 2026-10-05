"""town_test_support.py - shared helpers of the M-A / M-B town cache tests (test_town_cache_unit.py, test_town_transfer_protocol.py, test_town_fetch_real.py).

Builds DISPOSABLE copies of pc\\build64\\bin_fixture4 (only READ; its exe, save and everything else is never modified, never launched) in a NEW directory whose basename
starts with bin_fixture4 (so net_spike_lib's automatic whole-save snapshot / restore applies), copies the freshly built exe into it and sets NET_SPIKE_GAME_BIN.
The live save dir (pc\\build64\\bin\\save), bin_talkfix* and bin_fixture4 itself are never written. `rom` is a junction to the same read-only ISO dir."""
import hashlib
import os
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
BUILD64 = os.path.join(PC, "build64")
SRC_BIN = os.path.join(BUILD64, "bin_fixture4")                    # read-only source of the copies
NEW_EXE = os.path.join(BUILD64, "bin", "AnimalCrossing.exe")       # the freshly built exe (read-only input)
LOG_DIR = os.path.join(tempfile.gettempdir(), "acmp_town_logs")
MARKER = ".town_test_fixture"


def make_fixture(name, empty_save=False):
    """Fresh disposable copy named `name` (must start with bin_fixture4) under pc\\build64. Refuses to replace a directory that lacks our marker. Returns the path."""
    assert name.startswith("bin_fixture4") and name != "bin_fixture4", "the disposable dir name must start with bin_fixture4 and must not be bin_fixture4 itself"
    dst = os.path.join(BUILD64, name)
    assert os.path.isdir(SRC_BIN), SRC_BIN
    if os.path.isdir(dst):
        if not os.path.isfile(os.path.join(dst, MARKER)):
            raise SystemExit("REFUSING: %s exists and is not a town-test fixture" % dst)
        rom = os.path.join(dst, "rom")
        if os.path.isdir(rom):
            os.rmdir(rom)  # remove the junction only, never the target
        shutil.rmtree(dst)
    os.makedirs(dst)
    for n in ("SDL2.dll", "keybindings.ini", "settings.ini", "shader_cache.bin", ".four_resident_fixture"):
        p = os.path.join(SRC_BIN, n)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dst, n))
    shutil.copy2(NEW_EXE if os.path.isfile(NEW_EXE) else os.path.join(SRC_BIN, "AnimalCrossing.exe"), os.path.join(dst, "AnimalCrossing.exe"))
    shutil.copytree(os.path.join(SRC_BIN, "shaders"), os.path.join(dst, "shaders"))
    if empty_save:
        os.makedirs(os.path.join(dst, "save"))
    else:
        shutil.copytree(os.path.join(SRC_BIN, "save"), os.path.join(dst, "save"))
    target = os.path.realpath(os.path.join(SRC_BIN, "rom"))
    subprocess.check_call(["cmd", "/c", "mklink", "/J", os.path.join(dst, "rom").replace("/", "\\"), target.replace("/", "\\")], stdout=subprocess.DEVNULL)
    open(os.path.join(dst, MARKER), "w").write("disposable fixture of the town cache tests\n")
    os.makedirs(LOG_DIR, exist_ok=True)
    return dst


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def tree_md5(path):
    """{relative path: md5} of every file under `path` ({} when it does not exist)."""
    out = {}
    for root, _dirs, files in os.walk(path):
        for f in files:
            full = os.path.join(root, f)
            out[os.path.relpath(full, path).replace("\\", "/")] = md5_file(full)
    return out


def log_path(name):
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, name)


class GciWatcher:
    """Samples the md5 of a (host) GCI file every 0.25 s on a daemon thread and remembers every distinct value. The host re-saves its GCI every 60 s
    (periodic authoritative save; copy_protect is random, so the bytes differ every time): a transferred snapshot must equal SOME version the host had
    on disk (it is an atomic-rename file), not necessarily the first one."""

    def __init__(self, path):
        import threading
        self.path = path
        self.seen = set()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _sample(self):
        try:
            self.seen.add(md5_file(self.path))
        except OSError:
            pass

    def _run(self):
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(0.25)

    def start(self):
        self._sample()
        self._t.start()
        return self

    def stop(self):
        self._sample()
        self._stop.set()
        self._t.join(2.0)
        return self.seen
