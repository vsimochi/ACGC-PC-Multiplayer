# GBI static-pointer spike (experimental, only used when PC_LOW_ADDRESS_64 is on).
#
# _GBI_STATIC_PTR(sym) expands to (unsigned int)(uintptr_t)(sym). GCC on x64 rejects that in a
# C static initializer ("initializer element is not constant") but accepts it in C++, where it
# becomes dynamic initialization (a constructor stores the truncated address at startup). That is
# lossless as long as the process lives below 4 GB (see pc_lowaddr.h). These files are compiled
# as C++ to test that approach without touching the data format (Gfx stays 8 bytes).
#
# Paths are relative to the decomp root. DATA files are pure data (also built by the
# pc/tools/lowaddr_spike harness); MIXED files contain code + static Gfx data and are only
# compile-tested here.
set(PC_GBI_CXX_SPIKE_DATA_FILES
    # src/data/model
    src/data/model/act_ant.c
    src/data/model/act_m_dango2.c
    src/data/model/ef_think_l.c
    src/data/model/act_m_koorogi2.c
    src/data/model/obj_shop_akican.c
    src/data/model/obj_shop_candy.c
    # src/data/field
    src/data/field/bg/acre/grd_s_m_r1_5/grd_s_m_r1_5.c
    src/data/field/bg/acre/grd_s_f_ko_2/grd_s_f_ko_2.c
    src/data/field/bg/acre/grd_s_c1_r2_2/grd_s_c1_r2_2.c
    src/data/field/field_data.c
    # src/data/npc
    src/data/npc/model/mdl/mob_1.c
    src/data/npc/model/mdl/rhn_1.c
    src/data/npc/model/mdl/cow_1.c
    # src/data/scene (m_scene.h u32-pointer tables + Gfx)
    src/data/scene/BG_TEST01.c
    src/data/scene/NEEDLEWORK.c
    src/data/scene/player_room_s.c
    src/data/scene/museum_fish.c
    # src/static/bootdata
    src/static/bootdata/gam_win1.c
    src/static/bootdata/logo_nin.c
)
# Mixed code+data files are NOT compiled as C++ any more: their static Gfx tables were split into
# src/data/pc_split/*.c (see CMakeLists.txt). Kept empty so older tooling that reads it still works.
set(PC_GBI_CXX_SPIKE_MIXED_FILES)
