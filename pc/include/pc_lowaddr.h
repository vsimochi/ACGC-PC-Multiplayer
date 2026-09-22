/* pc_lowaddr.h - startup/runtime validation for the experimental 64-bit "low address space" build.
 *
 * The decomp stores host pointers in u32 (Gfx words, JKR, jaudio, actor labels ...). A 64-bit
 * process only stays correct if every such pointer is below 4 GB. CMake (-DPC_ALLOW_64BIT=ON)
 * links with --image-base=0x400000 --disable-dynamicbase --disable-high-entropy-va and defines
 * PC_LOW_ADDRESS_64; this header then turns the "below 4 GB" assumption into checked assertions.
 * Without PC_LOW_ADDRESS_64 every macro compiles to nothing / the plain cast.
 *
 * Violations are never truncated silently: they are printed (stderr, OutputDebugString and
 * lowaddr.log in the working directory) with the category and the offending address, then the
 * process aborts unless PC_LOWADDR_NONFATAL=1 is set in the environment (survey mode).
 */
#ifndef PC_LOWADDR_H
#define PC_LOWADDR_H

#include <stddef.h>
#include <stdint.h>

#define PC_LOWADDR_LIMIT 0x100000000ull

#ifdef __cplusplus
extern "C" {
#endif

#ifdef PC_LOW_ADDRESS_64

/* Record + validate [ptr, ptr+size). Use for one-time allocations (arenas, heaps, buffers). */
void pc_lowaddr_check(const char* category, const void* ptr, size_t size, const char* file, int line);
/* Slow path for hot-path asserts / checked u32 conversions. Does not return unless non-fatal. */
void pc_lowaddr_violation(const char* category, uint64_t addr, size_t size, const char* file, int line);
/* Checks executable image, stack, process heap, malloc probes (1 B, 64 KB, 24 MB) and thread stack. */
void pc_lowaddr_init(void);
/* printf-style log to stderr, OutputDebugString and lowaddr.log (stdout/stderr are NUL unless --verbose). */
void pc_lowaddr_log(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
/* `--lowaddr-selftest`: runs the real JKRExpHeap / __osMalloc allocators and checks layout + addresses.
 * Returns the number of failures. Implemented in pc_lowaddr_selftest.cpp. */
int pc_lowaddr_selftest(void);
/* Prints a per-category table (count, lowest, highest end address, violations). Returns #violations.
 * Also cross-checks every registered reserved range (pc_lowaddr_reserve_range) against every tracked
 * allocation category and reports a violation if any overlap (see pc_gbi_runtime.c's odd-pointer token
 * table for why this matters: a fixed sentinel range is only safe as long as no real allocation ever
 * lands inside it). */
int pc_lowaddr_report(void);
int pc_lowaddr_violation_count(void);
/* Register a fixed [lo, hi) address range that is reserved for a non-pointer sentinel purpose (e.g. a
 * synthetic token) and must never overlap a real tracked allocation. Checked by pc_lowaddr_report(). */
void pc_lowaddr_reserve_range(const char* name, uint64_t lo, uint64_t hi);

#define PC_LOWADDR_CHECK(cat, p, sz) pc_lowaddr_check((cat), (p), (size_t)(sz), __FILE__, __LINE__)
/* Hot-path form: only tests, no bookkeeping. */
#define PC_LOWADDR_ASSERT(cat, p)                                                                      \
    do {                                                                                               \
        if ((uint64_t)(uintptr_t)(p) >= PC_LOWADDR_LIMIT)                                              \
            pc_lowaddr_violation((cat), (uint64_t)(uintptr_t)(p), 0, __FILE__, __LINE__);              \
    } while (0)
/* Checked pointer -> 32-bit conversion (replaces a silent (u32)(uintptr_t)p). */
static inline uint32_t pc_lowaddr_ptr32_(const char* cat, const void* p, const char* file, int line) {
    if ((uint64_t)(uintptr_t)p >= PC_LOWADDR_LIMIT)
        pc_lowaddr_violation(cat, (uint64_t)(uintptr_t)p, 0, file, line);
    return (uint32_t)(uintptr_t)p;
}
#define PC_PTR32(cat, p) pc_lowaddr_ptr32_((cat), (const void*)(p), __FILE__, __LINE__)

#else /* 32-bit build: everything is a no-op / the original cast */

#define pc_lowaddr_init() ((void)0)
#define pc_lowaddr_report() (0)
#define pc_lowaddr_violation_count() (0)
#define pc_lowaddr_reserve_range(name, lo, hi) ((void)0)
#define PC_LOWADDR_CHECK(cat, p, sz) ((void)0)
#define PC_LOWADDR_ASSERT(cat, p) ((void)0)
#define PC_PTR32(cat, p) ((uint32_t)(uintptr_t)(p))

#endif

#ifdef __cplusplus
}
#endif

#endif /* PC_LOWADDR_H */
