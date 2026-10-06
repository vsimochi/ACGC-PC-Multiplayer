/* pc_relaunch.h - M4: the first "Play Online" cut started the network client by RELAUNCHING this executable with the right CLI; Play Online now connects in-process (pc_main.c
 * pc_main_play_online_poll) and this module remains for the promoted-guest relaunch (pc_main_relaunch_poll) and for validating the connect arguments (pc_relaunch_build_args). Windows: CreateProcess(exe from GetModuleFileName, same working directory),
 * the caller then exits the title process. Other platforms: a stub that prints the command and returns 0 (nothing is started, the caller must NOT exit). No game
 * headers here (windows.h stays out of the game translation units). */
#ifndef PC_RELAUNCH_H
#define PC_RELAUNCH_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
    PC_RELAUNCH_CHARACTER = 0, /* --character NAME|UUID   (store character, or a NEW name -> first-run creation) */
    PC_RELAUNCH_PROFILE = 1,   /* --guest-profile NAME    (legacy-only guest profile) */
    PC_RELAUNCH_DEFAULT_GUEST = 2 /* --guest               (the legacy default guest.ini; name ignored) */
};

/* M-C: remembers the whitelisted options of the title process (--verbose / -v, --no-framelimit, --framelimit N (digits only), --uber-shader) so the relaunched client
 * process gets them too. Call once from main(); everything else (including --fullscreen: it is a settings.ini value, read again by the new process) is not forwarded. */
void pc_relaunch_forward_capture(int argc, char** argv);
const char* pc_relaunch_forwarded(void); /* " --verbose ..." (leading space) or "" */

/* Builds the argument tail (without the exe), e.g. `--connect 192.168.1.5:7777 --character "ab12..." --town-fetch --online-ui [forwarded]`. `host` must be an IPv4 literal or a valid hostname, `port` 1..65535 and `name`
 * only [A-Za-z0-9-] (1..32) so no quoting / injection is possible. 1 = ok, 0 = refused. */
int pc_relaunch_build_args(const char* host, int port, int kind, const char* name, char* out, size_t cap);

/* Windows: starts the new process, 1 = started (the caller should now quit the current process). Else 0 with err. Non-Windows: prints the command, returns 0. */
int pc_relaunch_connect(const char* host, int port, int kind, const char* name, char* err, size_t errcap);

#ifdef __cplusplus
}
#endif
#endif
