#ifndef _JAUDIO_BX_H
#define _JAUDIO_BX_H

// This is an invented header containing several structs related to the file structure of pikibank.bx

#include "types.h"
#include "jaudio_NES/heapctrl.h"
#include "jaudio_NES/ja_fileptr.h"

#define BANK_INST_COUNT        (0xF0)
#define BANK_TEST_INST_COUNT   (0x80)
#define BANK_TEST_VOICE_OFFSET (BANK_TEST_INST_COUNT)
#define BANK_TEST_VOICE_COUNT  (0x64)
#define BANK_TEST_PERC_OFFSET  (BANK_TEST_INST_COUNT + BANK_TEST_VOICE_COUNT)
#define BANK_TEST_PERC_COUNT   (0x0C)

typedef struct Bank_ Bank_;
typedef struct Ibnk_ Ibnk_;
typedef struct Inst_ Inst_;
typedef struct Perc_ Perc_;
typedef struct Voice_ Voice_;
typedef struct Sense_ Sense_;
typedef struct Rand_ Rand_;
typedef struct Osc_ Osc_;
typedef struct Oscbuf_ Oscbuf_;

typedef struct Vmap_ Vmap_;
typedef struct InstKeymap_ InstKeymap_;
typedef struct PercKeymap_ PercKeymap_;

// these type names are confirmed:
typedef struct WaveArchiveBank_ WaveArchiveBank_;
typedef struct CtrlGroup_ CtrlGroup_;
typedef struct Ctrl_ Ctrl_;
typedef struct WaveArchive_ WaveArchive_;
typedef struct Wave_ Wave_;

// these type names are fabricated, feel free to rename if found:
typedef struct Wsys_ Wsys_;
typedef struct SCNE_ SCNE_;
typedef struct WaveID_ WaveID_;

//////////////////////////////////////////////////////////
////////////////////// BANK STRUCTS //////////////////////

struct Bank_ {
	int mMagic; // _00 | 'BANK'
	union {     // _04 | Can point to INST, VOICE, PERC.
		// There's no way this was actually an anonymous union, as this only became a feature
		// of the C programming language in C11. However, it does make the code nicer to read.
		JA_FPTR(Inst_) mInstruments[BANK_INST_COUNT];
		JA_FPTR(Voice_) mVoices[BANK_INST_COUNT];
		JA_FPTR(Perc_) mPercs[BANK_INST_COUNT];
	};
};

struct Ibnk_ {
	int magic;                            // _00 | 'IBNK'
	u32 _04;                              // _04
	u32 _08;                              // _08
	int _0C;                              // _0C
	JA_FPTR(WaveArchiveBank_) waveArcBank; // _10
	u8 _14[0x20 - 0x14];                  // _14
	Bank_ bank;                           // _20
};

/**
 * @brief This is an invented type of an unknown name.
 *
 * @note Size: 0x10.
 */
struct Vmap_ {
	u8 mBaseVelocity; // _00
	s16 mWsysID;      // _04
	s16 mWaveID;      // _06
	f32 mVolume;      // _08
	f32 mPitch;       // _0C
};

/**
 * @brief This is an invented type of an unknown name.
 *
 * @note Size: 0x10.
 */
struct InstKeymap_ {
	u8 mBaseKey;           // _00
	u32 mVelocityCount;    // _04
	JA_FPTR(Vmap_) mVelocities[2]; // _08
};

/**
 * @brief This is an invented type of an unknown name.
 *
 * @note Size: 0x18.
 */
struct PercKeymap_ {
	f32 mPitch;            // _00
	f32 mVolume;           // _04
	JA_FPTR(void) _08;     // _08, pointer type unknown, but gets hit by PTconvert in Bank_Test
	JA_FPTR(void) _0C;     // _0C, pointer type unknown, but gets hit by PTconvert in Bank_Test
	int mVelocityCount;    // _10
	JA_FPTR(Vmap_) mVelocities[2]; // _14
};

struct Sense_ {
	u8 id;        // _00
	u8 type;      // _01
	u8 threshold; // _02
	f32 min;      // _04
	f32 max;      // _08
};

struct Osc_ {
	u8 mode;               // _00
	f32 rate;              // _04
	JA_FPTR(s16) attackVecOffset;  // _08
	JA_FPTR(s16) releaseVecOffset; // _0C
	f32 width;             // _10
	f32 vertex;            // _14
};

/**
 * @note Size: 0x18.
 */
struct Oscbuf_ {
	u8 state;         // _00
	u8 curveType;     // _01
	u16 tableIndex;   // _02
	f32 timeCounter;  // _04
	f32 value;        // _08
	f32 targetValue;  // _0C
	f32 deltaRate;    // _10
	u16 releaseParam; // _14
};

/**
 * @note Size: 0x40.
 */
