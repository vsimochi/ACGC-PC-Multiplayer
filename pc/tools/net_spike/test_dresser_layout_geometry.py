#!/usr/bin/env python3
"""test_dresser_layout_geometry.py - PURE geometry self-test of the U3 dresser grid (no build, no game process).

Units: the overlay MODEL space shared by the gyroid window and the item icons. 6s: an icon whose position equals a cell's model centre sits in that cell on
screen, exactly like vanilla (mTG_haniwa_col_pos {-35,-11,13,37} / line 46 ARE the gyroid strip's cell centres; the icon draw's x * 0.975 is the mapping
the window gets too). The 6c..6r columns were x / 0.975 and pushed the icons outward (-2.7 .. +1.8 units).
The gyroid window rectangles below are the quads of hni_win_modelT measured at runtime (the [NET][DRESSER][LAYOUT] vertex dump of a --ui-gyroid run),
classified by the display list that draws them (src/data/model/hni_win.c):
  RING  = hni_nwin2_winT_model's ORANGE border band (textures ue1/ue2/ue3/ue5/ue6/ue7, prim colour 245,125,55; 24 px thick) + the w1..w12 shadow copies
  FILL  = winT's inner fill (ue4) + w13 background
  STRIP = hni_nwin2_win2T_model (the 4 vanilla cells; not drawn in dresser mode -> a HOLE in the fill)
Checks:
  C1 the CURRENT (23:12 build) grid overlaps the orange ring          (documents the reported defect: expected to be TRUE)
  P1..P7 the PROPOSED layout: 9-slice enlarged window (border pieces moved out, fill stretched), 9 x 3 cells of 22 px inside the inner rectangle,
         no cell touches the ring, uniform spacing, the window stays above the pocket grid and inside 320 x 240, the strip hole is covered.
"""
import sys

# (x0, x1, y0, y1) model units
RING = [
    (42, 90, 86, 110), (-126, -78, 14, 38),                  # ue7 corners (top-right, bottom-left)
    (-6, 42, 86, 110), (-78, -47, 34, 38), (-78, -30, 14, 34),  # ue6 top edge, bottom edge pieces
    (-126, -102, 38, 86), (66, 90, 38, 86),                 # ue5 left / right edge
    (-30, -6, 14, 34), (-30, -6, 86, 110),                  # ue3 bottom / top edge
    (-6, 42, 14, 34), (-78, -30, 86, 110),                  # ue2 bottom / top edge
    (49, 90, 34, 38), (-126, -78, 86, 110), (42, 90, 14, 34),  # ue1 bottom piece, top-left corner, bottom-right corner
]
FILL = [(-47, 49, 58, 86), (49, 66, 38, 86), (-102, -47, 38, 86), (-102, 66, 38, 86)]  # ue4 (3 quads) + w13 background
STRIP = (-47, 49, 34, 58)
POCKET_TOP = -10 + 12   # mTG_item_line_pos[0] + half an icon: the pockets' top edge
SCREEN = (-160, 160, -120, 120)
ICON_X_SCALE = 0.975


def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]


def inside(a, b):
    return a[0] >= b[0] and a[1] <= b[1] and a[2] >= b[2] and a[3] <= b[3]


def cells(cx, cy, pitch):
    h = pitch / 2.0
    return [(x - h, x + h, y - h, y + h) for y in cy for x in cx]


# ---- current (23:12 build): pitch 22, centres x = -18 + 22 (c - 4), y = 84 / 62 / 40 ----
CUR_X = [-18 + 22 * (c - 4) for c in range(9)]
CUR_Y = [84, 62, 40]

# ---- proposed: 9-slice enlargement of the gyroid window (pieces keep their pixel size, only the fill stretches) ----
IN_X0, IN_X1, IN_Y0, IN_Y1 = -102, 66, 38, 86          # the measured inner (fill) rectangle
NEW_IN = (-120, 84, 26, 94)                            # 204 x 68: 9 x 22 + 6, 3 x 22 + 2


def rx(x):
    if x <= IN_X0:
        return x + (NEW_IN[0] - IN_X0)
    if x >= IN_X1:
        return x + (NEW_IN[1] - IN_X1)
    return NEW_IN[0] + (x - IN_X0) * (NEW_IN[1] - NEW_IN[0]) / (IN_X1 - IN_X0)


