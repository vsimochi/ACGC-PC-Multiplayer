// pc_lowaddr_selftest.cpp - `AnimalCrossing.exe --lowaddr-selftest` (PC_LOW_ADDRESS_64 builds only).
//
// Exercises the game's *real* allocators (OSInit arena, JKRExpHeap root + child heap, __osMalloc) inside the real
// executable, without needing a disc image, and validates the structure layouts and address range that the
// low-address 64-bit strategy depends on. Every failure is printed to lowaddr.log; exit code = number of failures.
#ifdef PC_LOW_ADDRESS_64

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "JSystem/JKernel/JKRExpHeap.h"
#include "libc64/__osMalloc.h"
#include "pc_lowaddr.h"

extern "C" void OSInit(void);
extern "C" int pc_gbi_token_selftest(void); /* C linkage (defined in pc_gbi_runtime.c) */

static int s_fail = 0;
#define ST_CHECK(cond, ...)                                          \
    do {                                                             \
        if (!(cond)) {                                               \
            s_fail++;                                                \
            pc_lowaddr_log("[SELFTEST] FAIL %s:%d: ", __FILE__, __LINE__); \
            pc_lowaddr_log(__VA_ARGS__);                             \
            pc_lowaddr_log("\n");                                    \
        }                                                            \
    } while (0)

static uint32_t rng_state = 0x1234567u;
static uint32_t rnd(void) { rng_state = rng_state * 1664525u + 1013904223u; return rng_state >> 8; }

struct Alloc { unsigned char* p; uint32_t size; unsigned char tag; };

#include "JSystem/JKernel/JKRArchive.h"
static void test_archive_layout(void) {
    pc_lowaddr_log("[SELFTEST] JKRArchive::SDIFileEntry: sizeof=0x%zX (on-disc RARC entry 0x14) mData@0x%zX; SDIDirEntry=0x%zX (0x10) SArcDataInfo=0x%zX (0x20) SArcHeader=0x%zX (0x20)\n",
                   sizeof(JKRArchive::SDIFileEntry), offsetof(JKRArchive::SDIFileEntry, mData), sizeof(JKRArchive::SDIDirEntry),
                   sizeof(JKRArchive::SArcDataInfo), sizeof(JKRArchive::SArcHeader));
    ST_CHECK(sizeof(JKRArchive::SDIFileEntry) == 0x14, "SDIFileEntry size 0x%zX", sizeof(JKRArchive::SDIFileEntry));
    ST_CHECK(sizeof(JKRArchive::SDIDirEntry) == 0x10, "SDIDirEntry size");
    ST_CHECK(sizeof(JKRArchive::SArcDataInfo) == 0x20, "SArcDataInfo size");
    ST_CHECK(sizeof(JKRArchive::SArcHeader) == 0x20, "SArcHeader size");
    /* simulate the archive loader: index an on-disc entry table with the struct stride */
    static unsigned char raw[0x14 * 8];
    for (int i = 0; i < 8; i++) { raw[i * 0x14 + 0] = 0; raw[i * 0x14 + 1] = (unsigned char)i; }
    JKRArchive::SDIFileEntry* e = (JKRArchive::SDIFileEntry*)raw;
    ST_CHECK(e[5].mFileID == 5u << 8, "entry stride wrong: e[5].mFileID=0x%X (little-endian read of raw id 0x0005)", (unsigned)e[5].mFileID);
}

// ---- jaudio file-format tests: the real Wave_Test / Bank_Test on images laid out byte-by-byte per the GameCube format ----
#include "jaudio_NES/bx.h"
#include "jaudio_NES/waveread.h"
#include "jaudio_NES/bankread.h"

static void put32(unsigned char* img, u32 off, u32 v) { memcpy(img + off, &v, 4); }
static u32 get32(const unsigned char* img, u32 off) { u32 v; memcpy(&v, img + off, 4); return v; }

static void test_jaudio_wave(void) {
    /* GC layout (all fields 4 bytes): Wsys: 'WSYS' size id _0C | +0x10 -> WINF offset | +0x14 -> WBCT offset
     *   WINF @0x100: magic,count,waveGroups[count] @+8 -> WaveArchive @0x200 (0x78 bytes; waveCount @+0x70, waves[] @+0x74 -> Wave @0x300)
     *   WBCT @0x180: magic,_04,count @+8, scenes[] @+0xC -> SCNE @0x400: magic,_04,_08, cdf@+0xC cex@+0x10 cst@+0x14
     *   C-DF @0x500: magic,count,waveIDs[] @+8 -> WaveID @0x600 (0x38 bytes) */
    unsigned char* img = (unsigned char*)calloc(1, 0x1000);
    PC_LOWADDR_CHECK("selftest: jaudio wave image", img, 0x1000);
    put32(img, 0x00, 0x57535953); put32(img, 0x04, 0x1000); put32(img, 0x08, 7);
    put32(img, 0x10, 0x100); put32(img, 0x14, 0x180);
    put32(img, 0x100, 0x57494E46); put32(img, 0x104, 1); put32(img, 0x108, 0x200);
    put32(img, 0x200 + 0x70, 1); put32(img, 0x200 + 0x74, 0x300);
    put32(img, 0x180, 0x57424354); put32(img, 0x188, 1); put32(img, 0x18C, 0x400);
    put32(img, 0x400, 0x534E4345); put32(img, 0x400 + 0x0C, 0x500); /* cex, cst = 0 */
    put32(img, 0x500, 0x432D4446); put32(img, 0x504, 1); put32(img, 0x508, 0x600);
    CtrlGroup_* g = Wave_Test(img);
    ST_CHECK(g != nullptr, "Wave_Test returned NULL (the crash site: arcBank/group read at +0x10/+0x14)");
    if (!g) { free(img); return; }
    pc_lowaddr_log("[SELFTEST] Wave_Test ok: group=%p (image %p + 0x180)\n", (void*)g, (void*)img);
    ST_CHECK((unsigned char*)g == img + 0x180, "ctrlGroup resolved to %p, expected %p", (void*)g, (void*)(img + 0x180));
    /* raw 32-bit slots must now hold absolute addresses = image + offset (PTconvert on 4-byte slots) */
    u32 base = PC_PTR32("selftest wave image", img);
    ST_CHECK(get32(img, 0x10) == base + 0x100, "Wsys+0x10 = 0x%X, expected 0x%X", get32(img, 0x10), base + 0x100);
    ST_CHECK(get32(img, 0x14) == base + 0x180, "Wsys+0x14 = 0x%X, expected 0x%X", get32(img, 0x14), base + 0x180);
    ST_CHECK(get32(img, 0x108) == base + 0x200, "WINF waveGroups[0] = 0x%X, expected 0x%X", get32(img, 0x108), base + 0x200);
    ST_CHECK(get32(img, 0x200 + 0x74) == base + 0x300, "WaveArchive waves[0] = 0x%X", get32(img, 0x200 + 0x74));
    ST_CHECK(get32(img, 0x18C) == base + 0x400, "WBCT scenes[0] = 0x%X", get32(img, 0x18C));
    ST_CHECK(get32(img, 0x400 + 0x0C) == base + 0x500, "SCNE cdf = 0x%X", get32(img, 0x400 + 0x0C));
    ST_CHECK(get32(img, 0x400 + 0x10) == 0 && get32(img, 0x400 + 0x14) == 0, "NULL cex/cst must stay 0");
    ST_CHECK(get32(img, 0x508) == base + 0x600, "C-DF waveIDs[0] = 0x%X", get32(img, 0x508));
    /* typed access through the structs must agree with the raw slots */
    Wsys_* w = (Wsys_*)img;
    ST_CHECK((unsigned char*)(WaveArchiveBank_*)w->waveArcBank == img + 0x100, "Wsys_::waveArcBank typed read");
    ST_CHECK((unsigned char*)(CtrlGroup_*)w->ctrlGroup == img + 0x180, "Wsys_::ctrlGroup typed read");
    WaveArchive_* arc = ((WaveArchiveBank_*)w->waveArcBank)->waveGroups[0];
    ST_CHECK((unsigned char*)arc == img + 0x200 && arc->waveCount == 1, "waveGroups[0] typed read");
    WaveID_* wid = ((Ctrl_*)((SCNE_*)g->scenes[0])->cdf)->waveIDs[0];
    ST_CHECK((unsigned char*)wid == img + 0x600, "C-DF waveIDs[0] typed read");
    /* Jac_InitHeap ran on the embedded jaheap_: its 4-byte link fields must be zero and untouched neighbours intact */
    ST_CHECK(get32(img, 0x600 + 0x04 + 0x14) == 0 && get32(img, 0x600 + 0x30) == 0, "WaveID_ heap links/loadStatus not cleared");
    /* PTconvert is idempotent for already-absolute values */
    Wave_Test(img);
    ST_CHECK(get32(img, 0x10) == base + 0x100, "second Wave_Test changed Wsys+0x10 (0x%X)", get32(img, 0x10));
    free(img);
}

