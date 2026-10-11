# Third-party notices, provenance and open licensing questions

This is an information document, **not legal advice and not a statement that anything is "cleared"**. Status words used below:

- **Confirmed** - directly supported by a licence text or file in this repository / the cited upstream source.
- **Unverified** - supported by indirect evidence only (history, naming, comments).
- **Open - rights holder** - needs a statement from the named author/owner.
- **Open - legal** - needs a qualified legal opinion; no amount of repository inspection can settle it.

Unofficial fan project. Not affiliated with, authorised or endorsed by Nintendo. "Animal Crossing" and related names are the property of their owners. The distributions of this project contain no game disc image and no intentionally bundled extracted game data; users supply their own USA (GAFE01_00) disc image. **That does not clear the compiled code or compiled data tables** (see section 3).

## 1. How to read `LICENSE`

`LICENSE` is the inherited file of the original PC port (identical to flyngmt/ACGC-PC-Port, unchanged here). It says: upstream decompilation code = CC0 1.0; "the PC port layer (pc/ directory and all #ifdef TARGET_PC additions to decomp files)" = MIT, copyright line "Copyright (c) 2026 FlyingMeta"; fixNES = MIT (2017 FIX94).

- CC0 is a waiver by the person who applies it. Its own §4(c) (ACreTeam/ac-decomp `LICENSE`) says the waiver does not clear other persons' rights. It does **not** establish that Nintendo's or any other party's rights in the game, its SDK or material derived from them are cleared. Status: **Open - legal**.
- The MIT copyright line names one holder. Other contributors exist (section 2). This project does not change the line and does not claim authority over other people's contributions.
- Files outside both stated scopes (section 2, rows 4-6) have no licence statement of their own.

## 2. Inventory by origin

