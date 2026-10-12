#!/usr/bin/env python3
"""test_shop_preview_sync_src.py - Nook shop wall/carpet preview sync SOURCE AUDIT (no game process).
Checks: msg id 78 at the end of the enum, fixed 10-byte struct, host/client dispatch, relay filtered by announced scene, clear on scene change / disconnect / local leave,
the clip procs report local previews only (aSI_pc_no_net), vanilla (non TARGET_PC) branch of aSI_ChangeWall untouched, no Save_t change."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NG = (ROOT / "pc/src/pc_net_game.c").read_text(encoding="utf-8", errors="replace")
SI = (ROOT / "src/actor/ac_shop_indoor.c").read_text(encoding="utf-8", errors="replace")
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


check("enum id 78", re.search(r"PC_NETGAME_MSG_SHOP_PREVIEW\s*=\s*78", NG) is not None)
check("78 declared inside the enum before PCNetGameMsgType close", NG.index("PC_NETGAME_MSG_SHOP_PREVIEW") < NG.index("} PCNetGameMsgType;"))
check("10-byte struct + static assert", "sizeof(PCNetGameShopPreviewMsg) == 10" in NG)
check("host dispatch", re.search(r"sizeof\(PCNetGameShopPreviewMsg\)\s*&&\s*data\[0\]\s*==\s*\(uint8_t\)PC_NETGAME_MSG_SHOP_PREVIEW\)\s*\{\s*PCNetGameShopPreviewMsg sp;\s*memcpy\(&sp, data, sizeof\(sp\)\);\s*pcnetgame_handle_host_shop_preview", NG) is not None)
check("client dispatch", "pcnetgame_handle_client_shop_preview(&sp)" in NG)
m = re.search(r"static void pcnetgame_handle_host_shop_preview.*?\n\}\n", NG, re.S)
check("host handler READY + sender-scene validation", m is not None and "PC_NETGAME_LINK_READY" in m.group(0) and "sc.scene_id != in->scene_id" in m.group(0))
m = re.search(r"static void pcnetgame_host_shop_preview_relay.*?\n\}\n", NG, re.S)
check("relay: other READY peers announced in the same scene only", m is not None and "i != origin" in m.group(0) and "sc.scene_id == m.scene_id" in m.group(0) and "PC_NET_RELIABLE" in m.group(0))
check("own preview wins", "s_shop_local_prev_scene != 0" in NG)
check("clear on peer scene change", "pcnetgame_shop_preview_peer_left((int)peer, (int)s.scene_id)" in NG)
check("clear on peer disconnect", "pcnetgame_shop_preview_peer_left((int)peer, 0)" in NG)
check("clear on local scene leave", re.search(r"pcnetgame_shop_preview_local_left\(\);[^\n]*\n\s*pcnetgame_scene_fill\(&s_local_scene", NG) is not None)
check("local entry inert in single player", re.search(r"void pc_net_game_shop_preview_local\(int kind, uint16_t item\) \{\s*if \(\(s_role != PC_NETGAME_ROLE_HOST && s_role != PC_NETGAME_ROLE_CLIENT\)", NG) is not None)
check("no Save_t/protocol bump in the preview block", "PC_NETGAME_PROTOCOL_VERSION" not in NG[NG.index("Nook shop wall / carpet preview (PC_NETGAME_MSG_SHOP_PREVIEW"):NG.index("static void pcnetgame_host_peer_scene_gone")])
check("shop_indoor: local preview reported once, remote never re-sent",
      "static int aSI_pc_no_net" in SI and "pc_net_game_shop_preview_local(1," in SI and "pc_net_game_shop_preview_local(2," in SI and "pc_net_game_shop_preview_local(3, 0)" in SI)
check("shop_indoor: remote apply sets/clears the no-net flag", re.search(r"int aSI_pc_apply_remote_preview.*?aSI_pc_no_net = 1;.*?aSI_pc_no_net = 0;", SI, re.S) is not None)
check("shop_indoor: vanilla #else branch keeps the unconditional sound", re.search(r"#else\s*sAdo_SysTrgStart\(NA_SE_11B\);\s*#endif\s*return TRUE;", SI) is not None)
check("shop_indoor: agent C restock eject untouched", "aSI_pc_restock_eject" in SI and "pc_net_game_shop_restocking" in SI)
sys.exit(1 if fails else 0)