static void test_jaudio_bank(void) {
    /* IBNK-relative offsets (GC layout): Bank_ @+0x20: 'BANK' @+0x20, instruments[] @+0x24 (4 bytes each)
     *   INST @0x500 (0x40): osc[0] @+0x10 -> OSC @0x600 (0x18: attack @+8 -> 0x700, release @+0xC -> 0x710); keyRegionCount @+0x28 = 1;
     *   keyRegions[0] @+0x2C -> InstKeymap @0x800 (0x10): velocityCount @+4 = 1, velocities[0] @+8 -> Vmap @0x900 */
    unsigned char* img = (unsigned char*)calloc(1, 0x1000);
    PC_LOWADDR_CHECK("selftest: jaudio bank image", img, 0x1000);
    put32(img, 0x20, 0x42414E4B);
    put32(img, 0x24 + 0 * 4, 0x500);
    put32(img, 0x500, 0x494E5354);
    put32(img, 0x500 + 0x10, 0x600);
    put32(img, 0x500 + 0x28, 1); put32(img, 0x500 + 0x2C, 0x800);
    put32(img, 0x600 + 8, 0x700); put32(img, 0x600 + 0xC, 0x710);
    put32(img, 0x800 + 4, 1); put32(img, 0x800 + 8, 0x900);
    Bank_* b = Bank_Test(img);
    ST_CHECK(b != nullptr && (unsigned char*)b == img + 0x20, "Bank_Test returned %p, expected %p", (void*)b, (void*)(img + 0x20));
    u32 base = PC_PTR32("selftest bank image", img);
    ST_CHECK(get32(img, 0x24) == base + 0x500, "instruments[0] = 0x%X, expected 0x%X", get32(img, 0x24), base + 0x500);
    ST_CHECK(get32(img, 0x500 + 0x10) == base + 0x600, "Inst_::mOscillators[0] = 0x%X", get32(img, 0x500 + 0x10));
    ST_CHECK(get32(img, 0x500 + 0x14) == 0 && get32(img, 0x500 + 0x18) == 0, "Inst_ NULL slots must stay 0");
    ST_CHECK(get32(img, 0x500 + 0x2C) == base + 0x800, "Inst_::mKeyRegions[0] = 0x%X", get32(img, 0x500 + 0x2C));
    ST_CHECK(get32(img, 0x600 + 8) == base + 0x700 && get32(img, 0x600 + 0xC) == base + 0x710, "Osc_ vec offsets = 0x%X/0x%X", get32(img, 0x600 + 8), get32(img, 0x600 + 0xC));
    ST_CHECK(get32(img, 0x800 + 8) == base + 0x900, "InstKeymap_::mVelocities[0] = 0x%X", get32(img, 0x800 + 8));
    Inst_* inst = (Inst_*)(Inst_*)b->mInstruments[0];
    ST_CHECK((unsigned char*)inst == img + 0x500 && inst->mKeyRegionCount == 1, "typed Inst_ read");
    ST_CHECK((unsigned char*)(Osc_*)inst->mOscillators[0] == img + 0x600, "typed Osc_ read");
    pc_lowaddr_log("[SELFTEST] Bank_Test ok (instrument -> osc -> vectors, key region -> velocity map all resolved)\n");
    free(img);
}

// ---- DVDFileInfo: the fields pc_dvd.c writes must be the fields JKRDvdFile reads (needs a disc image in rom/) ----
#include "JSystem/JKernel/JKRDvdFile.h"
#include "pc_disc.h"
static void test_dvd_fileinfo(void) {
    pc_lowaddr_log("[SELFTEST] DVDFileInfo: sizeof=0x%zX (GC 0x3C) cb.addr@0x%zX startAddr@0x%zX length@0x%zX; JKRDvdFileInfo sizeof=0x%zX\n",
                   sizeof(DVDFileInfo), offsetof(DVDFileInfo, cb.addr), offsetof(DVDFileInfo, startAddr), offsetof(DVDFileInfo, length), sizeof(JKRDvdFileInfo));
    pc_disc_init();
    if (!pc_disc_is_open()) { pc_lowaddr_log("[SELFTEST] DVDFileInfo end-to-end check SKIPPED (no disc image in rom/)\n"); return; }
    static const char* names[] = { "forest_1st.arc", "forest_2nd.arc", "foresta.rel.szs", "COPYDATE" };
    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        u32 off = 0, sz = 0;
        if (!pc_disc_find_file(names[i], &off, &sz)) { ST_CHECK(false, "%s not found in the disc FST", names[i]); continue; }
        JKRDvdFile f;
        bool ok = f.open(DVDConvertPathToEntrynum((char*)names[i]));
        ST_CHECK(ok, "JKRDvdFile::open(%s) failed", names[i]);
        if (!ok) continue;
        DVDFileInfo* fi = f.getFileInfo();
        pc_lowaddr_log("[SELFTEST] %-16s FST: disc offset 0x%X size 0x%X | JKRDvdFile: getFileSize()=0x%X startAddr=0x%X\n", names[i], off, sz, f.getFileSize(), fi->startAddr);
        ST_CHECK(f.getFileSize() == sz, "%s: JKRDvdFile::getFileSize()=0x%X, FST size 0x%X", names[i], f.getFileSize(), sz);
        ST_CHECK(fi->startAddr == off, "%s: DVDFileInfo::startAddr=0x%X, FST offset 0x%X", names[i], fi->startAddr, off);
        /* read the first and last 32 bytes through DVDReadPrio and compare with a direct disc read */
        unsigned char a[32], b[32];
        s32 n = DVDReadPrio(fi, a, 32, 0, 2);
        ST_CHECK(n == 32 && pc_disc_read(off, b, 32) && memcmp(a, b, 32) == 0, "%s: first 32 bytes differ (DVDReadPrio=%d)", names[i], (int)n);
        if (sz >= 64) {
            u32 tail = sz & ~31u; if (tail == sz) tail -= 32;
            n = DVDReadPrio(fi, a, 32, (s32)tail, 2);
            ST_CHECK(n == 32 && pc_disc_read(off + tail, b, 32) && memcmp(a, b, 32) == 0, "%s: 32 bytes at +0x%X differ (DVDReadPrio=%d)", names[i], (unsigned)tail, (int)n);
        }
        f.close();
    }
}