struct Inst_ {
	int mMagic;                  // _00 | 'INST'
	u32 mFlag;                   // _04
	f32 mFreqMultiplier;         // _08
	f32 mGainMultiplier;         // _0C
	JA_FPTR(Osc_) mOscillators[2];       // _10
	JA_FPTR(Rand_) mEffects[2];          // _18
	JA_FPTR(Sense_) mSensors[2];         // _20
	int mKeyRegionCount;         // _28
	JA_FPTR(InstKeymap_) mKeyRegions[5]; // _2C
};

struct Voice_ {
	u8 _00[0x8];  // _00, unknown
	int size;     // _08, count of whatever's at 0xC
	JA_FPTR(void) _0C[1]; // _0C, unsure of length of array or type
};

struct Perc_ {
	int mMagic;                    // _00 | 'PER2' (or 'PERC'?)
	u8 _04[0x84];                  // _04, unknown
	JA_FPTR(PercKeymap_) mKeyRegions[128]; // _88
	s8 _288[128];                  // _288
	u16 _308[128];                 // _308
};

struct Rand_ {
	u8 id;               // _00
	f32 value;           // _04
	f32 range;           // _08
	u8 _0C[0x10 - 0x0C]; // _0C
};

struct Pmap_ {
	JA_FPTR(Rand_) _00; // _00
	int _04;    // _04
};

//////////////////////////////////////////////////////////
////////////////////// WAVE STRUCTS //////////////////////

// Name fabricated, but makes sense in line with Ibnk_
struct Wsys_ {
	int magic;                     // _00, 'WSYS'
	int size;                      // _04
	int globalID;                  // _08
	int _0C;                       // _0C, unused?
	JA_FPTR(WaveArchiveBank_) waveArcBank; // _10
	JA_FPTR(CtrlGroup_) ctrlGroup;         // _14
};

struct WaveArchiveBank_ {
	int magic;                   // _00, 'WINF'
	int count;                   // _04, same count as CtrlGroup_
	JA_FPTR(WaveArchive_) waveGroups[1]; // _08, array size variable
};

struct CtrlGroup_ {
	int magic;        // _00, 'WBCT'
	u32 _04;          // _04, unknown
	int count;        // _08, same count as WaveArchiveBank_
	JA_FPTR(SCNE_) scenes[1]; // _0C, array size variable
};

// Name fabricated based on magic ID.
struct SCNE_ {
	int magic;  // _00, 'SCNE'
	u32 _04;    // _04
	u32 _08;    // _08
	JA_FPTR(Ctrl_) cdf; // _0C
	JA_FPTR(Ctrl_) cex; // _10
	JA_FPTR(Ctrl_) cst; // _14
	int _18[1]; // _18, variable size?
};

struct Ctrl_ {
	int magic;           // _00, 'C-DF', 'C-EX' or 'C-ST'
	int count;           // _04
	JA_FPTR(WaveID_) waveIDs[1]; // _08, array size variable
};

// Name fabricated from Xayr's tools.
struct WaveID_ {
	u32 id;         // _00, split into sound id and ws id
	jaheap_ heap;   // _04
	u32 loadStatus; // _30
	JA_FPTR(Wave_) data; // _34
};

struct WaveArchive_ {
	char filePath[0x40]; // _00, might be smaller, unsure
	jaheap_ heap;        // _40
	u32 fileLoadStatus;  // _6C
	int waveCount;       // _70
	JA_FPTR(Wave_) waves[1];     // _74, array size variable
};

/**
 * @brief File data, size unknown
 */
struct Wave_ {
	u8 _00;                // _00
	u8 compBlockIdx;       // _01
	u8 key;                // _02
	f32 _04;               // _04
	int srcAddress;        // _08
	int length;            // _0C
	s32 isLooping;         // _10
	s32 loopAddress;       // _14
	s32 loopStartPosition; // _18
	s32 _1C;               // _1C
	s16 loopYN1;           // _20
	s16 loopYN2;           // _22
	JA_FPTR(u32) fileLoadStatus; // _24, set to dolphin/dvd.h 'DVD_RESULT_*' defines
};

/* ---- PTconvert: patch a file-image address field from "offset in file" to "absolute address" ----
 * Original (GameCube / 32-bit) code passes the slot as a void**; the PC_LOW_ADDRESS_64 build passes the 4-byte
 * PcLowPtr slot itself and adds the offset to the stored 32-bit value, so the on-disc field stays 4 bytes wide.
 * Same rule as the original: leave NULL and already-absolute (>= base) values alone. */
#if defined(PC_LOW_ADDRESS_64) && defined(__cplusplus)
template <class T>
static void PTconvert(PcLowPtr<T>* slot, u32 base_address)
{
	u32 stored = slot->mAddr;
	if (stored == 0 || stored >= base_address) {
		return;
	}
	slot->mAddr = PC_PTR32("jaudio PTconvert (file offset + base)", (void*)((uintptr_t)stored + base_address));
}
#define PTCONVERT(slot, base) PTconvert(slot, base)
#else
#define PTCONVERT(slot, base) PTconvert((void**)(slot), base)
#endif

