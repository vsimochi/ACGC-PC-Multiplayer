/* pc_lowaddr.c - "everything below 4 GB" validation for the experimental 64-bit build (see pc_lowaddr.h) */
#ifdef PC_LOW_ADDRESS_64

#include "pc_lowaddr.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif

#define MAX_CATEGORIES 64

typedef struct {
    const char* name;
    unsigned count;
    unsigned violations;
    uint64_t min_addr;
    uint64_t max_end; /* highest one-past-the-end address seen */
} LowAddrCat;

static LowAddrCat s_cats[MAX_CATEGORIES];
static int s_ncats = 0;
static int s_violations = 0;
static FILE* s_log = NULL;

#define MAX_RESERVED_RANGES 8
typedef struct { const char* name; uint64_t lo, hi; } ReservedRange;
static ReservedRange s_reserved[MAX_RESERVED_RANGES];
static int s_nreserved = 0;

void pc_lowaddr_reserve_range(const char* name, uint64_t lo, uint64_t hi) {
    if (s_nreserved >= MAX_RESERVED_RANGES) return;
    s_reserved[s_nreserved].name = name;
    s_reserved[s_nreserved].lo = lo;
    s_reserved[s_nreserved].hi = hi;
    s_nreserved++;
}

static void la_log(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
static void la_log(const char* fmt, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    /* stdout/stderr are redirected to NUL in non-verbose runs, so also keep a file + debugger channel. */
    fputs(buf, stderr);
    if (!s_log) s_log = fopen("lowaddr.log", "w");
    if (s_log) { fputs(buf, s_log); fflush(s_log); }
#ifdef _WIN32
    OutputDebugStringA(buf);
#endif
}

void pc_lowaddr_log(const char* fmt, ...) {
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    la_log("%s", buf);
}

static LowAddrCat* la_cat(const char* name) {
    for (int i = 0; i < s_ncats; i++) {
        if (s_cats[i].name == name || strcmp(s_cats[i].name, name) == 0) return &s_cats[i];
    }
    if (s_ncats >= MAX_CATEGORIES) return &s_cats[MAX_CATEGORIES - 1];
    LowAddrCat* c = &s_cats[s_ncats++];
    c->name = name;
    c->min_addr = ~0ull;
    return c;
}

void pc_lowaddr_violation(const char* category, uint64_t addr, size_t size, const char* file, int line) {
    LowAddrCat* c = la_cat(category);
    c->violations++;
    s_violations++;
    la_log("[LOWADDR] VIOLATION category=\"%s\" addr=0x%016llx size=0x%llx (>= 4 GB) at %s:%d\n", category,
           (unsigned long long)addr, (unsigned long long)size, file, line);
    const char* nf = getenv("PC_LOWADDR_NONFATAL");
    if (!nf || nf[0] != '1') {
        la_log("[LOWADDR] aborting: a game pointer above 4 GB would be truncated by (u32) casts. "
               "Set PC_LOWADDR_NONFATAL=1 to keep going (survey mode).\n");
        abort();
    }
}

void pc_lowaddr_check(const char* category, const void* ptr, size_t size, const char* file, int line) {
    uint64_t a = (uint64_t)(uintptr_t)ptr;
    uint64_t end = a + (size ? size : 1);
    LowAddrCat* c = la_cat(category);
    c->count++;
    if (a < c->min_addr) c->min_addr = a;
    if (end > c->max_end) c->max_end = end;
    if (end > PC_LOWADDR_LIMIT) pc_lowaddr_violation(category, a, size, file, line);
}

int pc_lowaddr_violation_count(void) { return s_violations; }

/* A category with count==0 has min_addr==~0ull (never touched by la_cat's initializer) and must not be
 * treated as occupying [0, max_end) below. */
static int cat_range_overlaps(const LowAddrCat* c, uint64_t lo, uint64_t hi) {
    if (c->count == 0) return 0;
    return c->min_addr < hi && lo < c->max_end;
}

/* Only "executable image" addresses are ever stored *untagged* in a place a reserved sentinel range could
 * be confused with (see pc_gbi_runtime.c: every dynamic/heap pointer embedded in a live GBI command word
 * is unconditionally tagged -- bit 0 forced -- before storage, and the tag survives exactly so a token
 * lookup can never misfire on it; only compile-time-static pointers, which are never tagged, are exposed
 * to a raw magnitude collision). A dynamic category (heap, arena, malloc probe, ...) overlapping a
 * reserved range is expected and harmless, so it is logged for visibility only, never counted as a
 * violation; the executable image is the one category where an overlap would be a genuine, unaddressed
 * risk, so that one is fatal. */
static void check_reserved_ranges(void) {
    for (int r = 0; r < s_nreserved; r++) {
        for (int i = 0; i < s_ncats; i++) {
            if (!cat_range_overlaps(&s_cats[i], s_reserved[r].lo, s_reserved[r].hi)) continue;
            int is_image = strcmp(s_cats[i].name, "executable image") == 0;
            if (is_image) {
                s_cats[i].violations++;
                s_violations++;
            }
            la_log("[LOWADDR] %s reserved range \"%s\" [0x%llx,0x%llx) overlaps category \"%s\" [0x%llx,0x%llx)%s\n",
                   is_image ? "VIOLATION" : "note:", s_reserved[r].name,
                   (unsigned long long)s_reserved[r].lo, (unsigned long long)s_reserved[r].hi, s_cats[i].name,
                   (unsigned long long)s_cats[i].min_addr, (unsigned long long)s_cats[i].max_end,
                   is_image ? " -- untagged static pointers here could be misread as a token" :
                              " -- harmless: dynamic pointers are always tagged before storage (see comment above)");
        }
    }
}

int pc_lowaddr_report(void) {
    check_reserved_ranges();
    la_log("[LOWADDR] ---- category summary (limit 0x%llx) ----\n", (unsigned long long)PC_LOWADDR_LIMIT);
    for (int i = 0; i < s_ncats; i++) {
        LowAddrCat* c = &s_cats[i];
        la_log("[LOWADDR] %-28s n=%-6u lowest=0x%09llx highest_end=0x%09llx violations=%u\n", c->name, c->count,
               (unsigned long long)c->min_addr, (unsigned long long)c->max_end, c->violations);
    }
    for (int r = 0; r < s_nreserved; r++) {
        la_log("[LOWADDR] reserved range: %-20s [0x%09llx,0x%09llx)\n", s_reserved[r].name,
               (unsigned long long)s_reserved[r].lo, (unsigned long long)s_reserved[r].hi);
    }
    la_log("[LOWADDR] total violations: %d\n", s_violations);
    return s_violations;
}

#ifdef _WIN32
static DWORD WINAPI la_thread_probe(LPVOID arg) {
    int local = 0;
    (void)arg;
    pc_lowaddr_check("thread stack (CreateThread)", &local, sizeof(local), __FILE__, __LINE__);
    return 0;
}
#endif

void pc_lowaddr_init(void) {
#ifdef _WIN32
    {
        HMODULE exe = GetModuleHandle(NULL);
        IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)exe;
        IMAGE_NT_HEADERS* nt = (IMAGE_NT_HEADERS*)((char*)exe + dos->e_lfanew);
        pc_lowaddr_check("executable image", exe, nt->OptionalHeader.SizeOfImage, __FILE__, __LINE__);
        if (nt->OptionalHeader.DllCharacteristics & IMAGE_DLLCHARACTERISTICS_HIGH_ENTROPY_VA) {
            la_log("[LOWADDR] WARNING: image is marked HIGH_ENTROPY_VA (link with --disable-high-entropy-va)\n");
        }
        if (nt->OptionalHeader.DllCharacteristics & IMAGE_DLLCHARACTERISTICS_DYNAMIC_BASE) {
            la_log("[LOWADDR] note: image is DYNAMIC_BASE (ASLR); still valid while it lands below 4 GB\n");
        }
    }
    {
        HANDLE t = CreateThread(NULL, 0, la_thread_probe, NULL, 0, NULL);
        if (t) { WaitForSingleObject(t, 5000); CloseHandle(t); }
    }
    pc_lowaddr_check("process heap (GetProcessHeap)", GetProcessHeap(), 1, __FILE__, __LINE__);
#endif
    {
        int local = 0;
        pc_lowaddr_check("main thread stack", &local, sizeof(local), __FILE__, __LINE__);
    }
    /* malloc / operator new (libstdc++ new forwards to malloc) at the sizes the game uses */
    {
        static const size_t sizes[] = { 1, 64 * 1024, 24u * 1024 * 1024 };
        for (int i = 0; i < 3; i++) {
            void* p = malloc(sizes[i]);
            if (p) { pc_lowaddr_check("malloc probe", p, sizes[i], __FILE__, __LINE__); free(p); }
        }
    }
    la_log("[LOWADDR] init: 64-bit process, low-address mode active\n");
}

#endif /* PC_LOW_ADDRESS_64 */