// ---- jaudio bank-table file format: the real Nas_BankOfsToAddr/__WaveTouch on a hand-built big-endian bank control block ----
#include "jaudio_NES/audiostruct.h"
#include "jaudio_NES/audiowork.h"
#include "jaudio_NES/audioconst.h"
#include "jaudio_NES/system.h"
s32 Nas_BankOfsToAddr(s32 bank_id, u8* ctrl_p, WaveMedia* wave_media, s32 async); /* C++ linkage (defined in system.c) */

static void be32(unsigned char* img, u32 off, u32 v) { img[off] = v >> 24; img[off + 1] = v >> 16; img[off + 2] = v >> 8; img[off + 3] = (unsigned char)v; }
static void be16(unsigned char* img, u32 off, u16 v) { img[off] = v >> 8; img[off + 1] = (unsigned char)v; }
static void bef32(unsigned char* img, u32 off, float f) { u32 v; memcpy(&v, &f, 4); be32(img, off, v); }
static float lef32(const unsigned char* img, u32 off) { float f; memcpy(&f, img + off, 4); return f; }

static void test_jaudio_banktables(void) {
    /* GameCube bank control block (all fields 4 bytes, big-endian on disc), offsets relative to the block start:
     *   0x000: u32 [0]=percussion-array ofs 0x100, [1]=sfx-table ofs 0x120, [2]=voicetable ofs 0x140      (n_voice=1, n_perc=3, n_sfx=2)
     *   0x100: perctable slots: [0]=0x160 [1]=0x160 (shared, must relocate once) [2]=0 (NULL stays 0)
     *   0x120: percvoicetable[2] (8 bytes each: wavetable ofs, f32 tuning): [0]={0x1A0, 2.5f} [1]={0, 0.75f} (NULL wavetable)
     *   0x140: voicetable (0x20): u8 reloc=0, low=0x10, high=0x7E, decay=3 | envelope ofs @4=0x200 | wtstr low @8 {0x1A0,1.25f}
     *          normal @0x10 {0x1A0,1.0f} | high @0x18 {0x1C0,2.0f}
     *   0x160: perctable (0x10): u8 decay=5, pan=0x40, reloc=0, pad | wtstr @4 {0x1A0,3.5f} | envelope ofs @0xC=0x200
     *   0x1A0: smzwavetable (0x10): bitfield BE = codec 2 | medium RAM(0) | size 0x123456 (bit26=0) | sample ofs @4=0x300 | loop ofs @8=0x220 | book ofs @0xC=0x260
     *   0x1C0: smzwavetable #2: size 0x400 | sample ofs 0x340 | loop ofs 0x220 (shared) | book ofs 0x260 (shared)
     *   0x200: envdat {delay,value} s16 pairs: {10,100},{20,200},{0,0}
     *   0x220: adpcmloop (0x30): start,end,count=1,sample_end | predictor_state[16] s16 @0x10
     *   0x260: adpcmbook: order=2, n_predictors=1, codebook[16] s16 @8 */
    const u32 IMG = 0x2000;
    /* __WaveTouch's PC branch treats a stored value >= 0x10000000 as "already an absolute address", so the image must
     * live above that: ask for a fixed low-address (< 4 GB) range instead of depending on where malloc lands. */
    unsigned char* mem = nullptr;
    for (uintptr_t want = 0x30000000u; want < 0x70000000u && !mem; want += 0x01000000u)
        mem = (unsigned char*)VirtualAlloc((void*)want, 0x10000, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!mem) { pc_lowaddr_log("[SELFTEST] bank-table test SKIPPED: no free range above 0x10000000 below 4 GB\n"); return; }
    unsigned char* img = mem;
    memset(mem, 0, 0x10000);
    PC_LOWADDR_CHECK("selftest: jaudio bank image", img, IMG);
    u32 base = PC_PTR32("selftest bank image", img);
    ST_CHECK(base >= 0x10000000u, "bank image at 0x%X is below the PC relocation heuristic", base);

    be32(img, 0x000, 0x100); be32(img, 0x004, 0x120); be32(img, 0x008, 0x140);
    be32(img, 0x100, 0x160); be32(img, 0x104, 0x160); be32(img, 0x108, 0);
    be32(img, 0x120, 0x1A0); bef32(img, 0x124, 2.5f); be32(img, 0x128, 0); bef32(img, 0x12C, 0.75f);
    img[0x140] = 0; img[0x141] = 0x10; img[0x142] = 0x7E; img[0x143] = 3;
    be32(img, 0x144, 0x200);
    be32(img, 0x148, 0x1A0); bef32(img, 0x14C, 1.25f);
    be32(img, 0x150, 0x1A0); bef32(img, 0x154, 1.0f);
    be32(img, 0x158, 0x1C0); bef32(img, 0x15C, 2.0f);
    img[0x160] = 5; img[0x161] = 0x40; img[0x162] = 0; img[0x163] = 0;
    be32(img, 0x164, 0x1A0); bef32(img, 0x168, 3.5f); be32(img, 0x16C, 0x200);
    be32(img, 0x1A0, (2u << 28) | (0u << 26) | 0x123456u); be32(img, 0x1A4, 0x300); be32(img, 0x1A8, 0x220); be32(img, 0x1AC, 0x260);
    be32(img, 0x1C0, (1u << 28) | 0x400u);                 be32(img, 0x1C4, 0x340); be32(img, 0x1C8, 0x220); be32(img, 0x1CC, 0x260);
    be16(img, 0x200, 10); be16(img, 0x202, 100); be16(img, 0x204, 20); be16(img, 0x206, 200); be16(img, 0x208, 0); be16(img, 0x20A, 0);
    be32(img, 0x220, 7); be32(img, 0x224, 900); be32(img, 0x228, 1); be32(img, 0x22C, 1000);
    for (int i = 0; i < 16; i++) be16(img, 0x230 + i * 2, (u16)(0x100 + i));
    be32(img, 0x260, 2); be32(img, 0x264, 1);
    for (int i = 0; i < 16; i++) be16(img, 0x268 + i * 2, (u16)(0x200 + i));

    /* audio-system state the real code reads */
    voiceinfo* old_vi = AG.voice_info;
    static voiceinfo vi[1];
    memset(vi, 0, sizeof(vi));
    vi[0].num_instruments = 1; vi[0].num_drums = 3; vi[0].num_sfx = 2;
    AG.voice_info = vi;
    AG.num_requested_samples = 0;
    static unsigned char wavebuf[0x1000];
    WaveMedia wm;
    memset(&wm, 0, sizeof(wm));
    wm.wave0_p = wavebuf; wm.wave1_p = wavebuf; wm.wave0_media = MEDIUM_RAM; wm.wave1_media = MEDIUM_RAM;
    u32 wbase = PC_PTR32("selftest wave buffer", wavebuf);

    Nas_BankOfsToAddr(0, img, &wm, FALSE);

    auto RD = [&](u32 off) { u32 v; memcpy(&v, img + off, 4); return v; };
    /* offset table */
    ST_CHECK(RD(0x000) == base + 0x100 && RD(0x004) == base + 0x120 && RD(0x008) == base + 0x140, "ctrl offset table = 0x%X 0x%X 0x%X", RD(0), RD(4), RD(8));
    /* percussion slot array: 4-byte slots, shared entry relocated once, NULL stays 0 */
    ST_CHECK(RD(0x100) == base + 0x160 && RD(0x104) == base + 0x160, "perc slots [0]=0x%X [1]=0x%X, expected 0x%X (shared entry relocated once)", RD(0x100), RD(0x104), base + 0x160);
    ST_CHECK(RD(0x108) == 0, "NULL perc slot changed to 0x%X", RD(0x108));
    /* perctable @0x160: tuned_sample {wavetable@4, tuning@8}, envelope@0xC; neighbours intact */
    ST_CHECK(RD(0x164) == base + 0x1A0, "perctable.tuned_sample.wavetable = 0x%X, expected 0x%X", RD(0x164), base + 0x1A0);
    ST_CHECK(lef32(img, 0x168) == 3.5f, "perctable.tuned_sample.tuning = %f (overlapped by an 8-byte wavetable pointer would differ)", lef32(img, 0x168));
    ST_CHECK(RD(0x16C) == base + 0x200, "perctable.envelope = 0x%X, expected 0x%X", RD(0x16C), base + 0x200);
    ST_CHECK(img[0x160] == 5 && img[0x161] == 0x40 && img[0x162] == 1, "perctable bytes decay/pan/is_relocated = %u/%u/%u (expect 5/64/1)", img[0x160], img[0x161], img[0x162]);
    /* sfx table (percvoicetable[2]) */
    ST_CHECK(RD(0x120) == base + 0x1A0 && lef32(img, 0x124) == 2.5f, "sfx[0] = {0x%X, %f}", RD(0x120), lef32(img, 0x124));
    ST_CHECK(RD(0x128) == 0 && lef32(img, 0x12C) == 0.75f, "sfx[1] (NULL wavetable) = {0x%X, %f}", RD(0x128), lef32(img, 0x12C));
    /* voicetable @0x140 */
    ST_CHECK(img[0x140] == 1 && img[0x141] == 0x10 && img[0x142] == 0x7E && img[0x143] == 3, "voicetable bytes = %u %u %u %u", img[0x140], img[0x141], img[0x142], img[0x143]);
    ST_CHECK(RD(0x144) == base + 0x200, "voicetable.envelope = 0x%X", RD(0x144));
    ST_CHECK(RD(0x148) == base + 0x1A0 && lef32(img, 0x14C) == 1.25f, "voicetable.low = {0x%X, %f}", RD(0x148), lef32(img, 0x14C));
    ST_CHECK(RD(0x150) == base + 0x1A0 && lef32(img, 0x154) == 1.0f, "voicetable.normal = {0x%X, %f}", RD(0x150), lef32(img, 0x154));
    ST_CHECK(RD(0x158) == base + 0x1C0 && lef32(img, 0x15C) == 2.0f, "voicetable.high = {0x%X, %f}", RD(0x158), lef32(img, 0x15C));
    /* smzwavetable #1 @0x1A0 (shared by perc/sfx/voice entries: must be relocated exactly once) */
    smzwavetable* wt = (smzwavetable*)(img + 0x1A0);
    ST_CHECK(wt->size == 0x123456 && wt->codec == 2 && wt->medium == MEDIUM_RAM && wt->is_relocated == 1, "smzwavetable fields size=0x%X codec=%u medium=%u reloc=%u", (unsigned)wt->size, (unsigned)wt->codec, (unsigned)wt->medium, (unsigned)wt->is_relocated);
    ST_CHECK(RD(0x1A4) == wbase + 0x300, "smzwavetable.sample = 0x%X, expected wave0_p+0x300 = 0x%X", RD(0x1A4), wbase + 0x300);
    ST_CHECK(RD(0x1A8) == base + 0x220 && RD(0x1AC) == base + 0x260, "smzwavetable.loop/book = 0x%X/0x%X", RD(0x1A8), RD(0x1AC));
    smzwavetable* wt2 = (smzwavetable*)(img + 0x1C0);
    ST_CHECK(wt2->size == 0x400 && wt2->codec == 1 && RD(0x1C4) == wbase + 0x340 && RD(0x1C8) == base + 0x220 && RD(0x1CC) == base + 0x260, "smzwavetable #2 wrong (size 0x%X sample 0x%X loop 0x%X book 0x%X)", (unsigned)wt2->size, RD(0x1C4), RD(0x1C8), RD(0x1CC));
    /* swapped payload behind the relocated pointers */
    adpcmloop* lp = (adpcmloop*)(img + 0x220);
    ST_CHECK(lp->loop_start == 7 && lp->loop_end == 900 && lp->count == 1 && lp->sample_end == 1000 && lp->predictor_state[0] == 0x100 && lp->predictor_state[15] == 0x10F, "adpcmloop swapped wrongly (%u %u %u %u ps0=0x%X)", (unsigned)lp->loop_start, (unsigned)lp->loop_end, (unsigned)lp->count, (unsigned)lp->sample_end, lp->predictor_state[0]);
    adpcmbook* bk = (adpcmbook*)(img + 0x260);
    ST_CHECK(bk->order == 2 && bk->n_predictors == 1 && bk->codebook[0] == 0x200 && bk->codebook[15] == 0x20F, "adpcmbook swapped wrongly (order %d n %d cb0 0x%X)", (int)bk->order, (int)bk->n_predictors, bk->codebook[0]);
    envdat* ev = (envdat*)(img + 0x200);
    ST_CHECK(ev[0].delay == 10 && ev[0].value == 100 && ev[1].delay == 20 && ev[1].value == 200 && ev[2].delay == 0, "envdat swapped once (0x%X,0x%X %d,%d)", (unsigned)(u16)ev[0].delay, (unsigned)(u16)ev[0].value, ev[1].delay, ev[1].value);
    /* typed access must agree with the raw slots */
    perctable* pt = (perctable*)(img + 0x160);
    ST_CHECK((unsigned char*)(smzwavetable*)pt->tuned_sample.wavetable == img + 0x1A0 && pt->tuned_sample.tuning == 3.5f && (unsigned char*)(envdat*)pt->envelope == img + 0x200, "typed perctable read");
    voicetable* vt = (voicetable*)(img + 0x140);
    ST_CHECK((unsigned char*)(smzwavetable*)vt->high_pitch_tuned_sample.wavetable == img + 0x1C0 && vt->high_pitch_tuned_sample.tuning == 2.0f, "typed voicetable read");
    /* runtime voiceinfo now points at the 4-byte slot arrays */
    ST_CHECK((u32)(uintptr_t)vi[0].percussion == base + 0x100 && (unsigned char*)(perctable*)vi[0].percussion[0] == img + 0x160 && (unsigned char*)(perctable*)vi[0].percussion[1] == img + 0x160 && (uintptr_t)(perctable*)vi[0].percussion[2] == 0,
             "voiceinfo.percussion[] (4-byte slots) wrong: [0]=%p [1]=%p [2]=%p", (void*)(perctable*)vi[0].percussion[0], (void*)(perctable*)vi[0].percussion[1], (void*)(perctable*)vi[0].percussion[2]);
    ST_CHECK((unsigned char*)vi[0].instruments == img + 0x008 && (unsigned char*)(voicetable*)vi[0].instruments[0] == img + 0x140, "voiceinfo.instruments[0] = %p, expected %p", (void*)(voicetable*)vi[0].instruments[0], (void*)(img + 0x140));
    ST_CHECK((unsigned char*)vi[0].effects == img + 0x120 && vi[0].effects[0].tuned_sample.tuning == 2.5f && vi[0].effects[1].tuned_sample.tuning == 0.75f, "voiceinfo.effects[] stride wrong (tuning %f %f)", vi[0].effects[0].tuned_sample.tuning, vi[0].effects[1].tuned_sample.tuning);
    ST_CHECK(AG.num_used_samples == 0, "unexpected used samples: %d", AG.num_used_samples);
    pc_lowaddr_log("[SELFTEST] bank tables ok: %s (Nas_BankOfsToAddr -> __WaveTouch on a GC-layout BE image; slots are 4 bytes, wtstr.wavetable/tuning separate)\n", s_fail ? "WITH FAILURES" : "all fields resolved");
    AG.voice_info = old_vi;
    VirtualFree(mem, 0, MEM_RELEASE);
}


