/* PC_LOW_ADDRESS_64 only: static GBI data of m_msg.c compiled as C++ (see 64BIT_MIGRATION_AUDIT.md). */
#ifdef PC_LOW_ADDRESS_64
#define PC_SPLIT_TU 1

#include "m_msg.h"
#include "libforest/gbi_extensions.h"
#include "pc_split.h"
#include "../src/data/pc_split/m_msg_split.h"
#include "../src/game/m_msg_data.c_inc"

#endif /* PC_LOW_ADDRESS_64 */