| # | Component | Where | Stated licence / evidence | Status |
|---|---|---|---|---|
| 1 | Upstream decompilation (ACreTeam/ac-decomp): about 6000 files in `src/`, `include/`, `config/`, `tools/` | most of the tree (6301 files in the upstream tree; of this fork's 6870 tracked files, 2975 are byte-identical to upstream and 3321 more exist upstream but differ, mostly through the original port's own edits) | CC0-1.0 (GitHub API licence field; upstream `LICENSE`); README: contains no game assets or assembly | CC0 text **Confirmed**; clearing of Nintendo/third-party rights **Open - legal** |
| 2 | Original PC port layer (FlyingMeta / flyngmt): `pc/` of the original port, TARGET_PC changes, first commit 5fb5dee | `pc/`, parts of `src/` | MIT, "Copyright (c) 2026 FlyingMeta" in `LICENSE` (identical to the original repo's file) | **Confirmed** for the text; holder = file adder per Git |
| 3 | Contributions inside the original port's own history by Cuyler36 (commit 8235fe0 "Initial work", 2026-04-14, others) and oprel (PR #105 "Quality of Life changes", merged 2026-08-01; commits c568a91, afeac49 ...) | 25 files in `src/`/`include/` (list: `docs/provenance/modified-files-outside-target-pc.txt`, group A) | no per-file notice; they are edits to decomp files without `TARGET_PC` guards, so the literal `LICENSE` scope does not name them. oprel's PR text says features "can also be found in Animal Crossing Deluxe" | **Open - rights holder** (Cuyler36, oprel; and whether code derives from Animal Crossing Deluxe) |
| 4 | This fork's multiplayer implementation: 35 files in `pc/src`, 41 in `pc/include`, 274 test/tool files in `pc/tools`, docs | listed in `docs/provenance/fork-added-files.txt` | authored by Yubel and by AI-assisted commits ("Claude", co-authored); **no licence text of its own**. The pc/ directory is MIT per row 2's scope sentence | Intended licence = the same MIT terms as `pc/` (**proposal, to be confirmed by the repository owner; not yet a grant by anyone else**); ownership of AI-assisted output **Open - legal** |
| 5 | Fork edits to decomp files outside literal `TARGET_PC` blocks (64-bit pointer width via `PC_LOW_ADDRESS_64`, RNG domains, test hooks): 17 files (group B) | `include/JSystem/JKernel/JKRArchive.h`, `src/static/jaudio_NES/internal/*.c` and others | edits of CC0-labelled upstream files; scope wording of `LICENSE` does not literally cover them | **Open - rights holder** (repository owner: confirm intended licence) |
| 6 | Relocated upstream data: display lists / vertex / texture tables moved into `*_gfx.c_inc` and wrapped by `src/data/pc_split/*` (27 files, group C). 93-99% of the lines removed from the original files reappear verbatim in the new files | `src/**/*_gfx.c_inc`, `src/data/pc_split/` | content is upstream-derived; moving it did not create new authorship | treat as upstream material: same status as row 1 |
| 7 | SGI/Nintendo-marked SDK headers `include/PR/gbi.h`, `abi.h`, `mbi.h` ("Copyright (C) 1994, Silicon Graphics, Inc ... unpublished proprietary information ... may not be ... copied ... without the prior written consent of Silicon Graphics"), `gs2dex.h` ("Copyright (C) 1997, Nintendo.") and `ultratypes.h` | `include/PR/`; present with the same names in upstream ac-decomp; this fork's `gbi.h` is further modified (+262/-43 lines) | the notices in the files themselves; no permission text found | **Open - legal / rights holders** (SGI successors, Nintendo). Included in ~3400 of 4041 compiled translation units; 388 of their 1093 identifiers are referenced (`docs/provenance/sdk-header-inventory.txt`). Replacement feasibility: see `docs/provenance/README.md` |
| 8 | Reconstructed Nintendo libraries and data compiled into `AnimalCrossing.exe`: JSystem (44 objects), jaudio_NES (59), Famicom (4), libforest/emu64, libc64, libu64, `src/data` (3221 objects) | `src/static/*`, `src/data/*` | reconstructed from the original game by the upstream project; no notice | **Open - legal** |
| 9 | Sources present in the repository but **excluded from the PC build** (CMake filters): MSL_C.PPCEABI.bare.H (16 files carry "Copyright (C) 1993 by Sun Microsystems" fdlibm notices), TRK_MINNOW_DOLPHIN, Runtime.PPCEABI.H | `src/static/...`, `include/MSL_C` | Sun fdlibm notice: permission "to use, copy, modify, and distribute this software is freely granted, provided that this notice is preserved" (kept in the files) | not compiled into the Windows exe (**Confirmed** by the object list); source-repository question only |
| 10 | fixNES (NES emulator core, compiled in) | `pc/lib/fixnes/` | MIT, "Copyright (c) 2017 FIX94", `pc/lib/fixnes/LICENSE` | **Confirmed**; text shipped in the ZIP |
| 11 | glad 2.0.8 generated OpenGL 3.3 loader (compiled in) | `pc/lib/glad/` | file headers: `SPDX-License-Identifier: (WTFPL OR CC0-1.0) AND Apache-2.0` | **Confirmed** from the headers; Apache-2.0 text shipped (apache.org, sha256 cfc7749b...) |
| 12 | Khronos `KHR/khrplatform.h` (compiled in via glad) | `pc/lib/glad/include/KHR/` | Khronos permission notice in the file header ("Copyright (c) 2008-2018 The Khronos Group Inc."), copied verbatim to `licenses/Khronos-khrplatform-NOTICE.txt` | **Confirmed** |
| 13 | XXHash64 implementation for Dolphin-compatible texture hashes | `pc/src/pc_texture_pack.c` lines 15-110 (introduced in 5fb5dee by FlyingMeta, no comment or notice) | Compared with xxHash v0.8.2 / dev (BSD-2-Clause): only 14-18 of 81 normalised lines match (the algorithm's forced steps); structure differs; macro names `XXH_PRIME64_1..5` equal the reference's. Not classifiable as copy or independent | **Open - rights holder** (FlyingMeta: was it adapted from xxHash?). Precaution: the xxHash BSD-2-Clause text is shipped (`licenses/xxHash-LICENSE.txt`, source https://github.com/Cyan4973/xxHash/blob/dev/LICENSE, sha256 6ffedbc0...) **without** claiming the code is derived from it |
| 14 | Previous icon `pc/res/icon.png` / `icon.ico` ("Add game icon, thanks YourPalFox!", commit cc35e53 by FlyingMeta, 2026-07-10; wooden "AC PC" sign) | kept in the repository, **no longer built into the executable** | no licence, no permission statement found | **Open - rights holder** (YourPalFox / FlyingMeta). Mitigated for the exe by row 15 |
| 15 | Generic icon `pc/res/icon_generic.png/.ico`, generated from scratch by `pc/res/generate_generic_icon.py` (plain geometric shapes, no game art, font or logo) | built into the exe via `app.rc` | created for this project; proposed to be licensed like the multiplayer additions (row 4) | **Confirmed** no third-party material used (Pillow only draws primitives); licence statement = same proposal as row 4 |
| 16 | SDL2 2.32.10 (`SDL2.dll`, MSYS2 package, byte-identical) | ZIP root | zlib licence (`licenses/SDL2-LICENSE.txt` from the same package) | **Confirmed**; third-party code compiled inside SDL2 not enumerated (**Unverified**) |
| 17 | MinGW-w64 winpthreads, GCC runtime (libgcc/libstdc++) statically linked | `AnimalCrossing.exe` | winpthreads `COPYING` (incl. a Lockless Inc. BSD-style notice); GPLv3 + GCC Runtime Library Exception 3.1 (`COPYING.RUNTIME`, `COPYING3` from gcc 16.2.0-4) | texts **Confirmed** and shipped; whether the GPL text is required is **Open - legal** (shipped as the conservative choice) |
| 18 | zlib 1.3.2 (launcher only) | `ACLauncher.exe` | zlib licence | **Confirmed** |
| 19 | Build tools in the repository: `tools/pyjkernel` (GPLv3, own `LICENSE`), `tools/ninja_syntax.py` (Apache-2.0, Google) and other upstream tools | `tools/` | licence files / headers | **Confirmed**; not compiled into or shipped with the ZIP |
| 20 | Windows UCRT / system DLLs | system | Microsoft | not redistributed |

## 3. What remains unresolved (blocks a public binary release until decided)

1. Rows 7 and 8: whether a compiled copy of the program may be redistributed given the SGI/Nintendo-marked headers and the reconstructed Nintendo library code and data tables it contains. Supplying one's own disc image does not answer this. **Open - legal.**
2. Rows 3 and 5: licence terms of third-party and fork edits to decomp files outside `TARGET_PC`. **Open - rights holders.**
3. Rows 13 and 14: provenance of the hash function and the previous icon. **Open - rights holder.**
4. Row 4: AI-assisted contributions and the missing explicit licence statement for the multiplayer code. **Open - repository owner / legal.**

## 4. Not claimed

Not claimed: that the project is authorised by Nintendo, that CC0 clears third-party rights, that the compiled executable is free of Nintendo-derived material, that any component marked Open is cleared, or that the licence of the original port can be replaced.
