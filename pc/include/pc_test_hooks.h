#ifndef PC_TEST_HOOKS_H
#define PC_TEST_HOOKS_H

/* Test-hook guard (guest -> resident lifecycle hardening). The hooks that inject state (--house-buy-test / --nook-test raise the LOCAL wallet, --house-test-host-edit,
 * --mail-test-*, --txn-fault, --promote-fault, --d3-test-wallet-add*, AC_RELAUNCH_DRYRUN, AC_TOWN_NO_MSGBOX) exist only in a build configured with -DPC_TEST_HOOKS=ON
 * (compile definition PC_NET_TEST_HOOKS; the default for the development build, OFF for release builds) AND are active only with the runtime env AC_TEST_HOOKS=1.
 * A hook FLAG without the env is refused (stderr message, exit 2); a hook ENV switch without it is ignored with a stderr note. */
#ifdef __cplusplus
extern "C" {
#endif
int pc_test_hooks_enabled(void);                 /* 1 only in a PC_NET_TEST_HOOKS build with AC_TEST_HOOKS=1 */
const char* pc_test_hook_getenv(const char* name); /* getenv(name) when enabled, else NULL (a note on stderr when the variable was set but ignored) */
#ifdef __cplusplus
}
#endif

#endif
