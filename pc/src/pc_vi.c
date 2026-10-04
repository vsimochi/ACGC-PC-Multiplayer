/* pc_vi.c - video interface → SDL window swap + frame pacing */
#include "pc_platform.h"
#include "pc_profiler.h"
#include "pc_net_game.h"
#include "pc_remote_player.h"
#include "pc_dedicated.h"

#define VI_TVMODE_NTSC_INT    0
#define VI_TVMODE_NTSC_DS     1
#define VI_TVMODE_PAL_INT     4
#define VI_TVMODE_MPAL_INT    8
#define VI_TVMODE_EURGB60_INT 20

/* Stage 0.5D: periodic authoritative-town save interval (see the periodic-save block in
 * VIWaitForRetrace() below). No existing save-interval convention was found anywhere in the repo
 * to reuse (the Stage 0.5 persistence audit confirmed no autosave/periodic-save mechanism existed
 * before this stage) -- 60 seconds is a conservative, development-testing value chosen for this
 * initial M1 implementation: frequent enough to bound data loss to a short, easily-observed window
 * during testing, infrequent enough that the synchronous whole-Save_t write (a few ms, measured in
 * the Stage 0.5D report) never has a realistic chance of coinciding with another one. Named
 * constant, not a magic number, matching this codebase's own PCNET_HEARTBEAT_INTERVAL_MS-style
 * naming (pc_net.c). Not user-configurable yet -- out of scope for this stage. */
#define PC_SAVE_INTERVAL_MS 60000u
/* D3-4 (Q5): minimum gap between two early host saves (a dirty client disconnect asks for an immediate save of the merged
 * resident record; bursts of disconnects coalesce into one save per gap). */
#define PC_EARLY_SAVE_MIN_GAP_MS 5000u

static u32 retrace_count = 0;
u32 pc_frame_counter = 0;
static Uint64 frame_start_time = 0;
static Uint64 perf_freq = 0;

// 16667us = 60.0 Hz (NTSC).
u32 g_frame_limiter = 60; // 60Hz
static void (*vi_pre_callback)(u32) = NULL;
static void (*vi_post_callback)(u32) = NULL;

void VIInit(void) {
    if (g_frame_limiter > 0) {
        printf("[VI] frame limit=%luus (%lu Hz)\n",
               (u32)((1.0 / (double)g_frame_limiter) * 1000000), (unsigned long)g_frame_limiter);
    } else {
        printf("[VI] frame limit=disabled\n");
    }
}

void VIConfigure(void* rm) { (void)rm; }

void VISetNextFrameBuffer(void* fb) { (void)fb; }

void VIFlush(void) {}