static void test_jkr(void) {
    pc_lowaddr_log("[SELFTEST] JKRExpHeap::CMemBlock: sizeof=0x%zX (GC 0x10) offsetof(mPrev)=0x%zX offsetof(mNext)=0x%zX\n",
                   sizeof(JKRExpHeap::CMemBlock), offsetof(JKRExpHeap::CMemBlock, mPrev), offsetof(JKRExpHeap::CMemBlock, mNext));
    ST_CHECK(sizeof(JKRExpHeap::CMemBlock) == 0x10, "CMemBlock size 0x%zX", sizeof(JKRExpHeap::CMemBlock));

    JKRExpHeap* root = JKRExpHeap::createRoot(1, false);
    ST_CHECK(root != nullptr, "createRoot failed");
    if (!root) return;
    PC_LOWADDR_CHECK("selftest: JKR root heap object", root, sizeof(JKRExpHeap));
    s32 free0 = root->getTotalFreeSize();
    pc_lowaddr_log("[SELFTEST] JKR root heap at %p, free=0x%X\n", (void*)root, (unsigned)free0);
    ST_CHECK(root->check(), "root heap inconsistent right after creation");

    JKRExpHeap* child = JKRExpHeap::create(0x100000, root, false);
    ST_CHECK(child != nullptr, "child heap create failed");
    JKRHeap* heaps[2] = { root, child };

    static const int aligns[] = { 4, 8, 16, 32, 64, 128, -4, -16, -32 };
    for (int h = 0; h < 2; h++) {
        JKRHeap* heap = heaps[h];
        if (!heap) continue;
        s32 total0 = heap->getTotalFreeSize();
        static Alloc live[256];
        int nlive = 0, ok_allocs = 0;
        for (int iter = 0; iter < 6000; iter++) {
            bool do_alloc = nlive == 0 || (nlive < 256 && (rnd() & 3) != 0);
            if (do_alloc) {
                int align = aligns[rnd() % 9];
                uint32_t size = 1 + rnd() % (h ? 3000 : 20000);
                unsigned char* p = (unsigned char*)heap->alloc(size, align);
                if (!p) continue;
                ok_allocs++;
                int a = align < 0 ? -align : align;
                ST_CHECK(((uintptr_t)p % a) == 0, "alloc(%u, %d) returned %p (not %d-aligned)", size, align, (void*)p, a);
                JKRExpHeap::CMemBlock* b = JKRExpHeap::CMemBlock::getHeapBlock(p);
                ST_CHECK(b && b->isValid() && b->getContent() == p, "block header of %p invalid (usage=0x%X)", (void*)p, b ? b->mUsageHeader : 0);
                ST_CHECK(heap->getSize(p) >= (s32)size, "getSize(%p)=%d < requested %u", (void*)p, heap->getSize(p), size);
                PC_LOWADDR_ASSERT("selftest: JKR allocation", p);
                unsigned char tag = (unsigned char)(rnd() | 1);
                memset(p, tag, size);
                live[nlive++] = { p, size, tag };
            } else {
                int i = rnd() % nlive;
                Alloc a = live[i];
                for (uint32_t k = 0; k < a.size; k++)
                    if (a.p[k] != a.tag) { ST_CHECK(false, "corruption in block %p at +%u (0x%02X != 0x%02X)", (void*)a.p, k, a.p[k], a.tag); break; }
                heap->free(a.p);
                live[i] = live[--nlive];
            }
            if ((iter % 500) == 0)
                ST_CHECK(heap->check(), "heap %d check() failed at iteration %d", h, iter);
        }
        while (nlive) {
            Alloc a = live[--nlive];
            for (uint32_t k = 0; k < a.size; k++)
                if (a.p[k] != a.tag) { ST_CHECK(false, "corruption at free-all in %p +%u", (void*)a.p, k); break; }
            heap->free(a.p);
        }
        ST_CHECK(heap->check(), "heap %d check() failed after freeing everything", h);
        s32 total1 = heap->getTotalFreeSize();
        pc_lowaddr_log("[SELFTEST] JKR heap %d: %d successful allocs, total free before=0x%X after=0x%X\n", h, ok_allocs, (unsigned)total0, (unsigned)total1);
        ST_CHECK(total1 == total0, "heap %d leaked/lost memory: 0x%X -> 0x%X", h, (unsigned)total0, (unsigned)total1);
    }
}

