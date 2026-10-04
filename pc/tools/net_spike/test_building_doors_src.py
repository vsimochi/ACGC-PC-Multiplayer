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
    disp = func(rp, "static void pc_remote_player_cosmetic_door(PCRemotePlayerActor* self, int exit_row, int cls, ACTOR* best)")
    check("dispatcher: gates snap_req + local field scene + local OUTDOOR + pending structure exit",
          all(k in disp for k in ("slot->snap_req", "pc_remote_player_scene_is_local_field", "mPlayer_INDEX_OUTDOOR", "str_door_name != EMPTY_NO")))
    check("dispatcher: calls all four hinged wrappers, only for PC_DOOR_HINGED with a found building",
          "cls != PC_DOOR_HINGED || best == NULL" in disp and
          all(k in disp for k in ("aMHS_pc_cosmetic_door", "aHUS_pc_cosmetic_door", "aPOFF_pc_cosmetic_door", "aNW_pc_cosmetic_door")))
    check("dispatcher: sends nothing / touches no scene", not re.search(r"pc_net_send|goto_\w*scene", disp))
    pick = func(rp, "static int pc_remote_player_door_pick(PCRemotePlayerActor* self, int exit_row, ACTOR** out_best)")
    pt = func(rp, "static int pc_remote_player_door_point(const ACTOR* a, int exit_row, float* out_x, float* out_z)")
    cls = func(rp, "static int pc_remote_player_door_class_of_scene(int scene_id)")
    check("door_pick: field scene only, walks the ITEM actor list, radius 60, nearest point wins, UNKNOWN when nothing near",
          "pc_remote_player_scene_is_local_field" in pick and "ACTOR_PART_ITEM" in pick and "next_actor" in pick and
          "DOOR_COSM_RADIUS 60.0f" in rp and "d2 < best_d2" in pick and "PC_DOOR_UNKNOWN" in pick)
    check("door_pick: exit rows prefer the interior the owner just left (prev_scene_id), entry rows are geometric",
          "slot->prev_scene_id" in pick and "if (exit_row)" in pick and "cls != want" in pick)
    check("door_point: hinged (house, my house, post office, needlework) + type-1 buildings with their entry/exit points",
          all(k in pt for k in ("mAc_PROFILE_MYHOUSE", "mAc_PROFILE_HOUSE", "mAc_PROFILE_POST_OFFICE", "mAc_PROFILE_NEEDLEWORK_SHOP",
                                "mAc_PROFILE_SHOP", "mAc_PROFILE_CONVENI", "mAc_PROFILE_SUPER", "mAc_PROFILE_DEPART", "mAc_PROFILE_MUSEUM",
                                "mAc_PROFILE_POLICE_BOX", "mAc_PROFILE_BRSHOP", "mAc_PROFILE_BUGGY", "mAc_PROFILE_KAMAKURA",
                                "mAc_PROFILE_TENT", "mAc_PROFILE_TOUDAI", "68.29f", "98.57f", "118.57f", "102.5f", "82.5f")))
    # the type-1 door request / exit positions, cross-checked against the actor sources
    for fn, needles in (("ac_shop_move.c_inc", ("world.position.x - 50.0f", "world.position.z + 50.0f", "x - 68.29f", "z + 68.29f")),
                        ("ac_conveni_move.c_inc", ("x - 25.0f", "z + 82.5f", "x - 42.00f", "z + 98.57f")),
                        ("ac_super_move.c_inc", ("x - 45.0f", "z + 102.5f", "x - 62.0f", "z + 118.57f")),
                        ("ac_depart_move.c_inc", ("x - 62.0f", "z + 118.57f")),
                        ("ac_museum.c", ("z + 100.0f", "z + 120.0f")),
                        ("ac_police_box_move.c_inc", ("x + 50.0f", "z + 50.0f", "x + 60.0f", "z + 60.0f")),
                        ("ac_br_shop_move.c_inc", ("64.0f +", "z + 100.0f")),
                        ("ac_buggy_move.c_inc", ("z + 64.0f", "z + 100.0f")),
                        ("ac_kamakura_move.c_inc", ("z + 68.0f", "z + 86.0f")),
                        ("ac_tent.c", ("68.0f +", "z + 86.0f")),
                        ("ac_toudai_move.c_inc", ("z - 60.0f", "z - 70.0f"))):
        t = rd("src", "actor", fn)
        check("type-1 door points of %s match the actor source" % fn, all(n in t for n in needles))
    check("door_class_of_scene: shops/museum/police/kamakura/buggy/lighthouse/tent -> SHOP; rooms/NPC house/post office/needlework/cottages -> HINGED",
          all(k in cls for k in ("SCENE_SHOP0", "SCENE_CONVENI", "SCENE_SUPER", "SCENE_DEPART_2", "SCENE_BROKER_SHOP", "SCENE_MUSEUM_ROOM_FISH",
                                 "SCENE_POLICE_BOX", "SCENE_KAMAKURA", "SCENE_BUGGY", "SCENE_LIGHTHOUSE", "SCENE_TENT", "return PC_DOOR_SHOP",
                                 "SCENE_MY_ROOM_BASEMENT_LL1", "SCENE_NPC_HOUSE", "SCENE_POST_OFFICE", "SCENE_NEEDLEWORK", "SCENE_COTTAGE_MY",
                                 "return PC_DOOR_HINGED")) and cls.index("SCENE_TENT") < cls.index("return PC_DOOR_SHOP") < cls.index("SCENE_NEEDLEWORK"))
    rs = func(rp, "static int pc_remote_player_row_start(")
    m = re.search(r"#ifdef TARGET_PC\n\s*if \(idx == \(int\)mPlayer_INDEX_DOOR \|\| idx == \(int\)mPlayer_INDEX_OUTDOOR\) \{.*?"
                  r"v->door_cosm_time != now\) \{.*?pc_remote_player_door_pick\(self, exit_row, &door_best\);\s*"
                  r"pc_remote_player_cosmetic_door\(self, exit_row, v->door_cls, door_best\);", rs, re.S)
    check("row_start hook: DOOR=enter / OUTDOOR=exit, pick + wrapper latched once per row instance", m is not None)
    check("row_start hook runs before the body clips are bound (it may swap them)", m is not None and m.start() < rs.index("pc_remote_player_bind_body(v, a0, a1"))
    check("row_start clip override: DOOR+SHOP INTO_S1, OUTDOOR+SHOP GO_OUT_S1 frame 1, OUTDOOR+HINGED frame 25, missing clip keeps the row clip",
          "mPlayer_ANIM_GO_OUT_S1 : (int)mPlayer_ANIM_INTO_S1" in rs and "mPlib_Get_Pointer_Animation(alt) != NULL" in rs and
          "v->door_cls == PC_DOOR_SHOP" in rs and "v->door_cls == PC_DOOR_HINGED && exit_row" in rs and "start_frame = 25.0f" in rs and
          "start_frame = 1.0f" in rs and ": start_frame;" in rs)
    check("slot.prev_scene_id is saved in on_scene before the overwrite, only on a scene id change",
          re.search(r"scene\.scene_id != scene->scene_id\) \{\s*slot->prev_scene_id = slot->scene\.scene_id;.*?slot->scene = \*scene;",
                    func(rp, "int pc_remote_player_on_scene("), re.S) is not None)

    poll_fix = re.search(r"if \(slot->snap_req\) \{[^}]*slot->snapshot_count = 0;[^}]*slot->scene_accept_frame = graph_dt_frame_time\(gamePT\);", rp, re.S)
    check("part B(1): a local scene change keeps snap_req, empties the ring and re-bases scene_accept_frame", poll_fix is not None)
    check("part B(1): the unconditional 'slot->snap_req = 0' on actor re-creation is gone",
          "slot->snap_req = 0; /* the new actor is created at the newest sample" not in rp)

    if FAILS:
        print("\n%d FAILED" % len(FAILS))
        sys.exit(1)
    print("\nALL PASS")


main()
