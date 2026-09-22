#ifndef _JAUDIO_JA_FILEPTR_H
#define _JAUDIO_JA_FILEPTR_H

#include "types.h"

/* JA_FPTR(T): the type of a field that lives inside a jaudio *file image* (AAF/pikibank/wave-system data) and holds a
 * 32-bit GameCube address: on disc it is an offset, after PTconvert it is the absolute address of the target.
 * The file format fixes such fields at 4 bytes, so on a 64-bit build they must not be native pointers.
 *
 *   32-bit / non-low-address build: T*                        (exactly the original code)
 *   PC_LOW_ADDRESS_64, C++:         PcLowPtr<T>               (4 bytes, converts to/from T*, range-checked)
 *   PC_LOW_ADDRESS_64, C:           JaFilePtr32               (4-byte opaque slot; C code cannot dereference it, so any
 *                                                              translation unit that reads these fields is compiled as C++)
 */
#ifdef PC_LOW_ADDRESS_64
#ifdef __cplusplus
#include "pc_lowptr.h"
#define JA_FPTR(T) PcLowPtr<T>
#else
typedef struct JaFilePtr32_ { u32 mAddr; } JaFilePtr32;
#define JA_FPTR(T) JaFilePtr32
#endif
#else
#define JA_FPTR(T) T*
#endif

/* Reading a JA_FPTR slot:
 *   JA_FPTR_GET(T, slot) - the T* it refers to (also usable from C, where the slot is an opaque JaFilePtr32)
 *   JA_FPTR_U32(slot)    - its raw 32-bit value (the original code's "(u32)slot")
 * Without PC_LOW_ADDRESS_64 both expand to the original pointer expressions. */
#ifdef PC_LOW_ADDRESS_64
#define JA_FPTR_GET(T, slot) ((T*)(uintptr_t)(slot).mAddr)
#define JA_FPTR_U32(slot) ((u32)(slot).mAddr)
#else
#define JA_FPTR_GET(T, slot) ((T*)(slot))
#define JA_FPTR_U32(slot) ((u32)(slot))
#endif

/* layout checks (PC_LOW_ADDRESS_64 only): the file format's sizes/offsets must not depend on sizeof(void*) */
#ifdef PC_LOW_ADDRESS_64
#include <stddef.h>
#ifdef __cplusplus
#define JA_LAYOUT_SIZE(T, sz) static_assert(sizeof(T) == (sz), #T " must match the GameCube/file-format size")
#define JA_LAYOUT_OFF(T, f, off) static_assert(offsetof(T, f) == (off), #T "." #f " must match the GameCube/file-format offset")
#else
#define JA_LAYOUT_SIZE(T, sz) _Static_assert(sizeof(T) == (sz), #T " must match the GameCube/file-format size")
#define JA_LAYOUT_OFF(T, f, off) _Static_assert(offsetof(T, f) == (off), #T "." #f " must match the GameCube/file-format offset")
#endif
#else
/* expands to a valid, repeatable declaration so "JA_LAYOUT_SIZE(...);" is not a stray semicolon on any compiler */
#define JA_LAYOUT_SIZE(T, sz) extern int _ja_layout_unused
#define JA_LAYOUT_OFF(T, f, off) extern int _ja_layout_unused
#endif

#endif