static void test_osmalloc(void) {
    pc_lowaddr_log("[SELFTEST] OSMemBlock: sizeof=0x%zX (GC 0x30), payload offset in block = sizeof\n", sizeof(OSMemBlock));
    const s32 arena_size = 0x200000;
    unsigned char* base = (unsigned char*)malloc(arena_size + 64);
    PC_LOWADDR_CHECK("selftest: __osMalloc arena", base, arena_size + 64);
    static OSArena arena;
    __osMallocInit(&arena, base, arena_size);
    u32 max0 = 0, free0 = 0, used0 = 0;
    __osGetFreeArena(&arena, &max0, &free0, &used0);
    pc_lowaddr_log("[SELFTEST] __osMalloc arena: max block=0x%X free=0x%X used=0x%X\n", (unsigned)max0, (unsigned)free0, (unsigned)used0);
    ST_CHECK(__osCheckArena(&arena) == 0, "__osCheckArena failed after init");
    static Alloc live[200];
    int nlive = 0, oks = 0;
    static const u32 aligns[] = { 8, 16, 32, 64 };
    for (int iter = 0; iter < 5000; iter++) {
        if (nlive == 0 || (nlive < 200 && (rnd() & 3) != 0)) {
            u32 align = aligns[rnd() % 4];
            u32 size = 1 + rnd() % 9000;
            unsigned char* p = (unsigned char*)(rnd() & 1 ? __osMallocAlign(&arena, size, align) : __osMalloc(&arena, size));
            if (!p) continue;
            oks++;
            PC_LOWADDR_ASSERT("selftest: __osMalloc allocation", p);
            ST_CHECK(((uintptr_t)p % 16) == 0, "__osMalloc payload %p not 16-aligned", (void*)p);
            unsigned char tag = (unsigned char)(rnd() | 1);
            memset(p, tag, size);
            live[nlive++] = { p, size, tag };
        } else {
            int i = rnd() % nlive;
            Alloc a = live[i];
            for (uint32_t k = 0; k < a.size; k++)
                if (a.p[k] != a.tag) { ST_CHECK(false, "__osMalloc corruption in %p +%u", (void*)a.p, k); break; }
            __osFree(&arena, a.p);
            live[i] = live[--nlive];
        }
        if ((iter % 500) == 0) ST_CHECK(__osCheckArena(&arena) == 0, "__osCheckArena failed at iteration %d", iter);
    }
    while (nlive) { Alloc a = live[--nlive]; __osFree(&arena, a.p); }
    u32 max1 = 0, free1 = 0, used1 = 0;
    __osGetFreeArena(&arena, &max1, &free1, &used1);
    pc_lowaddr_log("[SELFTEST] __osMalloc: %d successful allocs; after freeing all: max=0x%X free=0x%X used=0x%X\n", oks, (unsigned)max1, (unsigned)free1, (unsigned)used1);
    ST_CHECK(__osCheckArena(&arena) == 0, "__osCheckArena failed after freeing everything");
    ST_CHECK(free1 == free0 && used1 == used0, "__osMalloc leaked/lost memory (free 0x%X->0x%X used 0x%X->0x%X)", (unsigned)free0, (unsigned)free1, (unsigned)used0, (unsigned)used1);
}