def ry(y):
    if y <= IN_Y0:
        return y + (NEW_IN[2] - IN_Y0)
    if y >= IN_Y1:
        return y + (NEW_IN[3] - IN_Y1)
    return NEW_IN[2] + (y - IN_Y0) * (NEW_IN[3] - NEW_IN[2]) / (IN_Y1 - IN_Y0)


def rq(q):
    return (rx(q[0]), rx(q[1]), ry(q[2]), ry(q[3]))


NEW_X = [-18 + 22 * (c - 4) for c in range(9)]         # -106 .. 70
NEW_Y = [82, 60, 38]                                   # centred in 26..94 (= m_tag_ovl.c mTG_pc_dresser_line_pos)


def main():
    res = []

    def check(desc, ok):
        res.append(bool(ok))
        print(("PASS - " if ok else "FAIL - ") + desc)

    cur = cells(CUR_X, CUR_Y, 22)
    hits = sum(1 for c in cur for r in RING if overlap(c, r))
    check("C1 (defect reproduced) the CURRENT grid overlaps the orange ring: %d cell/ring overlaps; inner rect is x -102..66, y 38..86 (168 x 48), "
          "the grid needs 198 x 66" % hits, hits > 0)
    ring = [rq(r) for r in RING]
    fill = [rq(f) for f in FILL]
    win = (min(q[0] for q in ring), max(q[1] for q in ring), min(q[2] for q in ring), max(q[3] for q in ring))
    new = cells(NEW_X, NEW_Y, 22)
    check("P1 every proposed cell lies inside the stretched inner rectangle %s" % (NEW_IN,), all(inside(c, NEW_IN) for c in new))
    check("P2 no proposed cell touches the orange ring (border pieces keep their 24 px)", not any(overlap(c, r) for c in new for r in ring))
    check("P3 uniform spacing: 9 columns / 3 rows at exactly 22 px, centred in the inner rectangle",
          all(b - a == 22 for a, b in zip(NEW_X, NEW_X[1:])) and all(a - b == 22 for a, b in zip(NEW_Y, NEW_Y[1:])) and
          abs((NEW_X[0] + NEW_X[-1]) / 2 - (NEW_IN[0] + NEW_IN[1]) / 2) <= 1 and abs((NEW_Y[0] + NEW_Y[-1]) / 2 - (NEW_IN[2] + NEW_IN[3]) / 2) <= 1)
    check("P4 the enlarged window %s stays above the pocket grid (bottom >= %d) and inside 320 x 240" % (win, POCKET_TOP),
          win[2] >= POCKET_TOP and inside(win, SCREEN))
    # P5 / P9 (6s): read the REAL tables: the icon / hand positions (m_tag_ovl.c) and the drawn cell quads (m_haniwa_ovl.c mHW_pc_dresser_frame)
    import os
    import re
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
    tag_src = open(os.path.join(root, "src", "game", "m_tag_ovl.c"), encoding="utf-8").read()
    hw_src = open(os.path.join(root, "src", "game", "m_haniwa_ovl.c"), encoding="utf-8").read()
    col_pos = [int(v) for v in re.search(r"mTG_pc_dresser_col_pos\[9\] = \{([^}]*)\}", tag_src).group(1).split(",")]
    line_pos = [int(v) for v in re.search(r"mTG_pc_dresser_line_pos\[3\] = \{([^}]*)\}", tag_src).group(1).split(",")]
    van_col = [int(v) for v in re.search(r"mTG_haniwa_col_pos\[\] = \{([^}]*)\}", tag_src).group(1).split(",")]
    van_line = [int(v) for v in re.search(r"mTG_haniwa_line_pos\[\] = \{([^}]*)\}", tag_src).group(1).split(",")]
    row_y = [float(v.strip().rstrip("f")) for v in re.search(r"static const f32 row_y\[PC_DRESSER_UI_ROWS\] = \{([^}]*)\}", hw_src).group(1).split(",")]
    m = re.search(r"v\[i\]\.v\.ob\[0\] = \(s16\)\(right \? (-?\d+) \+ (\d+) : (-?\d+) - (\d+)\)", hw_src)
    strip_x0, strip_x1 = int(m.group(3)) - int(m.group(4)), int(m.group(1)) + int(m.group(2))
    pitch = (strip_x1 - strip_x0) / 9.0
    cell_cx = [strip_x0 + pitch * (c + 0.5) for c in range(9)]
    check("P5 the vanilla convention the dresser follows: the gyroid's own icon columns %s are its strip's cell centres (-47..49, pitch 24) and line %s its "
          "centre (34..58)" % (van_col, van_line), van_col == [-47 + 24 * c + 12 for c in range(4)] and van_line == [46])
    off = [(c, r, col_pos[c] - cell_cx[c], line_pos[r] - row_y[r]) for r in range(3) for c in range(9)]
    check("P9 EVERY one of the 27 icons (and the hand) is centred in its drawn cell: icon (col_pos, line_pos) == cell centre (strip %d..%d / 9, rows %s) "
          "-- max offset x %.2f, y %.2f" % (strip_x0, strip_x1, row_y, max(abs(o[2]) for o in off), max(abs(o[3]) for o in off)),
          pitch == 22 and all(abs(o[2]) < 0.01 and abs(o[3]) < 0.01 for o in off) and cell_cx == [float(x) for x in NEW_X] and row_y == [float(y) for y in NEW_Y])
    hole = rq(STRIP)
    covered = [c for c in new if overlap(c, hole)]
    rest = (hole[0], hole[1], hole[2], min(c[2] for c in new))
    check("P6 the strip HOLE %s (win2T not drawn) needs one extra fill quad below the cells: %s" % (tuple(round(v, 1) for v in hole), tuple(round(v, 1) for v in rest)),
          rest[3] - rest[2] > 0)
    check("P7 the fill still covers the inner rectangle edges (stretched ue4 / w13 quads reach it)",
          min(q[0] for q in fill) <= NEW_IN[0] and max(q[1] for q in fill) >= NEW_IN[1] and max(q[3] for q in fill) >= NEW_IN[3])
    # P8: the C implementation (pc/src/pc_dresser_layout.c) through its native selftest, on the REAL vertex table when the disc image is available:
    # byte-exact restore, double begin = no-op, end without begin = no-op, 50 repeated entries / exits, refusal of a foreign table, the fill's texel density,
    # the window inside the screen and above the pockets, no cell / ring overlap with the REMAPPED ring. Compiled into a scratch dir OUTSIDE pc/build64.
    import os
    import shutil
    import subprocess
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.abspath(os.path.join(here, "..", "..", ".."))
    gcc = shutil.which("gcc") or (r"C:\msys64\ucrt64\bin\gcc.exe" if os.path.isfile(r"C:\msys64\ucrt64\bin\gcc.exe") else None)
    if gcc is None:
        print("SKIP - P8 native layout selftest (no gcc)")
    else:
        scratch = tempfile.mkdtemp(prefix="dresser_layout_")
        exe = os.path.join(scratch, "dresser_layout_selftest.exe")
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.dirname(gcc)
        cp = subprocess.run([gcc, "-std=gnu11", "-Wall", "-Wextra", "-I" + os.path.join(repo, "pc", "include"), os.path.join(here, "dresser_layout_selftest.c"),
                             os.path.join(repo, "pc", "src", "pc_dresser_layout.c"), "-o", exe], capture_output=True, text=True, env=env)
        args = [exe]
        iso = os.path.join(repo, "pc", "build64", "bin", "rom", "Animal Crossing.iso")
        tab = os.path.join(scratch, "hni_win_v.bin")
        if cp.returncode == 0 and os.path.isfile(iso):
            if subprocess.run([sys.executable, os.path.join(here, "extract_hni_win_v.py"), iso, tab], capture_output=True, text=True).returncode == 0:
                args.append(tab)  # read-only extraction of the REAL table (never written into the repository)
        rr = subprocess.run(args, capture_output=True, text=True, env=env) if cp.returncode == 0 else None
        out = (rr.stdout if rr else cp.stderr).strip().splitlines()
        check("P8 native selftest of the C remap (%s): %s" % ("real table" if len(args) > 1 else "synthetic table", out[-1] if out else "no output"),
              rr is not None and rr.returncode == 0 and "PASS" in (out[-1] if out else ""))
        shutil.rmtree(scratch, ignore_errors=True)
    print("%d/%d checks passed" % (sum(res), len(res)))
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
