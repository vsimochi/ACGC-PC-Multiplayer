#!/usr/bin/env python3
"""test_roommates_native.py - ROOMMATES / MOVE-IN: compiles the PURE logic (pc/src/pc_roommates.c; no game state) alone with -Wall -Wextra -Werror and runs
pc_room_selftest() (request / cancel / accept / deny / remove / leave rules, one open request and one house per guest, 4 roommates + 4 requests per holder, the
10-minute cooldown, owner gone, drop guest, the GC of tombstones, the roommates.ini round trip + the tolerant parser, the message 104 validator, the notice charset),
plus a save / load round trip through a real file (atomic write, .bak generation, the .bak fallback when the main file is missing), and pc/src/pc_room_dlg.c (the
gyroid's move-in rows: charset, endings, every refusal reason; the patch of the vanilla gyroid menus 0x0925 / 0x0929 byte for byte; unarmed / foreign lists untouched).

TIER: NATIVE (no game process). Usage: python test_roommates_native.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))

MAIN = r'''
#include <stdio.h>
#include <string.h>
#include "pc_roommates.h"
int main(int argc, char** argv) {
    PCRoomTable t, u;
    uint8_t o[20], g[20];
    const uint8_t n[8] = {'A','l','i','c','e',' ',' ',' '};
    char path[512], bak[540];
    int fails = pc_room_selftest(), bad = 0, fb = 0, i;
    if (argc < 2) return 99;
    snprintf(path, sizeof(path), "%s/roommates.ini", argv[1]);
    snprintf(bak, sizeof(bak), "%s.bak", path);
    for (i = 0; i < 20; i++) { o[i] = (uint8_t)(i + 1); g[i] = (uint8_t)(0x80 + i); }
    pc_room_init(&t);
    if (pc_room_request(&t, o, g, n, n, 1700000000u) != PC_ROOM_R_OK) fails++;
    if (pc_room_load(path, &u, &bad, &fb) != PC_ROOM_FILE_MISSING || u.n != 0) { printf("FAIL missing file\n"); fails++; }
    if (!pc_room_save(path, &t)) { printf("FAIL save 1\n"); fails++; }
    if (pc_room_accept(&t, o, g, 1700000001u) != PC_ROOM_R_OK || !pc_room_save(path, &t)) { printf("FAIL save 2\n"); fails++; }
    if (pc_room_load(path, &u, &bad, &fb) != PC_ROOM_FILE_OK || u.n != 1 || u.e[0].state != PC_ROOM_ST_ROOMMATE || fb != 0 || bad != 0) { printf("FAIL load\n"); fails++; }
    if (pc_room_load(bak, &u, &bad, &fb) != PC_ROOM_FILE_OK || u.n != 1 || u.e[0].state != PC_ROOM_ST_PENDING) { printf("FAIL .bak holds the previous generation\n"); fails++; }
    remove(path);
    if (pc_room_load(path, &u, &bad, &fb) != PC_ROOM_FILE_OK || fb != 1 || u.n != 1) { printf("FAIL .bak fallback\n"); fails++; }
    {
        PCRoomHomes hs, hl;
        char hp[540];
        snprintf(hp, sizeof(hp), "%s/roommate_homes.ini", argv[1]);
        memset(&hs, 0, sizeof(hs));
        pc_room_homes_set(&hs, g, o, 2);
        if (!pc_room_homes_save(hp, &hs) || pc_room_homes_load(hp, &hl) != PC_ROOM_FILE_OK || hl.n != 1 || hl.e[0].house != 2 || memcmp(hl.e[0].holder, o, 20) != 0) {
            printf("FAIL roommate_homes.ini round trip\n");
            fails++;
        }
    }
    printf("[ROOM] file checks: %s\n", fails ? "FAIL" : "PASS");
    return fails;
}
'''


DLG_MAIN = r'''
#include <stdio.h>
#include "pc_room_dlg.h"
int main(void) {
    int b = pc_room_dlg_selftest();
    printf("[ROOM][DLG] selftest: %s (%d)\n", b ? "FAIL" : "PASS", b);
    return b;
}
'''


def main():
    cc =shutil.which("cc") or shutil.which("gcc") or r"C:\msys64\ucrt64\bin\cc.exe"
    if not os.path.isfile(cc) and shutil.which(cc) is None:
        cc = r"C:\msys64\ucrt64\bin\cc.exe"
    work = tempfile.mkdtemp(prefix="room_native_")
    ok_all = False
    try:
        main_c = os.path.join(work, "main.c")
        exe = os.path.join(work, "room_selftest.exe")
        with open(main_c, "w") as f:
            f.write(MAIN)
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + os.pathsep + r"C:\msys64\ucrt64\bin"
        cmd = [cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + os.path.join(PC, "include"), main_c, os.path.join(PC, "src", "pc_roommates.c"), "-o", exe]
        b = subprocess.run(cmd, capture_output=True, text=True, cwd=work, env=env)
        ok_build = b.returncode == 0
        print(("PASS" if ok_build else "FAIL") + " - N1 pc_roommates.c compiles alone with -Wall -Wextra -Werror (no game header)")
        if not ok_build:
            print(b.stdout + b.stderr)
            return 1
        r = subprocess.run([exe, work], capture_output=True, text=True, cwd=work, timeout=60, env=env)
        print(r.stdout.rstrip())
        ok1 = "[ROOM] selftest: PASS" in r.stdout
        ok2 = "[ROOM] file checks: PASS" in r.stdout and r.returncode == 0
        print(("PASS" if ok1 else "FAIL") + " - N2 pc_room_selftest(): every pure check passes")
        print(("PASS" if ok2 else "FAIL") + " - N3 roommates.ini save / load / .bak generation / .bak fallback, roommate_homes.ini round trip")
        dlg_c = os.path.join(work, "dlg.c")
        dlg_exe = os.path.join(work, "dlg_selftest.exe")
        with open(dlg_c, "w") as f:
            f.write(DLG_MAIN)
        d = subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I" + os.path.join(PC, "include"), dlg_c, os.path.join(PC, "src", "pc_room_dlg.c"), "-o", dlg_exe],
                           capture_output=True, text=True, cwd=work, env=env)
        ok3 = d.returncode == 0
        if ok3:
            dr = subprocess.run([dlg_exe], capture_output=True, text=True, cwd=work, timeout=60, env=env)
            print(dr.stdout.rstrip())
            ok3 = dr.returncode == 0 and "[ROOM][DLG] selftest: PASS" in dr.stdout
        else:
            print(d.stdout + d.stderr)
        print(("PASS" if ok3 else "FAIL") + " - N4 pc_room_dlg.c compiles alone (-Wall -Wextra -Werror, no game header) and pc_room_dlg_selftest() passes (rows + the gyroid menu patch)")
        ok_all = ok_build and ok1 and ok2 and ok3
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("-" * 60)
    print("%s" % ("ALL PASS" if ok_all else "FAILURES"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
