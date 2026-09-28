/* pc_rng_domains_selftest.h - `AnimalCrossing.exe --rng-selftest`.
 *
 * Regression/behavior test for libc64/qrand_domains.h (independent per-domain RNG streams) and a
 * regression check that the pre-existing global qrand()/sqrand()/fqrand() stream (libc64/qrand.h)
 * is bit-for-bit unchanged by that addition. Runs standalone, with no ROM/save required -- see
 * pc_main.c's dispatch of this flag, right next to --lowaddr-selftest.
 */
#ifndef PC_RNG_DOMAINS_SELFTEST_H
#define PC_RNG_DOMAINS_SELFTEST_H

#ifdef __cplusplus
extern "C" {
#endif

/* Runs every check and prints a PASS/FAIL line for each. Returns the number of failures (0 = all
 * passed). */
int pc_rng_domains_selftest(void);

#ifdef __cplusplus
}
#endif

#endif
