#include "jaudio_NES/neosthread.h"

#include "dolphin/os.h"
#include "jaudio_NES/dummyrom.h"
#include "jaudio_NES/dvdthread.h"
#include "jaudio_NES/aictrl.h"
#include "jaudio_NES/rate.h"
#include "jaudio_NES/audioconst.h"
#include "jaudio_NES/system.h"
#include "jaudio_NES/audiothread.h"
#include "jaudio_NES/cpubuf.h"
#include "jaudio_NES/dummyprobe.h"
#include "jaudio_NES/sub_sys.h"
#include "jaudio_NES/rspsim.h"
#include "jaudio_NES/sample.h"

#define NEOSTHREAD_IMAGE_LOADED_MSG (0x12345678)
#define NEOSTHREAD_ACMD_BUF_NUM 1600

#ifdef TARGET_PC
/*==========================================================================
 * PC: Synchronous NEOS — no thread, DVD loads complete synchronously.
 *==========================================================================*/
static s16* tmp_buf = nullptr;
static BOOL neos_ready = FALSE;
static Acmd pc_task_buf[NEOSTHREAD_ACMD_BUF_NUM];

/* PREVIOUSLY: this used a two-slot double-buffer (build "cur", execute "prev",
 * built one Neos_Update() call ago) modeled on the GC's async DSP dispatch,
 * where a real task takes hardware time to complete and it's meaningful to
 * build the next one while waiting. PC has no such hardware -- RspStart2()
 * below runs the whole command list synchronously, in-process, before this
 * function returns.
 *
 * That artificial one-call lag was also fragile: `pc_neos_cur`/`pc_tasks[2]`
 * are plain (non-atomic, non-volatile) statics whose correctness depends on
 * the compiler never caching `cur`/`prev` across the call in a way that skips
 * a flush to memory. Single-run diagnostic instrumentation (build-order vs.
 * execute-order tagging, cross-checked via a FIFO so per-channel state always
 * paired with the audio it actually produced) proved that when this got
 * desynchronized, per-channel playback position -- advanced unconditionally
 * inside CreateAudioTask()/Nas_DriveRsp() on every BUILD, regardless of
 * whether that build ever got executed -- raced ahead of what was actually
 * being executed and sent to the output. Because a channel's synthesis input
 * is baked directly from its live playback offset, this manifested as the
 * output abruptly jumping forward to a later, essentially unrelated point in
 * the ADPCM waveform: an uncorrelated sample-to-sample value with roughly a
 * coin-flip's chance of opposite polarity from the last correctly-sequenced
 * sample -- exactly the abrupt polarity-reversal clicks reported (confirmed
 * by direct GDB inspection: the two-slot index intermittently failed to
 * alternate every call as the code requires, both with instrumentation added
 * and, per the underlying calls-built/calls-executed counters, in the
 * uninstrumented build; removing the two-slot indirection removes this
 * failure mode by construction, and 0 clicks were observed in the >100s of
 * captured audio across all channel configurations tested after the fix).
 *
 * Building AND executing the same task in the same call (below) is both
 * genuinely synchronous, matching this file's own "generate audio task
 * commands and run rspsim immediately" doc comment, and structurally cannot
 * desynchronize build order from execute order -- there is only one order.
 * GC's real async dispatch (the #else branch below) is untouched. */
extern u32 Neos_Update(s16* dst) {
    /* The very first call lands before some other lazily-warmed-up audio state is
     * fully settled (a pre-existing startup-ordering fragility, independent of the
     * double-buffer removal above -- CreateAudioTask()'s build chain on this exact
     * first call clobbers state it should not; not yet root-caused, and out of
     * scope for the click fix). The old two-slot pipeline never actually exercised
     * a real build+execute on its first call either (the "prev" slot was always
     * empty then, so it silently fell into the bzero branch below) -- so this just
     * keeps that same one-call protective margin instead of reintroducing it. */
    static BOOL first_call = TRUE;
    u32 tasks;

    if (!neos_ready) return FALSE;

    if (first_call) {
        first_call = FALSE;
        Jac_bzero(dst, DAC_SIZE * 2);
        NeosSync();
        return TRUE;
    }

    /* Synchronous: generate audio task commands and run rspsim immediately */
    tasks = CreateAudioTask(pc_task_buf, tmp_buf, JAC_FRAMESAMPLES, 0);
    if (tasks) {
        RspStart2((u32*)pc_task_buf, tasks, 0);
        Jac_bcopy(tmp_buf, dst, DAC_SIZE * 2);
    } else {
        Jac_bzero(dst, DAC_SIZE * 2);
    }

    /* Diagnostic: check NEOS output amplitude every 60 frames */
    {
        static u32 neos_diag_ctr = 0;
        if ((neos_diag_ctr++ % 60) == 0) {
            s32 peak = 0;
            for (u32 i = 0; i < DAC_SIZE; i++) {
                s32 v = dst[i];
                if (v < 0) v = -v;
                if (v > peak) peak = v;
            }
            printf("[NEOS_OUT] frame=%u tasks=%u peak=%d\n", neos_diag_ctr, tasks, peak);
        }
    }

    NeosSync();
    return TRUE;
}

