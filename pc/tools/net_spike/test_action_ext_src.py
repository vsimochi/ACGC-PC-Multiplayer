#!/usr/bin/env python3
"""test_action_ext_src.py - PUPPET ACTION_EXT (msg 96, worker P2): source audit + the native selftest. No game process.

  W  wire: PC_NETGAME_MSG_ACTION_EXT = 96 (unique; 93-95 stay unused), PCActionExtMsg layout pinned (16 B, aux at 12), protocol still 8
  V  validation bounds pinned to the vanilla enums by _Static_assert (anim < mPlayer_ANIM_NUM 157, part table < 5, eye < 8, mouth < 6, every
     allowlisted index), reserved bits / rsv must be 0
  R  relay scope: the host accepts only a READY sender with a valid message and an announced scene, relays RELIABLE to every OTHER READY peer
     whose announced scene id AND owner equal the sender's, presents a client's message on its own puppet; the client drops its own id
  C  counter binding: the receiver applies a record only while the puppet's interpolated (main index, entry counter) equals it
     (pc_aext_decide in pc_puppet_aext_apply, wired into pc_remote_player_mv's row bookkeeping)
  S  sender: one generic hook (pcnetgame_aext_tick from pc_net_game_poll) reading the local player read-only; ON by default; PC_ACTION_EXT=0
     disables; the test hook is compiled only with PC_NET_TEST_HOOKS and needs AC_TEST_HOOKS=1
  O  old peers: id 96 is unknown to the released v0.1.1 and 0.2.1 (d17cae6) dispatchers, which end with the silent ignore fallthrough and
     count violations only in the 47-50 / 59-61 range branches
  N  native selftest (pc/tools/net_spike/action_ext_selftest.c, the REAL pc_action_ext.c)
Usage: python test_action_ext_src.py
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.abspath(os.path.join(PC, ".."))
sys.path.insert(0, HERE)
import wire_baseline  # noqa: E402

results = []


def ck(d, c):
    results.append((d, bool(c)))
    print(("PASS - " if c else "FAIL - ") + d)


def rd(*p):
    return open(os.path.join(*p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def strip_comments(t):
    return re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", t, flags=0), flags=re.S)


def body(src, head):
    i = src.index(head)
    return src[i:src.index("\n}\n", i)]


ng = rd(PC, "src", "pc_net_game.c")
gh = rd(PC, "include", "pc_net_game.h")
ah = rd(PC, "include", "pc_action_ext.h")
ac = rd(PC, "src", "pc_action_ext.c")
rp = rd(PC, "src", "pc_remote_player.c")
mp = rd(ROOT, "src", "game", "m_player.c")
cm = rd(PC, "CMakeLists.txt")

# ---- W
ids = dict((n, v) for n, v in wire_baseline.c_message_ids_raw(ng))
vals = sorted(ids.values())
ck("W PC_NETGAME_MSG_ACTION_EXT = 96, ids unique, 93 / 94 / 95 unused", ids.get("PC_NETGAME_MSG_ACTION_EXT") == 96 and len(vals) == len(set(vals))
   and not ({93, 94, 95} & set(vals)))
ck("W protocol version still 8", wire_baseline.header_protocol_ok(gh) and wire_baseline.EXPECTED_PROTOCOL_VERSION == 8)
st = re.search(r"typedef struct PCActionExtMsg \{(.*?)\} PCActionExtMsg;", ah, re.S)
norm = " ".join(strip_comments(st.group(1)).split()) if st else None
ck("W PCActionExtMsg pinned: %s" % norm, norm == "uint8_t msg_type; uint8_t net_player_id; uint8_t main_index; uint8_t entry_counter; uint8_t anim0; "
   "uint8_t anim1; uint8_t part_table; uint8_t flags; uint8_t speed_q; uint8_t start_frame; uint8_t face; uint8_t variant; uint16_t aux; "
   "uint8_t clip_seq; uint8_t rsv;")
ck("W 16-byte size + aux offset asserts", '_Static_assert(sizeof(PCActionExtMsg) == 16,' in ah and "__builtin_offsetof(PCActionExtMsg, aux) == 12" in ah)
ck("W the documented sprint ids include 96 / 97 / 98 (wire_baseline)", all(v in wire_baseline.SPRINT_EXTRA_MSG_IDS for v in (96, 97, 98)))
ck("W flags: bit0 REVERSE, bits1-3 pin mask, bit4 REPEAT, bits 5-7 reserved",
   "#define PC_AEXT_FL_REVERSE 0x01u" in ah and "#define PC_AEXT_FL_PIN_MASK 0x0Eu" in ah and "#define PC_AEXT_FL_REPEAT 0x10u" in ah
   and "#define PC_AEXT_FL_RESERVED 0xE0u" in ah)

# ---- V
ok = body(ac, "int pc_aext_msg_ok(const PCActionExtMsg* m) {")
ck("V pc_aext_msg_ok checks id, allowlist, counter, both anims, part table, reserved flags, rsv and the face nibbles",
   all(s in ok for s in ("m->msg_type != (uint8_t)PC_AEXT_MSG_ID", "pc_aext_index_allowed((int)m->main_index)", "m->entry_counter > 15u",
                         "m->anim0 >= (uint8_t)PC_AEXT_ANIM_NUM", "m->anim1 >= (uint8_t)PC_AEXT_ANIM_NUM", "m->part_table >= (uint8_t)PC_AEXT_PART_TABLE_NUM",
                         "(m->flags & PC_AEXT_FL_RESERVED) != 0u", "m->rsv != 0u", "(m->face >> 4) >= PC_AEXT_EYE_NUM", "(m->face & 0x0Fu) >= PC_AEXT_MOUTH_NUM")))
ck("V bounds pinned to vanilla by _Static_assert in pc_net_game.c (ANIM_NUM 157, PART_TABLE 5, EYE 8, MOUTH 6, INDEX 121, GAAAN2, id 96)",
   "#define PC_AEXT_ANIM_NUM 157" in ah and all(s in ng for s in ("_Static_assert(PC_AEXT_ANIM_NUM == mPlayer_ANIM_NUM",
                                                                  "_Static_assert(PC_AEXT_PART_TABLE_NUM == mPlayer_PART_TABLE_NUM",
                                                                  "PC_AEXT_EYE_NUM == mPlayer_EYE_TEX_NUM && PC_AEXT_MOUTH_NUM == mPlayer_MOUTH_TEX_NUM",
                                                                  "_Static_assert(PC_AEXT_INDEX_NUM == mPlayer_INDEX_NUM",
                                                                  "_Static_assert(PC_AEXT_ANIM_GAAAN2 == mPlayer_ANIM_GAAAN2",
                                                                  "_Static_assert(PC_AEXT_MSG_ID == (unsigned)PC_NETGAME_MSG_ACTION_EXT")))
allow = re.findall(r"case (PC_AEXT_IDX_\w+):", body(ac, "int pc_aext_index_allowed(int main_index) {"))
asserted = set(re.findall(r"(PC_AEXT_IDX_\w+) == \(int\)mPlayer_INDEX_\w+", ng))
ck("V the 21 allowlisted indexes are each asserted against mPlayer_INDEX_* (%d / %d), boat demos 112-117 and WAIT_BED are not allowlisted" % (len(allow), len(asserted)),
   len(allow) == 21 and set(allow) <= asserted and "PC_AEXT_IDX_WAIT_BED" not in allow and not re.search(r"PC_AEXT_IDX_\w*BOAT", ah))

# ---- R
hh = body(ng, "static void pcnetgame_handle_host_action_ext(PCNetPeerId peer, const PCActionExtMsg* in) {")
ck("R host: READY sender, pc_aext_msg_ok, an announced scene; dropped otherwise (counted, never a violation)",
   "s_host_peer_link[peer] != PC_NETGAME_LINK_READY" in hh and "!pc_aext_msg_ok(in)" in hh and "pc_remote_player_get_scene((PCNetPlayerId)peer, &sc)" in hh
   and "violation" not in hh)
rl = body(ng, "static void pcnetgame_aext_host_relay(int origin, const PCNetPlayerScene* sc, const PCActionExtMsg* in) {")
ck("R relay: stamps the origin, skips the origin, READY peers in the same scene id AND owner only, RELIABLE; the host presents a client's message",
   "m.net_player_id = (uint8_t)origin;" in rl and "i != origin && s_host_peer_link[i] == PC_NETGAME_LINK_READY" in rl
   and "pcnetgame_aext_same_scene(&ps, sc)" in rl and "PC_NET_RELIABLE" in rl and "pc_remote_player_on_action_ext(origin, &m);" in rl
   and "a->scene_id == b->scene_id && a->owner == b->owner" in ng)
hd = body(ng, "static void pcnetgame_handle_host_data(PCNetPeerId peer, const uint8_t* data, uint16_t size) {")
cd = body(ng, "static void pcnetgame_handle_client_data(const uint8_t* data, uint16_t size) {")
ck("R dispatch: host routes id 96 (a wrong size is dropped), the client accepts only the exact size",
   "data[0] == (uint8_t)PC_NETGAME_MSG_ACTION_EXT" in hd and "pcnetgame_handle_host_action_ext(peer, &ax);" in hd
   and "size == sizeof(PCActionExtMsg) && data[0] == (uint8_t)PC_NETGAME_MSG_ACTION_EXT" in cd and "pcnetgame_handle_client_action_ext(&ax);" in cd)
hc = body(ng, "static void pcnetgame_handle_client_action_ext(const PCActionExtMsg* in) {")
ck("R client: READY, valid, never its own id back", "s_client_link != PC_NETGAME_LINK_READY" in hc and "!pc_aext_msg_ok(in)" in hc
   and "(PCNetPlayerId)in->net_player_id == (PCNetPlayerId)s_client_assigned_peer_id" in hc)

# ---- C
ap = body(rp, "static int pc_puppet_aext_apply(PCRemotePlayerActor* self, PCRemotePlayerSlot* slot, GAME* game, double now) {")
ck("C the receiver binds to the puppet's interpolated MOVE pair (cosmetic_action_index / counter) through pc_aext_decide",
   "(int)self->cosmetic_action_index" in ap and "(int)self->cosmetic_action_counter" in ap and "pc_aext_decide(&pp->st, &pp->v, valid, idx, ctr, now, &rec)" in ap)
dc = body(ac, "int pc_aext_decide(PCAextStore* st, PCAextView* v, int pair_valid, int idx, int ctr, double now, const PCAextRecord** out) {")
ck("C pc_aext_decide: equality on (main_index, entry_counter); a shown-and-left or stale record expires",
   "(int)r->m.main_index == idx && (int)r->m.entry_counter == ctr" in dc and "r->seen || (now - r->recv_time) > PC_AEXT_PENDING_TTL" in dc)
ck("C wired into pc_remote_player_mv after the static row bookkeeping (owns the clips like a row) and into the face pattern",
   "row_on = pc_puppet_aext_apply(self, slot, game, now_time) || self->visual.row_active;" in rp
   and "pc_puppet_aext_face(self, a0, &e, &m);" in rp and "if (row_on && self->visual.row_active) {" in rp)
ck("C a static row of the same pair is released first; the overlay end lets the fallback rebind", 'pc_remote_player_row_release(self, "action_ext");' in ap
   and "v->current_anim_idx = -1;" in ap)
ck("C receiver validates again before storing", "!pc_aext_msg_ok(m)" in body(rp, "void pc_remote_player_on_action_ext(int player_id, const PCActionExtMsg* m) {"))
ck("C own blocks are delimited in both shared files", rp.count("/* ===== PUPPET ACTION_EXT BEGIN") == 1 and rp.count("/* ===== PUPPET ACTION_EXT END ===== */") == 1
   and ng.count("/* ===== PUPPET ACTION_EXT BEGIN") == 1 and ng.count("/* ===== PUPPET ACTION_EXT END ===== */") == 1)

# ---- S
tk = body(ng, "static void pcnetgame_aext_tick(void) {")
ck("S one generic sender hook in pc_net_game_poll", ng.count("pcnetgame_aext_tick();") == 1 and "pcnetgame_aext_tick(); /* PUPPET ACTION_EXT" in body(ng, "void pc_net_game_poll(void) {"))
ck("S the sample is read-only (animation0_idx / animation1_idx / part_table_idx / keyframe0 frame control / animation_enabled) and goes through the detector",
   all(s in tk for s in ("pl->animation0_idx", "pl->animation1_idx", "pl->part_table_idx", "pl->keyframe0.frame_control.speed",
                         "pl->keyframe0.animation_enabled & 7", "pc_aext_sender_step(&s_aext_snd, &smp, now, &m)"))
   and not re.search(r"\bpl->\w+(\.\w+)*\s*=[^=]", tk))
ck("S client sends to the host only; the listen host relays its own directly; the observer host sends nothing",
   "(void)pc_net_send(0, PC_NET_RELIABLE, &m, (uint16_t)sizeof(m));" in tk and "pcnetgame_aext_host_relay((int)PC_NETGAME_HOST_PLAYER_ID, &s_local_scene, &m);" in tk
   and "pc_host_observer_active()" in tk)
en = body(ng, "static int pcnetgame_aext_enabled(void) {")
ck("S ON by default, PC_ACTION_EXT=0 disables", 'getenv("PC_ACTION_EXT")' in en and "? 0 : 1; /* ON by default */" in en)
ck("S the test hook exists only under PC_NET_TEST_HOOKS and needs AC_TEST_HOOKS (pc_test_hook_getenv)",
   re.search(r"#ifdef PC_NET_TEST_HOOKS\n#include \"pc_test_hooks.h\"[\s\S]*?pc_test_hook_getenv\(\"AC_TEST_AEXT_SCRIPT\"\)[\s\S]*?#endif", ng) is not None
   and "int PC_Test_AextRequest(GAME* game, int what, int arg) {" in mp)
ck("S the pure module is built and has no game include", "src/pc_action_ext.c" in cm and not re.search(r'#include "(m_|ac_|pc_net_game)', ac))

# ---- O
for tag, ref in (("0.1.1", "v0.1.1"), ("0.2.1", "d17cae6")):
    try:
        old = subprocess.run(["git", "-C", ROOT, "show", ref + ":pc/src/pc_net_game.c"], capture_output=True, timeout=60).stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    except (OSError, subprocess.SubprocessError):
        old = ""
    if "pcnetgame_handle_host_data" not in old:
        print("SKIPPED - %s source not available" % tag)
        continue
    ohd = body(old, "static void pcnetgame_handle_host_data(PCNetPeerId peer, const uint8_t* data, uint16_t size) {")
    ocd = body(old, "static void pcnetgame_handle_client_data(const uint8_t* data, uint16_t size) {")
    oids = {int(v) for v in re.findall(r"^\s*PC_NETGAME_MSG_\w+\s*=\s*(\d+)\s*,", old, re.M)}
    rng = re.findall(r"data\[0\] >= \(uint8_t\)PC_NETGAME_MSG_(\w+) && data\[0\] <= \(uint8_t\)PC_NETGAME_MSG_(\w+)", ohd)
    oname = dict((n, int(v)) for n, v in re.findall(r"^\s*PC_NETGAME_MSG_(\w+)\s*=\s*(\d+)\s*,", old, re.M))
    ck("O %s: id 96 unknown (%s ids end at %d), outside its range branches %s, host dispatcher ends with the silent ignore" % (tag, tag, max(oids), rng),
       96 not in oids and all(not (oname[a] <= 96 <= oname[b]) for a, b in rng)
       and ohd.rstrip().endswith("/* malformed / short / unrecognized: ignore rather than misinterpret */"))
    ck("O %s: an old CLIENT ignores id 96 relayed by a new host (its dispatcher ends with the silent 'unrecognized: ignore', no range branch covers 96)" % tag,
       re.search(r"/\* malformed\s*/\s*short\s*/\s*unrecognized: ignore[^*]*\*/\s*$", ocd.rstrip()) is not None
       and all(not (oname.get(a, 0) <= 96 <= oname.get(b, 0)) for a, b in re.findall(r"data\[0\] >= \(uint8_t\)PC_NETGAME_MSG_(\w+) && data\[0\] <= \(uint8_t\)PC_NETGAME_MSG_(\w+)", ocd)))

# ---- N
src = os.path.join(HERE, "action_ext_selftest.c")
gcc = None
for cand in ("gcc", r"C:\msys64\ucrt64\bin\gcc.exe"):
    try:
        subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=30)
        gcc = cand
        break
    except (OSError, subprocess.SubprocessError):
        continue
ck("N native selftest present and includes the real module", os.path.isfile(src) and '#include "../../src/pc_action_ext.c"' in open(src).read())
if gcc is None:
    print("SKIPPED - gcc not found: native selftest not run")
else:
    out = os.path.join(os.environ.get("TEMP", HERE), "action_ext_selftest_src_audit.exe")
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + r"C:\msys64\ucrt64\bin"
    cp = subprocess.run([gcc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Iinclude", "tools/net_spike/action_ext_selftest.c", "-o", out], cwd=PC,
                        capture_output=True, timeout=120, env=env)
    ck("N selftest compiles warning-free (%s)" % cp.stderr.decode(errors="replace")[-300:], cp.returncode == 0)
    if cp.returncode == 0:
        r = subprocess.run([out], capture_output=True, timeout=60, env=env)
        tail = r.stdout.decode(errors="replace").strip().splitlines()[-1:] or [""]
        ck("N selftest passes (%s)" % tail[0], r.returncode == 0)

print("-" * 60)
n = sum(1 for _d, okv in results if okv)
print("%d/%d checks passed" % (n, len(results)))
sys.exit(0 if n == len(results) else 1)
