/* PC_LOW_ADDRESS_64 only: static GBI data of dvderr.c compiled as C++ (see 64BIT_MIGRATION_AUDIT.md). */
#ifdef PC_LOW_ADDRESS_64
#define PC_SPLIT_TU 1

#include "dvderr.h"
#include "libultra/libultra.h"
#include "dolphin/dvd.h"
#include "libforest/gbi_extensions.h"
#include "libforest/emu64/emu64_wrapper.h"
#include "jsyswrap.h"
#include "dolphin/vi.h"
#include "dolphin/gx.h"
#include "pc_split.h"
#include "../src/data/pc_split/dvderr_split.h"
#include "../src/static/dvderr_gfx.c_inc"

#endif /* PC_LOW_ADDRESS_64 */