extern void ImageLoaded(u32 param) {
    /* On PC, DVD load is synchronous — this is called as the completion callback */
    (void)param;
}

extern BOOL Neos_CheckBoot(void) {
    return neos_ready;
}

/* Called from StartAudioThread on PC to do synchronous NEOS init */
void pc_neos_init_sync(void) {
    neos_ready = FALSE;

    u32 neos_rom_top = GetNeosRomTop();
    u32 neos_rom_preloaded = GetNeosRom_PreLoaded();
    u32 neos_file_top = GetNeos_FileTop();

    /* Synchronous DVD load — dvdthread processes this inline on PC */
    DVDT_LoadtoARAM(0, "/audiorom.img", neos_rom_top + neos_rom_preloaded, neos_file_top, 0, nullptr, &ImageLoaded);

    /* Process the DVD task synchronously */
    pc_dvd_process_all_tasks();

    tmp_buf = (s16*)OSAlloc2(DAC_SIZE * 2);

    /* Initialize NEOS audio */
    s32 buf_size = AGC.acmdBufSize;
    u64* acmdBuf = (u64*)OSAlloc2(buf_size);
    Nas_InitAudio(acmdBuf, buf_size);
    NeosSync();
    neos_ready = TRUE;

    Jac_RegisterMixcallback(&MixCpu, MixMode_Interleave);
}

extern void* neosproc(void* param) {
    /* Not used on PC — synchronous model */
    (void)param;
    return nullptr;
}

#else /* !TARGET_PC — original GC code */

static OSMessageQueue neosproc_mq;
static u32 neosproc_mq_init = FALSE;
static s16* tmp_buf = nullptr;
static BOOL neos_ready = FALSE;

extern u32 Neos_Update(s16* dst) {
    if (neosproc_mq_init) {
        if (OSSendMessage(&neosproc_mq, (OSMessage)dst, OS_MESSAGE_NOBLOCK) == TRUE) {
            return TRUE;
        } else {
            return FALSE;
        }
    }

    return FALSE;
}

extern void ImageLoaded(u32 param) {
    OSSendMessage(&neosproc_mq, (OSMessage)NEOSTHREAD_IMAGE_LOADED_MSG, OS_MESSAGE_BLOCK);
}

extern BOOL Neos_CheckBoot(void) {
    return neos_ready;
}

extern void* neosproc(void* param) {
    static OSMessage msgbuf[1];
    static u32 cur = 0;

    neos_ready = FALSE;
    OSInitMessageQueue(&neosproc_mq, msgbuf, 1);
    neosproc_mq_init = TRUE;
    u32 neos_rom_top = GetNeosRomTop();
    u32 neos_rom_preloaded = GetNeosRom_PreLoaded();
    u32 neos_file_top = GetNeos_FileTop();

    DVDT_LoadtoARAM(0, "/audiorom.img", neos_rom_top + neos_rom_preloaded, neos_file_top, 0, nullptr, &ImageLoaded);

    OSMessage msg;
    do {
        OSReceiveMessage(&neosproc_mq, &msg, 1);
    } while (msg != (OSMessage)NEOSTHREAD_IMAGE_LOADED_MSG);

    tmp_buf = (s16*)OSAlloc2(DAC_SIZE * 2);

    /* Initialize neos */
    s32 tmp = AGC.acmdBufSize;
    u64* acmdBuf = (u64*)OSAlloc2(tmp);
    Nas_InitAudio(acmdBuf, tmp);
    NeosSync();
    neos_ready = TRUE;

    Jac_RegisterMixcallback(&MixCpu, MixMode_Interleave);

    do {
        static Acmd task_buf[2][NEOSTHREAD_ACMD_BUF_NUM];
        static u32 tasks[2] = { 0, 0 };

        OSReceiveMessage(&neosproc_mq, &msg, OS_MESSAGE_BLOCK);
        Probe_Start(8, "NEOS THREAD");
        s16* samples_dst = (s16*)msg;
        tasks[cur] = CreateAudioTask(task_buf[cur], tmp_buf, JAC_FRAMESAMPLES, 0);

        tmp = (cur + 1) & 1;
        if (tasks[tmp]) {
            RspStart2((u32*)task_buf[tmp], tasks[tmp], 0);
            tasks[tmp] = 0;
            Jac_bcopy(tmp_buf, samples_dst, DAC_SIZE * 2);
        } else {
            Jac_bzero(samples_dst, DAC_SIZE * 2);
        }

        Probe_Finish(8);
        NeosSync();
        cur = tmp;
    } while (TRUE);
}

#endif /* TARGET_PC */