/* ---------------- scene tables (m_scene.h) ---------------- */
#include "m_scene.h"

static u32 rd32(const unsigned char* p) { u32 v; memcpy(&v, p, 4); return v; }
static void wr32(unsigned char* p, u32 v) { memcpy(p, &v, 4); }

/* Walk a scene table exactly like Scene_ct (stride = sizeof(Scene_Word_u), stop at the END word). Returns the number of
 * words before END, or -1 if it did not terminate / had an invalid type. */
static int scene_walk(const char* name, Scene_Word_u* tbl) {
    const unsigned char* raw = (const unsigned char*)tbl;
    int n = 0;
    for (Scene_Word_u* w = tbl; n < 64; w++, n++) {
        ST_CHECK((const unsigned char*)w == raw + 8 * n, "%s: word %d at +0x%X, expected +0x%X (stride)", name, n, (unsigned)((const unsigned char*)w - raw), 8 * n);
        u32 type = w->misc.type;
        ST_CHECK(raw[8 * n] == type, "%s: word %d raw type 0x%X != typed 0x%X", name, n, raw[8 * n], (unsigned)type);
        if (type == mSc_SCENE_DATA_TYPE_END) return n;
        if (type >= mSc_SCENE_DATA_TYPE_NUM) {
            ST_CHECK(false, "%s: word %d has invalid type 0x%X (table read at the wrong stride/offset?)", name, n, (unsigned)type);
            return -1;
        }
        u32 slot = rd32(raw + 8 * n + 4);
        ST_CHECK(slot == w->misc.param3, "%s: word %d raw +4 = 0x%X, typed param3 = 0x%X", name, n, slot, (unsigned)w->misc.param3);
        const void* typed = nullptr;
        switch (type) {
            case mSc_SCENE_DATA_TYPE_PLAYER_PTR:
            case mSc_SCENE_DATA_TYPE_ACTOR_PTR: typed = (const void*)(Actor_data*)w->actor.data_p; break;
            case mSc_SCENE_DATA_TYPE_CTRL_ACTOR_PTR: typed = (const void*)(s16*)w->control_actor.ctrl_actor_profile_p; break;
            case mSc_SCENE_DATA_TYPE_OBJECT_EXCHANGE_BANK_PTR: typed = (const void*)(s16*)w->object_bank.banks_p; break;
            case mSc_SCENE_DATA_TYPE_DOOR_DATA_PTR: typed = (const void*)(Door_data_c*)w->door_data.door_data_p; break;
            default: continue;
        }
        ST_CHECK((u32)(uintptr_t)typed == slot, "%s: word %d typed pointer %p != raw slot 0x%X", name, n, typed, slot);
        if (slot == 0) ST_CHECK(raw[8 * n + 1] == 0, "%s: word %d (type %u, count %u) has a NULL data pointer", name, n, (unsigned)type, raw[8 * n + 1]);
        else ST_CHECK(slot >= 0x1000u, "%s: word %d (type %u) slot 0x%X is implausible", name, n, (unsigned)type, slot);
    }
    ST_CHECK(false, "%s: no END word within 64 words", name);
    return -1;
}

static void test_scene_layout(void) {
    ST_CHECK(sizeof(Scene_Word_u) == 8, "sizeof(Scene_Word_u) = 0x%zX, expected 0x8", sizeof(Scene_Word_u));
    ST_CHECK(offsetof(Scene_Word_Data_Misc_c, param3) == 4, "misc.param3 offset");
    ST_CHECK(offsetof(Scene_Word_Data_Actor_c, data_p) == 4 && sizeof(Scene_Word_Data_Actor_c) == 8, "actor.data_p offset/size");
    ST_CHECK(offsetof(Scene_Word_Data_Ctrl_Actor_c, ctrl_actor_profile_p) == 4 && sizeof(Scene_Word_Data_Ctrl_Actor_c) == 8, "ctrl_actor_profile_p offset/size");
    ST_CHECK(offsetof(Scene_Word_Data_Object_Bank_c, banks_p) == 4 && sizeof(Scene_Word_Data_Object_Bank_c) == 8, "banks_p offset/size");
    ST_CHECK(offsetof(Scene_Word_Data_Door_Data_c, door_data_p) == 4 && sizeof(Scene_Word_Data_Door_Data_c) == 8, "door_data_p offset/size");
    ST_CHECK(sizeof(Scene_Word_Data_FieldCt_c) == 8 && sizeof(Scene_Word_Data_ArrangeFurniture_ct_c) == 2, "FieldCt/ArrangeFurniture_ct size");
    ST_CHECK(sizeof(Actor_data) == 0x10 && sizeof(Door_data_c) == 0x14, "Actor_data/Door_data_c size (targets of the scene pointers)");
}

