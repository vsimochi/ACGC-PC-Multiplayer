import re, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[3]
def rd(p): return (ROOT / p).read_text(encoding="utf-8", errors="replace")
fails = []
def check(c, m):
    if not c: fails.append(m)
s = rd("pc/src/pc_remote_player.c")
check("#define PC_REMOTE_PLAYER_PUSH_WEIGHT 2" in s, "light push weight define")
check("status_data.weight = PC_REMOTE_PLAYER_PUSH_WEIGHT;" in s, "puppet uses light weight")
check("status_data.weight = MASSTYPE_HEAVY;" not in s, "no HEAVY puppet weight")
m = re.search(r"s_remote_player_pipe_data = \{.*?\};", s, re.S)
check(m and "{ { 16, 60," in m.group(0), "radius 16 height 60")
check(m and "{ 0x09, 0x10, ClObj_TYPE_PIPE }" in m.group(0), "pair flags unchanged (collision not disabled)")
w = int(re.search(r"PC_REMOTE_PLAYER_PUSH_WEIGHT (\d+)", s).group(1))
check(0 < w < 0xFE and w <= 4, "weight is a light NORMAL type (<0xFE, >0)")
check(50.0 / (50 + w) > 0.9 and w / (50.0 + w) < 0.08, "player push fraction under 8%")

if fails:
    print("FAIL: " + " | ".join(fails)); sys.exit(1)
print("PASS")
