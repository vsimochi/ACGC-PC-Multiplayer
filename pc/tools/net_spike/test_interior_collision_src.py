#!/usr/bin/env python3
"""Source audit: interior puppet collision (shared interior only, player rooms disarmed, local door/demo holds, no wire change)."""
import re, sys, pathlib
src = (pathlib.Path(__file__).resolve().parents[2] / "src" / "pc_remote_player.c").read_text(encoding="utf-8", errors="replace")
m = re.search(r"static const char\* pc_remote_player_collide_eval\(.*?\n\}\n", src, re.S)
body = m.group(0) if m else ""
checks = [
    ("collide_eval found", bool(body)),
    ("uses scene_is_local_shown for interiors", "pc_remote_player_scene_is_local_shown(slot, play)" in body),
    ("player house disarmed", "PC_NETSCENE_KIND_PLAYER_HOUSE" in body),
    ("field rule still first", "pc_remote_player_scene_is_local_field(slot, play)" in body),
    ("interior stale-gap hold", "PC_REMOTE_PLAYER_ACTION_GAP_FRAMES" in body),
    ("local DOOR/OUTDOOR/KNOCK/INTRO/DEMO holds", all(k in body for k in ("mPlayer_INDEX_DOOR", "mPlayer_INDEX_OUTDOOR", "mPlayer_INDEX_INTRO", "mPlayer_INDEX_DEMO_WALK", "mPlayer_INDEX_RETURN_OUTDOOR"))),
    ("holds are interior-only (in_interior gate)", "if (in_interior)" in body),
]
bad = 0
for n, ok in checks:
    print(("PASS" if ok else "FAIL"), "-", n)
    bad += (not ok)
print("ALL PASSED (0 failed)" if not bad else "%d FAILED" % bad)
sys.exit(1 if bad else 0)