/* (a) a table assembled byte by byte in the documented 8-byte layout */
static void test_scene_synthetic(void) {
    alignas(8) static unsigned char buf[0x400];
    memset(buf, 0xEE, sizeof(buf)); /* poison so overlap/stride bugs show */
    PC_LOWADDR_CHECK("selftest: scene image", buf, sizeof(buf));
    const u32 base = (u32)(uintptr_t)buf;

    const u32 ACT = 0x100, ACT2 = 0x140, CTRL = 0x180, BANK = 0x1A0, DOOR = 0x1C0;
    struct W { u8 type, cnt, p1, p2; u32 slot; };
    const W words[] = {
        { mSc_SCENE_DATA_TYPE_PLAYER_PTR, 1, 0, 0, base + ACT },
        { mSc_SCENE_DATA_TYPE_CTRL_ACTOR_PTR, 3, 0, 0, base + CTRL },
        { mSc_SCENE_DATA_TYPE_ACTOR_PTR, 2, 0, 0, base + ACT2 },
        { mSc_SCENE_DATA_TYPE_OBJECT_EXCHANGE_BANK_PTR, 4, 0, 0, base + BANK },
        { mSc_SCENE_DATA_TYPE_DOOR_DATA_PTR, 1, 0, 0, base + DOOR },
        { mSc_SCENE_DATA_TYPE_ACTOR_PTR, 0, 0, 0, 0 }, /* NULL data pointer must stay NULL */
        { mSc_SCENE_DATA_TYPE_ARRANGE_FURNITURE_CT, 7, 0, 0, 0 },
        { mSc_SCENE_DATA_TYPE_SOUND, 5, 6, 0, 0 },
        { mSc_SCENE_DATA_TYPE_END, 0, 0, 0, 0 },
    };
    const int NW = (int)(sizeof(words) / sizeof(words[0]));
    for (int i = 0; i < NW; i++) {
        buf[8 * i + 0] = words[i].type; buf[8 * i + 1] = words[i].cnt; buf[8 * i + 2] = words[i].p1; buf[8 * i + 3] = words[i].p2;
        wr32(buf + 8 * i + 4, words[i].slot);
    }
    Scene_Word_u* tbl = (Scene_Word_u*)buf;

    for (int pass = 0; pass < 3; pass++) { /* repeated processing must not modify the table */
        unsigned char before[8 * 16];
        memcpy(before, buf, sizeof(before));
        ST_CHECK(scene_walk("synthetic", tbl) == NW - 1, "synthetic table: wrong word count on pass %d", pass);
        ST_CHECK(memcmp(before, buf, sizeof(before)) == 0, "reading the scene table modified it (pass %d)", pass);
    }
    ST_CHECK((unsigned char*)(Actor_data*)tbl[0].actor.data_p == buf + ACT && tbl[0].actor.num_actors == 1, "PLAYER word typed read");
    ST_CHECK((unsigned char*)(s16*)tbl[1].control_actor.ctrl_actor_profile_p == buf + CTRL && tbl[1].control_actor.num_ctrl_actors == 3, "CTRL_ACTOR word typed read");
    ST_CHECK((unsigned char*)(Actor_data*)tbl[2].actor.data_p == buf + ACT2 && tbl[2].actor.num_actors == 2, "ACTOR word typed read (word 2 must not see word 1)");
    ST_CHECK((unsigned char*)(s16*)tbl[3].object_bank.banks_p == buf + BANK && tbl[3].object_bank.num_banks == 4, "OBJ_BANK word typed read");
    ST_CHECK((unsigned char*)(Door_data_c*)tbl[4].door_data.door_data_p == buf + DOOR && tbl[4].door_data.num_doors == 1, "DOOR word typed read");
    ST_CHECK((Actor_data*)tbl[5].actor.data_p == nullptr && tbl[5].actor.num_actors == 0, "NULL word must stay NULL");
    ST_CHECK(tbl[6].arrange_ftr_ct.arrange_ftr_num == 7 && tbl[7].misc.param0 == 5 && tbl[7].misc.param1 == 6, "ARRANGE_FTR/SOUND typed read");

    /* assigning a slot in word 2 must not touch any other word (the old bug: 16-byte words, pointer at +8) */
    unsigned char snap[8 * 16];
    memcpy(snap, buf, sizeof(snap));
    tbl[2].actor.data_p = (Actor_data*)(buf + 0x150);
    ST_CHECK(rd32(buf + 8 * 2 + 4) == base + 0x150, "assigned slot did not land at +4 of word 2 (=0x%X)", rd32(buf + 8 * 2 + 4));
    for (int i = 0; i < (int)sizeof(snap); i++) {
        if (i >= 8 * 2 + 4 && i < 8 * 2 + 8) continue;
        ST_CHECK(buf[i] == snap[i], "byte +0x%X changed when assigning word 2's pointer", i);
    }
    tbl[2].actor.data_p = (Actor_data*)(buf + ACT2);
    ST_CHECK(memcmp(snap, buf, sizeof(snap)) == 0, "restoring word 2 did not restore the table");
}

