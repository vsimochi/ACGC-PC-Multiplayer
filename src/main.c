#include "main.h"

#include "boot.h"
#include "irqmgr.h"
#include "sys_stacks.h"
#include "graph.h"
#include "libultra/osMesg.h"
#include "libultra/os_thread.h"
#include "jsyswrap.h"
#include "m_card.h"
#include "_mem.h"
#include "padmgr.h"
#include "libultra/setthreadpri.h"
#include "m_msg.h"
#include "Famicom/famicom.h"
#include "m_debug.h"
#include "dolphin/os.h"
#include "libforest/osreport.h"
#include "m_land.h"

// TODO: actually add all the stacks and headers

OSThread graphThread;
static OSMessage serialMsgBuf;
static OSMessageQueue l_serialMsgQ;
u8 SegmentBaseAddress[0x40];

int ScreenWidth = SCREEN_WIDTH;
int ScreenHeight = SCREEN_HEIGHT;

extern void mainproc(void* val) {

    irqmgr_client_t irqClient;
    OSMessageQueue irqMgrMsgQueue;
    OSMessage irqMsgBuf[10];
    OSMessage msg;

    ScreenWidth = SCREEN_WIDTH;
    ScreenHeight = SCREEN_HEIGHT;

#ifdef TARGET_PC
    OSReport("[PC] mainproc: JW_BeginFrame/EndFrame...\n");
#endif
    JW_BeginFrame();
    JW_EndFrame();
    mCD_init_card();

    osCreateMesgQueue(&l_serialMsgQ, &serialMsgBuf, 1);
    osCreateMesgQueue(&irqMgrMsgQueue, irqMsgBuf, 10);
#ifdef TARGET_PC
    OSReport("[PC] mainproc: CreateIRQManager...\n");
#endif
    CreateIRQManager(irqmgrStack + IRQMGR_STACK_SIZE, IRQMGR_STACK_SIZE, 18, 1);
    irqmgr_AddClient(&irqClient, &irqMgrMsgQueue);
    memset(padmgrStack, 0xEB, PADMGR_STACK_SIZE);

#ifdef TARGET_PC
    OSReport("[PC] mainproc: padmgr_Create...\n");
#endif
    padmgr_Create(&l_serialMsgQ, 7, 15, padmgrStack + PADMGR_STACK_SIZE, PADMGR_STACK_SIZE);

    osCreateThread2(&graphThread, 4, graph_proc, val, graphStack + GRAPH_STACK_SIZE, GRAPH_STACK_SIZE, 8);

    JW_BeginFrame();
    JW_EndFrame();

    osStartThread(&graphThread);
    osSetThreadPri(NULL, 5);

#ifdef TARGET_PC
    OSReport("[PC] mainproc: JW_Init3...\n");
#endif
    JW_Init3();
#ifdef TARGET_PC
    OSReport("[PC] mainproc: mMsg_aram_init2...\n");
#endif
    mMsg_aram_init2();
#ifdef TARGET_PC
    OSReport("[PC] mainproc: mLd_StartFlagOn...\n");
#endif
    mLd_StartFlagOn();
#ifdef TARGET_PC
    OSReport("[PC] mainproc: famicom_mount_archive...\n");
#endif
    famicom_mount_archive();

#ifdef TARGET_PC
    OSReport("[PC] mainproc: JC_JKRAramHeap_dump...\n");
#endif
    JC_JKRAramHeap_dump(JC_JKRAram_getAramHeap());
    osSetThreadPri(NULL, 13);

#ifdef TARGET_PC
    /* On PC, the graph thread was skipped (single-threaded).
     * Instead of blocking in the message loop, call graph_proc directly. */
    OSReport("[PC] mainproc: calling graph_proc directly (single-threaded)...\n");
    graph_proc(val);
    OSReport("[PC] mainproc: graph_proc returned\n");
    /* Stage 0.5E: one final authoritative-town save on the normal graceful shutdown path (this is
     * the game's only quit path -- reached identically whether the window was closed or the game
     * itself requested exit, since both simply return from graph_proc's loop, per pc_vi.c's
     * g_pc_running check). Reuses pc_save_write_authoritative() (Stage 0.5C) and the same
     * pcfa_save_ready() readiness predicate periodic saving already gates on (Stage 0.5D) -- no new
     * save mechanism, no serialization duplication, no signal handler (this path already runs
     * synchronously and unconditionally on every graceful exit; nothing about it requires one).
     * Skipped entirely if pcfa_save_ready() is false (no resident ever bound, still on the title/
     * select screen, etc.) so this never fires during early startup. No existing shutdown save
     * exists to integrate with instead -- confirmed absent in the Stage 0.5 persistence audit and
     * re-verified directly in this file before adding this.
     *
     * M1 hardening correction: also gated on this process NOT being a network CLIENT
     * (pc_net_game_role_is_client(), pc/src/pc_m_card.c), matching pc_vi.c's periodic-save gate
     * (which already excludes CLIENT -- see that file's doc comment). A client never owns the
     * authoritative Save_t it is rendering; only the host may write it. This is deliberately NOT
     * a simple "== HOST" check: ordinary single-player (PC_NETGAME_ROLE_NONE) must keep getting
     * this exact shutdown-save behavior unchanged (this mechanism predates any multiplayer
     * concern), so the condition below saves for HOST and for single-player alike, and refuses
     * only for CLIENT. pc_net_game_role_is_client() is a tiny extern wrapper around
     * pc_net_game_role() == PC_NETGAME_ROLE_CLIENT so this file does not need pc_net_game.h's
     * PCNetGameRole enum visible here -- same locally-declared-extern idiom as
     * pcfa_save_ready()/pc_save_write_authoritative() just below, matching this file's existing
     * convention of not #including PC-only headers to stay close to the original decomp source. */
    {
        extern int pcfa_save_ready(void);
        extern int pc_save_write_authoritative(void);
        extern int pc_net_game_role_is_client(void);
        extern void pc_net_game_client_record_quit_flush(unsigned max_ms);
        if (pc_net_game_role_is_client()) {
            /* D3: best-effort, bounded (1500 ms) upload of the client's dirty resident record to the HOST (which owns and
             * persists it). This writes NOTHING locally: the no-save-for-clients rule below is unchanged. */
            pc_net_game_client_record_quit_flush(1500u);
            OSReport("[PC] mainproc: final shutdown save SKIPPED (this process is a network "
                     "CLIENT; only the HOST may write the authoritative town)\n");
        } else if (pcfa_save_ready()) {
            OSReport("[PC] mainproc: final authoritative save before shutdown...\n");
            if (!pc_save_write_authoritative()) {
                OSReport("[PC] mainproc: final shutdown save FAILED\n");
            } else {
                OSReport("[PC] mainproc: final shutdown save OK\n");
            }
        }
    }
    {
        extern void pc_platform_shutdown(void);
        extern void pc_net_game_shutdown(void); /* no-op if networking was never started (single-player) */
        pc_net_game_shutdown();
#ifdef PC_LOW_ADDRESS_64
        extern int pc_lowaddr_report(void); /* final post-gameplay category/reserved-range summary --
             * this exit(0) is the game's only real quit path, so pc_main.c's own trailing
             * pc_lowaddr_report() call (after boot_main() returns) is unreachable code; call it here
             * instead so the full-session summary (JKR heap, ARAM, emu64, actors, jaudio, ...) is not
             * silently skipped every normal run. */
        pc_lowaddr_report();
#endif
        pc_platform_shutdown();
        exit(0);
    }
#else
    do {
        msg = NULL;
        while (irqMgrMsgQueue.usedCount != 0) {
            osRecvMesg(&irqMgrMsgQueue, NULL, 0);
        }

        osRecvMesg(&irqMgrMsgQueue, &msg, 1);
    } while (msg != NULL);
#endif
}

u32 entry(void) {
#ifdef TARGET_PC
    OSReport("[PC] entry() called\n");
#endif
    padmgr_Init(NULL);
    new_Debug_mode();

    SETREG(SREG, 0, 0);
#ifdef TARGET_PC
    OSReport("[PC] entry: calling mainproc...\n");
#endif
    mainproc(NULL);

    return 0;
}

void main(void) {
    OSReport("どうぶつの森 main2 開始\n");
    HotStartEntry = &entry;
}
