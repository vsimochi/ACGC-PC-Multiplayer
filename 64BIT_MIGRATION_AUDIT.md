# 64-bit Migration Audit

Audit date: 2026-09-21. Branch `multiplayer` @ `4099d24` (Version 0.9.3).
Toolchain used: MSYS2 UCRT64, GCC 16.2.0, CMake 4.4.3, SDL2 2.32.10.

> **Latest (section 12): the real game now mounts all three archives and reaches `graph_proc`/`trademark_init`; next blocker is the jaudio bank-table layout.**
>
> **Status update (section 10): `ac_pc` now compiles (0 errors) and links as a 64-bit low-address process and launches; it has not run past the missing-ROM check.** (Section 9: earlier phase.) Original status: the project does NOT compile as 64-bit. CMake now *configures* a 64-bit build
> (`pc/build64`, opt-in via `-DPC_ALLOW_64BIT=ON`), and the build then fails in 3,378 of ~3,997
> translation units, all for the root causes documented below. Nothing here has been run.

---

## 0. Summary

| Question | Answer |
|---|---|
| Is this a "host pointer" port or an emulator with emulated memory? | **Host pointers.** The game runs on real native pointers (`malloc`'d 24 MB arena, native `Gfx*`, native vtables). There is no emulated GC address space, so the emulated-vs-host distinction is rare; the real split is *pointer stored in a `u32`* vs *genuine 32-bit data*. |
| Root causes | **12** distinct root causes (section 4). Individual instances: **572** compiler-detected pointer<->integer cast lines, **27,909** static-initializer errors (~2,200 data TUs), **9** invisible `(u32)(uintptr_t)` truncations in `pc/src`, **5** hard 32-bit guards, **2** genuine type/layout compile errors. |
| Single biggest blocker | **The GBI display-list encoding.** `Gfx` is an 8-byte command (`w0`,`w1` = two `u32`); every pointer inside a display list is packed into one 32-bit word. GCC **cannot** emit a truncated address in a static initializer on x64 (`initializer element is not constant`), and ~2,200 data files do exactly that. |
| Does `u32 = unsigned long` break on Win64? | **No, and it must not be changed.** Windows is LLP64: `long` is 32-bit, so `u32`/`s32` stay 32-bit and every `u32` field layout is unchanged. (On Linux LP64 this typedef *would* break — Linux 64-bit is a much larger job.) |
| Is the save format at risk? | **No.** `sizeof(Save_t) == 0x242A0` under x64, which equals the documented end offset in `include/m_common_data.h:173`. No pointers in saved data. |
| Realistic without a major architectural change? | **Only via the "low address space" approach** (section 5): keep every game-visible pointer below 4 GB inside a 64-bit process, so `(u32)ptr` stays lossless. Verified with a test program (section 2.3), **not yet with the game**. A "clean" LP64-style port (all pointers real 64-bit) would require widening `Gfx`/Acmd and every u32-address field: that *is* a major rewrite and is not recommended. |
| Cheaper way to unblock the toolchain problem | A standalone **i686 mingw-w64 GCC** (e.g. a WinLibs 32-bit build) keeps the project on its supported path. Not verified here. |

---

## 1. Method (so findings can be re-checked)

1. Read all build files, `pc/src/*.c`, `pc/include/*.h`, JSystem, emu64, GBI headers.
2. **Compiler-driven sweep**: every non-data source (`src/**`, `pc/src/**`, using the same defines/includes as
   `pc/CMakeLists.txt`) was compiled with `-fsyntax-only` on x86_64 with `-Wpointer-to-int-cast
   -Wint-to-pointer-cast` (C) / `-fpermissive` "loses precision" diagnostics (C++). 4,631 files. This is far more reliable
   than grep for pointer<->integer casts. **Blind spot:** it cannot see `(u32)(uintptr_t)p` (explicit two-step
   casts), so those were found by grep (section 3, A2).
3. **Real build**: `mingw32-make -k` in `pc/build64` (3,997 objects attempted) to get true error classes.
4. **Experiments** (scratch dir, repo untouched): x64 low-address linking, static-init behaviour in C vs C++, struct sizes.
5. Per-site raw data: Appendix A.

Limits: no runtime testing was possible; no 32-bit compiler is installed, so 32-bit struct sizes were not measured
(only compared against documented offsets).

---

## 2. Facts established

### 2.1 What forces 32-bit today (5 independent guards + 1 design)

| # | Where | What |
|---|---|---|
| 1 | `pc/CMakeLists.txt:18` (before this session's edit) | `FATAL_ERROR` if `CMAKE_SIZEOF_VOID_P == 8` (now opt-out via `PC_ALLOW_64BIT`) |
| 2 | `pc/include/pc_platform.h:7` | `#error` if `UINTPTR_MAX != 0xFFFFFFFF` (hit by 31 TUs) |
| 3 | `include/PR/gbi.h:34` | `_Static_assert(sizeof(void*) == sizeof(unsigned int))` (hit by ~3,383 TUs) |
| 4 | `include/libforest/gbi_extensions.h:24` | same assert |
| 5 | `src/static/libforest/emu64/emu64_utility.c:8` | `static_assert(sizeof(void*) == sizeof(u32))` |
| design | `include/PR/gbi.h:47-56`, `pc/src/pc_gbi_runtime.c` | GBI pointer packing: static ptr = `(unsigned int)(uintptr_t)(sym)`; runtime ptr = `pc_gbi_pack_runtime_ptr()` sets bit 0 as a "this is a host pointer" tag, with an 8192-entry token table for odd addresses; `emu64::seg2k0()` un-tags. |

None of the guards is "wrong" on its own: they correctly detect that the encoding cannot hold a 64-bit pointer.

### 2.2 The compiler cannot statically truncate an address (measured)

```c
Gfx g[] = { { 1, (unsigned int)(uintptr_t)foo } };   // x86_64-w64-mingw32-gcc 16.2
// error: initializer element is not constant
```
A 32-bit absolute relocation *does* work if it is emitted by the assembler with a low image base
(`.long foo` linked with `--image-base=0x400000` -> `0x407020`), but C cannot express it.
**The same initializer compiled as C++ succeeds** because it becomes *dynamic initialization*
(a `_GLOBAL__sub_I_*` constructor stores the truncated address at startup); verified on
`src/data/model/act_ant.c` (object contains `_GLOBAL__sub_I_act_ant_tex`).

### 2.3 A 64-bit process can be forced to live below 4 GB (measured)

Test program (`malloc`, `VirtualAlloc(NULL,..)`, a static, a stack variable), linked with
`-Wl,--image-base=0x400000 -Wl,--disable-dynamicbase -Wl,--disable-high-entropy-va`:

```
foo=0000000000407020 heap=0000000000744380 big(24MB)=0000000000A3F040 valloc=0000000002250000 stack=000000000060FEA8
```
Default 64-bit link: image `00007FF7...`, heap `00000247...`. With only `--disable-high-entropy-va`
heap/VirtualAlloc/stack drop below 4 GB but the image stays at `0x7FF6...`, so **the low image base is required too**.
`--disable-large-address-aware` is *not* accepted by this x64 `ld`. DLLs (SDL2, opengl32, GL driver) still load high;
the game must never store their addresses in a `u32` (see D5).

### 2.4 x64 struct sizes (measured, `sizeof` with x64 GCC)

| Type | x64 | Note |
|---|---|---|
| `Gfx` / `Vtx` / `Mtx` | 8 / 16 / 64 | pointer-free unions/structs; unchanged (Category C) |
| `Save_t` | `0x242A0` | equals documented layout -> pointer-free |
| `Private_c` | `0x2440` | |
| `ACTOR` | `0x1B0` | contains pointers -> larger than on 32-bit |
| `NPC_ACTOR` | `0xBA8` | already exceeds hard-coded slot `0xA50` (B/A5) |

---

## 3. Findings

Format per entry: **File / function / line - current code - why 32-bit dependent - real host-pointer? - fix - risk - GC/decomp-correctness impact - dependencies.**
Site counts refer to the appendix. "Decomp correctness" = risk of diverging from the original GameCube behaviour.

### Category A - Safe host-portability fixes

**A1. Hard 32-bit guards** - `pc/CMakeLists.txt:18` (DONE, opt-in), `pc/include/pc_platform.h:7`, `include/PR/gbi.h:34`, `include/libforest/gbi_extensions.h:24`, `emu64_utility.c:8`.
Current: `#if UINTPTR_MAX != 0xFFFFFFFFu #error`, `_GBI_STATIC_ASSERT(sizeof(void*)==sizeof(unsigned int))`.
Why: they encode the current design. Host pointer? n/a. Fix: keep them for the default 32-bit build; under the low-address strategy replace with a **startup runtime check** (`assert(all arenas < 4 GB)`) plus a compile-time `PC_LOW_ADDRESS_64` opt-in. Risk: Low. Decomp correctness: none. Dependencies: only meaningful together with B1/B2 and the linker flags of section 5.

**A2. Explicit `(u32)(uintptr_t)` truncations in the PC layer (invisible to warnings)** - 9 sites:
| Site | Current | Meaning |
|---|---|---|
| `pc/src/pc_aram.c:45` `ARStartDMA` | `u32 base = (u32)(uintptr_t)aram_base;` | host pointer compared with an overloaded "aram address" arg |
| `pc/src/pc_aram.c:78` `ARQPostRequest` | `callback(... (u32)(uintptr_t)req)` | host pointer smuggled as callback arg (`JKRAramPiece::doneDMA(u32)` casts back) |
| `pc/src/pc_gx.c:2104` | `pc_gx_efb_capture_store((u32)(uintptr_t)dest, ...)` | host pointer as cache key |
| `pc/src/pc_gx_texture.c:205,226` | `o[TEXOBJ_IMAGE_PTR] = (u32)(uintptr_t)image_ptr;` | pointer stored in opaque `GXTexObj` u32[8] blob |
| `pc/src/pc_gx_texture.c:619` | `tlut_ptr_key = (u32)(uintptr_t)...data` | hash key |
| `pc/src/pc_gx_texture.c:854` | `o[TLUTOBJ_DATA] = (u32)(uintptr_t)lut;` | pointer in `GXTlutObj` blob |
| `pc/src/pc_main.c:355,362` | `pc_image_base = (unsigned int)(uintptr_t)exe;` | image range for `seg2k0` |

Real host pointers: yes. Fix: a single checked helper `PC_PTR32(p)` / `PC_U32_TO_PTR(u)` (asserts `< 4 GB` in debug) used everywhere; later, widen the blob slots (2 x u32 per pointer) if the low-address strategy is abandoned. Risk: Low. Decomp correctness: none (PC layer). Dependencies: A1.

**A3. `size_t*` vs `u32*` (real compile error on Win64)** - `src/static/Famicom/famicom.cpp:730` `SetupResBanner(banner_data, dst, 0x1800, &size, &banner_fmt);` declared `static int SetupResBanner(const ResTIMG*, u8*, size_t, size_t*, u8*)` (line 684). Win64 `size_t` is 64-bit, `u32` is 32-bit -> `cannot convert 'u32*' to 'size_t*'`. Not a pointer-size issue but an LLP64 type-width one. Fix: use `size_t`/`u32` consistently at both ends (or `u32*` in the prototype). Risk: Low. Decomp correctness: none.

**A4. Fixed-slot backing too small for the actor** - `src/actor/npc/ac_npc_ctrl.c_inc:523-525,715`: `#define aNPC_ACTOR_CLASS_SLOT_SIZE 0xA50` (PC) vs `sizeof(NPC_ACTOR)==0xBA8` on x64 -> `typedef char aNPC_pc_actor_slot_backing_too_small[-1]`. (The `0x9D0`/`0xA50` slot was already grown once for PC "dt fields".) Fix: derive from `sizeof(NPC_ACTOR)` (rounded up to 16) instead of a literal; the `size > SLOT_SIZE` runtime check at line 652 already guards it. Risk: Low-Medium (the sibling overlay buffers `aNPC_n_overlay_c`, 0x800/0x2000 at lines ~720-730 need the same review). Decomp correctness: none (allocation sizes only).

**A5. Pointer-count padding** - `include/ac_npc.h:345`: `/* 0x0FC */ void* _0FC[(0x108 - 0x0FC) / sizeof(void*)];` (12 bytes = 3 pointers on 32-bit, 1 pointer on 64-bit). The offset comments in this and other pointer-containing structs are wrong on 64-bit but only matter if code depends on offsets. Fix: `[3]`. Risk: Low.

**A6. Alignment/arena arithmetic through `u32`** (host pointers, ~45 lines): `include/types.h:95-97` (`ALIGN_NEXT/ALIGN_PREV`, no casts inside - callers cast), `include/JSystem/JKernel/JKRMacro.h:5-11` (`JKR_ISALIGNED/JKR_ALIGN` cast to `u32`), `include/dolphin/os/OSUtil.h:11-12` (`OSRoundUp32B/DownB`), `src/TwoHeadArena.c:64-74`, `src/static/libc64/__osMalloc.c:9,13,92,93,194-220,287,422-452`, `src/game/m_scene.c:43,117,137,144`, `m_scene_ftr.c:17`, `m_submenu.c:642`, `m_submenu_ovl.c:1979`, `m_field_make.c:870-871,1483,1821`, `m_play.c:476`, `famicom_emu.c:29-31`, `m_player_lib.c:1048`, `m_catalog_ovl.c:1455`, `m_mail.c:19`, `ac_npc_ctrl.c_inc:750`, `JUTGraphFifo.cpp:19`, `JUTDirectFile.cpp:30`, `emu64.c:406`.
Fix: `uintptr_t` variants of the helpers. Zero semantic change (same bit math). Risk: Low. Decomp correctness: none.

**A7. Pointer differences via `int`** - `include/graph.h:291` (`(int)tail_p - (int)size`), `src/game/m_debug_mode.c:740`, `src/TwoHeadArena.c:74`, `boot.c:623` (`(u32)base - (u32)basenext`), `JKRHeap.cpp:73`, `jsyswrap.cpp:493`. Fix: `(char*)a - (char*)b`. Risk: Low.

**A8. `OSMessage` (`void*`) used as an integer payload** - `JFWDisplay.cpp:331-332` (`(int)msg - (int)nextCount`), `JKRDvdFile.cpp:114,118`, `src/irqmgr.c:145` (`switch ((u32)msg)`), `src/static/initial_menu.c:404,428`, jaudio `system.c:607`. Fix: `(intptr_t)`/`(uintptr_t)`. Risk: Low.

**A9. Pointers printed as `%08x`** - `boot.c:780`, `__osMalloc.c:583`, emu64/jaudio `Printf0`/`OSReport` calls. Fix: `%p` or `PRIxPTR`. Risk: Low (diagnostics only). (Not exhaustively enumerated; needs a `-Wformat` pass once things compile - D3.)

### Category B - Needs careful migration

**B1. Static pointer initializers in GBI data (the dominant blocker)** - `include/PR/gbi.h:1934,1950,1964,2000,2561-2647`, `include/libforest/gbi_extensions.h:1106,1112` (`_GBI_STATIC_PTR(x)` = `(unsigned int)(uintptr_t)(x)`), used by ~2,200 files in `src/data/{model,field,npc,scene,item,font}` and `src/static/bootdata`. Real build: **27,909 `initializer element is not constant`** errors, whose macro expansions point at `gbi_extensions.h` (~25,000), `gbi.h` (~2,700) and `m_scene.h` (~370).
Why: `Gfx.words.w1` is 32-bit; GCC/x64 cannot relocate-truncate. Real host pointer: yes.
Options (choose one, see section 5):
 1. Compile the data TUs as C++ (dynamic init). Proven on 1 file; needs `gfxprint.h:91-93` (`this` used as a parameter name) fixed for files that include it (`src/data/field/field_data.c`, `src/data/npc/*_list.c` failed in a C++ syntax check). Depends on all data addresses being < 4 GB (section 2.3). Effort: low-medium. Risk: Medium (D2).
 2. Generator/post-pass emitting `.long sym` via assembler for each pointer word. Effort: high.
 3. Widen `Gfx` to 16 bytes with a 64-bit `w1`. Touches every GBI bit-packed struct, `sizeof(Gfx)` (8 hits + 49 `w1` uses in `emu64.c`) and every raw DL offset (`SEGMENT_ADDR` byte offsets, `anime_6_mdl` etc.). **Major; not recommended.**
Decomp correctness: options 1/2 do not change command semantics.

**B2. Runtime GBI pointer packing + segment resolution** - `pc/src/pc_gbi_runtime.c` (all), `gbi.h:57-64`, `src/static/libforest/emu64/emu64_utility.c:8-58` (`emu64::seg2k0`), `include/libforest/emu64/emu64.hpp:750-751` (`u32 segments[]`, `u32 DL_stack[]`), `pc_main.c:40-41,355-376` (`pc_image_base/end` as `unsigned int`).
Current: `_GBI_RUNTIME_PTR(s)` -> `pc_gbi_pack_runtime_ptr((uintptr_t)(s), ...)` returns `unsigned int` (`addr | 1`); `seg2k0(u32)` returns `u32`.
Why: 32-bit words hold host pointers with bit 0 as tag; segment bases and the DL call stack are stored as `u32`. Real host pointers: yes. Fix (low-address strategy): keep; add range asserts in `pack` and `seg2k0`. Fix (widened strategy): full redesign. Risk: High (rendering core). Decomp correctness: none if only asserts are added.
Also `seg2k0`'s heuristic (`segadr < 0x03000000` or `segadr>>28 != 0` => raw pointer; else image range; else segment) was tuned on a 32-bit address layout; with a low image base the exe image and low heap can sit in `0x00400000-0x0FFFFFFF`, the same window as N64 segments -> D8.

**B3. emu64 texture/TLUT/matrix/DL address handling** - `emu64.c` 28 lines, e.g. `dirty_check` :3369 (`((u32)img_addr & 0x1F)`), `dl_G_DL` :3486 (`DL_stack[...] = (u32)(this->gfx_p + 1)`), :3492/3495 (`(int)this->work_ptr - sizeof(Gfx)`), `dl_G_LOADTLUT` :3837-3922 (`tlut_addr`, `tlut_addresses[]`), `dl_G_MTX` :4481 (`disp_matrix((MtxP)seg2k0(gfx_copy.w1))`), :3678/3717/3749 (`texture_info[].img_addr`, `tmem_map[].addr` from `imgaddr`/`dram` u32), :406 `texture_cache_alloc`.
Why: addresses flow through `u32` fields (`now_setimg.setimg2.imgaddr`, `dma.addr`, `movemem->data`). Real host pointers: yes. Fix: low-address strategy: mark with `PC_PTR32/PC_U32_TO_PTR` helpers (A2). Risk: High. Decomp correctness: none if lossless. Dependencies: B1, B2.

**B4. Scene data tables carry pointers in `u32` words** - `include/m_scene.h:170,175,180,185,190,199` (`mSc_SCENE_DATA_*` macros: `(u32)actor_data_p`, `(u32)bank_list_p`, ...), ~51 `src/data/scene/*.c` files; consumed in `src/game/m_scene.c`.
Why: entries are 4 x `u32` records with a pointer in the last word; static-init error counted in B1 (374). Real host pointer: yes. Fix: same as B1 (C++ dynamic init) *or* `uintptr_t` word + consumer stride change. Risk: Medium. Decomp correctness: consumer logic unchanged in option 1.

**B5. JSystem (JKernel/JUtility): 75 lines** - the "JKRHeap crash" already documented in `pc/DOCUMENTATION.md:366`.
 - `include/JSystem/JKernel/JKRExpHeap.h:46` `getBlock(): (CMemBlock*)((u32)data + -0x10)`. **`-0x10` hard-codes `sizeof(CMemBlock)==0x10`** (u16,u8,u8,int + 2 pointers). With 8-byte pointers `sizeof(CMemBlock)==0x20`. Every heap operation (`JKRExpHeap.cpp` :77 `create`, :160/:220 `allocFromHead`, :302 `allocFromTail`, :435 `do_resize`, :555 `check`, :702 `joinTwoBlocks`, :840 `genData`, :896 `state_register`) adds `(u32)ptr` arithmetic and assumes the header size. Fix (low-address strategy): keep pointers as `u32`-castable but replace `-0x10` with `-(int)sizeof(CMemBlock)`; alternatively store `mPrev/mNext` as `u32` handles to keep the 16-byte header (preserves 16-byte content alignment and GC heap semantics). Risk: **High** (this is the allocator). Decomp correctness: allocator behaviour must be identical (alignment, block splitting) -> test with heap dumps.
 - `JKRHeap.cpp` `initArena` :62-63 (`arenaHi = (u8*)OSRoundDown32B(arenaHi)`), :73 (`*outUserRamSize = (u32)arenaHi - (u32)arenaLo`), `dispose_subroutine(u32,u32)` :217, `dispose(void*,u32)` :234-241; `jsyswrap.cpp:493 JW_Init` (`SystemHeapSize = (u32)arena_hi - (u32)arena_lo - 0xD0`).
 - ARAM path: `JKRAram.cpp` :88,145,159,196-197,226,253,459,481; `JKRAramPiece.cpp` :97-108 (`(u8*)cmd->mDestination` where `mDestination` is a `u32` that is either an ARAM offset *or* a host pointer); `JKRAramStream.cpp` :99,111,125,202; `JKRDvdAramRipper.cpp` :80,154,372-401; `JKRDvdRipper.cpp` :51,108; PC side `pc/src/pc_aram.c` `ARStartDMA(u32 type, u32 mram_addr, u32 aram_addr, u32 length)`.
   **Dual-use `u32`:** the same field is a genuine ARAM offset (Category C) or a host pointer (B). `ARStartDMA` disambiguates by range (`aram_addr >= base && < base+size`). Fix: introduce distinct types (`u32 aram_addr`, `uintptr_t mram_addr`) at the `AR*`/`ARQ*` boundary; keep `JKRAMCommand.mSource/mDestination` as `uintptr_t`. Risk: Medium-High.
 - Archives: `JKRCompArchive.cpp` :98,111-113,147,162-164,191,255-263; `JKRMemArchive.cpp` :26,87,106; `JKRArchivePub.cpp:85` (`check_mount_already((s32)p1, heap)`): `(u32)mArcInfoBlock + node_offset`. The *offsets* (`node_offset`, `file_data_offset`) are file-format 32-bit values (Category C); only the base pointer needs widening. Fix: `(u8*)mArcInfoBlock + offset`. Risk: Low-Medium.
 - Threads/console: `JKRThread.cpp:17,28` (stack pointer arithmetic), `JUTException.cpp:231,243,288,613-614`, `JUTVideo.cpp:161`, `JUTProcBar.cpp:235` (`(u32)param_0 - 0x80000000`, Category C/dead), `JUTGraphFifo.cpp:19`, `JUTDirectFile.cpp:30`.

**B6. jaudio_NES: 191 lines** - `src/static/jaudio_NES/internal/system.c` (81), `driver.c` (22), `rspsim.c` (15), `cmdstack.c` (14), `heapctrl.c` (13), `memory.c` (10), `dummyrom.c` (8), `sub_sys.c` (7), `include/jaudio_NES/audiocommon.h:100,108,116,166,973` (Acmd macros `_a->words.w1 = (u32)(dst)`), `game/game64.c_inc:1807`, `internal/jammain_2.c:2615`, `internal/heapctrl.c:45-78` (`ARQPostRequest(&request,(u32)&msgQueue,...)`), `driver.c:338-521` (`(s32)&del_p->left_reverb_buf[...]`), `system.c:573` (`header->entries[i].addr += (u32)data;` relocates an on-disc offset into a host pointer stored in a u32 field of a file-layout struct), `system.c:858,869` (`return (u32)ram_p;`).
Why: the audio engine is an N64-style ABI-list generator: 8-byte `Acmd` words hold addresses, and sample banks/wave headers are patched in place with host addresses. Mixed: on-disc offsets (C) vs runtime addresses (B). Real host pointers: yes for buffers/DMA addresses. Fix: low-address strategy: helper-macros only; otherwise widen `Acmd` words and the patched header fields (struct layout of file-format headers must stay 32-bit -> needs separate "runtime pointer" side tables). Risk: **High** (audio thread; hard to unit-test). Decomp correctness: byte-exact ABI list layout matters if the DSP/`rspsim.c` is fed those words.

**B7. Actors used "pointer as identity" labels** - `src/actor` + `src/effect` **113** lines, `src/game` **70**, `src/furniture` 43, `src/bg_item` 1. Patterns:
 - Sound-source IDs: `sAdo_OngenPos((u32)actorx, NA_SE_..., &pos)` (dozens: `ac_bee.c:356`, `ac_ins_*.c`, `ef_*.c`, `ac_museum_insect*.c_inc`, `ac_my_room*.c`, `npc/*`, `m_player_sound.c_inc:336`, `bg_item_common.c_inc:538`, `ac_hnw_common.c:345-415` `sAdo_RhythmStart((u32)ftr_actor,...)`, `m_train_control.c:71-72`), including **function addresses as IDs**: `ac_field_draw.c:284,292`, `ac_misin.c:293` (`(u32)&Bg_Draw_Actor_move`).
 - Player "labels": `u32 label` request fields and `mPlib_Get_item_net_catch_label()` returning `u32`, compared as `label == (u32)actorx` (`ac_ins_*.c`, `ac_ant.c:64-90`, `ac_bee.c:170-231`, `m_player_lib.c:1676,1701,2626`, `m_player_common.c_inc:2572,4554,6831,7070-7876`, `m_player_main_*_net.c_inc`), cast back to `(ACTOR*)label` (`m_player_main_swing_net.c_inc:255-360`).
 - Generic actor args: `int arg1..arg3` holding pointers (`ac_house.c:37-42`, `ac_train0.c:55-58`, `ac_train0_move.c_inc:6-166`), `mSM_open_submenu_new2(..., (int)mscore_ctrl->melody)` (`ac_mscore_control.c:77,274`), `m_mscore_ovl.c:270`.
Why: PPC pointers are 32-bit so they double as IDs. Real host pointers: yes, but they are runtime-only (not saved) and only compared for identity or cast back. Fix (low-address): unchanged/lossless (`(u32)ptr`). Fix (real 64-bit): change the fields/params to `uintptr_t` (struct-local types: `label`, `arg1-3`, `sAdo_*` id params, audio-side id tables). Risk: Medium (many files, but mechanical). Decomp correctness: identity comparison unchanged. Dependencies: the audio side (`sAdo_*` -> jaudio, B6) must accept the wider ID.

**B8. OS/arena layer** - `pc/src/pc_os.c:236-248` (`arena_memory = malloc(PC_MAIN_MEMORY_SIZE)`; `OSGetArenaLo/Hi`), `:341-345` (`OSPhysicalToCached`, `OSCachedToPhysical` return `(u32)(ptr - arena_memory)` - an *offset*, Category C), `:439-446` (`OSInitAlloc` already uses `uintptr_t`), `src/static/libc64/__osMalloc.c` (21 lines, and `OSMemBlock` header size feeds payload alignment), `src/TwoHeadArena.c`, `src/game/m_play.c:476`.
Why: the whole runtime allocator sits on one `malloc`; under the low-address strategy that is only *probably* below 4 GB. Fix: allocate the arena (and ARAM, texture caches, `THA`/game heaps) with `VirtualAlloc` at an explicit low address range, and assert. Risk: Medium. Decomp correctness: none.

**B9. NES emulator layer (Famicom)** - 18 lines: `famicom.cpp:819,1202,2047,2107`, `famicom_nesinfo.cpp:553,966`, `ks_nes_draw.cpp:132-277` (`(s32)`/`(u32)` of `u8*` pixel/palette pointers), plus A3. Real host pointers, image/palette buffers. Fix: `uintptr_t`. Risk: Low-Medium (one game mode; emulator core `pc/lib/fixnes` is separate and clean of these casts). Note `ks_nes_core.cpp:4964,5239` `sizeof(void*)` sits inside PPC asm text excluded on PC.

### Category C - Must remain 32-bit (do NOT widen)

| Item | Where | Why it must stay |
|---|---|---|
| `typedef unsigned long u32; typedef long s32;` | `pc/include/pc_types.h:11`, `include/PR/ultratypes.h:39` | LLP64: already 32-bit on Win64. Changing to `uintptr_t` would corrupt every struct layout. |
| `Gfx` (8 B), `Vtx` (16 B), `Mtx` (64 B), `Acmd` words | `gbi.h`, `abi.h`, `mbi.h` | Binary formats; bit-packed non-address words (`words.w0`, `_SHIFTL(...)` fields, opcodes, colours, tile params). Only the *address* word is B. |
| N64 segment addresses `SEGMENT_ADDR(seg, off)` | `gbi_extensions.h` (`anime_*`, `softsprite_mtx` macros) | Encoded `seg<<24 | offset` by design; resolved through `segments[]`. |
| Save data | `Save_t` (0x242A0), `Private_c`, GCI header, `pc/src/pc_save_bswap.c`, `pc_m_card.c:375-383` (`pc_checksum_be`) | Pointer-free, byte-swapped, Dolphin-compatible. |
| Disc / ROM offsets and sizes | `pc/src/pc_assets.c` (e.g. `pc_load_asset("...", buf, 0x690, 0x9D0508, 0, 2)`), `pc_disc.c`, `pc_dvd.c:137,171` (`(u32)ftell`, `base + (u32)offset`), CISO block map, GCM FST, Yaz0 sizes, `JKRArchive` `node_offset`/`file_data_offset` | 32-bit fields of the file formats. |
| ARAM addresses | `pc/src/pc_aram.c` (`ARAlloc`, `ARStartDMA aram_addr`, `PC_ARAM_SIZE`) | Genuine emulated 16 MB ARAM *offsets* (0-based). Only the MRAM argument is a host pointer. |
| Physical addresses | `OSCachedToPhysical/OSUncachedToPhysical` return `(u32)(ptr - arena_memory)` | An offset within the 24 MB arena. |
| GC hardware constants | `pc_platform.h:44` (`GC_BUS_CLOCK` etc.), `PC_MAIN_MEMORY_SIZE` | Emulated hardware values. |
| GC absolute address literals | `m_debug_hayakawa.c:364,369` (`*(u8*)(reg*0x100000 - 0x80000000)`), `JUTProcBar.cpp:235`, `OSMemory.c` `Config24MB` | `0x80000000` is the PPC cached-memory base. Debug/dead on PC: leave, or `#ifndef TARGET_PC` them out. |
| IDs / flags / bitfields | `mActor_name_t` (u16), `PC_NOOP_WIDESCREEN_*` (0xAC5701/0xAC5700 in NOOP words), item/scene/effect IDs, `HREG` regs, `_SHIFTL` fields, `PC_GBI_ODD_PTR_TOKEN_BASE 0x02F00000` token values inside the 32-bit `w1` | Not addresses. |
| Checksums, hashes, RNG | `pc_gx_texture.c:92-94` (FNV-1a `u32`), `pc_bswap.h`, libc64 `qrand` | Deliberately 32-bit. |

### Category D - Unknown / needs runtime validation

| # | Item | What to validate |
|---|---|---|
| D1 | Do **all** game-visible allocations land below 4 GB with the section-2.3 linker flags? | arena, ARAM, JKR heaps, texture cache (`emu64.c:406`), `m_fbdemo.c:133 malloc`, GX caches. Add a startup assertion; log the max address. |
| D2 | C++ compilation of data TUs (B1 option 1) | `const` arrays get internal linkage in C++ unless `extern`; static-init order (only addresses are stored, so should be benign); `gfxprint.h` `this` identifier; designated/out-of-order initializers under `-fpermissive`; startup cost (28k stores: expected negligible). |
| D3 | `printf`-family format specifiers | `-Wformat` pass in `pc/src` once it compiles (`%lu` with `size_t`/`u32`, `%08x` with pointers). |
| D4 | Dolphin type stubs | `OSContext {u8 _pad[1024]}`, `OSMessageQueue`/`msgArray` (`void*`), `OSThread`, `DVDFileInfo`, `CARDFileInfo`, `GXTexObj` sizes in `pc/src/pc_os.c`, `pc_stubs.c`, `pc_stubs_cpp.cpp` - not examined line by line. |
| D5 | Pointers from DLLs | SDL2/GL objects live above 4 GB; confirm none are cached in `u32` (found none, but not runtime tested). |
| D6 | Audio thread | `pc_audio.c:118` `(s16*)(uintptr_t)addr` (AI DMA address as `u32`) - fine only if buffers are < 4 GB. |
| D7 | `seg2k0` heuristic on the new layout | see B2/D8. |
| D8 | Image-range vs segment collision | with image base `0x400000` and a large BSS the exe can extend past `0x03000000`; `pc_image_base/end` handles the image but untagged static pointers in low heap `[0x03000000,0x0FFFFFFF]` would be mistaken for segments. Untested. |
| D9 | Token table fallback | `pc_gbi_pack_runtime_ptr` odd-alignment path (8192 slots) was only ever exercised on 32-bit. |
| D10 | Struct-size-dependent hard-coded constants beyond A4/A5 | `sizeof(ACTOR)`=0x1B0, `NPC_ACTOR`=0xBA8 etc. changed; offsets in comments are not enforced anywhere (only 25 `static_assert`/`offsetof` uses repo-wide, none on these structs), so silent mismatches are possible in any code that memcpy's/overlays structs by literal size (e.g. `ACTOR` class-size tables, `Clip_c` `m_clip.c:7` iterates `sizeof(Clip_c)/sizeof(void*)` which is safe). |

---

## 4. Root-cause list (12)

1. GBI static pointer initializers (B1) - 27,909 sites / ~2,200 TUs
2. GBI runtime pointer packing + `seg2k0` + `segments[]`/`DL_stack` (B2, B3)
3. Scene data `u32`-pointer tables (B4)
4. JKRExpHeap/CMemBlock size + JKRHeap arena (B5)
5. ARAM/MRAM dual-use `u32` (B5, A2)
6. JKR archive / thread / JUT casts (B5)
7. jaudio_NES address words (B6)
8. Actor identity labels/args (B7)
9. Arena/OS allocator (B8, A6)
10. Famicom (B9, A3)
11. Fixed-size slots / pointer-count padding (A4, A5)
12. Build/guards (A1) + printf/format (A9)

## 5. Strategy recommendation

**Strategy L ("low address space")** - a 64-bit process in which every game pointer is < 4 GB:
1. Link flags `-Wl,--image-base=0x400000 -Wl,--disable-dynamicbase -Wl,--disable-high-entropy-va` (section 2.3).
2. Runtime guard at startup + checked `PC_PTR32/PC_U32_TO_PTR` helpers; arena via `VirtualAlloc` in a low range.
3. Compile the data TUs that use static GBI/scene pointers as C++ (dynamic init), or generate them.
4. Fix the true type/layout errors (A3-A5, `CMemBlock`).
5. Remaining ~570 `(u32)ptr` lines become *lossless by construction*; convert them opportunistically to `uintptr_t` (A6-A9, B7) rather than in one pass.

Pros: no change to GBI encoding, save layout, or asset pipeline; the code that matters for gameplay (actors) is barely touched. Cons: not a "true" 64-bit address space; a low-memory allocation failure is fatal (guarded by asserts); ~40 MB of game memory is far below the 4 GB budget so this is comfortable.

**Strategy W ("wide")**: all pointers real 64-bit. Requires `Gfx`/`Acmd` widening (B1 option 3) and rewriting the JKR allocator, audio ABI, scene tables, and every actor label. Weeks of work with high regression risk. Not recommended unless a true >4 GB address space is required (it is not for multiplayer networking).

**Toolchain shortcut**: install an i686 mingw-w64 GCC and build 32-bit; gets development moving today, decoupled from this migration.

## 6. Build-system analysis (Phase 3)

| Question | Finding |
|---|---|
| Existing 64-bit path in CMake? | **No.** Only a `FATAL_ERROR`. Nothing else in `CMakeLists.txt` is 32-bit specific once that is passed: no `-m32`, no i686 flags; `-m32` exists only in `pc/cmake/Toolchain-linux32.cmake`; `Toolchain-mingw32.cmake` is an i686 cross file. Flags are `-O2 -fno-strict-aliasing -fwrapv` (+ `-w -fpermissive` for decomp code). |
| SDL2 | **Works.** `find_package(SDL2)` falls to the UCRT64 CMake config: `SDL2 include: C:/msys64/ucrt64/include;.../SDL2`, `SDL2 libraries: SDL2::SDL2main;SDL2::SDL2`. The hard-coded fallbacks (`C:/msys64/mingw32`, `../SDL2-2.30.10/i686-w64-mingw32`, `lib/SDL2/lib/libSDL2.a`) are 32-bit-only and unreachable here. Linking against SDL2 2.32.10 and the `SDL2.dll` copy step (`$ENV{MINGW_PREFIX}/bin`) are **untested** (no link yet). |
| MinGW Makefiles appropriate? | **Yes.** `mingw32-make` is just the name of the tool; it ran the 3,997-object build fine from UCRT64. (Ninja would work equally but was not tried.) `MSYSTEM=UCRT64` bash + `-G "MinGW Makefiles"` is what I used. |
| `build32` naming | **Historical.** Referenced only by `build_pc.sh` (lines 10-11,21,46,48), `pc/DOCUMENTATION.md`, `pc/tools/gen_runtime_assets.py:42` (`RUNTIME_BIN_DIR`, fallback `.bin` assets), `pc/tools/gen_shader_seed.py` docs, and `.gitignore`. No C/C++ code depends on it. |
| winpthread stripping | Works: `libwinpthread.a` found through `CMAKE_C_IMPLICIT_LINK_DIRECTORIES` (`C:/msys64/ucrt64/lib`); the `/mingw32/lib` hint is irrelevant. |
| C runtime | UCRT64 uses the UCRT, the old MINGW32 flow used msvcrt: subtle behavioural differences possible (`printf` `%lu`, locale, `mktime`, file I/O). Not examined. |
| `build_pc.sh` | Still says `pc/build32`, "MINGW32", `mingw32-make`, and configures only if no `Makefile` exists. It does not pass `-DPC_ALLOW_64BIT=ON`; **not modified** beyond your UCRT64 shell check. |
| Stale cache | `pc/build32/CMakeCache.txt` was produced by your earlier 64-bit configure attempt. If a real 32-bit toolchain is installed later, delete `pc/build32` first. |
| Loose files | Two empty untracked files `Appending` and `Updating` sit in the repo root (created 2026-09-21 04:43, before this session). Left alone. |

## 7. What was changed (Phase 4)

| File | Change | Why |
|---|---|---|
| `pc/CMakeLists.txt` (lines ~15-38) | Added `option(PC_ALLOW_64BIT ... OFF)`. With it ON a 64-bit configure emits a WARNING instead of `FATAL_ERROR`; default behaviour unchanged. Updated the error text (MINGW32 no longer exists in MSYS2). | Minimum change to let CMake configure a 64-bit build for testing. |
| `.gitignore` | Added `build64/`. | New build dir would otherwise show as untracked. |
| `64BIT_MIGRATION_AUDIT.md` | This file. | Deliverable. |

Not changed: `pc_platform.h`, `gbi.h`, `gbi_extensions.h`, `emu64_utility.c` guards (deliberately left failing so the build stops at the real blockers), `build_pc.sh`, any gameplay/actor/NPC/save/camera/input code.

Reproduce:
```bash
# from an MSYS2 UCRT64 shell
mkdir -p pc/build64 && cd pc/build64
cmake .. -G "MinGW Makefiles" -DPC_ALLOW_64BIT=ON     # succeeds
mingw32-make -k -j$(nproc)                            # fails: see below
```

### First compile errors in `pc/build64` (3,997 objects attempted, 3,378 failed)

| Count | Error | Where |
|---|---|---|
| 3,383 | `static assertion failed: "GBI pointer packing requires 32-bit pointers"` | `gbi.h:34`, `gbi_extensions.h:24` (nearly every TU) |
| 31 | `#error "This project must be compiled as 32-bit"` | `pc_platform.h:8` (all of `pc/src/*.c`) |
| 27,909 | `initializer element is not constant` | GBI macros in `src/data/**` (model 2,073 TUs failing, npc 320, field 307, scene 51, ...) |
| 1 | `static assertion failed: seg2k0 pointer resolution requires 32-bit pointers` | `emu64_utility.c:8` |
| 1 | `size of array 'aNPC_pc_actor_slot_backing_too_small' is negative` | `src/actor/npc/ac_npc_ctrl.c_inc:715` |
| 1 | `cannot convert 'u32*' to 'size_t*'` | `src/static/Famicom/famicom.cpp:730` |

The ~620 objects that did compile do not include `gbi.h`. Because these errors mask each other, more will appear once
the guards are lifted; the -Wpointer-to-int-cast sweep (Appendix A) predicts them as *warnings* that become silent runtime truncation unless Strategy L's low-address guarantee holds.

## 9. Low-address experiment (follow-up phase) - results

Date: 2026-09-21. Everything below was measured; "not tested" means not tested. **The game was never started**: `ac_pc` still
does not link (section 9.6), so the runtime validation is of the diagnostic + a harness that allocates like the game does,
not of the game itself.

### 9.1 What was added

| Item | Where | Notes |
|---|---|---|
| `PC_LOW_ADDRESS_64` mode | `pc/CMakeLists.txt` | Set by `-DPC_ALLOW_64BIT=ON`. Adds the define and, **on `ac_pc` only** (not glad/SDL2), `-Wl,--image-base=0x400000 -Wl,--disable-dynamicbase -Wl,--disable-high-entropy-va` (the last is redundant, see 9.2). Default build unchanged (still hard 32-bit). |
| Guards relaxed under that define | `pc_platform.h:7`, `gbi.h:34`, `gbi_extensions.h:24`, `emu64_utility.c:8` | Only the four 32-bit guards; wrapped in `#ifndef PC_LOW_ADDRESS_64`. |
| Diagnostic | `pc/include/pc_lowaddr.h`, `pc/src/pc_lowaddr.c` | `PC_LOWADDR_CHECK` (record + validate a range), `PC_LOWADDR_ASSERT` (hot path), `PC_PTR32` (checked pointer->u32). No-ops in a 32-bit build (verified to compile in both modes). |
| Hooks | see 9.3 | Check-only; no behaviour change. |
| GBI spike | `pc/cmake/GbiCxxSpike.cmake` + `set_source_files_properties` in `CMakeLists.txt` | 19 data files + 4 mixed code/data files compiled as C++ with `-Dthis=this_arg`. |
| Harness + verifier | `pc/tools/lowaddr_spike/{lowaddr_spike.cpp,verify_spike.py}` | Not part of the normal build. `python pc/tools/lowaddr_spike/verify_spike.py`; output in `pc/build64/lowaddr_spike/report.txt`. |
| Fixes | `ac_npc_ctrl.c_inc`, `famicom.cpp`, `pc_assets.h` | 9.5. |

### 9.2 Which linker flags are actually required (test program: x64 UCRT64, 64 MB `.bss`, 64 x 16 MB mallocs, 3 runs each)

| Flags (`-Wl,` omitted) | Image | malloc heap | Stack | Result |
|---|---|---|---|---|
| none | `0x7FF7...` | `0x020F...` (high) | `0x00A3...` (high) | everything above 4 GB |
| `--image-base=0x400000` | `0x00AC7020` (random low address) | `0x014E...` (high) | `0x0022...` (high) | **heap/stack still high** |
| `--image-base=0x400000 --disable-dynamicbase` | `0x00407020` (fixed) | `0x0468...` | `0x0460FE88` | **all < 4 GB, deterministic** |
| `--image-base=0x400000 --disable-high-entropy-va` | `0x00117020` (random low address) | `0x0473...` | `0x045F...` | all < 4 GB, image address varies per run |
| all three | `0x00407020` (fixed) | `0x0475...` | `0x0460FE88` | same as row 3 (`DllCharacteristics` = `0x100`: ASLR and HEVA both off) |
| `--disable-dynamicbase --disable-high-entropy-va` (no `--image-base`) | `0x140007020` | low | low | **image above 4 GB** |

In all low variants the 64 x 16 MB allocation loop also stayed below `0x47000000`.

Conclusion: a **low `--image-base` is required**, and it must be combined with **either** `--disable-dynamicbase` **or**
`--disable-high-entropy-va` (with ASLR off, HEVA is moot; with HEVA off, the image just relocates to a random low address).
The minimal deterministic set is therefore `--image-base=0x400000 --disable-dynamicbase`; `--disable-high-entropy-va` is redundant
with it. All three are kept in CMake as requested (harmless, and it keeps the range guarantee if someone re-enables ASLR).
No other flag was needed; `--disable-large-address-aware` is rejected by this x64 `ld`.

### 9.3 Runtime validation

Hooks (all through `PC_LOWADDR_CHECK/ASSERT/PTR32`; compiled successfully into the real `ac_pc` objects):

| Category | Where |
|---|---|
| executable image, image base (`pc_image_base` no longer truncates silently), main/thread stacks, process heap, malloc probes (1 B / 64 KB / 24 MB), thread stack | `pc_lowaddr_init()` called from `main()` |
| OS arena | `pc_os.c` `OSInit`, `OSAllocFromHeap` |
| ARAM host buffer; ARQ request pointer | `pc_aram.c` |
| JKR arena and every JKR heap | `JKRHeap::initArena`, `JKRHeap::JKRHeap` |
| game heap (`__osMalloc`) | `__osMallocAddBlock` |
| emu64 instance and both texture caches | `emu64::emu64_init` |
| GBI runtime display-list pointers (no silent truncation) | `pc_gbi_pack_runtime_ptr` |
| GX texture/TLUT objects, EFB copy dest | `pc_gx_texture.c`, `pc_gx.c` |
| actor allocations, actor overlay buffers | `m_actor.c` |
| asset source images | `pc_disc.c` (DOL). Runtime asset destinations are static arrays in the image, covered by the image check. |

Harness (loads SDL2 + a GL 3.3 core context on the NVIDIA driver, **then** allocates like the game). Result: **0 violations**.

| Region | Lowest .. highest end |
|---|---|
| executable image | `0x00400000 .. 0x00AF0000` |
| main thread stack / CreateThread stack / SDL thread stack | `0x00CEF4DC` / `0x00303FF4C` / `0x04C23FEAC` |
| process heap | `0x00E90000` |
| OS arena (24 MB), ARAM buffer (16 MB) | `0x04C04E040..0x04D84E040`, `..0x04E85A040` |
| JKR-style heaps, actor-sized allocs, `operator new`, `std::vector` growth (to 128 MB), `VirtualAlloc(NULL)` | all `< 0x05F303040` |
| emu64 texture caches (static image data) | `0x0043A020..0x0073A020` |

- **Headroom:** after all of the above, plain `malloc` handed out another **2,496 MB below 4 GB** before crossing (cap 64 MB blocks). The game's footprint is on the order of 100 MB plus textures, so the budget is comfortable. A *single* large request can still land above 4 GB: a 2 GB `std::vector` returned `0x00000000E0015040` and the diagnostic fired as designed (`VIOLATION category="std::vector growth" addr=0x00000000e0015040 size=0x80000000`, then abort; `PC_LOWADDR_NONFATAL=1` = survey mode). This was a real earlier run of the harness.
- **Can be kept below 4 GB (verified):** the executable image (code, `.data`, `.bss`, static texture buffers), the CRT/`malloc`/`operator new` heap, `HeapAlloc`, `VirtualAlloc(NULL)`, and the main and CreateThread/SDL thread stacks.
- **Cannot be kept below 4 GB:** DLL images: 72 of 73 loaded modules are above 4 GB (SDL2.dll, libwinpthread, `nvoglv64.dll`, the whole Windows system DLL set). Also anything a DLL/driver allocates internally (GL driver buffers, WASAPI/DirectInput objects). The game does not store those in `u32` (`glMapBuffer*` is not used; no SDL/GL handle is cast to `u32` in `pc/src`), but that is by grep, not by running the game.

### 9.4 GBI spike (`_GBI_STATIC_PTR` compiled as C++)

19 representative data files (6 `src/data/model`, 4 `field`, 3 `npc`, 4 `scene`, 2 `static/bootdata`) + 4 mixed code/data files (`ac_mbg.c`, `ac_haniwa.c`, `m_choice.c`, `dvderr.c`).

| Question | Result |
|---|---|
| Does compilation succeed? | **Yes**: 19/19 data files and 4/4 mixed files compile as C++ (0.26 s per file), including the scene files with `m_scene.h` `(u32)ptr` tables. Same source compiled as C fails (2-73 errors per file). |
| Needed source changes? | **None to the data files.** Needs `-Dthis=this_arg`: decomp C headers use `this` as a parameter name (`gfxprint.h`, `m_fbdemo_fade.h`, ...; renaming header-by-header is whack-a-mole). One header fix: `pc_assets.h` lacked `extern "C"` (C++-compiled files referenced a mangled `pc_load_asset`); fixed. |
| Is the data correct? | **Yes.** Each data symbol was dumped after startup and compared against a reference compiled with `_GBI_STATIC_PTR(s)=0` (all 19 files; C where possible, else C++). 9,992 32-bit words: 9,807 identical; 101 differ where the reference was 0 and the spike word equals the address (+offset) of a real linked symbol; 8 are N64 segment addresses (`0x0Sxxxxxx`, correctly left as integers); 76 are non-zero in both builds and resolve to the same target symbol; **0 unexpected**. Sizes and external linkage of all 63 global data symbols are identical to C (no `const` linkage loss in these files; C++ mangles file-local statics as `_ZL...`, harmless). |
| Initialization order problems? | **None found.** The stores are addresses of other objects (link-time constants), independent of any other dynamic value, so cross-TU order does not matter. Not proven for every possible static constructor in the game (none of the 4,600 files was audited for constructors that read these tables). |
| Startup cost? | Spike: `.text.startup` 2,768 bytes for 101 pointers; whole harness first-constructor->`main` = 18-25 us. **Full-scale synthetic** (2,232 TUs, 27,909 pointer initializers, compiled as C++, low-address link): median **~0.4 ms** (0.37-0.65 ms over 10 runs), +420 KB of code. Negligible. |
| Memory layout? | `.data`/`.bss` byte totals are unchanged vs the reference build (14,304 / 25,664 for the 63 symbols). |

Caveat: the 4 mixed files are *logic* files. They compiled as C++, but compiling game logic as C++ changes language semantics (implicit conversions, `sizeof` of character literals, tentative definitions...); for those the safer route is moving the static `Gfx` tables into a separate data file. 14 mixed files remain in total (`ac_field_draw.c`, `ac_mailbox.c`, `m_fbdemo_wipe1.c`, `m_msg.c`, `m_player.c`, `bootdata/gam_win2.c`, `gam_win3.c`, `initial_menu.c` + the 4 in the spike + `logo_nin.c`/`gam_win1.c` which are pure data).

### 9.5 The two genuine compile errors

1. `src/actor/npc/ac_npc_ctrl.c_inc:715` (`aNPC_pc_actor_slot_backing_too_small`): the 64-bit NPC slot was 0xA50 but `sizeof(NPC_ACTOR)` is 0xBA8. Measured all 228 actor `ACTOR_PROFILE` class sizes on x64 (`sizeof` probes): the largest NPC-derived class is `0xC28` (`NPC_SUPER_MASTER/SHOP_MASTER/DEPART_MASTER/CONV_MASTER/MAMEDANUKI_ACTOR`); `NPC_CONTROL_ACTOR` (`ac_npc2.c`, 0xFD0) is not routed through the slots (`aNPC_NPC2` frees with `zelda_free`). Fix: under `PC_LOW_ADDRESS_64` the slot is `0xD00` (0xC28 + up to 15 bytes of alignment loss + headroom). 32-bit keeps `0xA50`. It is slot capacity only; the runtime `size > slot` rejection is unchanged. Nine slots -> +about 6 KB.
2. `src/static/Famicom/famicom.cpp:730`: `u32 size;` passed as `size_t*` to `SetupResBanner`. `u32 == size_t` only on 32-bit; on Win64 `size_t` is 8 bytes, so the callee would write 8 bytes into a 4-byte local. Fix: `size_t size;` (every use of `size` in that function is already `size_t`-typed: `getBannerSizeFromFormat`, `memcpy`, pointer arithmetic).

Both are semantics-preserving for the original 32-bit case.

### 9.6 Rebuild (`pc/build64`, `mingw32-make -k`, 3,998 objects attempted)

- **1,840 objects compile**, including every PC-layer file, all of JSystem, emu64, jaudio_NES, Famicom, all game logic except 8 files, and all 23 spike files.
- **2,158 objects fail, all for one reason**: `initializer element is not constant` (27,623 errors = 27,909 - 286 that the spike fixed).
  By area: `src/data/model` 1,769; `src/data/field` 266; `src/data/npc` 69; `src/data/scene` 47; `src/game` 3 (`m_fbdemo_wipe1.c`, `m_msg.c`, `m_player.c`); `src/actor` 2 (`ac_field_draw.c`, `ac_mailbox.c`); `src/static` 3 (`bootdata/gam_win2.c`, `gam_win3.c`, `initial_menu.c`).
- No other error class remains: zero `#error`/static-assert failures, zero type errors.
- I found and fixed one mistake of mine on the way (`pc_disc.c` used the new macro without including `pc_lowaddr.h`); the counts above are after that fix (the full run had 1,839 / 2,159, the extra failure was that file).
- **Not tested:** link, boot, any run of the game.

### 9.7 New blockers / risks found

1. **Address range is not the only 32-bit dependency.** The low-address trick makes `(u32)ptr` lossless but does nothing for *layout*: `JKRExpHeap::CMemBlock::getBlock` still hard-codes `-0x10` (block header is 0x20 with 8-byte pointers), `__osMalloc.c`'s `OSMemBlock` header size feeds payload alignment, `ac_npc.h:345` padding, and struct offsets in emu64 etc. These compile silently. They must be fixed (A4/A5/B5 in section 3) and will only show up when the game runs.
2. **Mixed code+data files (14)** cannot be handled by "compile as C++" without accepting a language change for game logic; split or ifdef them.
3. **Every C++-compiled data TU must see `extern "C"` declarations** for anything it references from C files (found: `pc_load_asset`; the full set of symbols is only known after a full attempt). Mangled unresolved symbols will appear at link time.
4. **Build volume:** ~2,150 more files switch to C++ (about 9-10 min of extra single-core compile time at 0.26 s/file).
5. Static-init order across the whole game is unaudited (no constructor known to read these tables).
6. Large single allocations (> ~1.5 GB) may be placed above 4 GB; the game does not make any, and `PC_LOWADDR_*` will report it if it ever does.

### 9.8 Verdict

- **Is the low-address strategy viable?** For the *address-range* problem, yes: verified for the image, CRT heap, VirtualAlloc, stacks and thread stacks with SDL2 and an OpenGL 3.3 context loaded, with 2.5 GB of headroom. For the *static pointer initializer* problem, yes, without widening `Gfx` or audio command words: 19/19 spike data files compile as C++, the resulting data is bit-identical to the reference except for the initialized pointers, and the full-scale startup cost is about 0.4 ms. **It is not yet proven for the game as a whole**: the game has not linked or run.
- **Recommendation: proceed one more bounded step, then decide.** (1) Apply the C++ rule to all of `src/data/**` with a single CMake glob (`LANGUAGE CXX` + `this=this_arg`, no source edits) and split the 8 remaining mixed files; (2) get `ac_pc` to link (fix the `extern "C"` fallout); (3) fix `CMemBlock`/`OSMemBlock` layout; (4) boot to the title screen with `PC_LOWADDR` reporting on. Exit criteria for falling back to a 32-bit i686 toolchain: any `VIOLATION` that cannot be resolved by an allocation-site change, or heap/audio corruption that needs more than the known layout fixes. If the only goal is to get building and testing multiplayer *this week*, the 32-bit toolchain remains the faster path; the low-address port buys a modern 64-bit toolchain, not a real 64-bit address space.

## 10. Whole-repo C++ data rule, link, layout fixes and first launch

Date: 2026-09-21 (third phase). Everything is behind `-DPC_ALLOW_64BIT=ON` (`PC_LOW_ADDRESS_64`); a configure without it still stops with the original "MUST be built as 32-bit" error (re-verified).

### 10.1 Result summary

| Question | Answer |
|---|---|
| Remaining compile errors | **0** (4,007 objects; last full build `exit=0`) |
| Does `ac_pc` link? | **Yes.** `pc/build64/bin/AnimalCrossing.exe`, PE32+, 14.2 MB, `ImageBase=0x400000`, `DllCharacteristics=0x100` (no ASLR/HEVA), `SizeOfImage=0x1FDD000` (image ends at `0x23DD000`) |
| Does it launch? | **Yes.** It starts, loads SDL2, creates the OpenGL 3.3 core context, loads settings/keybindings/shaders, pre-compiles ~55 shader variants and initializes the texture pack, all with 0 low-address violations. |
| Title screen reached? | **No, and it cannot be on this machine.** There is no GameCube disc image here (`orig/GAFE01_00` is empty; no `.iso/.gcm/.ciso/.rvz` found). `pc_assets_init()` fails with "No ROM data found" and the game stops at its own "Missing ROM" message box, before `boot_main`. Nothing past that point (asset load, `OSInit` in the real boot chain, JFWSystem, emu64, actors, audio) has run in the real game. |
| Crashes | None observed. The process stays alive until the message box is dismissed. |
| Low-address violations | **0** (every category below). |

### 10.2 What was changed

1. **CMake rule for all of `src/data/**` and `src/static/bootdata/**`** (`pc/CMakeLists.txt`): `LANGUAGE CXX` + `COMPILE_DEFINITIONS this=this_arg` on every matching source, no edits to the data files. 3,225 translation units are compiled as C++ (3,215 data files + 10 split units). With the rule alone, exactly 10 files still failed to compile, all mixed code+data files; every one of the ~2,150 data files compiled unchanged.
   `gam_win2.c` / `gam_win3.c` turned out to be pure data (Vtx/texture/Gfx + the generated asset loader), so they are handled by this rule, not split.
2. **C/C++ boundary (`extern "C"`)**: the C++-compiled data files define ~768 asset loaders (`_pc_load_*`, called from the C file `pc_assets.c`) and call `pc_load_asset`. C++ mangles those names (776 unresolved references on the first link). CMake now generates `pc_cxx_data_linkage.h` (via `file(GENERATE)`) from the `extern void _pc_load_...(void);` lines already present in `pc_assets.c`, wraps them in `extern "C"`, and force-includes it into the C++-compiled data files only. Result: the link succeeded with no undefined references and no multiple definitions.
3. **Split of the mixed files** (`src/data/pc_split/*.c` are empty translation units unless `PC_LOW_ADDRESS_64`; the data lives in `*_gfx.c_inc` files that are `#include`d in place in every other build):

| Original | Moved | Split unit |
|---|---|---|
| `src/actor/ac_field_draw.c` | culling Vtx + 2 Gfx | `pc_split/ac_field_draw.c` |
| `src/actor/ac_mailbox.c` | 3 Gfx | `pc_split/ac_mailbox.c` |
| `src/actor/ac_mbg.c` | Vtx + Gfx + asset loader | `pc_split/ac_mbg.c` |
| `src/actor/ac_haniwa.c` | function-local `static Gfx hnw_tex_model[]` -> `extern` in low mode, defined in the split unit (4 duplicated lines) | `pc_split/ac_haniwa.c` |
| `src/game/m_fbdemo_wipe1.c` | Vtx/texture + Gfx | `pc_split/m_fbdemo_wipe1.c` |
| `src/game/m_msg.c` (`m_msg_data.c_inc`) | whole data include | `pc_split/m_msg.c` |
| `src/game/m_choice.c` (`m_choice_draw.c_inc`) | data + loader | `pc_split/m_choice.c` |
| `src/game/m_player.c` (`m_player_tools.c_inc`) | whole data include | `pc_split/m_player_tools.c` |
| `src/static/initial_menu.c` | Mtx/Vp/Gfx block | `pc_split/initial_menu.c` |
| `src/static/dvderr.c` | ~900 lines of Vtx/tex/Gfx + `Dvderr_work` + loader | `pc_split/dvderr.c` |

   The task listed 8 mixed files; the real set was 10 (`ac_mbg.c`, `ac_haniwa.c`, `m_choice.c`, `dvderr.c` were previously compiled as C++ by the spike and are now C again; `gam_win2/3.c` were never mixed).
   Mechanism: only symbols the remaining C code references are exported (`PC_SPLIT_STATIC` = `static` everywhere except in the split units; extern declarations in `src/data/pc_split/*_split.h`). **32-bit path check:** the preprocessed output (`-E`, existing PC config, without `PC_LOW_ADDRESS_64`) of all 10 originals is line-for-line identical (as a multiset, `__FILE__/__LINE__` normalized) to `HEAD`, except the intentional `ac_npc.h` `_0FC[3]` (identical value on 32-bit).
4. **Layout fixes** (measured on x64, see 10.3): `JKRExpHeap::CMemBlock` and `JKRArchive::SDIFileEntry::mData` use `PcLowPtr<T>` (`pc/include/pc_lowptr.h`, a 4-byte range-checked pointer); `ac_npc.h` `_0FC[3]`; compile-time `static_assert`s on both sizes; `getBlock()` no longer round-trips through `u32`.
5. **Diagnostics**: `pc_lowaddr_log()`, a low-address report on the ROM-missing exit path, and `AnimalCrossing.exe --lowaddr-selftest` (`pc/src/pc_lowaddr_selftest.cpp`), which runs the real allocators inside the real executable without a ROM.

### 10.3 Structure layouts (measured with x64 GCC, not assumed)

| Structure | GC / on-disc | x64 native (before) | After fix | Consequence if unfixed |
|---|---|---|---|---|
| `JKRExpHeap::CMemBlock` | 0x10 | **0x18** (`mPrev@0x8`, `mNext@0x10`) | 0x10 (`PcLowPtr`, `mPrev@8`, `mNext@0xC`) | `getBlock()` subtracts 0x10 -> off by 8; **negative control**: reverting the fix makes the self-test fail 28 checks (`root heap inconsistent right after creation`, `check()` fails every 500 iterations) |
| `OSMemBlock` (`__osMalloc`) | 0x30 | **0x40** (`next@8 prev@0x10 file@0x18 line@0x20 threadId@0x24 arena@0x28 time@0x30 pad@0x38`); `OSArena` 0x2C -> 0x50 | unchanged (no fix needed) | Not a bug: every use goes through `sizeof(OSMemBlock)`, no literal 0x30 exists; payload alignment is 0x40 (16-byte guarantee kept). 2,600 random alloc/free cycles pass with arena accounting exactly restored. |
| `ac_npc_clip_s` (`aNPC_Clip_c`) | function table | 0x218; `_0FC` was 1 pointer instead of 3 | `_0FC[3]` | Only padding; `_0FC` is never referenced. All members are accessed by name. |
| `JKRArchive::SDIFileEntry` (found by this pass) | **0x14** on-disc RARC entry | **0x18** (`void* mData@0x10`) | 0x14 (`PcLowPtr<void>`) | `mFileEntries[i]` is an array cast onto the archive image: every entry after the first would be misread (forest_1st.arc, forest_2nd.arc ...). `SDIDirEntry` 0x10, `SArcDataInfo` 0x20, `SArcHeader` 0x20 are already correct. |

Self-test result with the fixes (real exe, SDL + GL loaded first): **0 failures**; JKRExpHeap root + 1 MB child heap, 3,127 + 3,128 random allocations (alignments 4..128, head and tail), block-header validation, content tagging, `check()` every 500 steps, free-all restores the exact free size; `__osMalloc` 2,600 allocations with alignment 8..64 and `__osCheckArena` every 500 steps.

**Survey of all other documented struct sizes** (`sizesurvey.py`: every `sizeof(T) == 0x..` / `size = 0x..` comment in headers vs real x64 `sizeof`): 185 documented types, **90 differ**. They are all types that contain pointers: actors (`HANIWA_ACTOR`, `GYOEI_ACTOR`, `aINS_INSECT_ACTOR`, `NPC_*_ACTOR` ...), clips (`Clip_c`, `aINS_Clip_c`), `common_data_t` (0x2DC00), `ACTOR_PROFILE` (0x24), `Shape_Info`, `EVW_ANIME_*`, libultra `OSMesgQueue/OSIoMesg/OSPiHandle`, and ~35 `jaudio_NES/audiostruct.h` types (`AudioGlobals`, `channel`, `note`, `group`, `SZHeap`, ...). These are runtime structures accessed by member name, so a different size is harmless **unless** something memcpy's them by literal size, overlays them onto binary data, or stores them in a fixed-size slot (the NPC slot was the one such case already found and fixed). `Save_t` (0x242A0) is not in the list. Not yet examined per type; needs ROM runtime.

### 10.4 Low-address violations recorded

None. Categories checked in the real executable (normal launch and `--lowaddr-selftest`), all `violations=0`: executable image `0x400000..0x23E0000`, main/CreateThread stacks (`0x25DFE1C`, `0x49DFF4C`), process heap `0x2660000`, malloc probes (1 B / 64 KB / 24 MB), OS arena (24 MB) `0x4EFD2040..0x507D2040`, JKR arena, JKR heaps, `__osMalloc` arena. Not reached without a ROM: ARAM buffer, emu64 instance/texture caches, actor allocations, GX texture objects, asset source images (the hooks are compiled in; they have run only in the harness of section 9).

### 10.5 Remaining structural / runtime risks

1. **Nothing after the ROM check has run.** All conclusions about boot, JFWSystem, ARAM archives, emu64, jaudio and actors are untested. The first real test needs a disc image.
2. The 90 documented-size mismatches (10.3) and the jaudio structs are unaudited individually. Highest risk: jaudio_NES (191 `(u32)ptr` lines; file-format headers patched in place, e.g. `system.c:573 header->entries[i].addr += (u32)data`).
3. `JKRExpHeap::check()` prints block links through `JUTWarningConsole_f` varargs as a 4-byte struct (`PcLowPtr`); harmless on x64 (passed as a 4-byte value) but only exercised on a failing heap.
4. C++-compiled data files: static-init order across TUs is still unaudited (no reading constructor is known); `const` linkage differences did not show up at link time (0 undefined/duplicate symbols).
5. 3,225 TUs compile as C++ (about +0.26 s each); a rebuild of all C++ data files took about 10 minutes with 12 jobs.
6. `pc_assets.c` and its generator `gen_runtime_assets.py` were not modified; the header `pc_assets.h` now has `extern "C"` (the generator template still lacks it and was already out of sync with the checked-in header).

### 10.6 Verdict

The low-address 64-bit strategy is **viable and should be continued**: the whole code base compiles and links as a 64-bit process without widening `Gfx` or audio words; the process lives entirely below 4 GB (image, CRT heap, thread stacks, arena); the two real layout defects found so far (`CMemBlock`, `SDIFileEntry`) were fixable with a 4-byte pointer wrapper at no cost to GC semantics, and the negative control shows the self-test detects that class of bug. It is **not yet proven** for the actual game because no run got past the ROM check. Decision point: with a legally obtained disc image in `pc/build64/bin/rom/`, boot to the title screen and record violations/crashes; if that hits allocation violations that cannot be fixed at their allocation site or jaudio/heap corruption that needs more than layout wrappers, fall back to a 32-bit toolchain.

## 11. Boot-blocker log: system heap, jaudio file formats, DVD layout

Third-to-fifth verified blockers after the first real-ROM run (GAFE01 rev 0 disc image in `pc/build64/bin/rom/`, always run from `pc/build64/bin`; the exe resolves `shaders/`, `rom/`, `settings.ini` relative to the working directory, and stderr is discarded without `--verbose`).

| # | Blocker | Category | Status |
|---|---|---|---|
| 1 | `exit(1)` in `pc_gx_tev_init()` when started from another directory (`shaders/default.vert` not found) | launch environment (also true for 32-bit) | no code change; run from `bin/` |
| 2 | `JFWSystem::systemHeap == NULL` -> `JKRHeap::alloc(this==NULL)` in `JFWSystem::init` | structure size (`sizeof(JKRExpHeap)` is 0x100 on x64; `JW_Init` reserves 0xD0) | fixed, `PC_LOW_ADDRESS_64` only: `JFWSystem::firstInit` clamps the system-heap request to the root heap's real free space (`0x17FCE30` requested, `0x17FCDD0` free, `0x17FCDD0` used; root free afterwards 0) |
| 3 | SIGSEGV in `Wave_Test` (`cmpl 'WINF',(r13)`, r13 == 0) from `Wavegroup_Regist` <- `BootSound` <- `Na_InitAudio` | on-disc structure layout (32-bit file offsets typed as native pointers) | fixed (below) |
| 4 | Hang in `JKRDvdRipper::loadToMainRAM` <- `JKRAramArchive::open` <- `JW_Init2` (`forest_1st.arc`) | structure layout of `DVDFileInfo` | **characterized, not fixed** (below) |

### 11.1 jaudio file-format fix

`Wsys_`, `Bank_`, `Ibnk_`, ... hold 32-bit GameCube addresses (file offsets, converted in place to absolute addresses by `PTconvert`) in fields declared as pointers. On x64 each grew to 8 bytes, so every offset after the first pointer moved and the hard-coded `data + 0x10` / `data + 0x14` reads in `Wave_Test` were 8-byte loads spanning two fields.

Fix: every such field is now `JA_FPTR(T)` (`include/jaudio_NES/ja_fileptr.h`): `T*` in every build without `PC_LOW_ADDRESS_64` (source-identical), `PcLowPtr<T>` (4 bytes, range-checked, converts to/from `T*`) in C++ and an opaque 4-byte `JaFilePtr32` in C, so a C file can never dereference one by accident. Serialized widths and offsets are unchanged; no native pointer was touched.

Structures changed (sizes derived from the header's own offset comments, which describe the 4-byte GameCube layout; there is no GC compiler here to measure):

| Structure | GC size | x64 before | x64 after |
|---|---|---|---|
| `jaheap_` | 0x2C | 0x48 | 0x2C |
| `Bank_` | 0x3C4 | 0x788 | 0x3C4 |
| `Ibnk_` | 0x3E4 | 0x7B0 | 0x3E4 |
| `Inst_` | 0x40 | 0x70 | 0x40 |
| `InstKeymap_` | 0x10 | 0x18 | 0x10 |
| `PercKeymap_` | 0x1C | 0x30 | 0x1C |
| `Osc_` | 0x18 | 0x20 | 0x18 |
| `Voice_` | 0x10 | 0x18 | 0x10 |
| `Perc_` | 0x408 | 0x608 | 0x408 |
| `Pmap_` | 0x8 | 0x10 | 0x8 |
| `Wsys_` | 0x18 | 0x20 | 0x18 |
| `WaveArchiveBank_` | 0xC | 0x10 | 0xC |
| `CtrlGroup_` | 0x10 | 0x18 | 0x10 |
| `SCNE_` | 0x1C | 0x30 | 0x1C |
| `Ctrl_` | 0xC | 0x10 | 0xC |
| `WaveID_` | 0x38 | 0x60 | 0x38 |
| `WaveArchive_` | 0x78 | 0x98 | 0x78 |
| `Wave_` | 0x28 | 0x30 | 0x28 |

Fields changed (all now 4 bytes): `jaheap_::{firstChild,parent,nextSibling,groupOwner,firstGroupedHeap,nextGroupedHeap}`; `Bank_::{mInstruments,mVoices,mPercs}[0xF0]`; `Ibnk_::waveArcBank`; `Inst_::{mOscillators[2],mEffects[2],mSensors[2],mKeyRegions[5]}`; `InstKeymap_::mVelocities[2]`; `PercKeymap_::{_08,_0C,mVelocities[2]}`; `Osc_::{attackVecOffset,releaseVecOffset}`; `Voice_::_0C[]`; `Perc_::mKeyRegions[128]`; `Pmap_::_00`; `Wsys_::{waveArcBank,ctrlGroup}`; `WaveArchiveBank_::waveGroups[]`; `CtrlGroup_::scenes[]`; `SCNE_::{cdf,cex,cst}`; `Ctrl_::waveIDs[]`; `WaveID_::data`; `WaveArchive_::waves[]`; `Wave_::fileLoadStatus`. Before: 34 offset mismatches; after: 0. `static_assert`s on every size and every pointer-field offset (`JA_LAYOUT_SIZE/OFF`, active in `PC_LOW_ADDRESS_64`, expand to a repeatable `extern int` declaration otherwise). One documented size was wrong: `PercKeymap_` says "Size: 0x18" but its own fields end at 0x1C.

`PTconvert`: the original takes `void**` and adds the base to an 8-byte slot. The low-address build uses a template in `bx.h` that operates on the 4-byte `PcLowPtr` slot (`mAddr`), same rule (leave NULL and values >= base), range-checks the sum. Call sites use `PTCONVERT(slot, base)`, which expands to the original `PTconvert((void**)(slot), base)` in every other build. `Wave_Test` now reads `((Wsys_*)data)->waveArcBank/ctrlGroup` instead of two pointer-width loads at `+0x10/+0x14` (32-bit path keeps the original loads).

The 9 translation units that dereference these fields are compiled as C++ (CMake list, `bankread waveread bankdrv connect oneshot noteon driverinterface jamosc heapctrl`; their functions are `extern "C"` in the jaudio headers). Other source edits: `(Voice_*)(void*)`, `(Perc_*)(void*)`, `(Pmap_*)(void*)` casts, `JA_FPTR(WaveID_)* wave2`, and `JA_WAVE_DATA_INT()` for the `0xFFFFFFFF` "load failed" sentinel. `pc_lowptr.h` now protects the real `this` from the `-Dthis=this_arg` workaround.

Verification: (a) baseline probe before the change and `static_assert`s after (C and C++ views); (b) `--lowaddr-selftest` now builds GameCube-layout images byte-by-byte from the format's documented offsets and runs the **real** `Wave_Test` and `Bank_Test`: every 32-bit slot becomes `image + offset`, NULL slots stay 0, typed reads agree with raw reads, a second run is idempotent (0 failures); (c) preprocessed output of the 9 files without `PC_LOW_ADDRESS_64` equals `HEAD` except `struct WaveArchiveBank_*` -> `WaveArchiveBank_*` (same typedef), redundant parentheses and same-width `(void*)` casts.

Result in the real game: `Wave_Test`/`Wavegroup_Regist`/`BootSound`/`Na_InitAudio`/`sound_initial` complete (`AUDIOHEAP SET`, audio thread starts), boot continues through `initial_menu_init`, `dvderr_init`, `sound_initial2`, COPYDATE, the string table and into `JW_Init2`. Not exercised yet: `Bank_Test` and the bank/wave paths on real ROM data beyond `BootSound`.

### 11.2 DVDFileInfo layout (blocker 4; fixed in section 12)

`pc_dvd.c` stores its own fields in `DVDFileInfo` at hard-coded GameCube offsets (`FILE*` @0x18, `startAddr` @0x30, `length` @0x34, `memset(...,0x3C)`), but `sizeof(DVDFileInfo)` is **0x58** on x64 (`DVDCommandBlock` 0x30 -> 0x48; `startAddr` @0x48, `length` @0x4C, `callback` @0x50). Everything that reads the file through the real struct sees zeros:

```
DVDReadPrio(length=32  offset=0 ) disc_base=0x569f7d20 file_len=0xd03a0 | x64 fields: startAddr=0x27c7300 length=0x0
DVDReadPrio(length=0   offset=0 )                                          (JKRDvdFile::getFileSize() == 0)
DVDReadPrio(length=32  offset=32)
DVDReadPrio(length=-32 offset=32)  -> pc_disc_read(len=4294967264) fails -> -1 -> VIWaitForRetrace() -> retry forever
```
`loadToMainRAM` computes `readSize = fileSize - offset = 0 - 32` and its `while (DVDReadPrio(...) < 0) VIWaitForRetrace();` never ends; the log shows `[PC] RARC header: sig=00000000 len=0`. Candidate fix (not applied): use the real struct members (`cb.addr` for the `FILE*`, which is offset 0x18 on 32-bit, `startAddr`, `length`, `sizeof(DVDFileInfo)`), identical on 32-bit.

## 12. DVDFileInfo layout fix and the next blocker (jaudio bank tables)

### 12.1 The problem (blocker 4 of section 11)

`pc_dvd.c` did not include `dolphin/dvd.h`: it took `void*` for every file-info pointer and addressed the structure with literal GameCube offsets (`FILE*` @0x18, `startAddr` @0x30, `length` @0x34, `memset(..., 0, 0x3C)`). `DVDFileInfo` contains pointers (`DVDCommandBlock::{next,prev,addr,id,callback,userData}`, `DVDFileInfo::callback`), so on x64 it is 0x58 bytes with `cb.addr`@0x20, `startAddr`@0x48, `length`@0x4C. Every reader that uses the real members (`JKRDvdFile::getFileSize()` returns `mDvdFileInfo.length`) saw zeros: `readSize = 0 - 32 = -32`, `pc_disc_read(len=4294967264)` failed, `DVDReadPrio` returned -1 and `JKRDvdRipper::loadToMainRAM` retried forever (`while (DVDReadPrio(...) < 0) VIWaitForRetrace();`).

### 12.2 The fix (`pc/src/pc_dvd.c`, `pc/include/pc_disc.h`)

- `pc_dvd.c` now includes `dolphin/dvd.h` and the accessors use the real members: `dvd_fi_fp()` -> `&fi->cb.addr` (the FILE*; this *is* offset 0x18 on the GameCube), `dvd_fi_startAddr()` -> `&fi->startAddr`, `dvd_fi_length()` -> `&fi->length`; both `memset(fileInfo, 0, 0x3C)` are `memset(fileInfo, 0, sizeof(DVDFileInfo))`.
- To use the real type the function definitions had to take the header's prototypes (previously `void*`, `const char*` and a private duplicate `DVDDiskID` typedef): `DVDOpen/DVDFastOpen/DVDClose/DVDReadPrio/DVDRead/DVDGetLength/DVDReadAsyncPrio/DVDGetTransferredSize/DVDFastClose/DVDPrepareStreamAsync` -> `DVDFileInfo*`, `DVDCancel/DVDCancelAsync/DVDChangeDisk(Async)/DVDGetCommandBlockStatus/DVDCancelStream` -> `DVDCommandBlock*` (+ `DVDDiskID*`, callbacks), `DVDConvertPathToEntrynum(char*)`. Names, linkage and behaviour are unchanged. The header's `DVDGetFileInfoStatus` macro is `#undef`'d locally so the out-of-line symbol is still defined.
- Compile-time checks: `FILE*` fits `cb.addr` (all builds); on 32-bit `cb.addr`@0x18, `startAddr`@0x30, `length`@0x34, `sizeof == 0x3C` (identical to the removed literals). A model of `DVDFileInfo` with 4-byte pointers (built in scratch, since there is no 32-bit compiler here) satisfies exactly those assertions.
- `pc_disc.h` gained the missing `extern "C"` guard (the C++ self-test calls it); a verbose `[PC/DVD] open <path>: startAddr length` log was added to `DVDFastOpen`.

### 12.3 Verification

`--lowaddr-selftest` (real disc image in `rom/`) opens `forest_1st.arc`, `forest_2nd.arc`, `foresta.rel.szs` and `COPYDATE` through `JKRDvdFile` - the exact reader path that failed - and compares with the disc FST and a direct disc read: 0 failures.

| File | FST offset / size | `JKRDvdFile::getFileSize()` | `startAddr` |
|---|---|---|---|
| `forest_1st.arc` | `0x569F7D20` / `0xD03A0` | `0xD03A0` | `0x569F7D20` |
| `forest_2nd.arc` | `0x56AC80C0` / `0x3F0F00` | `0x3F0F00` | `0x56AC80C0` |
| `foresta.rel.szs` | `0x5641D6EC` / `0x5DA631` | `0x5DA631` | `0x5641D6EC` |
| `COPYDATE` | `0x5641D6A0` / `0x13` | `0x13` | `0x5641D6A0` |

(the first and last 32 bytes of each file read via `DVDReadPrio` equal a direct `pc_disc_read`; `DVDFileInfo` sizeof=0x58, `cb.addr`@0x20, `startAddr`@0x48, `length`@0x4C). Build: 0 errors, links.

### 12.4 Real game (GAFE01 rev 0, run from `pc/build64/bin`)

Before: hung in `JW_Init2` with `RARC header: sig=00000000`. After: `forest_1st.arc` (`sig=52415243` 'RARC', 29 files, 851,744 bytes to ARAM), `forest_2nd.arc` (57 files, 4,130,656 bytes) and `famicom.arc` (51 files, 1,697,888 bytes) mount; `JW_Init2`, `JW_Init3`, `mMsg_aram_init2`, `famicom_mount_archive` complete; `HotStartEntry` -> `entry()` -> `mainproc` (`CreateIRQManager`, `padmgr_Create`) -> `graph_proc` -> "No save file found" -> `trademark_init: enter`. Low-address violations: 0.

### 12.5 Next blocker: jaudio bank/wave tables (`audiostruct.h`), SIGSEGV in the audio producer thread

```
Thread "AudioProducer" received signal SIGSEGV
#0 __WaveTouch                 (system.c)      mov %ecx,(%eax)   rax=0x318e2bf (odd, unmapped), rcx=0
#1 Nas_BankOfsToAddr
#2 __Load_Ctrl
#3 __Nas_StartSeq
#4 Nas_StartMySeq
#5 Nap_AudioSysProcess
#6 Nap_AudioPortProcess
#7 CreateAudioTask
#8 Neos_Update
#9 CpubufProcess / Jac_VframeWork / Jac_UpdateDAC / pc_audio_producer_func
```
Root cause category: the same on-disc-layout class as section 11.1, one layer down. The instrument-bank data (`__Load_Ctrl` -> `__WaveTouch` relocates 32-bit offsets in place) is described by structs that hold the target offsets in pointer-typed fields: `smzwavetable{sample,loop,book}`, `wtstr{wavetable,tuning}`, `voicetable`, `perctable`, `percvoicetable`, `adpcmloop`, `tmtable`, `envdat` pointers. Measured: `wtstr` 0x10 (documented 0x8), `percvoicetable` 0x10 (0x8), `perctable` 0x20 (0x10), `voicetable` 0x40 (0x20), `smzwavetable` 0x20 (0x10), `adpcmloop` 0x30 (0x10). `Nas_BankOfsToAddr` therefore steps with the wrong stride and `wtstr::wavetable` (8 bytes) overlays `wtstr::tuning`; the resulting bogus address is dereferenced. This is a pointer-width/layout issue, not a truncation or init-order issue. Not fixed in this pass.

## 13. jaudio instrument/bank tables (`__WaveTouch`, `Nas_BankOfsToAddr`, `__Load_Ctrl`) and the next blocker

### 13.1 Original structures and the mismatch

`__Load_Ctrl` -> `Nas_BankOfsToAddr` -> `__WaveTouch` relocate 32-bit offsets in place inside a big-endian bank image and then walk it through `voiceinfo`, `voicetable`, `perctable`, `percvoicetable`, `wtstr`, `smzwavetable`, `envdat` (`audiostruct.h`). Those structures were declared with native pointers where the file holds 32-bit offsets, and `voiceinfo::{instruments,percussion}` was a pointer-to-pointer array (`voicetable**`), so *indexing* used an 8-byte stride on x64 while the file uses 4.

| Structure | GC size | x64 before | x64 after | Fields changed |
|---|---|---|---|---|
| `smzwavetable` | 0x10 | 0x20 | 0x10 | `sample`, `loop`, `book` |
| `wtstr` | 0x08 | 0x10 | 0x08 | `wavetable` |
| `voicetable` | 0x20 | 0x40 | 0x20 | `envelope` |
| `perctable` | 0x10 | 0x20 | 0x10 | `envelope` |
| `percvoicetable` | 0x08 | 0x10 | 0x08 | (contains `wtstr`) |
| `voiceinfo` | - | slot arrays stride 8 | slot arrays stride 4 | `instruments`, `percussion` are `JA_FPTR(T)*` |
| `envdat`, `adpcmbook`, `adpcmloop` | 0x04 / 0x08 / 0x30 | unchanged | unchanged | pointer-free (`adpcmloop` 0x30 includes the predictor state; an earlier note calling it oversized was wrong) |

Before: 15 offset/size mismatches over 5 structures. After: 0. `JA_LAYOUT_SIZE/OFF` `static_assert`s (`audiostruct.h`, `bx.h`, `heapctrl.h`) pin every size and every offset in the C and the C++ view of the header. `PercKeymap_` documents "Size: 0x18" but its own fields end at 0x1C; the assert records 0x1C.

Consumers fixed: `system.c` (`Nas_BankOfsToAddr_Inner`, `__WaveTouch`, `__Load_Ctrl`; slot-array reads now `((JA_FPTR(perctable)*)*BANK_ENTRY(...))[i]`, addresses written through `JA_FPTR_U32`), `sub_sys.c` (`JA_FPTR_GET(smzwavetable, ...)`; stays C). `channel.c`, `driver.c`, `track.c`, `system.c`, `memory.c` moved to C++ (CMake list) because they dereference `PcLowPtr` fields; they needed no other source edits apart from below.

### 13.2 C -> C++ hazard found on the way: missing `return`

Flowing off the end of a non-void function is undefined in C++ (GCC emits `ud2`/falls through). `Nas_BankOfsToAddr` (an `s32` function whose tail returns nothing), `Nas_Init_Single` and three UNUSED stubs in `memory.c`/`bankdrv.c`/`connect.c` did this. The synthetic bank test crashed in it before any real data was involved. Each now ends with `#ifdef PC_LOW_ADDRESS_64 return 0; #endif` (found for the other three with `-Wreturn-type`). Not visible in the 32-bit configuration.

### 13.3 Relocation logic

`PTconvert` for `PcLowPtr` slots (template in `bx.h`, `PTCONVERT(slot, base)` macro; non-low: `PTconvert((void**)(slot), base)`) is unchanged in rule (skip 0 and values >= base, otherwise add base) but works on the 4-byte slot and range-checks the sum. `__WaveTouch` keeps its heuristic that a stored value `< 0x10000000` is an *unrelocated offset*; on a 64-bit process with everything below 4 GB this is still true for the real ROM-loaded image only while the bank buffer sits above 0x10000000. This is fragile (a low-address heap that happens to start below 0x10000000 would be mistaken for offsets). It is a pre-existing GC assumption (MRAM starts at 0x80000000 there); not changed, but the synthetic test places its image with `VirtualAlloc` at a fixed address >= 0x30000000 so it tests the real relocation path deterministically.

### 13.4 Tests

- Layout: static asserts (above); a scratch probe (`japrobe.c`, `audprobe.c`) before/after.
- Synthetic loader test `test_jaudio_banktables` in `pc/src/pc_lowaddr_selftest.cpp` (`--lowaddr-selftest`, no ROM needed): builds a big-endian GC-layout bank byte by byte (`Ibnk_` header, `voiceinfo` slots, `voicetable`, `perctable`, `wtstr`, `smzwavetable`, `envdat`), runs the **real** `Nas_BankOfsToAddr` and checks every 32-bit slot equals `image + offset`, NULL slots stay 0, typed reads agree with raw reads, a second run is idempotent. 0 failures; with the ROM present also the `DVDFileInfo` end-to-end check. 0 low-address violations.
- Build: full build exits 0.

### 13.5 32-bit compatibility (modeled, not built)

There is no 32-bit compiler on this machine. Verified: (a) the default configure still refuses (`PC_ALLOW_64BIT` off); (b) preprocessed output of 13 jaudio TUs (`system memory bankdrv connect channel driver track sub_sys bankread waveread oneshot noteon heapctrl`) without `PC_LOW_ADDRESS_64`, compared with `git archive HEAD`, differs only by: `extern int _ja_layout_unused;` (the repeatable no-op that replaces the layout asserts), `struct WaveArchiveBank_*` -> `WaveArchiveBank_*` (same typedef), redundant parentheses / same-width `(void*)` and `(void**)` casts (`PTCONVERT`), the `JA_FPTR_GET` cast `((smzwavetable*)x)` (same type as before), and ten extern declarations from the new `#include "jaudio_NES/dummyrom.h"` in `system.c`. `memory`, `bankdrv`, `connect`, `channel`, `driver`, `track`, `oneshot`, `noteon`, `heapctrl`, `waveread`: zero residual difference. The guarded `return 0;` additions do not appear. (c) Layout asserts equal the 4-byte GC sizes by construction (they are the documented ones); a 4-byte model in scratch satisfies them. Not verified: a real 32-bit compile/run.

### 13.6 Real game

GAFE01 rev 0, from `pc/build64/bin`: `__Load_Ctrl`, `Nas_BankOfsToAddr` and `__WaveTouch` now run on the real ROM bank data (the `Bank_Test` path was exercised by the synthetic test); the `AudioProducer` SIGSEGV is gone. Boot continues `trademark_init` -> `game_ct` -> `play_init` -> `Scene_ct`. Low-address violations: 0.

### 13.7 Next blocker (characterized only, not fixed): scene tables

```
Thread 1 SIGSEGV  mem_copy  <- Scene_Proc_Player_Ptr  <- Scene_ct  <- play_init  <- game_ct
                   <- graph_proc <- mainproc <- entry <- boot_main <- main
rcx (dst) = 2 (== &((Actor_data*)NULL)->position), rdx (src) = 0x2012870, r8 (len) = 6
```

Cause: `Scene_Word_u` (`m_scene.h`) is a union of 4-byte-pointer structs whose data is written by the `mSc_DATA_*` macros as `{type, n, 0, 0, (u32)ptr}` (the first union member, `Scene_Word_Data_Misc_c{u8,u8,u8,u8,u32 param3}`, 8 bytes). Every other member has a native pointer at +4:

```
sizeof(Scene_Word_u) = 16 (GC: 8)   misc = 8   actor = 16
offsetof(param3)=4   offsetof(actor.data_p) = 8   ctrl_actor_profile_p = 8   banks_p = 8   door_data_p = 8
```

So the initializer stores the pointer at +4 (and the table stride is 16), but `scene_data->actor.data_p` is read at +8, which is zero -> `data == NULL` -> `mem_copy(&data->position = 2, ...)`. Same class as 13.1 (serialized 8-byte-per-word table with a native pointer field), with two twists: the pointers come from `(u32)` casts in static initializers, and the table stride changed too (every `xxx_info[]` in `src/data/scene/` has 16-byte elements). Affected (probe-confirmed for Actor, Ctrl_Actor, Object_Bank, Door_Data; FieldCt/ArrangeFurniture_ct to be audited): `Scene_Word_Data_*_c`, `Scene_Word_u`, all `Scene_Proc_*` consumers in `m_scene.c`, and the `src/data/scene/*.c` tables. Not touched in this pass (the user's rule: characterize a new blocker before modifying it). **Fixed in section 14** (which also corrects the FieldCt/ArrangeFurniture_ct remark above: both are pointer-free).


## 14. Scene tables (`m_scene.h`, `Scene_ct`) and the next blocker

### 14.1 What a scene table is (and is not)

`src/data/scene/*.c` (51 files, each one `Scene_Word_u <name>_info[]`) are *compiled C data*, not a disc file: the `mSc_DATA_*` macros expand to `{type, count, p1, p2, (u32)pointer}` and initialise the first member of the union, `Scene_Word_Data_Misc_c { u8 type, param0, param1, param2; u32 param3; }` (8 bytes, `param3` at +4). Every table is terminated by `mSc_DATA_END()`; `Scene_ct` walks it with `scene_data++` and dispatches on `type` to the `Scene_Proc_*` functions. The record is therefore 8 bytes with a 32-bit pointer/data word at +0x04 on the GameCube. (On the PC port the multi-byte fields are host-endian, because the tables are compiled, not loaded; this is why `Scene_Proc_Field_ct` already had a `TARGET_PC` branch that reads `misc.param3` instead of the `field_ct` view. That branch is unchanged.)

The other members of the union view the same 8 bytes as `actor`, `control_actor`, `object_bank`, `door_data` (pointer at +4), `field_ct` and `arrange_ftr_ct` (no pointers).

### 14.2 Complete affected-structure list (baseline probe, x64 `PC_LOW_ADDRESS_64`, C and C++ views identical)

| Structure | GC size / ptr offset | x64 before | x64 after |
|---|---|---|---|
| `Scene_Word_u` (the table stride) | 0x8 | **0x10** | 0x8 |
| `Scene_Word_Data_Actor_c` (`data_p`) | 0x8 / +4 | 0x10 / **+8** | 0x8 / +4 |
| `Scene_Word_Data_Ctrl_Actor_c` (`ctrl_actor_profile_p`) | 0x8 / +4 | 0x10 / **+8** | 0x8 / +4 |
| `Scene_Word_Data_Object_Bank_c` (`banks_p`) | 0x8 / +4 | 0x10 / **+8** | 0x8 / +4 |
| `Scene_Word_Data_Door_Data_c` (`door_data_p`) | 0x8 / +4 | 0x10 / **+8** | 0x8 / +4 |
| `Scene_Word_Data_Misc_c` (`param3`) | 0x8 / +4 | 0x8 / +4 | unchanged |
| `Scene_Word_Data_FieldCt_c` | 0x8 (`bg_disp_size` +4, `room_type` +6, `draw_type` +7) | 0x8 | unchanged (pointer-free) |
| `Scene_Word_Data_ArrangeFurniture_ct_c` | 0x2 | 0x2 | unchanged (pointer-free) |
| `Actor_data`, `Door_data_c` (pointer targets) | 0x10, 0x14 | same | unchanged (pointer-free) |
| `Door_info_c` (member of `GAME_PLAY`) | 0x8 / +4 | 0x10 / +8 | **left as is**: a runtime struct filled by `Scene_Proc_Door_Data_Ptr`, never serialized; the pointer stays native |

Mismatches: 11 before (9 serialized + the 2 for `Door_info_c`), 2 after (both the deliberate, runtime-only `Door_info_c`). Serialized: 9 -> 0. Correction to section 13.7: it said "FieldCt/ArrangeFurniture_ct to be audited" and implied they were pointer-affected; they are pointer-free and already the right size, they only matter through the union stride. The scene pointers' targets (`Actor_data`, `Door_data_c`, `s16` lists) contain no pointers, so nothing deeper is affected.

Every `Scene_Word_u` user in the repo: `m_scene.c` (`Scene_ct` and the ten `Scene_Proc_*`; only the four pointer-reading ones needed changes), `m_play.c` (`scene_word_data[]`, a table of native `Scene_Word_u*`, correct as is; `Gameplay_Scene_Read`), `m_play.h` (`Scene_Word_u* current_scene_data`), `src/data/scene/*.c` (writers). There is no `sizeof(Scene_Word_u)`, `mSc_DATA_*`-independent indexing or other cast to the union anywhere else.

### 14.3 The fix

`include/m_scene.h` includes `jaudio_NES/ja_fileptr.h` (only `types.h` + `pc_lowptr.h`) and defines `mSc_SCENE_PTR(T)` = `JA_FPTR(T)` and `mSc_SCENE_PTR_GET(T, slot)` = `JA_FPTR_GET(T, slot)`. The four pointer fields are `mSc_SCENE_PTR(...)`: `T*` in every non-`PC_LOW_ADDRESS_64` build (source-identical to the original), `PcLowPtr<T>` in C++ (the data files), an opaque 4-byte `JaFilePtr32` in C (`m_scene.c`). `JA_FPTR` was chosen over a scene-specific type because it is exactly the same problem (a 4-byte slot inside a fixed-layout record holding a low address) and the same helpers already exist and are tested. `PcLowPtr` is trivially default-constructible, so the union stays an aggregate; the writer macros still initialise `misc` (`u32 param3`), so no data file needed any change (0 of 51 touched) and cannot produce an 8-byte pointer.

`src/game/m_scene.c` stays C: five reads become `mSc_SCENE_PTR_GET(T, scene_data->...)` (`Scene_Proc_Player_Ptr`, `_Ctrl_Actor_Ptr`, `_Actor_Ptr`, `_Object_Exchange_Bank_Ptr`, `_Door_Data_Ptr`). No CMake change and no new C++ file: the C/C++ boundary is unchanged.

Static assertions (`m_scene.h`, active in `PC_LOW_ADDRESS_64`, compiled in both the C view (`m_scene.c`, `m_play.c`) and the C++ view (all data files, the self-test)): `sizeof` of `Scene_Word_Data_{Misc,Actor,Ctrl_Actor,Object_Bank,Door_Data,FieldCt}_c` = 8, `ArrangeFurniture_ct_c` = 2, `Scene_Word_u` = 8, and `param3`, `data_p`, `ctrl_actor_profile_p`, `banks_p`, `door_data_p` at +4, `bg_disp_size`/`room_type`/`draw_type` at +4/+6/+7.

### 14.4 Tests

`--lowaddr-selftest` (`test_scene_tables`, no ROM needed; 0 failures, 0 low-address violations):
- layout: the sizes/offsets above at run time;
- synthetic table assembled byte by byte (9 words, stride 8, poisoned buffer): typed reads of PLAYER/CTRL_ACTOR/ACTOR/OBJ_BANK/DOOR/ARRANGE_FTR/SOUND words; a NULL data pointer stays NULL; raw `+4` equals the typed slot in every word; word 2 does not overlap word 1; assigning word 2's pointer changes only bytes `+0x14..+0x17`; three repeated walks leave the table unchanged;
- the real `mSc_DATA_*` macros build a 10-word table: `sizeof == 0x50`, every type/count/pointer byte checked, `FIELDCT` packing checked;
- all 51 real tables linked into the executable: 4-byte aligned, terminate with END, every type < `mSc_SCENE_DATA_TYPE_NUM` (a wrong stride/offset shows up immediately as an invalid type), pointer types have a plausible non-NULL low address equal to the raw slot, exactly one PLAYER_PTR and one FIELD_CT word each: 329 words checked. (My first version assumed the first word is PLAYER_PTR; it is not, e.g. `title_demo_info` starts with SOUND. The test, not the code, was wrong.)

### 14.5 32-bit equivalence (modeled, not a 32-bit build)

There is no 32-bit compiler here. Preprocessing `m_scene.c`, `m_play.c` and all 51 `src/data/scene/*.c` without `PC_LOW_ADDRESS_64` and comparing with `git archive HEAD`: the 51 data files and `m_play.c` are identical except the `_0FC[3]` line from the earlier `ac_npc.h` edit (`(0x108-0x0FC)/sizeof(void*)` = 3 on a 4-byte-pointer target, so equal there); `m_scene.c` differs only by the five `((T*)(slot))` casts to the field's own type. Layout model: the header's documented GC offsets (0x8 / +0x4) are what the assertions pin. Not verified: an actual 32-bit compile or run.

### 14.6 Real game (GAFE01 rev 0, from `pc/build64/bin`)

`Scene_Proc_Player_Ptr` now succeeds. Under gdb, for `title_demo_info` (stride 8): the PLAYER word `0x00000100 0x00a856c0` -> `TITLE_DEMO_player_data`, then `Scene_Proc_Ctrl_Actor_Ptr`, `_Actor_Ptr`, `_Object_Exchange_Bank_Ptr` and `_Field_ct` are entered with `+4` words `0xa856a0`, `0xa85690`, `0xa85680` and the FIELDCT param `0x20000000`. `Scene_ct` completes, the Animal Logo actor is created and the title logo runs through its actions 0 -> 5 (about 3,300 frames in a 60 s run), then `[PC] toNextLand: l_keepSave not set, aborting` / `[SCENE_MODE] 0 -> 3`. Low-address violations: 0.

### 14.7 Next blocker (characterized only): intermittent SIGSEGV in `AudioProducer` / `RspStart`

4 of 5 plain runs crash within 7-20 s (NEOS frame 301-1081), 1 ran 60 s; under gdb roughly 1 in 3-4. Always the same place:

```
Thread "AudioProducer" SIGSEGV   RspStart+752   movzbl (%r12),%edx   r12 = 0x96F
  <- RspStart2 <- Neos_Update <- CpubufProcess <- Jac_VframeWork <- Jac_UpdateDAC <- pc_audio_producer_func
```

`rspsim.c` `A_CMD_ADPCM`: `u8* src = sp128 + DMEMIn - sp12C`, where `sp128` is `cmdLo` of the preceding `A_CMD_LOADCACHE`. At the fault (stack slots read in gdb) `sp128 = 0xC8`, `DMEMIn = 0x8A7`, `sp12C = 0`, so the sample-data address the driver put into the command is **0xC8**, an unrelocated *offset*, not an address. `driver.c` builds it from `smzwavetable::sample` (`sampleAddr = sample->sample`) with `medium == MEDIUM_RAM`. The relocation that should have made it `offset + wave_media->wave0_p` is in `system.c` `__WaveTouch` (`sample = JA_FPTR_U32(sample) + (u32)wave_media->wave0_p`); `wave0_p` is set to `NULL` by `__Load_Wave_Check` when the wave archive is not loaded (`system.c` ~1937), which would give exactly `0 + 0xC8`. Not established: whether the wave0 base is NULL because the wave load is asynchronous (timing dependent, matches the intermittency), or whether a `__WaveTouch` heuristic (`value < 0x10000000` = unrelocated) mishandles a table. The audio command words are 32-bit and unchanged; the `smzwavetable` layout is asserted (0x10). This is a jaudio wave-load/relocation ordering problem; no code changed. It started to be reachable only now: before this pass the main thread crashed in `play_init` before the title sequence began.

Second, non-crashing observation (not investigated): after the logo, `toNextLand: l_keepSave not set, aborting`.

## 15. PC RSP simulator state-persistence bug (not a 64-bit/`PC_LOW_ADDRESS_64` issue)

### 15.1 Important distinction

Everything in this section is a **pre-existing bug in the PC-only software RSP simulator** (`src/static/jaudio_NES/internal/rspsim.c`, which has no GameCube equivalent - real hardware runs actual RSP microcode with genuinely persistent registers). It has nothing to do with `PC_LOW_ADDRESS_64`, `JA_FPTR`/`PcLowPtr`, the DVD fix, or the scene-table fix in sections 11-14, and needed no `#ifdef` gating: it applies identically to a 32-bit PC build. It was only reachable now because sections 11-14 got the game far enough to start real sequence/note playback for the first time.

### 15.2 The bug

`RspStart()` processes one RSP command buffer per call. Several pieces of state represent the RSP's own DMA-window/register state - which source window an in-progress ADPCM stream is reading from, and where the next call's history should come from - and are used by `A_CMD_ADPCM` (and `A_CMD_RESAMPLE`/others) regardless of whether *this* call's buffer contains its own `A_CMD_LOADCACHE`/`A_CMD_SETBUFF`. On real hardware this is exactly the kind of state that lives in the RSP's own registers/DMEM and survives between separate microcode task dispatches - which is why `DMEM`, `ADPCM_BOOKBUF` and `FINALR_STATE_BUF` in the same file are already correctly `static`. But `DMEMCount`, `DMEMIn`, `DMEMOut`, `loop_point`, `sp12C` and `sp128` were declared as ordinary automatic locals inside `RspStart()`, so every call started them from whatever garbage happened to be on the stack.

Confirmed live with temporary instrumentation (reverted): an `A_CMD_ADPCM` command executing in a call where neither `A_CMD_LOADCACHE` nor `A_CMD_SETBUFF` had run yet showed `sp128=0xC8`, `sp12C=25360` - stale garbage, not a real source address. The common case pairs this garbage with a stale `DMEMCount=0` (the decode loop's iteration count is 0, so nothing is read - harmless). The crash-producing case, captured directly, was:

```
saw_loadcache=0 saw_setbuff=1 sp128=0xC8 sp12C=25360 DMEMIn=0x8A7 DMEMOut=1344 DMEMCount=256
```

A different channel's `A_CMD_SETBUFF` legitimately ran this frame with a real, nonzero `DMEMCount=256`, but `sp128`/`sp12C` were never refreshed (no `LOADCACHE` this frame), so the decoder computed `src = sp128 + DMEMIn - sp12C = 0xC8 + 0x8A7 - 0x6310`, which wraps to `0xFFFFA65F`, and reading from it faulted. This is the same defect behind both the intermittent `AudioProducer`/`A_ADPCM` SIGSEGV (section 14.7) and the audible clicking/distortion investigated separately: the non-crashing instances of the same stale-state read still decode from a wrong address, corrupting that note's PCM without necessarily faulting.

### 15.3 The fix

`DMEMCount`, `DMEMIn`, `DMEMOut`, `loop_point`, `sp12C` and `sp128` are now `static` at file scope in `rspsim.c` (next to `DMEM`/`ADPCM_BOOKBUF`/`FINALR_STATE_BUF`), initialized once (`0`/`nullptr`), and the corresponding automatic-local declarations were removed from `RspStart()`. No other line in `RspStart()`, `RspStart2()`, or anywhere else changed: every use of these six names elsewhere in the file already referred to them by name and now resolves to the persistent statics unchanged. Nothing resets them at the start of a call - that was the point of the fix. There is no separate "reset on simulator init" path in this file (the simulator has no such explicit reset entry point today); if one is added later, resetting these six there would be appropriate.

### 15.4 Verification

- **Build:** 0 compile errors, 0 link errors, 0 new warnings for `rspsim.c`.
- **Self-test:** `--lowaddr-selftest` (unrelated to this fix, exercises no RSP code): 0 failures, 0 low-address violations - unchanged.
- **Stress test, 5 launches, 90 s each** (previously the game crashed within 7-20 s in the large majority of runs, and never survived past ~60 s):

| Run | Result | Notes |
|---|---|---|
| 1 | ran the full 90 s, no `A_ADPCM`/RSP crash | hit a **different, new** blocker at ~62 s (below) |
| 2 | ran the full 90 s, no `A_ADPCM`/RSP crash | same new blocker |
| 3 | ran the full 90 s, no `A_ADPCM`/RSP crash | same new blocker |
| 4 | ran the full 90 s, no `A_ADPCM`/RSP crash | reached NEOS frame 5101 with no issue |
| 5 | ran the full 90 s, no `A_ADPCM`/RSP crash | reached NEOS frame 5101 with no issue |

Zero `A_ADPCM`/RSP SIGSEGVs across all 5 runs (previously 100% reproducible within under a minute). `AudioProducer` stayed alive in every run.

- **Audio quality:** I captured pre-SDL PCM (same method as the earlier investigation) for 20 s post-fix and compared the same impulse/click statistic against the earlier pre-fix 12 s capture: 3.20 impulses/s post-fix vs 2.25/s pre-fix, i.e. **no clear reduction** by this metric. This capture reaches much further into the boot/logo sequence than the earlier one (more on-screen action, likely louder/busier music and SFX), so the statistic isn't a controlled comparison, and a simple "large sample-to-sample delta" threshold cannot distinguish a genuine percussion hit from a corrupted sample. I cannot listen to the audio directly. What is certain: the specific, confirmed mechanism that produced garbage source addresses (stale `sp128`/`sp12C` feeding a legitimate `SETBUFF`-driven decode) can no longer happen, since that state now persists correctly - but I am not claiming a perceptual "sounds clean now" result without an actual listening test, which I'd ask you to do.
- **SDL:** untouched; not part of this change.

### 15.5 32-bit compatibility

Not gated behind `PC_LOW_ADDRESS_64` and doesn't need to be: `static` local promotion to file scope has identical semantics regardless of pointer width or `TARGET_PC`/32-bit vs 64-bit. There is still no 32-bit compiler available here, so this hasn't been built for 32-bit; the change is textually identical there (same six lines moved from function-local to file-scope `static`), so there's nothing pointer-width- or ABI-dependent to model.

### 15.6 Next blocker (characterized only, not fixed): `__osFree` invalid-free panic loop

3 of the 5 stress-test runs (1, 2, 3) hit, at almost the same point (LOGO sequence reaching `aAL_setupAction: 3 -> 6`, roughly 60-65 s / NEOS frame ~3700-4000 in each case):

```
__osFree:不正解放(0172d8e0)      (“__osFree: irregular deallocation”)
OSPanic at .../src/static/libc64/__osMalloc.c:738: 
```

repeating in a **tight, unbounded loop** (over 100,000 repetitions logged before the test harness killed the process at the 90 s mark) rather than crashing outright. In `__osMalloc.c`, `OSPanic(...)` is followed by a plain `return;` (`__osFree_NoLock`, ~line 330-334), so on this PC port a panic does not halt execution - whatever loop is repeatedly calling `__osFree`/`OSFree` on the same already-invalid pointer (`0172d8e0`) just keeps retrying forever instead of stopping. Runs 4 and 5 did not hit this in 90 s. This looks like a heap-corruption or double/invalid-free bug that is independent of both the RSP fix and the earlier low-address work (`OSMemBlock`/heap layout was already asserted and verified in section 10); it was not reachable before because the game never survived this long. Not investigated further and not fixed, per the one-blocker-at-a-time rule.

## Appendix A - all compiler-detected pointer<->integer cast sites (572 lines)

Columns: file | count | line numbers. (Generated by the x64 `-fsyntax-only` sweep; excludes `src/data`, and the 27,909
static-initializer errors in section 3/B1.) `src/furniture` files are `#include`d into `f_furniture.c`; their lines are real.

| File | Sites | Lines |
|---|---|---|
| `include/JSystem/JKernel/JKRExpHeap.h` | 1 | 46 |
| `include/PR/abi.h` | 4 | 277, 345, 386, 402 |
| `include/PR/gbi.h` | 1 | 3227 |
| `include/dolphin/os/OSLink.h` | 1 | 84 |
| `include/dolphin/os/OSUtil.h` | 2 | 11, 12 |
| `include/graph.h` | 1 | 291 |
| `include/jaudio_NES/audiocommon.h` | 5 | 100, 108, 116, 166, 973 |
| `src/TwoHeadArena.c` | 3 | 64, 69, 74 |
| `src/actor/ac_ant.c` | 4 | 64, 71, 73, 90 |
| `src/actor/ac_bee.c` | 5 | 170, 205, 207, 231, 356 |
| `src/actor/ac_fallSESW_move.c_inc` | 1 | 8 |
| `src/actor/ac_fallS_move.c_inc` | 1 | 8 |
| `src/actor/ac_field_draw.c` | 3 | 167, 284, 292 |
| `src/actor/ac_gyo_kaseki.c` | 1 | 623 |
| `src/actor/ac_gyo_test.c` | 2 | 636, 649 |
| `src/actor/ac_house.c` | 2 | 37, 42 |
| `src/actor/ac_house_goki.c` | 2 | 258, 318 |
| `src/actor/ac_ins_amenbo.c` | 2 | 84, 208 |
| `src/actor/ac_ins_batta.c` | 6 | 140, 143, 155, 188, 426, 570 |
| `src/actor/ac_ins_chou.c` | 2 | 292, 623 |
| `src/actor/ac_ins_dango.c` | 1 | 397 |
| `src/actor/ac_ins_goki.c` | 6 | 46, 174, 215, 237, 282, 479 |
| `src/actor/ac_ins_hitodama.c` | 1 | 307 |
| `src/actor/ac_ins_hotaru.c` | 1 | 475 |
| `src/actor/ac_ins_ka.c` | 2 | 284, 288 |
| `src/actor/ac_ins_kabuto.c` | 3 | 131, 199, 299 |
| `src/actor/ac_ins_kera.c` | 5 | 252, 270, 290, 342, 505 |
| `src/actor/ac_ins_mino.c` | 1 | 630 |
| `src/actor/ac_ins_semi.c` | 4 | 149, 251, 280, 381 |
| `src/actor/ac_ins_tentou.c` | 3 | 138, 280, 501 |
| `src/actor/ac_ins_tonbo.c` | 1 | 847 |
| `src/actor/ac_insect_clip.c_inc` | 1 | 70 |
| `src/actor/ac_insect_draw.c_inc` | 1 | 135 |
| `src/actor/ac_insect_move.c_inc` | 5 | 261, 267, 271, 279, 344 |
| `src/actor/ac_kamakura_indoor.c` | 1 | 324 |
| `src/actor/ac_koinobori_move.c_inc` | 1 | 21 |
| `src/actor/ac_misin.c` | 1 | 293 |
| `src/actor/ac_mscore_control.c` | 2 | 77, 274 |
| `src/actor/ac_museum_insect.c` | 6 | 902, 907, 914, 916, 927, 937 |
| `src/actor/ac_museum_insect_batta.c_inc` | 2 | 207, 217 |
| `src/actor/ac_museum_insect_goki.c_inc` | 1 | 155 |
| `src/actor/ac_museum_insect_ka.c_inc` | 1 | 79 |
| `src/actor/ac_museum_insect_okera.c_inc` | 2 | 185, 188 |
| `src/actor/ac_museum_insect_semi.c_inc` | 1 | 86 |
| `src/actor/ac_my_room.c` | 1 | 2138 |
| `src/actor/ac_my_room_melody.c_inc` | 2 | 13, 16 |
| `src/actor/ac_my_room_move.c_inc` | 1 | 3003 |
| `src/actor/ac_shrine_move.c_inc` | 1 | 384 |
| `src/actor/ac_snowman.c` | 1 | 1043 |
| `src/actor/ac_train0.c` | 2 | 55, 58 |
| `src/actor/ac_train0_move.c_inc` | 3 | 6, 10, 166 |
| `src/actor/npc/ac_countdown_npc1_talk.c_inc` | 1 | 171 |
| `src/actor/npc/ac_hanabi_npc1_talk.c_inc` | 1 | 51 |
| `src/actor/npc/ac_hanami_npc0_talk.c_inc` | 1 | 38 |
| `src/actor/npc/ac_harvest_npc0.c_inc` | 1 | 39 |
| `src/actor/npc/ac_npc_ctrl.c_inc` | 1 | 750 |
| `src/actor/npc/ac_tamaire_npc0_schedule.c_inc` | 1 | 58 |
| `src/actor/npc/ac_tokyoso_npc1_schedule.c_inc` | 2 | 198, 200 |
| `src/actor/npc/event/ac_ev_soncho2_think.c_inc` | 1 | 151 |
| `src/bg_item/bg_item_common.c_inc` | 1 | 538 |
| `src/effect/ef_ase2.c` | 1 | 53 |
| `src/effect/ef_buruburu.c` | 1 | 53 |
| `src/effect/ef_kangaeru.c` | 1 | 74 |
| `src/effect/ef_lovelove.c` | 1 | 38 |
| `src/effect/ef_naku.c` | 1 | 60 |
| `src/effect/ef_otikomi.c` | 2 | 85, 89 |
| `src/effect/ef_siawase_hikari.c` | 1 | 48 |
| `src/effect/ef_warau.c` | 1 | 37 |
| `src/famicom_emu.c` | 3 | 29, 30, 31 |
| `src/furniture/ac_hnw_common.c` | 6 | 345, 409, 414, 415, 469, 504 |
| `src/furniture/ac_ike_island_hako01.c` | 2 | 110, 113 |
| `src/furniture/ac_ike_jny_syon01.c` | 1 | 39 |
| `src/furniture/ac_ike_kama_danro01.c` | 1 | 14 |
| `src/furniture/ac_ike_tent_fire01.c` | 1 | 25 |
| `src/furniture/ac_ike_tent_fire02.c` | 1 | 26 |
| `src/furniture/ac_iku_mario_star.c` | 1 | 13 |
| `src/furniture/ac_iku_turkey_TV.c` | 1 | 13 |
| `src/furniture/ac_kon_snowtv.c` | 1 | 135 |
| `src/furniture/ac_nog_fan01.c` | 1 | 73 |
| `src/furniture/ac_nog_ka.c` | 1 | 15 |
| `src/furniture/ac_nog_kaeru.c` | 1 | 14 |
| `src/furniture/ac_nog_kera.c` | 1 | 14 |
| `src/furniture/ac_nog_nabe.c` | 1 | 46 |
| `src/furniture/ac_nog_sprinkler.c` | 1 | 35 |
| `src/furniture/ac_sugi_barbecue.c` | 1 | 6 |
| `src/furniture/ac_sugi_torch.c` | 1 | 24 |
| `src/furniture/ac_sum_abura.c` | 1 | 23 |
| `src/furniture/ac_sum_fruittv01.c` | 1 | 10 |
| `src/furniture/ac_sum_higurashi.c` | 1 | 23 |
| `src/furniture/ac_sum_kirigirisu.c` | 1 | 23 |
| `src/furniture/ac_sum_kisha.c` | 1 | 49 |
| `src/furniture/ac_sum_koorogi.c` | 1 | 23 |
| `src/furniture/ac_sum_matumushi.c` | 1 | 23 |
| `src/furniture/ac_sum_minmin.c` | 1 | 23 |
| `src/furniture/ac_sum_pet01.c` | 1 | 23 |
| `src/furniture/ac_sum_slot.c` | 1 | 37 |
| `src/furniture/ac_sum_suzumushi.c` | 1 | 23 |
| `src/furniture/ac_sum_syouryou.c` | 1 | 23 |
| `src/furniture/ac_sum_tonosama.c` | 1 | 23 |
| `src/furniture/ac_sum_tukutuku.c` | 1 | 23 |
| `src/furniture/ac_sum_tv01.c` | 1 | 110 |
| `src/furniture/ac_sum_tv02.c` | 1 | 102 |
| `src/furniture/ac_tak_ham1.c` | 1 | 22 |
| `src/furniture/ac_tak_ice.c` | 1 | 56 |
| `src/furniture/ac_tak_lion.c` | 1 | 35 |
| `src/furniture/ac_tak_stew.c` | 1 | 12 |
| `src/game/m_catalog_ovl.c` | 1 | 1455 |
| `src/game/m_debug_hayakawa.c` | 2 | 364, 369 |
| `src/game/m_debug_mode.c` | 1 | 740 |
| `src/game/m_field_assessment.c` | 2 | 230, 379 |
| `src/game/m_field_make.c` | 4 | 870, 871, 1483, 1821 |
| `src/game/m_mail.c` | 2 | 19, 25 |
| `src/game/m_mail_check_ovl.c` | 1 | 1151 |
| `src/game/m_mscore_ovl.c` | 1 | 270 |
| `src/game/m_play.c` | 2 | 476, 480 |
| `src/game/m_player_common.c_inc` | 8 | 2572, 4554, 6831, 7070, 7081, 7826, 7868, 7876 |
| `src/game/m_player_item_common.c_inc` | 1 | 400 |
| `src/game/m_player_lib.c` | 11 | 1041, 1048, 1081, 1111, 1246, 1259, 1368, 1369, 1676, 1701, 2626 |
| `src/game/m_player_main_demo_wait.c_inc` | 2 | 7, 20 |
| `src/game/m_player_main_door.c_inc` | 1 | 10 |
| `src/game/m_player_main_notice_net.c_inc` | 5 | 39, 113, 114, 115, 257 |
| `src/game/m_player_main_pull_net.c_inc` | 3 | 65, 143, 144 |
| `src/game/m_player_main_push_snowball.c_inc` | 2 | 14, 20 |
| `src/game/m_player_main_putaway_net.c_inc` | 5 | 48, 52, 53, 54, 85 |
| `src/game/m_player_main_stung_mosquito.c_inc` | 1 | 31 |
| `src/game/m_player_main_swing_net.c_inc` | 4 | 255, 257, 287, 360 |
| `src/game/m_player_main_wade_snowball.c_inc` | 1 | 123 |
| `src/game/m_player_sound.c_inc` | 1 | 336 |
| `src/game/m_scene.c` | 4 | 43, 117, 137, 144 |
| `src/game/m_scene_ftr.c` | 1 | 17 |
| `src/game/m_submenu.c` | 1 | 642 |
| `src/game/m_submenu_ovl.c` | 1 | 1979 |
| `src/game/m_train_control.c` | 2 | 71, 72 |
| `src/irqmgr.c` | 1 | 145 |
| `src/static/Famicom/famicom.cpp` | 4 | 819, 1202, 2047, 2107 |
| `src/static/Famicom/famicom_nesinfo.cpp` | 2 | 553, 966 |
| `src/static/Famicom/ks_nes_draw.cpp` | 9 | 132, 133, 134, 175, 176, 177, 275, 276, 277 |
| `src/static/JSystem/JFramework/JFWDisplay.cpp` | 2 | 331, 332 |
| `src/static/JSystem/JKernel/JKRAram.cpp` | 9 | 88, 145, 159, 196, 197, 226, 253, 459, 481 |
| `src/static/JSystem/JKernel/JKRAramPiece.cpp` | 4 | 97, 99, 106, 108 |
| `src/static/JSystem/JKernel/JKRAramStream.cpp` | 4 | 99, 111, 125, 202 |
| `src/static/JSystem/JKernel/JKRArchivePub.cpp` | 1 | 85 |
| `src/static/JSystem/JKernel/JKRCompArchive.cpp` | 12 | 98, 111, 112, 113, 147, 162, 163, 164, 191, 255, 257, 263 |
| `src/static/JSystem/JKernel/JKRDecomp.cpp` | 1 | 53 |
| `src/static/JSystem/JKernel/JKRDvdAramRipper.cpp` | 3 | 80, 154, 401 |
| `src/static/JSystem/JKernel/JKRDvdFile.cpp` | 2 | 114, 118 |
| `src/static/JSystem/JKernel/JKRDvdRipper.cpp` | 2 | 51, 108 |
| `src/static/JSystem/JKernel/JKRExpHeap.cpp` | 13 | 77, 160, 220, 302, 303, 307, 435, 555, 702, 704, 840, 896, 899 |
| `src/static/JSystem/JKernel/JKRHeap.cpp` | 7 | 62, 63, 73, 217, 234, 235, 241 |
| `src/static/JSystem/JKernel/JKRMemArchive.cpp` | 3 | 26, 87, 106 |
| `src/static/JSystem/JKernel/JKRThread.cpp` | 2 | 17, 28 |
| `src/static/JSystem/JUtility/JUTDirectFile.cpp` | 1 | 30 |
| `src/static/JSystem/JUtility/JUTException.cpp` | 5 | 231, 243, 288, 613, 614 |
| `src/static/JSystem/JUtility/JUTGraphFifo.cpp` | 1 | 19 |
| `src/static/JSystem/JUtility/JUTProcBar.cpp` | 1 | 235 |
| `src/static/JSystem/JUtility/JUTVideo.cpp` | 1 | 161 |
| `src/static/boot.c` | 5 | 233, 301, 618, 623, 780 |
| `src/static/initial_menu.c` | 2 | 404, 428 |
| `src/static/jaudio_NES/game/emusound.c` | 1 | 1582 |
| `src/static/jaudio_NES/game/game64.c_inc` | 1 | 1807 |
| `src/static/jaudio_NES/game/melody.c` | 1 | 668 |
| `src/static/jaudio_NES/game/rhythm.c` | 2 | 37, 38 |
| `src/static/jaudio_NES/internal/aictrl.c` | 2 | 63, 285 |
| `src/static/jaudio_NES/internal/aramcall.c` | 1 | 106 |
| `src/static/jaudio_NES/internal/bankdrv.c` | 1 | 103 |
| `src/static/jaudio_NES/internal/bankread.c` | 2 | 16, 30 |
| `src/static/jaudio_NES/internal/channel.c` | 4 | 516, 523, 530, 714 |
| `src/static/jaudio_NES/internal/cmdstack.c` | 9 | 57, 59, 62, 64, 78, 82, 124, 141, 143 |
| `src/static/jaudio_NES/internal/connect.c` | 3 | 177, 185, 192 |
| `src/static/jaudio_NES/internal/driver.c` | 20 | 338, 340, 346, 348, 515, 516, 520, 521, 651, 918, 921, 923, 928, 978, 989, 1002, 1047, 1179, 1202, 1204 |
| `src/static/jaudio_NES/internal/driverinterface.c` | 2 | 899, 1046 |
| `src/static/jaudio_NES/internal/dspbuf.c` | 1 | 55 |
| `src/static/jaudio_NES/internal/dspdriver.c` | 2 | 65, 110 |
| `src/static/jaudio_NES/internal/dspinterface.c` | 1 | 462 |
| `src/static/jaudio_NES/internal/dummyrom.c` | 7 | 30, 41, 48, 50, 61, 88, 90 |
| `src/static/jaudio_NES/internal/dvdthread.c` | 3 | 189, 215, 223 |
| `src/static/jaudio_NES/internal/heapctrl.c` | 11 | 25, 26, 45, 47, 69, 71, 72, 76, 78, 392, 539 |
| `src/static/jaudio_NES/internal/ipldec.c` | 2 | 99, 111 |
| `src/static/jaudio_NES/internal/jammain_2.c` | 1 | 2615 |
| `src/static/jaudio_NES/internal/memory.c` | 8 | 269, 270, 284, 643, 1372, 1421, 1425, 1513 |
| `src/static/jaudio_NES/internal/oneshot.c` | 4 | 222, 239, 294, 341 |
| `src/static/jaudio_NES/internal/rspsim.c` | 15 | 99, 101, 117, 211, 240, 290, 341, 398, 431, 437, 587, 601, 620, 629, 655 |
| `src/static/jaudio_NES/internal/seqsetup.c` | 3 | 175, 395, 401 |
| `src/static/jaudio_NES/internal/sub_sys.c` | 7 | 138, 142, 144, 146, 278, 689, 713 |
| `src/static/jaudio_NES/internal/system.c` | 70 | 573, 607, 858, 869, 895, 901, 939, 1062, 1063, 1094, 1098, 1103, 1104, 1105, 1116, 1117, 1118, 1134, 1140, 1141, 1148, 1161, 1170, 1174, 1178, 1181, 1182, 1192, 1193, 1212, 1216, 1220, 1225, 1228, 1229, 1235, 1256, 1265, 1300, 1386, 1437, 1438, 1439, 1445, 1448, 1546, 1579, 1580, 1581, 1602, 1658, 1737, 1746, 1842, 1936, 1942, 1953, 1982, 1993, 2019, 2039, 2042, 2071, 2075, 2081, 2086, 2240, 2285, 2309, 2456 |
| `src/static/jaudio_NES/internal/waveread.c` | 2 | 26, 39 |
| `src/static/jsyswrap.cpp` | 1 | 493 |
| `src/static/libc64/__osMalloc.c` | 12 | 9, 13, 92, 93, 194, 196, 220, 287, 422, 442, 452, 583 |
| `src/static/libforest/emu64/emu64.c` | 28 | 406, 3369, 3455, 3486, 3492, 3495, 3549, 3567, 3678, 3717, 3749, 3837, 3860, 3897, 3915, 3917, 3922, 4481, 4490, 4629, 4635, 5300, 5317, 5321, 5542, 5589, 5600, 5610 |

## 16. Final systematic audit pass (continuation session) — findings and verification

Date: 2026-09-22. Branch `multiplayer` (same commit `4099d24` as section 1; all work below is still uncommitted
working-tree state). Scope: re-verify everything sections 1-15 claim is fixed, re-run the test protocol, and sweep
for any remaining B/D-category issue before calling the x64 conversion done. This section does **not** duplicate
sections 1-15's per-site tables; it records what was re-checked, what (if anything) was fixed, and what is still open.

### 16.1 What this session found already in place (verified against the actual working tree, not just prior notes)

Two fixes existed in the working tree that are **not documented anywhere in sections 1-15** (they happened after
section 15 was written, in a later continuation of the project not captured in this file until now):

- **`src/actor/ac_structure.c`**: the `SHRINE_ACTOR` 8-byte slot overflow (`aSTR_PC_ACTOR_SLOT_SIZE = ALIGN_NEXT(sizeof(SHRINE_ACTOR), 16)`, union-based `aSTR_pc_actor_storage_c`, two `_Static_assert`s). This is the actual root-cause fix for the `__osFree:不正解放` panic loop that section 15.6 left as an open, uncharacterized blocker. Confirmed present and matches the described mechanism exactly (read in full; see the in-code comment at lines 29-42 for the root-cause writeup).
- **`src/static/jaudio_NES/internal/neosthread.c`**: `Neos_Update()` rewritten to build+execute the audio task synchronously in one call, removing the fragile `pc_neos_cur`/`pc_tasks[2]`/`pc_task_buf[2]` double-buffer. Confirmed present, matches the described fix exactly, including the documented first-call guard.
- **`src/static/jaudio_NES/internal/rspsim.c`**: `DMEMCount/DMEMIn/DMEMOut/loop_point/sp12C/sp128` are file-scope `static` (section 15's fix) — still in place, unchanged.

Also re-verified directly against source (all match sections 1-15's claims with no drift):

- `JKRExpHeap::CMemBlock` (`include/JSystem/JKernel/JKRExpHeap.h`): `PcLowPtr<CMemBlock> mPrev/mNext` under `PC_LOW_ADDRESS_64`, `getBlock()` uses `(u8*)data - 0x10`, `_Static_assert(sizeof(CMemBlock) == 0x10)`.
- `JKRArchive::SDIFileEntry` (`include/JSystem/JKernel/JKRArchive.h`): `PcLowPtr<void> mData`, `_Static_assert(sizeof(SDIFileEntry) == 0x14)`.
- `ac_npc.h:345`: `void* _0FC[3]` (fixed from the raw `sizeof(void*)`-divided count).
- `pc/src/pc_aram.c`: `ARStartDMA`/`ARQPostRequest` use `PC_PTR32(...)` for the dual-use ARAM/host-pointer disambiguation exactly as A2/B5 describe.
- `src/static/libforest/emu64/emu64_utility.c` `seg2k0()` (`PC_LOW_ADDRESS_64` branch): resolution order is (1) runtime-packed-pointer unpack via `pc_gbi_unpack_runtime_ptr`, (2) bit-0 tag for a direct PC pointer, (3) `segadr>>28 != 0 || segadr < 0x03000000` ⇒ raw pointer, (4) inside `[pc_image_base, pc_image_end)` ⇒ raw pointer, (5) otherwise resolve via `segments[]`. This is the D7/D8 fix — it did not exist as such in section 1's snapshot and closes the "image/heap address mistaken for a segment address" collision that sections 2/9 worried about, *for the image itself*. See 16.3 for a residual gap this does **not** close.
- `src/static/jaudio_NES/internal/system.c` and related jaudio files: on-disc pointer fields use the `JA_FPTR(...)`/`JA_FPTR_U32(...)` wrapper (from the now-tracked `include/jaudio_NES/ja_fileptr.h`) everywhere sections 1/8-9 flagged raw `(u32)ptr` casts on serialized structures. Spot-checked `Nas_BankOfsToAddr_Inner`, `__WaveTouch`, `VoiceLoad`, `Nas_BankOfsToAddr` — all converted, none left as bare casts.
- `pc/src/pc_gbi_runtime.c` (`pc_gbi_pack_runtime_ptr`/`pc_gbi_unpack_runtime_ptr`): asserts via `PC_LOWADDR_ASSERT` before packing; odd-alignment fallback table bounds-checks with unsigned wraparound-safe arithmetic (`token = packed - BASE; token < COUNT*2`) — confirmed correct even when `packed < BASE` (wraps to a huge value that safely fails the range check).

### 16.2 Verification re-run this session

1. **Build**: `mingw32-make -j16` in `pc/build64` (UCRT64 GCC 16.2.0, via explicit `PATH=/c/msys64/ucrt64/bin:...` — the default shell's `mingw32-make`/`gcc` resolve to a *different*, non-UCRT64 MinGW and must not be used for this project). Result: **0 files rebuilt** — `[100%] Built target ac_pc` with no compilation, i.e. the object files on disk already reflect every file currently shown modified by `git diff`, confirming the working tree state was already built and is not stale relative to the last successful build.
2. **Low-address self-test**: `./AnimalCrossing.exe --verbose --lowaddr-selftest`. Result: `[SELFTEST] done: 0 failure(s)`, `[LOWADDR] total violations: 0` (13 tracked categories: executable image, thread/process/main stacks, malloc probes, OS arena, jaudio wave/bank/scene selftest images, JKR arena/heap, `__osMalloc` arena — all below the 4 GB limit). Exit code 0.
3. **Stability run A** (`--verbose --no-framelimit`, uncapped speed, real ROM `GAFE01`): ran unattended, reached NEOS audio frame 6061+ (far more audio-thread iterations than the 90-second/60 Hz runs in section 15.4) sitting at the title "press start" screen, then killed manually. Zero occurrences of `不正解放`/`OSPanic`/`SIGSEGV`/`VIOLATION` in the log. This is the scenario that reproduced the `__osFree` panic loop 3/5 times in section 15.6 (same LOGO/`aAL_setupAction` path, now run far longer); it did not reproduce, consistent with the `ac_structure.c` fix in 16.1 being the correct root-cause fix.
4. **Stability run B** (`--verbose`, normal frame-limited speed): boots identically (disc mount, RARC loads, `trademark_init`, LOGO attract-mode cycle `action 0→1→2→3` repeating cleanly). Attempted to advance past the title screen with synthetic keyboard input (Win32 `SetForegroundWindow` + `SendKeys`) to reach real gameplay/actor spawn; **this did not work** — the title screen's own attract-mode loop kept cycling on a timer regardless of the synthetic key events, so no evidence either way was gained about further-in gameplay. This is a limitation of the sandboxed test environment (synthetic `SendKeys` doesn't reach SDL2's input backend), not a finding about the game. **Real actor gameplay (spawning NPCs/items, the exact code path the `ac_structure.c` fix and the B7 "actor identity label" cases exercise) was not re-verified interactively this session** — see limitations below.

### 16.3 New residual risk found (not fixed — no evidence it has ever fired)

`pc_gbi_pack_runtime_ptr`'s odd-pointer fallback token table (`pc/src/pc_gbi_runtime.c`) maps unusual (odd-address,
i.e. not 2-byte-aligned) runtime pointers to synthetic tokens in the fixed range `[0x02F00000, 0x02F00000 + 8192*2)`
= `[0x02F00000, 0x02F08000)`. `emu64::seg2k0()`'s `PC_LOW_ADDRESS_64` path (16.1) calls
`pc_gbi_unpack_runtime_ptr()` **unconditionally first**, for every address it resolves, not just ones that actually
went through the packer. Since `0x02F00000` is inside the "below `0x03000000`" window that the rest of `seg2k0`
treats as unambiguous, any *unrelated* 32-bit value that happens to land in that ~32 KB window (for example a real
N64 segment address with `seg=2` and a large offset near `0xF00000-0xF08000`, should the game ever populate segment
2 with an offset that large) would be misread as a token lookup. If that slot was never actually assigned by the
packer, `s_odd_ptr_tokens[...]` is `0`, and `pc_gbi_unpack_runtime_ptr` correctly returns `0`, which `seg2k0` treats
as "not a special case" and falls through correctly — **so the failure mode requires a real odd-pointer token to
already occupy that exact slot from an unrelated, no-longer-valid earlier packing**, which requires (a) the
odd-pointer fallback path to have actually been exercised (its own `fprintf` warns the first time it is — not seen
in any capture this session, meaning it has apparently never fired against the real ROM) and (b) a segment address
to coincidentally collide with an occupied slot. Given no capture in this or prior sessions has ever printed the
"`[GBI] odd pointer alignment`" warning, this is classified **D (needs runtime validation, not proven broken)**,
not fixed speculatively. If it is ever seen, gate `pc_gbi_unpack_runtime_ptr`'s lookup on the caller side (only call
it from the one runtime-pointer-tagged path, not unconditionally in `seg2k0`) rather than widening the token range.

### 16.4 Format-specifier sweep (D3) — not done exhaustively

A full `-Wformat` compiler pass (the method section 6/D3 recommends) requires changing global `CMAKE_C_FLAGS`,
which is not tracked by GNU Make's incremental dependency check the way this project's Makefiles are generated —
it would force a full ~4,000-object rebuild to get the warnings. Given the build already passes runtime validation
with zero observed corruption from this class of bug, and every `%x`/pointer `printf` site found in earlier greps
(section "Appendix A9") is a `Printf0`/`OSReport`/diagnostic-only call (never a value fed back into game logic),
this was left undone this session as low-value/high-cost. Not a correctness risk to the game, only to diagnostic
output readability. Left as an open item for a session that can afford the full rebuild.

### 16.5 Files changed this session

**None.** Every item audited (16.1) was already correct in the working tree; 16.3 is a documented residual risk
with no reproduction, not a fix. This file (`64BIT_MIGRATION_AUDIT.md`) is the only file touched, to record the
re-verification (per this project's own convention of logging each audit pass here — see sections 9-15, each
added by a different session).

### 16.6 Updated overall status

- Build: **0 errors**, working tree fully built (no stale objects).
- Low-address self-test: **0 failures, 0 violations**.
- Boot: disc mount → `trademark_init` → LOGO attract loop, clean, repeatable.
- Audio: confirmed fix in place (`neosthread.c`), stress-tested to 6000+ NEOS frames with 0 crashes this session
  (user's own prior 95-second/0-click listening test from the task brief is the authoritative audio-quality
  signal; this session only re-confirmed structural/crash stability, not perceptual audio quality).
- Actors: confirmed fix in place (`ac_structure.c`); the specific panic loop it targets did not reproduce in a
  longer stress run than previously achieved. **Not re-verified against live actor spawning this session** — no
  interactive input reached the game (16.2.4).
- Outstanding: 16.3 (unreproduced, low-likelihood token-table collision), 16.4 (format-specifier warnings pass,
  deferred, diagnostics-only), and everything sections 1-15 already listed as Category D/未検証 that this session
  did not re-touch (D4 Dolphin type stubs enumeration, D6 audio-thread AI-DMA address bound, full D1 coverage of
  every allocation site beyond what the low-address self-test's 13 tracked categories cover).

## 17. Closing the remaining audit items (continuation session)

Date: 2026-09-22, same session as section 16 continued. Scope: close 16.3 (seg2k0/token collision), D7/D8, B5
(ARAM dual-use), D1 (self-test/report coverage), D4 (Dolphin type stubs), D6 (audio DMA bounds), D3 (format sweep),
in that priority order, per explicit instruction to trace rather than assert "unlikely." Two real, if narrow, gaps
were found and fixed; everything else traced to a documented, evidence-based A/C/E classification with no fix
needed. Unlike section 16, this section made source changes (listed in 17.7).

### 17.1 seg2k0 / GBI odd-pointer token collision (16.3) - full trace, classification D -> fixed with guard + test

**Token allocation**: `pc_gbi_pack_runtime_ptr()` (`pc/src/pc_gbi_runtime.c`) is called only via `_GBI_RUNTIME_PTR(s)`
(`include/libforest/gbi_extensions.h`), which itself is reached only from the *live* (non-`s`-prefixed) GBI macros:
the base `gDma0p/gDma1p/gDma2p` in `include/PR/gbi.h` (which `gSPVertex`, `gSPMatrix`, `gSPDisplayList`, and every
other live SP/DP command macro expand through) and the `_Dolphin` texture/TLUT extension macros in
`gbi_extensions.h`. **Exhaustively grepped every `->words.w1 =` / `.words.w1 =` assignment in `src/` and `pc/src/`**:
the only non-macro direct assignments found are bit-packed non-address parameter words (vertex-triangle indices,
alpha, tile size, mode flags in `gbi_extensions.h` and `pc/src/pc_text_draw.c`) and jaudio `Acmd` words (a
completely separate command format, not resolved by `seg2k0()` at all - see 17.6). **No code path writes a
runtime/dynamic pointer into a live `Gfx` word without going through `_GBI_RUNTIME_PTR`.**

**Token lifetime/release**: `s_odd_ptr_tokens[8192]` is a ring buffer; `s_odd_ptr_token_next` increments
unconditionally and wraps via `& (COUNT-1)`. There is no explicit release - a slot's old value is simply
overwritten when the counter wraps back to it (verified in 17.1's new self-test, see below). A token is produced
only when the incoming address already has bit 0 set (`(addr & 1u) == 0` fails); this requires a **misaligned**
runtime pointer, which none of the live macros above are ever fed in this codebase (all measured/typical texture,
palette and vertex-buffer pointers are compiler/allocator-aligned to >= 2 bytes) - the fallback path is defensive,
not the common case. Its own one-shot `fprintf` warning (`s_warned_odd_ptr`) has never fired in any capture from
any session, including the extended runs in this one.

**Segment address formation**: `SEGMENT_ADDR(num, off) = (num<<24)+off` (`include/PR/mbi.h:94`). Grepped every
direct call site of `SEGMENT_ADDR(` in `src/` (3 total: `initial_menu.c` segments 8/9 offset 0, `ac_field_draw_gfx.c_inc`
segment `G_MWO_SEGMENT_A` offset 0) and every use of the named `anime_1..6_txt/model`/`softsprite_mtx` constants
(`gbi_extensions.h:44-50`, segments 7-13 only). **Extracted every literal offset ever added to one of those named
constants across the whole tree** (`anime_N_txt/model + <offset>`, all `src/data/npc/model/mdl/*.c`): maximum found
is `0x780` (1920) bytes. **Segment index 2 - the only index whose `(seg<<24)+off` range for any offset 0..0xFFFFFF
can reach the reserved token window `[0x02F00000,0x02F04000)` - is never used anywhere in this codebase.** A real
segment-encoded literal reaching the token window is therefore not just "unlikely," it requires a segment index
that literally does not exist in any macro, constant or call site in the current source tree.

**seg2k0() range checks / all callers**: all 17 callers of `seg2k0(` (`emu64.c`, `emu64_print.cpp`) pass a value
taken directly from a `Gfx`/`movemem`/`rdpHalf` word - i.e. exactly the same value space just traced. The
`PC_LOW_ADDRESS_64` resolution order is: (1) `pc_gbi_unpack_runtime_ptr` token lookup, (2) bit-0 tag, (3)
`segadr>>28!=0 || segadr<0x03000000` -> raw pointer, (4) inside `[pc_image_base,pc_image_end)` -> raw pointer, (5)
segment table. Step (1) runs *before* (3)/(4), which is what makes the token window's exact address range matter
at all - a value in `[0x02F00000,0x02F04000)` skips straight past the "obviously a raw pointer" heuristic in (3)
into the token check. Given (a) no real segment literal ever reaches that window (proven above) and (b) every real
dynamic pointer is tagged (bit 0 forced) before storage - and `pc_gbi_unpack_runtime_ptr` only matches an *even*
delta from the base, so a tagged (odd) pointer numerically inside the window is *already* excluded by construction,
regardless of its magnitude - the only theoretical remaining exposure is an **untagged**, **even**, **compile-time
static** pointer (`_GBI_STATIC_PTR`, no tagging) landing in the window. Measured: the executable image (where every
static pointer lives) currently spans `0x00400000-0x0023e4000` (~35.6 MB), comfortably under the window
(~47.0-47.02 MB); dynamic allocations (JKR heap, OS arena, ARAM) start at ~79-84 MB in every observed run, above it.

**Fix (classification D, closed)**: rather than rely on this margin staying true forever, added a generic reserved-
range API: `pc_lowaddr_reserve_range(name, lo, hi)` + an automatic cross-check inside `pc_lowaddr_report()`
(`pc/src/pc_lowaddr.c`) that compares every registered reserved range against every tracked allocation category's
observed `[min,max_end)`. `pc_gbi_runtime.c` registers the token window via an `__attribute__((constructor))`
(runs before `main()`, so it is always registered before the first report). The check treats an overlap with the
**executable image** category as a real, fatal violation (that is the one category using untagged addresses) and
logs, but does not fail on, an overlap with any dynamic category (heap/arena/malloc - expected and harmless per the
tagging argument above; verified this distinction is necessary, not cosmetic, in 17.2). This turns "we believe the
image will never grow into the window" into something the game itself checks on every run, forever, instead of a
one-time human argument.

**Deterministic synthetic test (new)**: `pc_gbi_token_selftest()` (`pc/src/pc_gbi_runtime.c`, wired into
`pc_lowaddr_selftest()`) exercises, with fabricated addresses (no real pointers dereferenced):
1. an even address packs to `addr|1` and never lands in the token window;
2. a non-pointer expression (`is_ptr=0`, e.g. a raw `SEGMENT_ADDR()` integer) passes through untouched;
3. a deliberately odd address lands in the token window and round-trips exactly through `unpack()`;
4. a never-assigned address inside the window unpacks to 0 (falls through correctly), not garbage;
5. a value just *below* the window's base (`BASE-4`) does not underflow into a false-positive match;
6. filling all 8192 slots with distinct odd values round-trips every one, and one further pack correctly evicts
   slot 0 to the newest value (verifies the wraparound is a clean overwrite, not corruption of an unrelated slot).

All 6 pass. Verified live: `--lowaddr-selftest` logs `[GBI] odd pointer alignment: odd_test ... = 0x87654321` (test
3 deliberately triggering the fallback path) and `[SELFTEST] GBI odd-pointer token table: pack/unpack round-trip,
underflow, and 8192-slot wraparound all correct`.

### 17.2 Why the reserved-range check must not fire on dynamic categories (measured, not assumed)

First implementation flagged *any* category overlap as a violation. Running `--lowaddr-selftest` immediately
produced: `VIOLATION reserved range "GBI odd-pointer token table" [0x2f00000,0x2f04000) overlaps category
"malloc probe" [0x2707610,0x5eb6040)`. This is real - a plain `malloc()` probe's address range does span through
the window on this system - but it is **not a bug**: per 17.1, any pointer that reaches a live `Gfx` word is always
tagged (bit 0 forced) first, so its raw magnitude is irrelevant to whether it can be confused for a token (the
odd/even check in `unpack()` already excludes it). The "malloc probe" category is also itself synthetic (allocated
and freed immediately by `pc_lowaddr_init()`, never embedded in a `Gfx` word at all). Narrowed the check to treat
only the "executable image" category's overlap as fatal (17.1); the malloc-probe overlap is now logged as an
explicit, permanent `note:` line, not a failure. This is not a hypothetical distinction - it was measured to fire
falsely on the very first run, which is exactly why it needed a real trace instead of a one-line "seems fine."

### 17.3 B5: ARAM dual-use fields - full per-field classification

Traced every field the audit's B5 flagged, plus their actual callers (not just the declarations):

| Field | Type | Classification | Evidence |
|---|---|---|---|
| `JKRAMCommand::mSource`/`mDestination` (`JKRAram.h:179-180`) | `u32`, dual-use | **Dual-use (by design), correctly resolved** | Every caller (`JKRAramPiece::prepareCommand`, all `JKRAramPcs()` call sites in `JKRAram.cpp`/`JKRAramStream.cpp`) passes the MRAM side as a real host pointer (`(u32)buf`/`(u32)allocatedMem`/etc.) and the ARAM side as a genuine byte offset (`block->getAddress()`, a running counter, or a caller-supplied `u32 address`). `ARQPostRequest`/`ARStartDMA` (`pc/src/pc_aram.c`) apply the `type==0`/`type==1` swap exactly matching each call site's convention, then treat the MRAM arg as `(void*)(uintptr_t)` and the ARAM arg as a 0-based offset into `aram_base`, with a defensive `base <= aram_addr < base+SIZE` re-normalization in case a caller passes `aram_base+offset` instead of a bare offset (the comment there says this happens; not verified which caller, but the guard is measured to be exercised safely either way). |
| `JKRAramBlock::mAddress` (`JKRAramBlock.cpp`) | `u32` | **A - genuine ARAM offset** | Never a host pointer; arithmetic (`mAddress + mSize`, etc.) is pure ARAM-space bookkeeping. |
| `JKRAramStream::write_StreamToAram_Async(JKRAramBlock* addr, ...)` casting `(u32)addr` (`JKRAramStream.cpp:125`) | `u32` from a real C++ object pointer | **E - dead code** | The function's own comment reads "Unused function, made-up contents. Do not take this seriously!"; the real call sites (`JKRDvdAramRipper.cpp:142,145`) resolve to the *other* overload that takes a plain `u32 addr` directly (from `command->mBlock->mAddress`, a genuine ARAM offset), never this one. Confirmed by grep: no caller of the `JKRAramBlock*`-taking overload exists anywhere in `src/`. |
| `ALIGN_NEXT((u32)buffer, 32)` style casts in `JKRDvdRipper.cpp`, `JKRAram.cpp` (stack/heap alignment scratch) | host pointer -> u32 | **C - lossless under the low-address guarantee** | Same pattern as the already-audited A6 category; no dual-use ambiguity, just alignment math on a real pointer that is guaranteed < 4 GB. |

No B5 fix needed; all dual-use sites were already correctly disambiguated by direction flag + range check, and the
one suspicious cast is unreachable dead code.

### 17.4 D1: self-test/report coverage - real gap found and fixed

Enumerated every `PC_LOWADDR_CHECK`/`PC_LOWADDR_ASSERT`/`PC_PTR32` call site in the tree (grep, ~20 distinct
category names). `--lowaddr-selftest` only exercises the categories reachable *before* `pc_disc_init()`/`ac_entry()`
(executable image, stacks, malloc probes, DOL asset source) - by design, it runs without a ROM. The categories that
only exist during real gameplay (OS arena, JKR arena/heap, ARAM buffer, emu64 instance/texture caches, GX texobj,
actor allocations, jaudio PTconvert) are supposed to be covered by the **second** `pc_lowaddr_report()` call in
`pc_main.c`, placed after `boot_main()` returns, at the natural end of a play session.

**That second call is dead code.** `boot_main()` never returns on `TARGET_PC`: `mainproc()` (`src/main.c:97-103`)
calls `graph_proc()` directly (single-threaded) and, when it returns (the game's only quit path, `SDL_QUIT` via
window close), calls `pc_platform_shutdown(); exit(0);` directly - bypassing `pc_main.c`'s own cleanup and its
trailing `pc_lowaddr_report()` entirely. Confirmed by running a full session (boot -> title -> clean window-close
via `WM_CLOSE`) before this fix: the log contained exactly one `[LOWADDR] ---- category summary ----` block (the
early one, 6 categories only), never a second one.

**Fix**: added the `pc_lowaddr_report()` call to `src/main.c` right before the real `exit(0)` (guarded
`#ifdef PC_LOW_ADDRESS_64`), and left `pc_main.c`'s original trailing call in place with a comment explaining it is
unreachable today but harmless to keep for a future code path that might restore a normal return from `boot_main()`.

**Verified**: same boot -> title -> `WM_CLOSE` sequence after the fix now prints **two** summary blocks - the early
6-category one and a full one at real exit showing `OS arena (24 MB)`, `JKR arena (initArena)`, `JKR heap`,
`ARAM host buffer (16 MB)`, `emu64 instance`/`emu64 texture cache (.data/.bss)` (all with `n` in the thousands -
once per LOGO-screen render call - and 0 violations), `game heap (__osMalloc arena)`. **Categories still not seen
in any run this session**: `GX texobj image`, `GX TLUT`, `EFB copy dest`, `actor allocation`/`actor overlay
buffer`, `jaudio PTconvert (file offset + base)`. These are reached only past the title screen (real scene/actor
load, palette-based UI screens); this session could not get synthetic input to advance past the title (same
limitation as section 16.2.4), so their coverage remains **unverified by a live run**, though every one of those
call sites already uses `PC_PTR32`/`PC_LOWADDR_CHECK` correctly per direct code reading (section 16 and prior
sessions already audited `pc_gx_texture.c`, `m_actor.c`, `bx.h` directly).

Per the instruction to not count allocations that never enter the game's 32-bit representation: the "malloc probe"
allocations (freed immediately, never embedded in a `Gfx`/ARAM/jaudio word) and any host-only `new`/`std::vector`
growth used purely for C++ bookkeeping (not found in this codebase's hot paths - grep shows no `std::vector`/`new`
storing into a `u32` field anywhere outside the already-audited `pc_lowaddr_spike` diagnostic tool, section 9.3)
are correctly *not* tracked as categories, since narrowing them to 32 bits never happens.

### 17.5 D4: Dolphin type-stub enumeration - no issues found

Enumerated every stub type actually used cross-boundary: `CARDFileInfo`/`CARDDir` (`include/dolphin/card.h`) -
**all-integer fields, no pointers at all** (`s32 chan/fileNo/offset/length`, `u16 iBlock`; `CARDDir.iconAddr` is a
documented on-card serialized offset, not a host pointer) - layout is pointer-width-independent by construction,
Category A. `OSThread` (`include/dolphin/os/OSThread.h`) - real native pointer fields (`OSThread*`, `OSMutex*`,
`u8* stackBase`), but this is a pure in-memory runtime struct never serialized to disk/ROM and never accessed by
hardcoded byte offset from outside its own API (`pc_os.c`'s single-threaded stub implementation treats it as an
opaque handle) - widening on x64 is free, Category B already correctly handled by construction. `OSContext`/
`OSAlarm` (`pc_os.c:322,456`) - opaque `u8 _pad[N]` stub structs, never given real fields, never serialized -
Category E. `pc_stubs.c`/`pc_stubs_cpp.cpp` (101+34 lines) - every function takes `void*`/opaque handles, no struct
layout exposed or assumed anywhere. `GXTexObj` - already handled via the `u32[22]` blob + `PC_PTR32` pattern
(`pc_gx_texture.c`, audited in section 16.1). No fix needed; D4 closed.

### 17.6 D6: audio-thread AI-DMA bounds - real gap found and fixed

Traced the full path: `Jac_Init()`/`Jac_UpdateDAC()` (`src/static/jaudio_NES/internal/aictrl.c`) allocate `dac[3]`
via `OSAlloc2()` (the jaudio heap, backed by the game's `__osMalloc` arena - already low-address-checked) and call
`AIInitDMA(addr, size)`. On PC (`pc/src/pc_audio.c`), `AIInitDMA` converts `addr` back to a pointer
(`(s16*)(uintptr_t)addr`) and copies `size/2` samples (rounded to whole stereo frames, **clamped to the ring
buffer's actual free space** - `if (n_samples > free) n_samples = free & ~1u;`) into a lock-free SPSC ring buffer
(`ring_buffer[RING_BUF_SAMPLES]`, power-of-2 size, `& RING_BUF_MASK` indexing). The ring-buffer indexing itself is
pure sample-count arithmetic with no pointer-width dependency - not an x64 risk. The SDL callback (`pc_audio_callback`)
consumer side has its own, separate overrun guard (`if (used > RING_BUF_SAMPLES) rp = wp - RING_BUF_SAMPLES;`).
**No out-of-bounds read or write is possible from either side**: the source read (`src[i]`, `i < n_samples`) never
exceeds the caller-declared `size`, and every ring-buffer write/read is masked to `RING_BUF_MASK`.

**Gap found**: `AIInitDMA((u32)dac[2], ...)` and `AIInitDMA((u32)use_rsp_madep, ...)` (`aictrl.c:63,~296`) truncate a
real host pointer (from the low-address-checked `__osMalloc`/jaudio heap) with a bare `(u32)` cast, **not** the
`PC_PTR32` checked helper used at every other real-host-pointer-to-u32 site in the codebase (A2's own pattern).
Currently harmless (the jaudio heap is itself always < 4 GB, verified via `game heap (__osMalloc arena)` in the
17.4 report), but it is a real, silent gap in the established safety net - exactly the class of site A2 was written
to close, just two instances A2's original grep (scoped to `pc/src`) did not reach (`aictrl.c` is in
`src/static/jaudio_NES`). **Fixed**: both sites now use `PC_PTR32("AIInitDMA dac buffer", ...)` under
`#ifdef TARGET_PC` (the `#else` branch, real GC 32-bit build, is untouched - `(u32)ptr` there is already lossless
by definition since pointers are natively 32-bit). Verified: rebuilds clean, `--lowaddr-selftest` still 0/0, and a
subsequent stability run produced continuous audio (`[NEOS_OUT]` frames) with no `PC_LOWADDR_ASSERT` firing.

### 17.7 D3: format-specifier sweep - scoped, evidence-based (not a full ~4000-object rebuild)

A full rebuild with `-Wformat` enabled project-wide would require temporarily removing the `-w` flag that the
decomp code (`src/**`, the vast majority of the ~4,000 objects) is deliberately compiled with (see
`pc/DOCUMENTATION.md`'s "Common Pitfalls"; confirmed present via `CMakeFiles/ac_pc.dir/flags.make`). Doing that
project-wide would produce a volume of warnings (most from ~2,200 pure-data `Gfx`-array files with no `printf`
calls at all, so zero relevant signal from the bulk of the object count) that cannot be triaged manually in this
session. Instead: (1) `pc/src/*.c` already compiles with `-Wall -Wextra` (which includes `-Wformat`) on **every**
normal build via a per-file `CMakeLists.txt` override (`flags.make` confirms this) - already effectively swept
continuously, and produced zero format warnings in every rebuild this session. (2) Ran a targeted
`-fsyntax-only -Wformat -Wformat-security` pass (same defines/includes as the real build, `-w` *not* applied) over
every file Appendix A9 named plus the full jaudio internal set most likely to combine a `u32`/pointer value with a
`printf`-family call: `boot.c`, `__osMalloc.c`, `emu64.c`, `initial_menu.c`, and `jaudio_NES/internal/{system,aictrl,
driver,rspsim,neosthread}.c`.

**Result**: zero pointer/size-mismatch warnings (the dangerous class - e.g. printing a truncated 64-bit pointer with
`%x` and losing the top bits - which would be a real x64 bug). The only warnings found are in `system.c` (3 sites,
line ~1617/1629/1645) and `neosthread.c` (1 site, line 102): `%d`/`%u` given a `u32`/`s32`-typed argument, which
GCC reports because `u32`/`s32` are `typedef`d to `unsigned long`/`long` and are therefore not the *identical* type
as plain `int`/`unsigned int` in C's format-checking rules - **but on this project's LLP64 Windows x64 target,
`long` and `int` are both exactly 32 bits**, so the value is read correctly regardless (this is the exact
distinction Category C of section 0 already makes: "`u32 = unsigned long` ... No, and it must not be changed ...
On Linux LP64 this typedef *would* break"). Confirmed harmless by the already-observed correct output of these
exact log lines in every capture this session (sensible, non-garbage frame/sample counts). Classification: **E**
(false positive on this target; would only be a real bug on a hypothetical LP64 Linux x64 build, which this
project does not target). No fix applied - "fixing" these (e.g. to `%lu`/`%ld`) would just be `printf`-pedantry
with no behavior change on the only platform this project builds for, and section D3 explicitly asks to
distinguish diagnostic-only findings from correctness problems.

### 17.8 Files changed this session (17)

| File | Change |
|---|---|
| `pc/include/pc_lowaddr.h` | Added `pc_lowaddr_reserve_range()` declaration + no-op 32-bit stub. |
| `pc/src/pc_lowaddr.c` | Added the reserved-range registry and the `executable image`-only-fatal cross-check inside `pc_lowaddr_report()`. |
| `pc/src/pc_gbi_runtime.c` | Registered the GBI odd-pointer token window as a reserved range (constructor); added `pc_gbi_token_selftest()` (6 checks, see 17.1). |
| `pc/src/pc_lowaddr_selftest.cpp` | Wired `pc_gbi_token_selftest()` into `pc_lowaddr_selftest()`. |
| `pc/src/pc_main.c` | Comment only, clarifying the trailing `pc_lowaddr_report()` call is unreachable (17.4) and why it is kept. |
| `src/main.c` | Added the real final `pc_lowaddr_report()` call before the game's actual `exit(0)` (17.4). |
| `src/static/jaudio_NES/internal/aictrl.c` | Two `(u32)ptr` -> `PC_PTR32(...)` fixes under `TARGET_PC` (17.6); GC 32-bit path untouched. |
| `64BIT_MIGRATION_AUDIT.md` | This section. |

### 17.9 Verification re-run after all 17.x changes

1. **Build**: clean, 0 errors (only the file(s) actually touched recompiled each time; full `mingw32-make -j16`
   after the final edit reports `[100%] Built target ac_pc` with 0 further rebuilds needed).
2. **Low-address self-test**: `--lowaddr-selftest` exit code 0 (0 failures + 0 violations), including the 6 new
   `pc_gbi_token_selftest()` checks and the reserved-range cross-check (correctly non-fatal `note:` on the
   malloc-probe overlap, would be fatal on an executable-image overlap).
3. **Runtime**: two full boot -> LOGO/title -> clean-quit (`WM_CLOSE` -> real `SDL_QUIT` -> real `exit(0)` path)
   sessions, one at capped 60 Hz and one uncapped (reaching NEOS audio frame counts in the hundreds of thousands of
   `emu64 instance` calls). Zero `不正解放`/`OSPanic`/`SIGSEGV`/`VIOLATION` in either log. Both produced the full
   two-block `[LOWADDR]` report confirming 17.4's fix.
4. **Not verified this session**: live gameplay past the title screen (no working synthetic input path in this
   sandbox - same limitation noted in section 16), so `GX texobj`/`actor allocation`/`jaudio PTconvert` categories'
   *runtime* addresses were not captured in a report this session (their code is still directly audited and
   believed correct, per 17.4's last paragraph and prior sessions' section 16.1 findings). **Closed in section 18**,
   including a correction to how "coverage" of these three categories should be read at all.

## 18. Live gameplay verification (user-provided) - closes the runtime-coverage gap, corrects a misreading in 17.4

Date: 2026-09-22, same day. The automated-input investigation requested after section 17 confirmed the project's
real input path (`PADRead()` in `pc/src/pc_pad.c`, `SDL_GetKeyboardState`/`SDL_GameController`, edge-triggered
`pads[PAD0].on.button` in `padmgr.c:295-296`) and, after exhausting `SendKeys`, held `keybd_event`, mouse-click
focus acquisition and `AttachThreadInput` - all standard OS-level techniques, no game-code changes - proved via a
controlled zero-input experiment (identical attract-mode transition at the identical log line in both an
input-injection run and a no-input run) that this sandbox cannot deliver synthetic keyboard input to the game
window (`AttachThreadInput` failed; `GetFocus()` returned NULL for the game's own window even while it was the
foreground window). Per instruction, no workaround was hacked into the input system; the user was given exact
manual steps and ran them on their own machine: launched the exe, played for real (town/save selected, walked
around spawning actors, opened menus/inventory, heard audio), saved, and exited via the window's close button,
then supplied the resulting `lowaddr.log`.

### 18.1 The log

```
[LOWADDR] total violations: 0        (early checkpoint, after asset init)
...
[JFWSystem] system heap request 0x17FCE30 does not fit in root heap (free 0x17FCDD0); using 0x17FCDD0
[LOWADDR] executable image             n=1      lowest=0x000400000 highest_end=0x0023e4000 violations=0
[LOWADDR] thread stack (CreateThread)  n=1      lowest=0x0047eff4c highest_end=0x0047eff50 violations=0
[LOWADDR] process heap (GetProcessHeap) n=1      lowest=0x002670000 highest_end=0x002670001 violations=0
[LOWADDR] main thread stack            n=1      lowest=0x0025efe1c highest_end=0x0025efe20 violations=0
[LOWADDR] malloc probe                 n=3      lowest=0x002689df0 highest_end=0x005dfa040 violations=0
[LOWADDR] asset source: DOL image      n=1      lowest=0x04ece4240 highest_end=0x04edc4700 violations=0
[LOWADDR] OS arena (24 MB)             n=1      lowest=0x0506f5040 highest_end=0x051ef5040 violations=0
[LOWADDR] JKR arena (initArena)        n=1      lowest=0x0506f8160 highest_end=0x051ef5040 violations=0
[LOWADDR] JKR heap                     n=2      lowest=0x0506f8260 highest_end=0x051ef5040 violations=0
[LOWADDR] ARAM host buffer (16 MB)     n=1      lowest=0x051f0d040 highest_end=0x052f0d040 violations=0
[LOWADDR] emu64 instance               n=543431 lowest=0x0022caec0 highest_end=0x0022cd210 violations=0
[LOWADDR] emu64 texture cache (.data)  n=543431 lowest=0x0022d6400 highest_end=0x002356400 violations=0
[LOWADDR] emu64 texture cache (.bss)   n=543431 lowest=0x0022d2400 highest_end=0x0022d6400 violations=0
[LOWADDR] game heap (__osMalloc arena) n=24     lowest=0x052f10060 highest_end=0x0695779c0 violations=0
[LOWADDR] total violations: 0
```

This is unambiguously a real, extended play session, not a title-screen idle: the game heap grew 24 times (`n=24`
vs. `n=1` in every boot-only capture this project has ever produced - each growth is `__osMallocAddBlock` being
called because the arena needed more room, i.e. real scene/save-data churn), the heap's high-water mark reached
`0x0695779c0` (~2.5x further than any prior capture), and `emu64 instance` was invoked 543,431 times (vs. ~500-
120,000 in this session's own boot-only/title-idle captures) - consistent with sustained real rendering over
actual gameplay time, not a static screen. The `[JFWSystem]` line (system heap sizing right at the root-heap free
budget) is expected, cosmetic boot-time output, unrelated to x64 (present in every capture this project has ever
made, including 32-bit-era ones referenced in `pc/DOCUMENTATION.md`). **Zero `VIOLATION` lines anywhere in the
log, zero crash, zero corrupted save (the user reports a normal save + clean exit).**

### 18.2 Correcting 17.4: why GX texobj / actor allocation / jaudio PTconvert never appear - and why that is correct

Section 17.4 treated the absence of `GX texobj image`, `GX TLUT`, `EFB copy dest`, `actor allocation`,
`actor overlay buffer`, `ARQ request` and `jaudio PTconvert (file offset + base)` from every report as *unverified
coverage*, to be closed by getting real gameplay. That framing was wrong about the mechanism, discovered only now
by re-reading `pc_lowaddr.h` against this real log: **every one of those sites uses `PC_LOWADDR_ASSERT` or
`PC_PTR32`, not `PC_LOWADDR_CHECK`.**

```c
/* pc_lowaddr.h */
#define PC_LOWADDR_CHECK(cat, p, sz) pc_lowaddr_check((cat), (p), (size_t)(sz), __FILE__, __LINE__)  /* always registers */
#define PC_LOWADDR_ASSERT(cat, p) do { if (... >= LIMIT) pc_lowaddr_violation(...); } while (0)       /* silent on success */
static inline uint32_t pc_lowaddr_ptr32_(...) { if (... >= LIMIT) pc_lowaddr_violation(...); return ...; } /* silent on success */
```

`pc_lowaddr_check()` (used by `PC_LOWADDR_CHECK`, e.g. `executable image`, `JKR heap`, `emu64 instance`, `ARAM host
buffer`, `game heap`) unconditionally registers the category and updates its `[min,max_end)`/count every call -
that is why those categories accumulate visibly and grow (`n=24`, `n=543431`, ...) across a session. `PC_LOWADDR_
ASSERT` and `PC_PTR32` (used by every actor allocation, every GX texture/TLUT/EFB setup, every jaudio file-offset
resolve, every ARQ request) are deliberately the opposite: **zero bookkeeping on the success path**, by design, so
that a check running on every single actor spawn and every single texture upload during real 60 Hz gameplay costs
nothing extra when nothing is wrong. They only ever touch `s_cats[]`/print anything by calling `pc_lowaddr_
violation()`, and only when the address is actually `>= 4 GB`.

**This means their absence from a report was never evidence of "not yet exercised."** They run - and check - on
literally every call, every session, whether or not the report shows them. The only observable signal they produce
is negative: a `[LOWADDR] VIOLATION category="..."` line (and, unless `PC_LOWADDR_NONFATAL=1`, an immediate abort)
if one of them ever actually failed. Given this real session spawned actors (walking around), triggered GX texture/
TLUT uploads (menus, inventory, the town itself all render textured models), and resolved jaudio file offsets
(any music or sound effect), and the process did not abort and produced `total violations: 0` twice, **every
`PC_LOWADDR_ASSERT`/`PC_PTR32` call in this entire session - across all of actor allocation, GX texture setup, and
jaudio PTconvert - passed.** That is the actual, correct way to read "coverage" for this class of check, and it is
exactly the guarantee the low-address strategy needs: not "we saw the category appear," but "nothing in the entire
session ever tried to store an address >= 4 GB where a `u32` would truncate it."

(The same reasoning already applied correctly to the two other hot-path `PC_LOWADDR_ASSERT` sites checked directly
in section 16/17 - `GBI runtime display-list pointer` in `pc_gbi_pack_runtime_ptr()`, and `selftest: JKR/`__osMalloc`
allocation` in the self-test - none of those appear as report categories either, for the identical reason, and this
was never in question because the self-test's *own* pass/fail (`ST_CHECK`) independently confirms them.)

### 18.3 Status of every category/mechanism after this real session

| Item | Mechanism | Result |
|---|---|---|
| Structural layout (`CMemBlock`, `SDIFileEntry`, `Scene_Word_u`, JAUDIO structs, DVDFileInfo) | Compile-time `static_assert` + `--lowaddr-selftest` | Unchanged from section 16/17, still passing. |
| One-time allocations (image, JKR heap/arena, OS arena, ARAM buffer, malloc probes, DOL asset, `emu64` instance/caches, game heap) | `PC_LOWADDR_CHECK` (always registers) | **Directly observed in this real session's report, 0 violations, heap-growth (`n=24`) and draw-call volume (`n=543431`) both consistent with genuine extended gameplay.** |
| Per-call hot-path checks (actor allocation, actor overlay buffer, GX texobj/TLUT/EFB, ARQ request, jaudio PTconvert, GBI runtime pointer) | `PC_LOWADDR_ASSERT`/`PC_PTR32` (silent on success, fatal on failure) | **Exercised continuously throughout this real session (actors spawned, menus/inventory opened, audio played) - zero violations means every single call passed; absence from the printed report is the expected, correct behavior of these macros, not missing coverage.** |
| seg2k0/GBI odd-pointer token window (17.1-17.3) | Runtime reserved-range cross-check (new, section 17) + `pc_gbi_token_selftest()` | Still 0 violations; the `[GBI] odd pointer alignment` fallback warning did not fire in this real session either (consistent with 17.1's proof that no live macro ever feeds it a misaligned pointer). |
| AIInitDMA truncation fix (17.6) | `PC_PTR32` (silent on success) | Audio played throughout this real session (per the user's account) with no crash and no violation - consistent with the fix being correct and the jaudio heap staying low-address as designed. |

### 18.4 Conclusion

With this real, extended, save-and-exit gameplay session producing `total violations: 0` across every tracked
category and no crash, every open item from sections 16 and 17 is now closed:

- 16.3/17.1-17.3 (seg2k0/token collision): closed by trace + guard + synthetic test (section 17), now also silent
  (no fallback-path warning) across a real session.
- D1 (self-test/report coverage): closed - the reporting mechanism itself was fixed (17.4) and this section
  corrects the remaining misunderstanding about what "coverage" means for `PC_LOWADDR_ASSERT`/`PC_PTR32` sites.
- B5 (ARAM), D4 (Dolphin stubs), D6 (audio DMA bounds), D3 (format sweep): closed in section 17, unaffected by and
  consistent with this session's data (ARAM buffer allocated once, 16 MB, 0 violations; audio played with no
  crash).
- Actor-pool fix (`ac_structure.c`, `SHRINE_ACTOR` slot sizing) and the `neosthread.c` audio fix: both now
  exercised under real, extended gameplay (not just boot/title) with zero crashes and a successful save.

No further speculative code changes were made or are believed necessary. The x64 conversion's low-address
strategy is verified, by direct measurement, end-to-end: compile-time layout asserts, boot-time allocator
self-tests, and now a real, save-completing gameplay session with continuous per-call hot-path checking, all
passing with zero violations.
