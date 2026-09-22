/* PC_LOW_ADDRESS_64 only: static GBI data of initial_menu.c compiled as C++ (see 64BIT_MIGRATION_AUDIT.md). */
#ifdef PC_LOW_ADDRESS_64
#define PC_SPLIT_TU 1

#include "initial_menu.h"
#include "PR/mbi.h"
#include "libforest/gbi_extensions.h"
#include "m_nmibuf.h"
#include "dolphin/dvd.h"
#include "jsyswrap.h"
#include "boot.h"
#include "dolphin/os/OSFont.h"
#include "bootdata.h"
#include "libultra/libultra.h"
#include "libforest/emu64/emu64_wrapper.h"
#include "dolphin/vi.h"
#include "dolphin/os/OSMessage.h"
#include "dolphin/os/OSResetSW.h"
#include "dolphin/os/OSReset.h"
#include "dolphin/os.h"
#include "m_controller.h"
#include "dvderr.h"
#include "pc_split.h"
#include "../src/data/pc_split/initial_menu_split.h"
#include "../src/static/initial_menu_gfx.c_inc"

#endif /* PC_LOW_ADDRESS_64 */