void VIWaitForRetrace(void) {
    if (!perf_freq) perf_freq = SDL_GetPerformanceFrequency();

    /* --- frame time diagnostic --- */
    Uint64 vi_enter = SDL_GetPerformanceCounter();
    double frame_ms = 0.0;
    if (frame_start_time) {
        frame_ms = (double)(vi_enter - frame_start_time) * 1000.0 / (double)perf_freq;
    }

    Uint64 t_before_poll = pc_profiler_begin_timer();
    if (!pc_platform_poll_events()) {
        g_pc_running = 0;
        return;
    }
    pc_profiler_add_time(PC_PROF_TIMER_POLL_EVENTS, t_before_poll);

    /* Stage 1: once-per-frame, non-blocking. No-op if networking was never started. Placed here
     * (a pure PC-layer function, called unconditionally every frame regardless of game state --
     * menus, loading, gameplay) rather than in any decomp game-loop file. */
    pc_net_game_poll();

    /* --dedicated only: drain the stdin command queue and run the console commands (help/status/players/save/stop) on THIS thread, right next to the
     * network poll. A console `save` only sets a flag that the save block below consumes, so every save gate applies. */
    if (g_pc_dedicated) {
        pc_dedicated_console_poll();
    }

    /* Stage 2: retries deferred remote-player actor creation once gamePT/the local player actor
     * are valid. Also unconditional every frame; a no-op whenever nothing is pending. */
    pc_remote_player_poll();

    /* Stage 0: --bootstrap-resident N (see pc_main.c/pc_m_card.c). Unconditional every frame,
     * matching pc_net_game_poll()'s own placement; a no-op unless the flag was passed, and fires
     * at most once per process (pc_bootstrap_resident_poll() tracks its own one-shot state). */
    {
        extern void pc_bootstrap_resident_poll(void);
        extern void pc_bootstrap_guest_poll(void); /* guests G2: --bootstrap-guest (TEST-ONLY, CLIENT only, default off; one-shot like the resident poll) */
        pc_bootstrap_resident_poll();
        pc_bootstrap_guest_poll();
    }

    /* --host-observer (HOST role + flag only; a no-op otherwise): the hidden server observer's one-shot bootstrap and readiness latch. */
    {
        extern void pc_host_observer_poll(void);
        pc_host_observer_poll();
    }

    /* Stage 0.5D: periodic authoritative-town save. Fires only while BOTH:
     *   - this process is the host (pc_net_game_role() == PC_NETGAME_ROLE_HOST) -- an M1 hardening
     *     correction: the original Stage 0.5D condition below gated on readiness alone, which meant
     *     an ordinary single-player launch (no --host/--connect at all) silently picked up this same
     *     background autosave the moment a resident was in town, even though M1 is specifically
     *     about the hosted-server's persistent town, not vanilla single-player. Deliberately a ROLE
     *     check, not a peer-count/ready-peer-count/human-presence check: a host with zero connected
     *     clients must keep saving exactly as before -- this only excludes PC_NETGAME_ROLE_NONE
     *     (plain single-player) and PC_NETGAME_ROLE_CLIENT (a client never owns the authoritative
     *     Save_t it's rendering -- the host is the only correct writer of it).
     *   - the field-authority readiness predicate (pcfa_save_ready(), pc_field_authority.h -- the
     *     same one every world mutation pcfa_set_tile()/pcfa_set_deposit() already gates on: a bound
     *     resident, a valid land id, and a live town scene) is true -- independent of network peer
     *     *count*, player input, NPC dialogue, UI, or scene-transition state, so this behaves
     *     identically with zero clients connected as with several, for as long as this process is
     *     hosting. Reuses the existing public accessors as-is; no change to pc_field_authority.c or
     *     pc_net_game.c, and no second readiness system invented.
     *
     * Timing reuses this file's own existing wall-clock source (SDL_GetPerformanceCounter()/
     * perf_freq, already computed every frame above for frame-time diagnostics) rather than
     * pc_frame_counter, since the frame counter's real-time rate varies with g_frame_limiter (an
     * uncapped/--no-framelimit server would otherwise save far more often than intended). No new
     * thread, timer, or async I/O: a single synchronous comparison, plus (at most once per
     * PC_SAVE_INTERVAL_MS) one synchronous call into the existing writer, on this same frame-loop
     * thread, right alongside pc_net_game_poll()/pc_bootstrap_resident_poll() above.
     *
     * Writes unconditionally on each elapsed interval rather than only-if-changed: the existing
     * field-authority dirty-acre mask exists solely for network-delta encoding (Stage 0.5
     * persistence audit, confirmed no general-purpose "needs saving" indicator exists anywhere in
     * the repo); building one would be exactly the dirty-state subsystem this stage is not meant
     * to add. Re-serializing an unchanged Save_t every interval is the simplest correct behavior --
     * harmless, since the writer already performs a full atomic write regardless of whether
     * contents actually differ.
     *
     * Failure handling: the wrapper's return value is checked and logged; a failed save is neither
     * retried nor treated as fatal -- the next interval simply tries again on its own schedule. */
    {
        extern int pcfa_save_ready(void);
        extern int pc_save_write_authoritative(void);
        static Uint64 l_last_save_time = 0;
        static Uint64 l_last_early_save_time = 0; /* D3-4 Q5: start of the last early (dirty-disconnect) save attempt */

        if (pc_net_game_role() == PC_NETGAME_ROLE_HOST && pcfa_save_ready() && l_last_save_time != 0 &&
            pc_net_game_record_early_save_due() &&
            (l_last_early_save_time == 0 ||
             (double)(vi_enter - l_last_early_save_time) * 1000.0 / (double)perf_freq >= (double)PC_EARLY_SAVE_MIN_GAP_MS)) {
            /* D3-4 (Q5): a peer with unsaved accepted record uploads disconnected: save now (same gates as the periodic save:
             * HOST role + pcfa_save_ready, plus the host world being ready) instead of waiting up to PC_SAVE_INTERVAL_MS, but
             * at most once per PC_EARLY_SAVE_MIN_GAP_MS (coalesced). Main thread, same call as the periodic save; a failed
             * save keeps the request pending and is retried after the gap. */
            Uint64 t_save_begin = SDL_GetPerformanceCounter();
            int save_ok;
            l_last_early_save_time = vi_enter;
            save_ok = pc_save_write_authoritative();
            printf("[PC] early save (dirty client disconnect) %s (%.1fms, frame %lu)\n", save_ok ? "OK" : "FAILED",
                   (double)(SDL_GetPerformanceCounter() - t_save_begin) * 1000.0 / (double)perf_freq,
                   (unsigned long)pc_frame_counter);
            l_last_save_time = vi_enter; /* the periodic cadence restarts from this save */
        } else if (pc_net_game_role() == PC_NETGAME_ROLE_HOST && pcfa_save_ready()) {
            if (l_last_save_time == 0) {
                /* World just became ready (or this is the first ready frame) -- wait one full
                 * interval before the first periodic save rather than saving immediately. */
                l_last_save_time = vi_enter;
            } else {
                double since_last_ms = (double)(vi_enter - l_last_save_time) * 1000.0 / (double)perf_freq;
                if (since_last_ms >= (double)PC_SAVE_INTERVAL_MS) {
                    Uint64 t_save_begin = SDL_GetPerformanceCounter();
                    int save_ok = pc_save_write_authoritative();
                    double save_ms =
                        (double)(SDL_GetPerformanceCounter() - t_save_begin) * 1000.0 / (double)perf_freq;
                    if (!save_ok) {
                        printf("[PC] periodic save FAILED after %.1fms (frame %lu)\n", save_ms,
                               (unsigned long)pc_frame_counter);
                    } else {
                        printf("[PC] periodic save OK (%.1fms, frame %lu)\n", save_ms,
                               (unsigned long)pc_frame_counter);
                    }
                    l_last_save_time = vi_enter;
                }
            }
        } else {
            /* Not ready (startup, scene transition, no bound resident, etc.) -- reset so the
             * "wait one interval" grace period re-applies whenever the world next becomes ready. */
            l_last_save_time = 0;
        }

        /* --dedicated only: a console `save` request. Handled HERE (main thread, same function and gates as the periodic/early saves: HOST role, host
         * world ready, pcfa_save_ready; the sidecar hooks (records.dat / guests.dat) run inside pc_save_write_authoritative exactly as for any other
         * save). The outcome is reported only after the call returned (or the gate refused); never claimed early. */
        if (pc_dedicated_save_request_pending()) {
            if (pc_net_game_role() != PC_NETGAME_ROLE_HOST) {
                pc_dedicated_save_report(0, "not hosting");
            } else if (!pc_net_game_dedicated_world_ready()) {
                pc_dedicated_save_report(0, "world not ready");
            } else if (!pcfa_save_ready()) {
                pc_dedicated_save_report(0, "save not ready (no live town scene yet)");
            } else {
                int console_save_ok = pc_save_write_authoritative();
                l_last_save_time = vi_enter; /* the periodic cadence restarts from this save */
                pc_dedicated_save_report(console_save_ok, NULL);
            }
        }
    }

    /* Drain the frame's last deferred batch here so its cost bills to
     * gx_flush instead of inflating the swap timer. */
    {
        extern void pc_gx_draw_pending(void);
        Uint64 t_drain = pc_profiler_begin_timer();
        if (!g_pc_dedicated) pc_gx_draw_pending(); /* --dedicated: nothing was drawn */
        pc_profiler_add_time(PC_PROF_TIMER_GX_FLUSH, t_drain);
    }

    Uint64 t_before_swap = SDL_GetPerformanceCounter();
    Uint64 t_before_swap_prof = pc_profiler_begin_timer();
    if (!g_pc_dedicated) pc_platform_swap_buffers(); /* --dedicated: no buffer swap (hidden window, nothing drawn); pacing below is unchanged */
    pc_profiler_add_time(PC_PROF_TIMER_SWAP, t_before_swap_prof);
    Uint64 t_after_swap = SDL_GetPerformanceCounter();

    Uint64 t_before_pace = SDL_GetPerformanceCounter();
    Uint64 t_before_pace_prof = pc_profiler_begin_timer();
    {
        extern int g_pc_nes_active;
        int pace_frame = g_pc_nes_active || g_frame_limiter > 0;
        int pace_us = 0;

        if (g_pc_nes_active) {
            pace_us = 16667;
        } else if (g_frame_limiter > 0) {
            pace_us = (int)((1.0 / (double)g_frame_limiter) * 1000000);
        }

        if (pace_frame) {
            /* Timer-based pacing: sleep until 16ms per frame (~60 FPS).
             * Audio production runs on a dedicated thread and is no longer
             * tied to game frame timing. */
            if (frame_start_time) {
                Uint64 now = SDL_GetPerformanceCounter();
                Uint64 elapsed_us = (now - frame_start_time) * 1000000 / perf_freq;
                /* Spin for sub-ms precision. */
                while (elapsed_us < (Uint64)pace_us) {
                    Uint64 remain_us = (Uint64)pace_us - elapsed_us;
                    if (remain_us > (g_pc_dedicated ? 1000u : 2000u)) { /* --dedicated: spin only the last 1 ms (a server has no presentation to align with; idle CPU) */
                        SDL_Delay(1);
                    }
                    now = SDL_GetPerformanceCounter();
                    elapsed_us = (now - frame_start_time) * 1000000 / perf_freq;
                }
            }
        }
    }
    pc_profiler_add_time(PC_PROF_TIMER_PACE, t_before_pace_prof);
    Uint64 t_after_pace = SDL_GetPerformanceCounter();
    double profile_frame_ms = frame_ms;
    if (frame_start_time) {
        profile_frame_ms = (double)(t_after_pace - frame_start_time) * 1000.0 / (double)perf_freq;
    }

    /* report slow frames (>20ms = missed 60fps by >4ms) */
    if (frame_ms > 20.0 && g_pc_verbose) {
        double swap_ms = (double)(t_after_swap - t_before_swap) * 1000.0 / (double)perf_freq;
        double pace_ms = (double)(t_after_pace - t_before_pace) * 1000.0 / (double)perf_freq;
        double work_ms = (double)(vi_enter - frame_start_time) * 1000.0 / (double)perf_freq;
        int audio_fill = pc_audio_get_buffer_fill();
        printf("[STUTTER] frame %lu: total=%.1fms work=%.1fms swap=%.1fms pace=%.1fms audio_fill=%d\n",
               (unsigned long)pc_frame_counter, frame_ms, work_ms - swap_ms - pace_ms, swap_ms, pace_ms, audio_fill);
    }

    pc_profiler_end_frame(profile_frame_ms, pc_audio_get_buffer_fill());

    {
        static Uint64 fps_start = 0;
        static int fps_count = 0;
        if (fps_start == 0) fps_start = SDL_GetPerformanceCounter();
        fps_count++;
        if (fps_count >= 60) {
            Uint64 now = SDL_GetPerformanceCounter();
            double secs = (double)(now - fps_start) / (double)perf_freq;
            double fps = (double)fps_count / secs;
            char title[64];
            snprintf(title, sizeof(title), "Animal Crossing - %.1f FPS", fps);
            if (!g_pc_dedicated) SDL_SetWindowTitle(g_pc_window, title); /* --dedicated: hidden window, no title updates */
            fps_start = now;
            fps_count = 0;
        }
    }

    frame_start_time = SDL_GetPerformanceCounter();

    retrace_count++;
    pc_frame_counter++;
}

u32 VIGetRetraceCount(void) { return retrace_count; }

void VISetBlack(BOOL black) { (void)black; }

u32 VIGetTvFormat(void) { return 0; /* VI_NTSC */ }
u32 VIGetDTVStatus(void) { return 0; }

void* VISetPreRetraceCallback(void* cb) {
    void* old = (void*)vi_pre_callback;
    vi_pre_callback = (void (*)(u32))cb;
    return old;
}

void* VISetPostRetraceCallback(void* cb) {
    void* old = (void*)vi_post_callback;
    vi_post_callback = (void (*)(u32))cb;
    return old;
}

u32 VIGetCurrentLine(void) { return 0; }

void VISetNextXFB(void* xfb) { (void)xfb; }
