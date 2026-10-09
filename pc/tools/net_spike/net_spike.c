/* net_spike.c - standalone Stage-0 harness for pc_net.c (see pc/include/pc_net.h).
 *
 * NOT part of the normal ac_pc build (matches the project's existing pc/tools/lowaddr_spike/
 * convention: a small throwaway harness built and run by a Python driver, not wired into
 * CMakeLists.txt). Built and driven by run_net_spike.py, which spawns this program as a "host"
 * process and one or more "client" processes and checks the resulting stdout logs for the
 * exact sequence of events Stage 0 must prove (see the header comment in run_net_spike.py).
 *
 * Every event this program observes is logged to stdout with a stable "SPIKE ..." prefix so
 * the driver script can grep for it reliably; nothing here depends on timing beyond the
 * caller-supplied duration.
 *
 * Usage:
 *   net_spike.exe host   <port> <duration_ms>
 *   net_spike.exe client <ip> <port> <duration_ms> <label>
 */
#include "pc_net.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <windows.h>

static void log_line(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout); /* the driver reads this incrementally; never let it sit in a libc buffer */
}

static int run_host(uint16_t port, uint32_t duration_ms) {
    uint32_t start, now;
    int send_counter = 0;
    char msg[64];

    if (!pc_net_init()) { log_line("SPIKE FAIL init\n"); return 1; }
    if (!pc_net_host_start(port)) { log_line("SPIKE FAIL host_start\n"); return 1; }
    log_line("SPIKE HOST_STARTED port=%u\n", (unsigned)port);

    start = GetTickCount();
    for (;;) {
        PCNetEvent ev;
        now = GetTickCount();
        if (now - start > duration_ms) break;

        pc_net_poll(); /* must return immediately every call -- this loop's own wall-clock
                         * duration (checked by the driver) is the proof it never blocks/hangs */

        while (pc_net_next_event(&ev)) {
            if (ev.type == PC_NET_EVENT_PEER_CONNECTED) {
                log_line("SPIKE HOST_PEER_CONNECTED peer=%d\n", (int)ev.peer);
            } else if (ev.type == PC_NET_EVENT_PEER_DISCONNECTED) {
                log_line("SPIKE HOST_PEER_DISCONNECTED peer=%d\n", (int)ev.peer);
            } else if (ev.type == PC_NET_EVENT_DATA) {
                log_line("SPIKE HOST_RECV peer=%d kind=%d size=%u data=%.*s\n", (int)ev.peer, (int)ev.kind,
                         (unsigned)ev.size, (int)ev.size, (const char*)ev.data);
                /* echo a reply so the client side proves round-trip receipt too */
                snprintf(msg, sizeof(msg), "host_reply_%d", send_counter++);
                if (pc_net_send(ev.peer, PC_NET_UNRELIABLE, msg, (uint16_t)(strlen(msg) + 1))) {
                    log_line("SPIKE HOST_SEND peer=%d data=%s\n", (int)ev.peer, msg);
                }
            }
        }

        Sleep(10); /* stand-in for a frame tick; pc_net_poll() itself never sleeps internally */
    }

    log_line("SPIKE HOST_DONE peers_remaining=%d\n", pc_net_peer_count());
    pc_net_shutdown();
    return 0;
}

static int run_client(const char* ip, uint16_t port, uint32_t duration_ms, const char* label) {
    uint32_t start, now;
    int send_counter = 0;
    int have_connected = 0;
    char msg[64];

    if (!pc_net_init()) { log_line("SPIKE FAIL init\n"); return 1; }
    if (!pc_net_client_connect(ip, port)) { log_line("SPIKE FAIL client_connect\n"); return 1; }
    log_line("SPIKE CLIENT_STARTED label=%s target=%s:%u\n", label, ip, (unsigned)port);

    start = GetTickCount();
    for (;;) {
        PCNetEvent ev;
        now = GetTickCount();
        if (now - start > duration_ms) break;

        pc_net_poll();

        while (pc_net_next_event(&ev)) {
            if (ev.type == PC_NET_EVENT_PEER_CONNECTED) {
                log_line("SPIKE CLIENT_CONNECTED label=%s peer=%d\n", label, (int)ev.peer);
                have_connected = 1;
            } else if (ev.type == PC_NET_EVENT_PEER_DISCONNECTED) {
                log_line("SPIKE CLIENT_DISCONNECTED label=%s peer=%d\n", label, (int)ev.peer);
            } else if (ev.type == PC_NET_EVENT_DATA) {
                log_line("SPIKE CLIENT_RECV label=%s kind=%d size=%u data=%.*s\n", label, (int)ev.kind,
                         (unsigned)ev.size, (int)ev.size, (const char*)ev.data);
            }
        }

        /* Send a handful of test packets once connected, spaced out over the run, to exercise
         * "multiple packets exchanged" without flooding. */
        if (have_connected && send_counter < 5 && (now - start) > (uint32_t)(send_counter * 150 + 50)) {
            snprintf(msg, sizeof(msg), "%s_msg_%d", label, send_counter);
            if (pc_net_send(0, PC_NET_UNRELIABLE, msg, (uint16_t)(strlen(msg) + 1))) {
                log_line("SPIKE CLIENT_SEND label=%s data=%s\n", label, msg);
            }
            send_counter++;
        }

        Sleep(10);
    }

    log_line("SPIKE CLIENT_DONE label=%s connected=%d\n", label, pc_net_is_connected());
    pc_net_disconnect(PC_NET_INVALID_PEER); /* explicit goodbye: proves the fast disconnect path, not just timeout */
    pc_net_poll();                          /* one more poll purely to flush the just-queued send */
    pc_net_shutdown();
    return 0;
}

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr,
                "usage: net_spike host <port> <duration_ms>\n"
                "       net_spike client <ip> <port> <duration_ms> <label>\n");
        return 1;
    }
    if (strcmp(argv[1], "host") == 0 && argc >= 4) {
        return run_host((uint16_t)atoi(argv[2]), (uint32_t)strtoul(argv[3], NULL, 10));
    }
    if (strcmp(argv[1], "client") == 0 && argc >= 6) {
        return run_client(argv[2], (uint16_t)atoi(argv[3]), (uint32_t)strtoul(argv[4], NULL, 10), argv[5]);
    }
    fprintf(stderr, "bad arguments\n");
    return 1;
}