/* (b) tables written by the real mSc_DATA_* macros (this is what src/data/scene/*.c do), then read back */
static void test_scene_macros(void) {
    alignas(8) static Actor_data actors[2];
    alignas(8) static s16 ctrl[3] = { 1, 2, 3 };
    alignas(8) static s16 banks[2] = { 4, 5 };
    alignas(8) static Door_data_c doors[1];
    Scene_Word_u t[] = {
        mSc_DATA_PLAYER(&actors[0]),
        mSc_DATA_CTRL_ACTORS(3, ctrl),
        mSc_DATA_ACTORS(2, actors),
        mSc_DATA_OBJ_BANK(2, banks),
        mSc_DATA_DOOR_DATA(1, doors),
        mSc_DATA_FIELDCT(1, 2, 0x1234, 3, 4),
        mSc_DATA_ARRANGE_ROOM_CT(),
        mSc_DATA_ARRANGE_FTR(9),
        mSc_DATA_SOUND(5, 6),
        mSc_DATA_END(),
    };
    const unsigned char* raw = (const unsigned char*)t;
    ST_CHECK(sizeof(t) == 10 * 8, "macro-built table is 0x%zX bytes, expected 0x50 (16-byte words?)", sizeof(t));
    ST_CHECK(raw[0] == mSc_SCENE_DATA_TYPE_PLAYER_PTR && raw[1] == 1 && rd32(raw + 4) == (u32)(uintptr_t)&actors[0], "mSc_DATA_PLAYER bytes");
    ST_CHECK(raw[8] == mSc_SCENE_DATA_TYPE_CTRL_ACTOR_PTR && raw[9] == 3 && rd32(raw + 12) == (u32)(uintptr_t)ctrl, "mSc_DATA_CTRL_ACTORS bytes");
    ST_CHECK(raw[16] == mSc_SCENE_DATA_TYPE_ACTOR_PTR && raw[17] == 2 && rd32(raw + 20) == (u32)(uintptr_t)actors, "mSc_DATA_ACTORS bytes");
    ST_CHECK(raw[24] == mSc_SCENE_DATA_TYPE_OBJECT_EXCHANGE_BANK_PTR && raw[25] == 2 && rd32(raw + 28) == (u32)(uintptr_t)banks, "mSc_DATA_OBJ_BANK bytes");
    ST_CHECK(raw[32] == mSc_SCENE_DATA_TYPE_DOOR_DATA_PTR && raw[33] == 1 && rd32(raw + 36) == (u32)(uintptr_t)doors, "mSc_DATA_DOOR_DATA bytes");
    ST_CHECK(raw[40] == mSc_SCENE_DATA_TYPE_FIELD_CT && raw[41] == 1 && raw[42] == 2 && rd32(raw + 44) == ((0x1234u << 16) | (3u << 8) | 4u), "mSc_DATA_FIELDCT bytes (param3 = 0x%X)", rd32(raw + 44));
    ST_CHECK(raw[48] == mSc_SCENE_DATA_TYPE_ARRANGE_ROOM_CT, "mSc_DATA_ARRANGE_ROOM_CT type at +0x30");
    ST_CHECK(raw[56] == mSc_SCENE_DATA_TYPE_ARRANGE_FURNITURE_CT && raw[57] == 9, "mSc_DATA_ARRANGE_FTR bytes");
    ST_CHECK(raw[64] == mSc_SCENE_DATA_TYPE_SOUND && raw[65] == 5 && raw[66] == 6, "mSc_DATA_SOUND bytes");
    ST_CHECK(raw[72] == mSc_SCENE_DATA_TYPE_END, "mSc_DATA_END at +0x48");
    ST_CHECK(scene_walk("macro", t) == 9, "macro-built table: wrong word count");
    ST_CHECK((Actor_data*)t[2].actor.data_p == actors && (s16*)t[1].control_actor.ctrl_actor_profile_p == ctrl, "macro-built typed reads");
}

/* (c) every real scene table linked into the executable (written by src/data/scene/*.c) */
struct SceneTab { const char* name; Scene_Word_u* tbl; };
static const SceneTab kSceneTabs[] = {
    {"test01_info", test01_info},
    {"test02_info", test02_info},
    {"test03_info", test03_info},
    {"water_test_info", water_test_info},
    {"test_step01_info", test_step01_info},
    {"test04_info", test04_info},
    {"npc_room01_info", npc_room01_info},
    {"test_fd_npc_land_info", test_fd_npc_land_info},
    {"field_tool_field_info", field_tool_field_info},
    {"shop01_info", shop01_info},
    {"BG_TEST01_info", BG_TEST01_info},
    {"BG_TEST01_XLU_info", BG_TEST01_XLU_info},
    {"broker_shop_info", broker_shop_info},
    {"fg_tool_in_info", fg_tool_in_info},
    {"post_office_info", post_office_info},
    {"start_demo1_info", start_demo1_info},
    {"start_demo2_info", start_demo2_info},
    {"police_box_info", police_box_info},
    {"buggy_info", buggy_info},
    {"player_select_info", player_select_info},
    {"player_room_s_info", player_room_s_info},
    {"player_room_m_info", player_room_m_info},
    {"player_room_l_info", player_room_l_info},
    {"shop02_info", shop02_info},
    {"shop03_info", shop03_info},
    {"shop04_1f_info", shop04_1f_info},
    {"test05_info", test05_info},
    {"PLAYER_SELECT2_info", PLAYER_SELECT2_info},
    {"PLAYER_SELECT3_info", PLAYER_SELECT3_info},
    {"shop04_2f_info", shop04_2f_info},
    {"event_notification_info", event_notification_info},
    {"kamakura_info", kamakura_info},
    {"title_demo_info", title_demo_info},
    {"PLAYER_SELECT4_info", PLAYER_SELECT4_info},
    {"museum_entrance_info", museum_entrance_info},
    {"museum_picture_info", museum_picture_info},
    {"museum_fossil_info", museum_fossil_info},
    {"museum_insect_info", museum_insect_info},
    {"museum_fish_info", museum_fish_info},
    {"player_room_ll1_info", player_room_ll1_info},
    {"player_room_ll2_info", player_room_ll2_info},
    {"p_room_bm_s_info", p_room_bm_s_info},
    {"p_room_bm_m_info", p_room_bm_m_info},
    {"p_room_bm_l_info", p_room_bm_l_info},
    {"p_room_bm_ll1_info", p_room_bm_ll1_info},
    {"NEEDLEWORK_info", NEEDLEWORK_info},
    {"player_room_island_info", player_room_island_info},
    {"npc_room_island_info", npc_room_island_info},
    {"start_demo3_info", start_demo3_info},
    {"lighthouse_info", lighthouse_info},
    {"tent_info", tent_info}
};

static void test_scene_real_tables(void) {
    int ntab = 0, nwords = 0;
    for (const SceneTab& s : kSceneTabs) {
        ST_CHECK(((uintptr_t)s.tbl & 3) == 0, "%s: table not 4-byte aligned", s.name);
        const unsigned char* raw = (const unsigned char*)s.tbl;
        int n = scene_walk(s.name, s.tbl);
        ST_CHECK(n >= 3, "%s: only %d words", s.name, n);
        int np = 0, nf = 0; /* every scene has exactly one PLAYER_PTR and one FIELD_CT word (51 of each in src/data/scene) */
        for (int i = 0; i < n; i++) { np += raw[8 * i] == mSc_SCENE_DATA_TYPE_PLAYER_PTR; nf += raw[8 * i] == mSc_SCENE_DATA_TYPE_FIELD_CT; }
        ST_CHECK(np == 1 && nf == 1, "%s: %d PLAYER_PTR / %d FIELD_CT words (expected 1 / 1)", s.name, np, nf);
        nwords += n;
        ntab++;
    }
    pc_lowaddr_log("[SELFTEST] scene tables: %d tables, %d words, sizeof(Scene_Word_u)=%zu\n", ntab, nwords, sizeof(Scene_Word_u));
}

static void test_scene_tables(void) {
    test_scene_layout();
    test_scene_synthetic();
    test_scene_macros();
    test_scene_real_tables();
}

extern "C" int pc_lowaddr_selftest(void) {
    pc_lowaddr_log("[SELFTEST] start (sizeof(void*)=%zu)\n", sizeof(void*));
    OSInit();
    test_archive_layout();
    test_dvd_fileinfo();
    test_jaudio_wave();
    test_jaudio_bank();
    test_jaudio_banktables();
    test_scene_tables();
    test_jkr();
    test_osmalloc();
    s_fail += pc_gbi_token_selftest();
    pc_lowaddr_log("[SELFTEST] done: %d failure(s)\n", s_fail);
    return s_fail;
}

#endif /* PC_LOW_ADDRESS_64 */
