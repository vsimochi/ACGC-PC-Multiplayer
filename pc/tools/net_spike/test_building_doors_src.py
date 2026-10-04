#!/usr/bin/env python3
"""test_building_doors_src.py - SOURCE AUDIT of the cosmetic building doors for remote puppets (and the two isolation fixes).
TIER: SOURCE AUDIT only. It proves structure (wrappers exist, are TARGET_PC guarded, cannot reach a scene change, are hooked in row_start),
not that the door visibly opens - that needs a manual two-player run (see docs/multiplayer-guest-roadmap.md, Building interactions).
Usage: python test_building_doors_src.py"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
FAILS = []


def check(name, ok):
    print(("PASS - " if ok else "FAIL - ") + name)
    if not ok:
        FAILS.append(name)


def rd(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def func(text, sig):
    i = text.index(sig)
    while True:
        nxt = re.search(r"[{;]", text[i:])
        if nxt.group(0) == "{":
            break
        i = text.index(sig, i + 1)
    j = i + nxt.start()
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[i:k + 1]
    raise ValueError(sig)


def main():
    specs = [("ac_house_move.c_inc", "aHUS_pc_cosmetic_door", "mAc_PROFILE_HOUSE", "aHUS_wait", "ac_house.h"),
             ("ac_my_house_move.c_inc", "aMHS_pc_cosmetic_door", "mAc_PROFILE_MYHOUSE", "aMHS_wait", "ac_my_house.h"),
             ("ac_post_office_move.c_inc", "aPOFF_pc_cosmetic_door", "mAc_PROFILE_POST_OFFICE", "aPOFF_wait", "ac_post_office.h"),
             ("ac_needlework_shop_move.c_inc", "aNW_pc_cosmetic_door", "mAc_PROFILE_NEEDLEWORK_SHOP", "aNW_wait", "ac_needlework_shop.h")]
    for fn, name, prof, wait, hdr in specs:
        t = rd("src", "actor", fn)
        body = func(t, "int %s(ACTOR* actorx, int exit)" % name)
        pre = t[:t.index("int %s(ACTOR* actorx, int exit)" % name)]
        check("%s: defined under #ifdef TARGET_PC" % name, pre.rstrip().rsplit("\n", 3)[-3:].count("#ifdef TARGET_PC") == 1 or
              pre[pre.rfind("#ifdef TARGET_PC"):].count("#endif") == 0)
        check("%s: never reaches a scene change / demo end / demo request" %
              name, not re.search(r"goto_\w*scene|goto_other_scene|mDemo_End|mDemo_Request|rewrite_\w*out_data|world\.angle\.z", body))
        check("%s: guards (profile id, wait proc, request_type==0)" % name,
              prof in body and ("&%s" % wait) in body and "request_type != 0" in body)
        check("%s: guards (mDemo_Check DOOR/DOOR2/SPEAK, local door label)" % name,
              all(k in body for k in ("mDemo_TYPE_DOOR,", "mDemo_TYPE_DOOR2", "mDemo_TYPE_SPEAK", "get_door_label_proc")))
        h = rd("include", hdr)
        check("%s: prototype in %s under TARGET_PC" % (name, hdr),
              re.search(r"#ifdef TARGET_PC[^#]*int %s\(ACTOR\* actorx, int exit\);\s*#endif" % name, h) is not None)
        if name == "aHUS_pc_cosmetic_door":
            check("house: request 1/2 + NO_MOVE_WHILE_CULLED like aNPC_request_house",
                  "request_type = exit ? 2 : 1" in body and "ACTOR_STATE_NO_MOVE_WHILE_CULLED" in body)
        elif name == "aMHS_pc_cosmetic_door":
            check("my_house: request 4/5, arg2_f 1/2 (never 0 or 3), OPEN_DOOR directly",
                  "request_type = exit ? 4 : 5" in body and "arg2_f = exit ? 1.0f : 2.0f" in body and
                  "aMHS_setup_action(str, aMHS_ACTION_OPEN_DOOR)" in body and "arg2_f = 0" not in body and "arg2_f = 3" not in body)
        else:
            check("%s: request 4/2 (never 3), OPEN_DOOR directly" % name,
                  "request_type = exit ? 4 : 2" in body and "request_type = 3" not in body and "ACTION_OPEN_DOOR)" in body)

    rp = rd("pc", "src", "pc_remote_player.c")
    disp = func(rp, "static void pc_remote_player_cosmetic_door(PCRemotePlayerActor* self, int exit_row)")
    check("dispatcher: gates snap_req + local field scene + local OUTDOOR + pending structure exit",
          all(k in disp for k in ("slot->snap_req", "pc_remote_player_scene_is_local_field", "mPlayer_INDEX_OUTDOOR", "str_door_name != EMPTY_NO")))
    check("dispatcher: walks the ITEM actor list and calls all four wrappers",
          "ACTOR_PART_ITEM" in disp and "next_actor" in disp and
          all(k in disp for k in ("aMHS_pc_cosmetic_door", "aHUS_pc_cosmetic_door", "aPOFF_pc_cosmetic_door", "aNW_pc_cosmetic_door")))
    check("dispatcher: sends nothing / touches no scene", not re.search(r"pc_net_send|goto_\w*scene", disp))
    rs = func(rp, "static int pc_remote_player_row_start(")
    m = re.search(r"#ifdef TARGET_PC\n\s*if \(\(idx == \(int\)mPlayer_INDEX_DOOR \|\| idx == \(int\)mPlayer_INDEX_OUTDOOR\) && "
                  r"v->door_cosm_time != now\) \{.*?pc_remote_player_cosmetic_door\(self, idx == \(int\)mPlayer_INDEX_OUTDOOR\);", rs, re.S)
    check("row_start hook: DOOR=enter / OUTDOOR=exit, latched once per row instance", m is not None)
    check("row_start hook comes after row_start_time is set", m is not None and rs.index("v->row_start_time = now") < m.start())

    poll_fix = re.search(r"if \(slot->snap_req\) \{[^}]*slot->snapshot_count = 0;[^}]*slot->scene_accept_frame = graph_dt_frame_time\(gamePT\);", rp, re.S)
    check("part B(1): a local scene change keeps snap_req, empties the ring and re-bases scene_accept_frame", poll_fix is not None)
    check("part B(1): the unconditional 'slot->snap_req = 0' on actor re-creation is gone",
          "slot->snap_req = 0; /* the new actor is created at the newest sample" not in rp)

    if FAILS:
        print("\n%d FAILED" % len(FAILS))
        sys.exit(1)
    print("\nALL PASS")


main()
