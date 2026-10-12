#!/usr/bin/env python3
r"""test_fly_rod_watchdog_model.py - MODEL tier (no game process): the FLY_ROD watchdog's frame accumulator at 30 / 60 / 144 / 240 FPS.

The constants are read from the source (src/game/m_player_main_fly_rod.c_inc: PC_FLY_ROD_WATCHDOG_FRAMES, the f32 accumulator and its `>` test; src/graph.c: the 4.0 clamp of
dt_num_60fps_frames). The model replays graph.c's per-frame dt (SECONDS_TO_FRAMES(delta), clamped to 4) and the watchdog's f32 sum, frame by frame, with f32 rounding, and reports the wall time
at which the timeout branch would fire. Expected: ~20 s of game time at every frame rate (a stalled 1 s frame only counts 4 frames); the control (the old int truncation) never fires above 60 FPS.
This is a MODEL of the arithmetic, NOT a runtime result.
Usage: python test_fly_rod_watchdog_model.py"""
import os
import random
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import net_spike_lib as L  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def rd(*p):
    return open(os.path.join(REPO, *p), encoding="utf-8", errors="replace").read()


def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def fire_time(frame_seconds, limit, clamp, truncate=False, cap_s=120.0):
    """wall seconds until `acc > limit`, None if not within cap_s. frame_seconds: iterator of real frame durations."""
    acc, t = f32(0.0), 0.0
    for dt_s in frame_seconds:
        t += dt_s
        if t > cap_s:
            return None
        d = dt_s * 60.0
        if d > clamp:
            d = clamp
        acc = f32(acc + (float(int(d)) if truncate else f32(d)))
        if acc > limit:
            return t
    return None


def steady(fps):
    while True:
        yield 1.0 / fps


def jitter(fps, seed):
    r = random.Random(seed)
    while True:
        yield (1.0 / fps) * r.uniform(0.7, 1.3)


def with_stall(fps, at_s, stall_s):
    t, done = 0.0, False
    while True:
        if not done and t >= at_s:
            done = True
            t += stall_s
            yield stall_s
        t += 1.0 / fps
        yield 1.0 / fps


def main():
    results = []
    ck = lambda d, c: L.check(d, bool(c), results)  # noqa: E731
    fly = rd("src", "game", "m_player_main_fly_rod.c_inc")
    gr = rd("src", "graph.c")
    m = re.search(r"#define PC_FLY_ROD_WATCHDOG_FRAMES \((\d+) \* (\d+)\)", fly)
    limit = float(int(m.group(1)) * int(m.group(2))) if m else None
    cm = re.search(r"if \(dt_num_60fps_frames > ([\d.]+)\) \{\s*dt_num_60fps_frames = ([\d.]+);", gr)
    clamp = float(cm.group(2)) if cm else None
    ck("constants read from source (limit %s frames, dt clamp %s)" % (limit, clamp), limit == 1200.0 and clamp == 4.0)
    ck("source accumulates the f32 dt and compares with '>' (what this model replays)",
       "s_fly_rod_wait_frames += (f32)gamePT->graph->dt_num_60fps_frames;" in fly and "s_fly_rod_wait_frames > (f32)PC_FLY_ROD_WATCHDOG_FRAMES" in fly)
    if limit is None or clamp is None:
        return L.summary_and_exit_code(results)
    for fps in (30, 60, 144, 240):
        t = fire_time(steady(fps), limit, clamp)
        ck("MODEL %d FPS steady: fires after %.3f s (expect 20 s +- 1 frame)" % (fps, t or -1), t is not None and abs(t - 20.0) <= 1.0 / fps + 1e-6)
        tj = fire_time(jitter(fps, fps), limit, clamp)
        ck("MODEL %d FPS +-30%% frame jitter: fires after %.3f s (expect 20 s +- 0.1)" % (fps, tj or -1), tj is not None and abs(tj - 20.0) <= 0.1)
        tc = fire_time(steady(fps), limit, clamp, truncate=True)
        if fps > 60:
            ck("MODEL control %d FPS with the OLD int truncation: never fires within 120 s (%s)" % (fps, tc), tc is None)
        else:
            ck("MODEL control %d FPS with int truncation still fires (%s s)" % (fps, tc), tc is not None)
    ts = fire_time(with_stall(144, 5.0, 1.0), limit, clamp)
    ck("MODEL 144 FPS with one 1 s stall: the stall counts only %d frames, fires after %.3f s of wall time (= 20 s game + 1 s - 4/60 s)" % (clamp, ts or -1),
       ts is not None and abs(ts - (21.0 - clamp / 60.0)) <= 0.02)
    return L.summary_and_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