/* ---- file-format layout: must be identical to the GameCube (32-bit) layout on every build ---- */
JA_LAYOUT_SIZE(Bank_, 4 + BANK_INST_COUNT * 4);
JA_LAYOUT_OFF(Bank_, mInstruments, 0x04);
JA_LAYOUT_SIZE(Ibnk_, 0x20 + 4 + BANK_INST_COUNT * 4);
JA_LAYOUT_OFF(Ibnk_, waveArcBank, 0x10);
JA_LAYOUT_OFF(Ibnk_, bank, 0x20);
JA_LAYOUT_SIZE(Inst_, 0x40);
JA_LAYOUT_OFF(Inst_, mOscillators, 0x10);
JA_LAYOUT_OFF(Inst_, mEffects, 0x18);
JA_LAYOUT_OFF(Inst_, mSensors, 0x20);
JA_LAYOUT_OFF(Inst_, mKeyRegionCount, 0x28);
JA_LAYOUT_OFF(Inst_, mKeyRegions, 0x2C);
JA_LAYOUT_SIZE(InstKeymap_, 0x10);
JA_LAYOUT_OFF(InstKeymap_, mVelocities, 0x08);
JA_LAYOUT_SIZE(PercKeymap_, 0x1C); /* the "Size: 0x18" doc comment is inconsistent with its own fields (0x14 + 2*4) */
JA_LAYOUT_OFF(PercKeymap_, _08, 0x08);
JA_LAYOUT_OFF(PercKeymap_, _0C, 0x0C);
JA_LAYOUT_OFF(PercKeymap_, mVelocityCount, 0x10);
JA_LAYOUT_OFF(PercKeymap_, mVelocities, 0x14);
JA_LAYOUT_SIZE(Osc_, 0x18);
JA_LAYOUT_OFF(Osc_, attackVecOffset, 0x08);
JA_LAYOUT_OFF(Osc_, releaseVecOffset, 0x0C);
JA_LAYOUT_OFF(Osc_, width, 0x10);
JA_LAYOUT_OFF(Osc_, vertex, 0x14);
JA_LAYOUT_SIZE(Voice_, 0x10);
JA_LAYOUT_OFF(Voice_, _0C, 0x0C);
JA_LAYOUT_SIZE(Perc_, 0x408);
JA_LAYOUT_OFF(Perc_, mKeyRegions, 0x88);
JA_LAYOUT_OFF(Perc_, _288, 0x288);
JA_LAYOUT_OFF(Perc_, _308, 0x308);
JA_LAYOUT_SIZE(struct Pmap_, 0x08);
JA_LAYOUT_SIZE(Wsys_, 0x18);
JA_LAYOUT_OFF(Wsys_, waveArcBank, 0x10);
JA_LAYOUT_OFF(Wsys_, ctrlGroup, 0x14);
JA_LAYOUT_SIZE(WaveArchiveBank_, 0x0C);
JA_LAYOUT_OFF(WaveArchiveBank_, waveGroups, 0x08);
JA_LAYOUT_SIZE(CtrlGroup_, 0x10);
JA_LAYOUT_OFF(CtrlGroup_, scenes, 0x0C);
JA_LAYOUT_SIZE(SCNE_, 0x1C);
JA_LAYOUT_OFF(SCNE_, cdf, 0x0C);
JA_LAYOUT_OFF(SCNE_, cex, 0x10);
JA_LAYOUT_OFF(SCNE_, cst, 0x14);
JA_LAYOUT_OFF(SCNE_, _18, 0x18);
JA_LAYOUT_SIZE(Ctrl_, 0x0C);
JA_LAYOUT_OFF(Ctrl_, waveIDs, 0x08);
JA_LAYOUT_SIZE(WaveID_, 0x38);
JA_LAYOUT_OFF(WaveID_, heap, 0x04);
JA_LAYOUT_OFF(WaveID_, loadStatus, 0x30);
JA_LAYOUT_OFF(WaveID_, data, 0x34);
JA_LAYOUT_SIZE(WaveArchive_, 0x78);
JA_LAYOUT_OFF(WaveArchive_, heap, 0x40);
JA_LAYOUT_OFF(WaveArchive_, fileLoadStatus, 0x6C);
JA_LAYOUT_OFF(WaveArchive_, waveCount, 0x70);
JA_LAYOUT_OFF(WaveArchive_, waves, 0x74);
JA_LAYOUT_SIZE(Wave_, 0x28);
JA_LAYOUT_OFF(Wave_, fileLoadStatus, 0x24);
JA_LAYOUT_SIZE(Vmap_, 0x10);
JA_LAYOUT_SIZE(Sense_, 0x0C);
JA_LAYOUT_SIZE(Rand_, 0x10);
JA_LAYOUT_SIZE(Oscbuf_, 0x18);

#endif
