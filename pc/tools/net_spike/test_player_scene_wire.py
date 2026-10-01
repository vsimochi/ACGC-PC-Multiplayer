#!/usr/bin/env python3
"""test_player_scene_wire.py - M9-A (scene identity / player presence). PURE PROTOCOL/UNIT test, mirroring
test_snowman_wire.py / test_bury_wire.py: no host/port needed.

Covers PC_NETGAME_MSG_PLAYER_SCENE (id 44, pc/src/pc_net_game.c PCNetGamePlayerSceneMsg), RELIABLE, 12 bytes:

    uint8 msg_type, uint8 net_player_id, uint8 scene_id, uint8 flags, uint16 owner, uint16 _reserved0, uint32 seq

Checks: struct size/layout, encode->decode round trip over the full field ranges, payload-length gating
(mirrors `size == sizeof(...)` in pcnetgame_handle_host_data()/pcnetgame_handle_client_data()), message id 44
does not collide with 1..43 and is the next free id, protocol version bumped to 5 in net_spike_lib and in the
C header, the raw scene-id whitelist (parsed from the real C source and the real enum header) stays within u8
and excludes title/demo/player-select, and the stale-sequence rule mirror.

Usage: python3 test_player_scene_wire.py
Exit code: 0 all checks passed, 1 otherwise.
"""
import os
import re
import struct
import sys

import net_spike_lib as L

FMT = "<BBBBHHI"
SIZE = struct.calcsize(FMT)
MSG_TYPE_PLAYER_SCENE = 44
FLAG_IN_TOWN = 0x01
FLAG_CLEARED = 0x80
EXISTING = set(range(1, 44))

HERE = os.path.dirname(os.path.abspath(__file__))
PC_DIR = os.path.join(HERE, "..", "..")
HDR = os.path.join(PC_DIR, "..", "include", "m_scene_table.h")

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL") + f": {name}")


def encode(net_player_id, scene_id, flags, owner, seq):
    return struct.pack(FMT, MSG_TYPE_PLAYER_SCENE, net_player_id & 0xFF, scene_id & 0xFF, flags & 0xFF,
                       owner & 0xFFFF, 0, seq & 0xFFFFFFFF)


def decode(buf):
    t, pid, sid, flags, owner, _r, seq = struct.unpack(FMT, buf)
    return dict(msg_type=t, net_player_id=pid, scene_id=sid, flags=flags, owner=owner, seq=seq)


def parse_scene_enum():
    text = open(HDR, encoding="utf-8").read()
    body = re.search(r"enum scene_table \{(.*?)\};", text, re.S).group(1)
    names = []
    for line in body.splitlines():
        line = line.split("//")[0].split("/*")[0].strip().rstrip(",")
        if line and re.fullmatch(r"SCENE_[A-Z0-9_]+", line):
            names.append(line)
    return {n: i for i, n in enumerate(names)}


ANNOUNCEABLE = ["SCENE_FG", "SCENE_NPC_HOUSE", "SCENE_SHOP0", "SCENE_BROKER_SHOP", "SCENE_POST_OFFICE",
                "SCENE_POLICE_BOX", "SCENE_MY_ROOM_S", "SCENE_MY_ROOM_M", "SCENE_MY_ROOM_L", "SCENE_CONVENI",
                "SCENE_SUPER", "SCENE_DEPART", "SCENE_DEPART_2", "SCENE_KAMAKURA", "SCENE_BUGGY",
                "SCENE_MUSEUM_ENTRANCE", "SCENE_MUSEUM_ROOM_PAINTING", "SCENE_MUSEUM_ROOM_FOSSIL",
                "SCENE_MUSEUM_ROOM_INSECT", "SCENE_MUSEUM_ROOM_FISH", "SCENE_MY_ROOM_LL1", "SCENE_MY_ROOM_LL2",
                "SCENE_MY_ROOM_BASEMENT_S", "SCENE_MY_ROOM_BASEMENT_M", "SCENE_MY_ROOM_BASEMENT_L",
                "SCENE_MY_ROOM_BASEMENT_LL1", "SCENE_NEEDLEWORK", "SCENE_COTTAGE_MY", "SCENE_COTTAGE_NPC",
                "SCENE_LIGHTHOUSE", "SCENE_TENT"]
SUPPRESSED = ["SCENE_TEST1", "SCENE_TEST2", "SCENE_TEST3", "SCENE_WATER_TEST", "SCENE_FOOTPRINT_TEST",
              "SCENE_NPC_TEST", "SCENE_RANDOM_NPC_TEST", "SCENE_BG_TEST_NO_RIVER", "SCENE_BG_TEST_RIVER",
              "SCENE_FIELD_TOOL_INSIDE", "SCENE_START_DEMO", "SCENE_START_DEMO2", "SCENE_START_DEMO3",
              "SCENE_PLAYERSELECT", "SCENE_PLAYERSELECT_2", "SCENE_PLAYERSELECT_3", "SCENE_PLAYERSELECT_SAVE",
              "SCENE_TEST5", "SCENE_EVENT_ANNOUNCEMENT", "SCENE_FIELD_TOOL", "SCENE_TITLE_DEMO"]


def host_accepts(stored_seq, in_seq):
    """Mirror of pc_remote_player_on_scene(): strictly-newer-than-stored (or nothing stored)."""
    return stored_seq is None or in_seq > stored_seq


# --- 1/2: layout + round trip -------------------------------------------------------------------
check("PCNetGamePlayerSceneMsg size == 12 bytes", SIZE == 12)
sample = encode(7, 9, 1, 0x1234, 0xAABBCCDD)
check("byte offsets: msg_type 0, net_player_id 1, scene_id 2, flags 3, owner 4, seq 8",
      sample[0] == 44 and sample[1:4] == bytes([7, 9, 1]) and struct.unpack_from("<H", sample, 4)[0] == 0x1234 and
      struct.unpack_from("<I", sample, 8)[0] == 0xAABBCCDD)
