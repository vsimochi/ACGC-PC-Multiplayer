/* pc_relaunch.c - see pc_relaunch.h. */
#include "pc_relaunch.h"
#include "pc_test_hooks.h" /* test-hook guard: AC_RELAUNCH_DRYRUN needs a PC_TEST_HOOKS build + AC_TEST_HOOKS=1 */
#include "pc_servers.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#endif

static int name_ok(const char* name) {
    size_t i, n = name != NULL ? strlen(name) : 0;
    if (n < 1 || n > 32) return 0;
    for (i = 0; i < n; i++) {
        const char c = name[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '-')) return 0;
    }
    return 1;
}

/* M-C: the whitelisted display / diagnostic options the title process was started with (forwarded to the client process). A FIXED set, a number only for --framelimit. */
static char s_forward[96];

void pc_relaunch_forward_capture(int argc, char** argv) {
    int i;
    size_t off = 0;
    s_forward[0] = '\0';
    for (i = 1; argv != NULL && i < argc; i++) {
        const char* a = argv[i];
        int n = 0;
        if (a == NULL) continue;
        if (strcmp(a, "--verbose") == 0 || strcmp(a, "-v") == 0 || strcmp(a, "--no-framelimit") == 0 || strcmp(a, "--uber-shader") == 0) {
            n = snprintf(s_forward + off, sizeof(s_forward) - off, " %s", a);
        } else if (strcmp(a, "--framelimit") == 0 && i + 1 < argc && argv[i + 1] != NULL) {
            const char* v = argv[i + 1];
            size_t k, vl = strlen(v);
            int digits = vl >= 1 && vl <= 4;
            for (k = 0; digits && k < vl; k++) {
                if (v[k] < '0' || v[k] > '9') digits = 0;
            }
            if (digits) {
                n = snprintf(s_forward + off, sizeof(s_forward) - off, " --framelimit %s", v);
                i++;
            }
        }
        if (n < 0 || (size_t)n >= sizeof(s_forward) - off) {
            s_forward[off] = '\0'; /* does not fit: drop the rest (never a half option) */
            break;
        }
        off += (size_t)n;
    }
}

const char* pc_relaunch_forwarded(void) {
    return s_forward;
}

int pc_relaunch_build_args(const char* host, int port, int kind, const char* name, char* out, size_t cap) {
    int n;
    if (host == NULL || !pc_servers_address_check(host, NULL, 0) || !pc_servers_port_check(port)) return 0;
    /* M-C: every Play Online client fetches the host's town first (--town-fetch) and uses the interactive failure boxes (--online-ui, hidden) */
    if (kind == PC_RELAUNCH_DEFAULT_GUEST) {
        n = snprintf(out, cap, "--connect %s:%d --guest --town-fetch --online-ui%s", host, port, s_forward);
    } else if (kind == PC_RELAUNCH_CHARACTER || kind == PC_RELAUNCH_PROFILE) {
        if (!name_ok(name)) return 0;
        n = snprintf(out, cap, "--connect %s:%d %s \"%s\" --town-fetch --online-ui%s", host, port, kind == PC_RELAUNCH_CHARACTER ? "--character" : "--guest-profile", name, s_forward);
    } else {
        return 0;
    }
    return n > 0 && (size_t)n < cap;
}

int pc_relaunch_connect(const char* host, int port, int kind, const char* name, char* err, size_t errcap) {
    char args[320];
    if (!pc_relaunch_build_args(host, port, kind, name, args, sizeof(args))) {
        snprintf(err, errcap, "cannot build the connect command line");
        return 0;
    }
    if (pc_test_hook_getenv("AC_RELAUNCH_DRYRUN") != NULL) { /* M-I test hook: log the exact command line, start nothing (the caller must not quit) */
        printf("[PC] RELAUNCH DRYRUN: <this executable> %s\n", args);
        fflush(stdout);
        snprintf(err, errcap, "dry run");
        return 0;
    }
#ifdef _WIN32
    {
        char exe[MAX_PATH];
        char cmd[MAX_PATH + 360];
        STARTUPINFOA si;
        PROCESS_INFORMATION pi;
        const DWORD n = GetModuleFileNameA(NULL, exe, (DWORD)sizeof(exe));
        if (n == 0 || n >= sizeof(exe)) {
            snprintf(err, errcap, "cannot determine the executable path");
            return 0;
        }
        snprintf(cmd, sizeof(cmd), "\"%s\" %s", exe, args);
        memset(&si, 0, sizeof(si));
        si.cb = sizeof(si);
        memset(&pi, 0, sizeof(pi));
        /* lpCurrentDirectory NULL = the same working directory (the save dir is relative to it) */
        if (!CreateProcessA(exe, cmd, NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi)) {
            snprintf(err, errcap, "CreateProcess failed (error %lu)", (unsigned long)GetLastError());
            return 0;
        }
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        return 1;
    }
#else
    printf("[PC] Play Online: would relaunch: <this executable> %s (relaunch is only implemented on Windows)\n", args);
    snprintf(err, errcap, "relaunch is only implemented on Windows; run: AnimalCrossing %s", args);
    return 0;
#endif
}
