/* pc_relaunch.c - see pc_relaunch.h. */
#include "pc_relaunch.h"
#include "pc_servers.h"

#include <stdio.h>
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

int pc_relaunch_build_args(const char* host, int port, int kind, const char* name, char* out, size_t cap) {
    int n;
    if (host == NULL || !pc_servers_address_check(host, NULL, 0) || !pc_servers_port_check(port)) return 0;
    if (kind == PC_RELAUNCH_DEFAULT_GUEST) {
        n = snprintf(out, cap, "--connect %s:%d --guest", host, port);
    } else if (kind == PC_RELAUNCH_CHARACTER || kind == PC_RELAUNCH_PROFILE) {
        if (!name_ok(name)) return 0;
        n = snprintf(out, cap, "--connect %s:%d %s \"%s\"", host, port, kind == PC_RELAUNCH_CHARACTER ? "--character" : "--guest-profile", name);
    } else {
        return 0;
    }
    return n > 0 && (size_t)n < cap;
}

int pc_relaunch_connect(const char* host, int port, int kind, const char* name, char* err, size_t errcap) {
    char args[200];
    if (!pc_relaunch_build_args(host, port, kind, name, args, sizeof(args))) {
        snprintf(err, errcap, "cannot build the connect command line");
        return 0;
    }
#ifdef _WIN32
    {
        char exe[MAX_PATH];
        char cmd[MAX_PATH + 260];
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
