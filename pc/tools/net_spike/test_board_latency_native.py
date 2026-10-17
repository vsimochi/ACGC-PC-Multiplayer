#!/usr/bin/env python3
r"""test_board_latency_native.py - "bulletin board is super slow" (worker BB): NATIVE + SOURCE tier, no game process.

N1 (native, real function text): the REAL add_calc + mlib_calc_dt_fraction (src/game/m_lib.c) and the REAL mNT_add_calc_dt (src/game/m_notice_ovl.c) are
   compiled into a harness that animates the notice board exactly as mNT_Play_page_move (move_time 1) / mNT_Play_page_to_write do, at 30..2000 fps.
   A single page turn and the slide to the new-post page must take the SAME wall time at every fps >= 60 (within 10 %, ~180 / ~130 ms). The pre-fix
   wrapper (rate and steps converted to dt a second time) is run side by side for the report: it scaled with fps (~3 s per page at 1000 fps).
N2 (native): the REAL mNT_roll_control2 (+ _step) with the 60 Hz tick accumulator scrolls back in the same wall time at 30..1000 fps.
S1 (source) a notice board write marks the town pages dirty (m_notice.c mNtc_notice_write -> pc_net_game_pages_dirty, declared in pc_net_game.h).
S2 (source) pc_net_game.c: page scan PC_NETGAME_PAGE_SCAN_MS = 250 used by host and client tick (no literal 500 left), dirty hook forces the next tick,
   client upload budget 16 / s < host refusal limit 20 / s, a refused send is not marked in flight, latency log lines on client and host.
Usage: python test_board_latency_native.py"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
GCC = r"C:\msys64\ucrt64\bin\gcc.exe"
results = []


def check(desc, ok):
    results.append(bool(ok))
    print("[%s] %s" % ("PASS" if ok else "FAIL", desc))


def rd(*p):
    return open(os.path.join(REPO, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def fn(src, start_re):
    m = re.search(start_re + r".*?\n\}\n", src, re.S)
    return m.group(0) if m else None


HARNESS = r"""
#include <stdio.h>
#include <math.h>
#include <stdlib.h>
typedef float f32;
typedef struct { double dt_num_60fps_frames; } GRAPH;
typedef struct { GRAPH* graph; } GAME;
static GRAPH g_graph; static GAME g_game = { &g_graph }; static GAME* gamePT = &g_game;
#define powf_ powf
#define DTCONV_GRAPH(v, graph) powf(v, (graph)->dt_num_60fps_frames)
#define TARGET_PC 1
typedef struct { f32 position[2]; f32 speed[2]; } mSM_MenuInfo_c;
int graph_dt_60hz_ticks(GAME* game, float* accum) {
    int max_ticks = 4; int ticks;
    *accum += (float)game->graph->dt_num_60fps_frames; ticks = (int)*accum; *accum -= (float)ticks;
    if (ticks > max_ticks) ticks = max_ticks;
    return ticks;
}
static f32 mlib_calc_dt_fraction(f32 fraction);
#include "extract.inc"
/* the pre-fix wrapper, for the before/after numbers */
static f32 old_add_calc_dt(f32* value, f32 target, f32 rate, f32 max_step, f32 min_step) {
    f32 dt = (f32)gamePT->graph->dt_num_60fps_frames;
    f32 dt_rate = 1.0f - powf(1.0f - rate, dt);
    return add_calc(value, target, dt_rate, max_step * dt, min_step * dt);
}
typedef f32 (*CALC)(f32*, f32, f32, f32, f32);
static double turn_ms(CALC c, double fps) {           /* mNT_Play_page_move, move_time == 1: 320 -> 0 until |rest| < 0.1 */
    f32 pos = 320.0f; long n = 0;
    g_graph.dt_num_60fps_frames = 60.0 / fps;
    for (;;) { f32 r = c(&pos, 0.0f, 0.4f, 74.0f, 2.5f); n++; if (fabsf(r) < 0.1f) break; if (n > 50000000) return -1; }
    return n * 1000.0 / fps;
}
static double to_write_ms(CALC c, double fps) {       /* mNT_Play_page_to_write: page 320 -> 0 AND controls 0 -> -100 */
    f32 pos = 320.0f, ctl = 0.0f; long n = 0; int a = 0, b = 0;
    g_graph.dt_num_60fps_frames = 60.0 / fps;
    while (!(a && b)) { if (!a && fabsf(c(&pos, 0.0f, 0.4f, 74.0f, 2.5f)) < 0.1f) a = 1; if (!b && fabsf(c(&ctl, -100.0f, 0.4f, 74.0f, 2.5f)) < 0.1f) b = 1; n++; if (n > 50000000) return -1; }
    return n * 1000.0 / fps;
}
static double roll_ms(double fps) {                    /* mNT_roll_control2: scroll 48 -> 0 */
    mSM_MenuInfo_c m = { { 0.0f, 48.0f }, { 0.0f, 1.0f } }; long n = 0;
    g_graph.dt_num_60fps_frames = 60.0 / fps;
    while (m.position[1] > 0.0f) { mNT_roll_control2(&m); n++; if (n > 50000000) return -1; }
    return n * 1000.0 / fps;
}
int main(void) {
    static const double fps[] = { 30, 60, 120, 144, 240, 500, 1000, 2000 };
    int i, fail = 0; double t60, w60, r60;
    t60 = turn_ms(mNT_add_calc_dt, 60); w60 = to_write_ms(mNT_add_calc_dt, 60); r60 = roll_ms(60);
    for (i = 0; i < 8; i++) {
        double t = turn_ms(mNT_add_calc_dt, fps[i]), to = turn_ms(old_add_calc_dt, fps[i]);
        double w = to_write_ms(mNT_add_calc_dt, fps[i]), wo = to_write_ms(old_add_calc_dt, fps[i]);
        double r = roll_ms(fps[i]);
        int ok = fps[i] < 60 || (fabs(t - t60) <= 0.1 * t60 + 17 && fabs(w - w60) <= 0.1 * w60 + 17);
        int rok = fps[i] > 1000 || fabs(r - r60) <= 0.15 * r60 + 34;
        printf("ROW fps=%4.0f page_turn_ms=%6.0f (before %6.0f) to_write_ms=%6.0f (before %6.0f) roll_back_ms=%5.0f %s %s\n", fps[i], t, to, w, wo, r,
               ok ? "OK" : "BAD", rok ? "OK" : "BAD");
        if (!ok || !rok) fail = 1;
    }
    return fail;
}
"""


def main():
    lib = rd("src", "game", "m_lib.c")
    ntc_ovl = rd("src", "game", "m_notice_ovl.c")
    parts = [fn(lib, r"static f32 mlib_calc_dt_fraction\(f32 fraction\) \{"), fn(lib, r"extern f32 add_calc\(f32\* pValue"),
             fn(ntc_ovl, r"static f32 mNT_add_calc_dt\("), "static void mNT_roll_control2_step(mSM_MenuInfo_c* menu_info);\n",
             fn(ntc_ovl, r"static void mNT_roll_control2\(mSM_MenuInfo_c\* menu_info\) \{"),
             fn(ntc_ovl, r"static void mNT_roll_control2_step\(mSM_MenuInfo_c\* menu_info\) \{")]
    check("extracted add_calc, mlib_calc_dt_fraction, mNT_add_calc_dt, mNT_roll_control2(+_step) from the real sources", all(parts))
    if all(parts):
        wrap = parts[2]
        check("N1 mNT_add_calc_dt no longer converts the rate / steps to dt a second time (add_calc already does)", "powf" not in wrap and "* dt" not in wrap)
        d = tempfile.mkdtemp(prefix="acmp_bb_")
        open(os.path.join(d, "extract.inc"), "w").write("\n".join(p.replace("extern f32 add_calc", "static f32 add_calc") for p in parts))
        open(os.path.join(d, "h.c"), "w").write(HARNESS)
        exe = os.path.join(d, "h.exe")
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.dirname(GCC)
        cp = subprocess.run([GCC, "-std=gnu11", "-O1", "-I", d, os.path.join(d, "h.c"), "-o", exe, "-lm"], capture_output=True, text=True, env=env)
        check("N1 harness compiled" + ("" if not cp.returncode else ": " + cp.stderr[:1500]), cp.returncode == 0)
        if cp.returncode == 0:
            rp = subprocess.run([exe], capture_output=True, text=True, timeout=300)
            print(rp.stdout)
            rows = [ln for ln in rp.stdout.splitlines() if ln.startswith("ROW")]
            check("N1/N2 page turn + slide to write: same wall time at 60..2000 fps; roll back: same at 30..1000 fps (%d rows)" % len(rows), rp.returncode == 0 and len(rows) == 8)
            m = re.search(r"fps=1000 page_turn_ms=\s*(\d+) \(before\s*(\d+)\)", rp.stdout)
            check("N1 at 1000 fps one page turn takes < 250 ms now (was %s ms)" % (m.group(2) if m else "?"), m and int(m.group(1)) < 250 and int(m.group(2)) > 2000)
    # ---- source ----
    ntc = rd("src", "game", "m_notice.c")
    body = fn(ntc, r"extern void mNtc_notice_write\(mNtc_board_post_c\* new_post\) \{") or ""
    check("S1 mNtc_notice_write marks the town pages dirty (TARGET_PC)", "pc_net_game_pages_dirty();" in body and "#ifdef TARGET_PC" in body)
    hdr = rd("pc", "include", "pc_net_game.h")
    check("S1 pc_net_game_pages_dirty is declared in pc_net_game.h", "void pc_net_game_pages_dirty(void);" in hdr)
    ng = rd("pc", "src", "pc_net_game.c")
    host = fn(ng, r"static void pcnetgame_page_host_tick\(void\) \{") or ""
    cl = fn(ng, r"static void pcnetgame_page_client_tick\(void\) \{") or ""
    dirty = fn(ng, r"void pc_net_game_pages_dirty\(void\) \{") or ""
    hw = fn(ng, r"static void pcnetgame_page_handle_host\(PCNetPeerId peer, const PCNetGamePageMsg\* in\) \{") or ""
    hc = fn(ng, r"static void pcnetgame_page_handle_client\(const PCNetGamePageMsg\* in\) \{") or ""
    def const(name):
        m = re.search(r"#define %s (\d+)u" % name, ng)
        return int(m.group(1)) if m else None
    check("S2 PC_NETGAME_PAGE_SCAN_MS = 250 (was 500)", const("PC_NETGAME_PAGE_SCAN_MS") == 250)
    check("S2 host and client page ticks use the constant (no literal 500 ms scan left)", "PC_NETGAME_PAGE_SCAN_MS" in host and "PC_NETGAME_PAGE_SCAN_MS" in cl and ">= 500u" not in host and "< 500u" not in cl)
    check("S2 the dirty hook makes the NEXT host / client tick scan (both roles)", "s_pg_next_scan_ms = now - PC_NETGAME_PAGE_SCAN_MS" in dirty and "s_pgc_next_ms = now - PC_NETGAME_PAGE_SCAN_MS" in dirty)
    b, lim = const("PC_NETGAME_PAGE_CLIENT_WR_BUDGET"), const("PC_NETGAME_PAGE_HOST_WR_LIMIT")
    check("S2 client upload budget %s / s < host refusal limit %s / s, both enforced (full-board shift = 15 writes fits)" % (b, lim),
          b is not None and lim == 20 and 15 <= b < lim and "s_pgc_wr_n >= PC_NETGAME_PAGE_CLIENT_WR_BUDGET" in cl and "> PC_NETGAME_PAGE_HOST_WR_LIMIT" in hw)
    check("S2 a write the transport refused is not marked in flight (retried next scan, not after 3 s)", re.search(r"if \(!pcnetgame_page_send\(0, k, x, 0, \(uint8_t\)PCNG_PAGE_F_WRITE[^\n]*\n\s*continue;", cl) is not None)
    check("S2 revision / STALE safety untouched: host applies only when the base revision matches, STALE otherwise", "if (in->aux != s_pg_rev[slot])" in hw and "PCNG_PAGE_F_STALE" in hw)
    check("S2 client latency line '[NET][PAGE] board action took N ms' on APPLIED", "board action took %u ms" in hc)
    check("S2 host latency line 'pushed N changed page(s) to peer P, M ms after the change'", "changed page(s) to peer %d, %u ms after the change" in host and "s_pg_chg_ms[slot] = now;" in hw)
    check("S2 board UI latency lines (open / page turn / slide / post / close) in m_notice_ovl.c", ntc_ovl.count("mNT_PC_BEGIN(") >= 6 and ntc_ovl.count("mNT_PC_END()") >= 5)
    n_ok = sum(results)
    print("\n%d/%d checks passed" % (n_ok, len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
