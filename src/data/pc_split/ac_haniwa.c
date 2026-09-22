/* PC_LOW_ADDRESS_64 only: static GBI data of ac_haniwa.c compiled as C++ (see 64BIT_MIGRATION_AUDIT.md).
 * ac_haniwa.c keeps this table as a function-local static in every other build (it cannot be a static
 * initializer on x64: gsDPLoadTLUT_Dolphin embeds the address of hnw_face). */
#ifdef PC_LOW_ADDRESS_64
#define PC_SPLIT_TU 1

#include "ac_haniwa.h"
#include "m_rcp.h"
#include "libforest/gbi_extensions.h"

extern u16 hnw_face[];

Gfx hnw_tex_model[] = {
    gsDPLoadTLUT_Dolphin(15, 16, 1, hnw_face),
    gsSPEndDisplayList(),
};

#endif /* PC_LOW_ADDRESS_64 */
