#ifndef _JAUDIO_HEAPCTRL_H
#define _JAUDIO_HEAPCTRL_H

#include "types.h"
#include "jaudio_NES/ja_fileptr.h"

typedef struct jaheap_ jaheap_;
typedef struct jaheap_ jaheap;

struct jaheap_ {
	u8 isRootHeap;             // _00, is this a 'mother' heap?
	u8 memoryType;             // _01, 0 = ARAM, 1 = DRAM
	u16 childCount;            // _02
	u32 heapId;                // _04
	u32 startAddress;          // _08
	u32 usedSize;              // _0C
	u32 size;                  // _10
	JA_FPTR(jaheap_) firstChild;       // _14
	JA_FPTR(jaheap_) parent;           // _18
	JA_FPTR(jaheap_) nextSibling;      // _1C
	JA_FPTR(jaheap_) groupOwner;       // _20
	JA_FPTR(jaheap_) firstGroupedHeap; // _24
	JA_FPTR(jaheap_) nextGroupedHeap;  // _28
};

/* jaheap_ is embedded in file images (WaveID_ / WaveArchive_): its size and offsets are part of the format */
JA_LAYOUT_SIZE(jaheap_, 0x2C);
JA_LAYOUT_OFF(jaheap_, firstChild, 0x14);
JA_LAYOUT_OFF(jaheap_, parent, 0x18);
JA_LAYOUT_OFF(jaheap_, nextSibling, 0x1C);
JA_LAYOUT_OFF(jaheap_, groupOwner, 0x20);
JA_LAYOUT_OFF(jaheap_, firstGroupedHeap, 0x24);
JA_LAYOUT_OFF(jaheap_, nextGroupedHeap, 0x28);

#ifdef __cplusplus
extern "C" {
#endif

void Jac_GetUnlockHeap(jaheap_*);
void Jac_CheckAlloc(jaheap_*);
void Jac_InitHeap(jaheap_*);
void Jac_SelfInitHeap(jaheap_*, u32, u32, u32);
BOOL Jac_SelfAllocHeap(jaheap_*, jaheap_*, u32, u32);
BOOL Jac_SetGroupHeap(jaheap_*, jaheap_*);
void Jac_CutdownHeap(jaheap_*);
void Jac_InitMotherHeap(jaheap_*, u32, u32, u8);
BOOL Jac_AllocHeap(jaheap_*, jaheap_*, u32);
BOOL Jac_DeleteHeap(jaheap_*);
void Jac_GarbageCollection_St(jaheap_*);
void Jac_CheckFreeHeap_Total(jaheap_*);
void Jac_CheckFreeHeap_Linear(jaheap_*);
void Jac_ShowHeap(jaheap_*, u32);

#ifdef __cplusplus
}
#endif

#endif