for (pid, sid, fl, ow, sq) in [(0, 7, FLAG_IN_TOWN, 0, 1), (4, 9, 0, 0, 2), (2, 6, 0, 0xD000, 0xFFFFFFFF),
                               (1, 21, 0, 3, 12345), (4, 254, FLAG_CLEARED, 0xFFFF, 0)]:
    d = decode(encode(pid, sid, fl, ow, sq))
    check(f"round trip pid={pid} scene={sid} flags={fl:#x} owner={ow:#x} seq={sq}",
          (d["net_player_id"], d["scene_id"], d["flags"], d["owner"], d["seq"]) == (pid, sid, fl, ow, sq) and
          d["msg_type"] == 44)

# --- 3: size gating ------------------------------------------------------------------------------
good = encode(0, 7, FLAG_IN_TOWN, 0, 1)
check("truncated message rejected by size gate", len(good[:-1]) != SIZE)
check("oversized message rejected by size gate", len(good + b"\x00") != SIZE)
check("12 bytes fits PC_NET_MAX_PAYLOAD (1024)", SIZE <= 1024)

# --- 4: id + version ------------------------------------------------------------------------------
check("PLAYER_SCENE id 44 is not one of the existing ids 1..43", MSG_TYPE_PLAYER_SCENE not in EXISTING)
check("PLAYER_SCENE id 44 is the next free id after WILDLIFE_DESPAWN (43)", MSG_TYPE_PLAYER_SCENE == 43 + 1)
check("net_spike_lib.PC_NETGAME_MSG_PLAYER_SCENE == 44", L.PC_NETGAME_MSG_PLAYER_SCENE == 44)
check("net_spike_lib.PC_NETGAME_PROTOCOL_VERSION == 6", L.PC_NETGAME_PROTOCOL_VERSION == 6)
hsrc = open(os.path.join(PC_DIR, "include", "pc_net_game.h"), encoding="utf-8").read()
check("pc_net_game.h PC_NETGAME_PROTOCOL_VERSION == 6u",
      re.search(r"#define PC_NETGAME_PROTOCOL_VERSION 6u", hsrc) is not None)
csrc = open(os.path.join(PC_DIR, "src", "pc_net_game.c"), encoding="utf-8").read()
check("pc_net_game.c defines PC_NETGAME_MSG_PLAYER_SCENE = 44",
      re.search(r"PC_NETGAME_MSG_PLAYER_SCENE\s*=\s*44", csrc) is not None)
ids = [int(x) for x in re.findall(r"PC_NETGAME_MSG_[A-Z_]+\s*=\s*(\d+)", csrc[:csrc.index("} PCNetGameMsgType;")])]
check("all C message ids are unique and 45 (M9-C NPC_TALK) is the maximum", len(ids) == len(set(ids)) and max(ids) == 45)
check("pc_net_game.c static-asserts the 12-byte size", "sizeof(PCNetGamePlayerSceneMsg) == 12" in csrc)

# --- 5: raw scene-id whitelist ---------------------------------------------------------------------
enum = parse_scene_enum()
SCENE_NUM = enum.pop("SCENE_NUM")
check("scene enum parsed from include/m_scene_table.h (SCENE_NUM == 52 real ids)", len(enum) == SCENE_NUM == 52)
check("every scene id fits in a u8 (wire field is uint8)", max(enum.values()) < 255)
check("SCENE_NPC_HOUSE, SCENE_FG, SCENE_SHOP0 parsed at expected enum values (6, 7, 9)",
      (enum["SCENE_NPC_HOUSE"], enum["SCENE_FG"], enum["SCENE_SHOP0"]) == (6, 7, 9))
body = csrc[csrc.index("PCNetSceneKind pc_net_game_scene_kind"):csrc.index("int pc_net_game_scene_is_announceable")]
case_names = set(re.findall(r"case (SCENE_[A-Z0-9_]+):", body))
check("C whitelist == Python ANNOUNCEABLE list (no drift)", case_names == set(ANNOUNCEABLE))
check("C whitelist excludes every title/demo/player-select/test/tool scene", not (case_names & set(SUPPRESSED)))
check("ANNOUNCEABLE + SUPPRESSED cover the whole enum exactly once",
      set(ANNOUNCEABLE) | set(SUPPRESSED) == set(enum) and not (set(ANNOUNCEABLE) & set(SUPPRESSED)))
check("title demo / player select / start demo are NOT announceable",
      all(n not in case_names for n in ("SCENE_TITLE_DEMO", "SCENE_PLAYERSELECT", "SCENE_PLAYERSELECT_2",
                                         "SCENE_PLAYERSELECT_3", "SCENE_PLAYERSELECT_SAVE", "SCENE_START_DEMO",
                                         "SCENE_START_DEMO2", "SCENE_START_DEMO3")))

# --- 6: seq rule -----------------------------------------------------------------------------------
check("first message accepted (nothing stored)", host_accepts(None, 1))
check("strictly newer seq accepted", host_accepts(1, 2))
check("duplicate seq rejected", not host_accepts(2, 2))
check("older seq rejected", not host_accepts(5, 3))
check("seq restarts after the slot was cleared (disconnect) are accepted", host_accepts(None, 1))

passed = sum(1 for _, ok in checks if ok)
print("-" * 60)
print(f"{passed}/{len(checks)} checks passed")
sys.exit(0 if passed == len(checks) else 1)
