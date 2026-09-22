#include <stdint.h>
#include <stdio.h>

#include "pc_lowaddr.h"

#define PC_GBI_ODD_PTR_TOKEN_BASE 0x02F00000u
#define PC_GBI_ODD_PTR_TOKEN_COUNT 8192u

static uintptr_t s_odd_ptr_tokens[PC_GBI_ODD_PTR_TOKEN_COUNT];
static unsigned int s_odd_ptr_token_next = 0;
static int s_warned_odd_ptr = 0;

/* This fixed range is only safe as long as no real segment address or raw pointer ever numerically lands
 * inside it (seg2k0() checks pc_gbi_unpack_runtime_ptr() before it checks anything else, including the
 * "< 0x03000000 => raw pointer" heuristic -- see 64BIT_MIGRATION_AUDIT.md section 16.3/17 for the full
 * trace). Registering it here lets pc_lowaddr_report() catch a real collision automatically instead of
 * relying on it never being tested. */
__attribute__((constructor)) static void pc_gbi_register_token_range(void) {
    pc_lowaddr_reserve_range("GBI odd-pointer token table",
                              PC_GBI_ODD_PTR_TOKEN_BASE,
                              PC_GBI_ODD_PTR_TOKEN_BASE + (uint64_t)PC_GBI_ODD_PTR_TOKEN_COUNT * 2u);
}

unsigned int pc_gbi_pack_runtime_ptr(uintptr_t addr, int is_ptr, const char* expr, const char* file, int line) {
    unsigned int slot;

    if (!is_ptr) {
        return (unsigned int)addr;
    }

    /* The packed word is only 32 bits: refuse to truncate a real host pointer silently. */
    PC_LOWADDR_ASSERT("GBI runtime display-list pointer", addr);

    if ((addr & 1u) == 0) {
        return (unsigned int)(addr | 1u);
    }

    slot = s_odd_ptr_token_next++ & (PC_GBI_ODD_PTR_TOKEN_COUNT - 1u);
    s_odd_ptr_tokens[slot] = addr;

    if (!s_warned_odd_ptr) {
        fprintf(stderr,
                "[GBI] odd pointer alignment: %s at %s:%d = 0x%08x; using fallback token table\n",
                expr, file, line, (unsigned int)addr);
        s_warned_odd_ptr = 1;
    }

    return PC_GBI_ODD_PTR_TOKEN_BASE + slot * 2u;
}

uintptr_t pc_gbi_unpack_runtime_ptr(unsigned int packed) {
    unsigned int token = packed - PC_GBI_ODD_PTR_TOKEN_BASE;

    if (token < PC_GBI_ODD_PTR_TOKEN_COUNT * 2u && (token & 1u) == 0) {
        return s_odd_ptr_tokens[token / 2u];
    }

    return 0;
}

#ifdef PC_LOW_ADDRESS_64
/* Deterministic self-test for the pack/unpack mechanism and the specific collision this session's audit
 * traced (64BIT_MIGRATION_AUDIT.md section 17): emu64::seg2k0() calls pc_gbi_unpack_runtime_ptr() on every
 * address it resolves, unconditionally, before it applies the bit-0 tag check or the "< 0x03000000 => raw
 * pointer" heuristic. That is only safe because (a) a genuine even/aligned runtime pointer always gets
 * bit 0 set before storage (never reaches unpack() looking like a token), and (b) no real segment address
 * or raw pointer used anywhere in this game's data numerically falls inside the reserved token range
 * (verified by exhaustive source audit: the only segment indices ever used are 7 through 13 via the named
 * anime_N and softsprite constants, plus 8, 9 and 10 via direct SEGMENT_ADDR() calls, with a maximum
 * literal offset of 0x780 bytes across the entire codebase -- see the audit doc for the exact grep). This test exercises
 * the mechanism itself; it does not and cannot prove (b) for all possible future data, which is why
 * pc_gbi_register_token_range() above also registers the range for the runtime pc_lowaddr_report() cross
 * check against every real tracked allocation. Returns the number of failures. */
int pc_gbi_token_selftest(void) {
    int fail = 0;
    unsigned int packed;
    uintptr_t back;

    /* 1) An even/aligned "pointer" must come back with bit 0 set and must NOT land in the token range. */
    packed = pc_gbi_pack_runtime_ptr((uintptr_t)0x12345678u, 1, "even_test", __FILE__, __LINE__);
    if (packed != 0x12345679u) { fprintf(stderr, "[GBI selftest] FAIL: even pack = 0x%08x, expected 0x12345679\n", packed); fail++; }
    if (packed >= PC_GBI_ODD_PTR_TOKEN_BASE && packed < PC_GBI_ODD_PTR_TOKEN_BASE + PC_GBI_ODD_PTR_TOKEN_COUNT * 2u) {
        fprintf(stderr, "[GBI selftest] FAIL: even pack 0x%08x landed inside the token range\n", packed);
        fail++;
    }

    /* 2) A non-pointer expression (is_ptr=0, e.g. a raw SEGMENT_ADDR() integer) passes through untouched. */
    packed = pc_gbi_pack_runtime_ptr((uintptr_t)0x08000000u, 0, "segment_test", __FILE__, __LINE__);
    if (packed != 0x08000000u) { fprintf(stderr, "[GBI selftest] FAIL: non-ptr pack = 0x%08x, expected 0x08000000\n", packed); fail++; }

    /* 3) A deliberately odd "pointer" must fall into the token range and round-trip exactly via unpack(). */
    packed = pc_gbi_pack_runtime_ptr((uintptr_t)0x87654321u, 1, "odd_test", __FILE__, __LINE__);
    if (packed < PC_GBI_ODD_PTR_TOKEN_BASE || packed >= PC_GBI_ODD_PTR_TOKEN_BASE + PC_GBI_ODD_PTR_TOKEN_COUNT * 2u) {
        fprintf(stderr, "[GBI selftest] FAIL: odd pack 0x%08x did not land in the token range\n", packed);
        fail++;
    }
    back = pc_gbi_unpack_runtime_ptr(packed);
    if (back != 0x87654321u) { fprintf(stderr, "[GBI selftest] FAIL: odd round-trip = 0x%zx, expected 0x87654321\n", (size_t)back); fail++; }

    /* 4) An address that was never packed, chosen from inside the reserved range, must unpack to 0 (falls
     * through to seg2k0()'s remaining checks) rather than returning garbage from an untouched slot. */
    back = pc_gbi_unpack_runtime_ptr(PC_GBI_ODD_PTR_TOKEN_BASE + 6000u * 2u);
    if (back != 0) { fprintf(stderr, "[GBI selftest] FAIL: never-assigned token slot returned 0x%zx, expected 0\n", (size_t)back); fail++; }

    /* 5) A value below the reserved range's base (unsigned underflow in unpack()'s subtraction) must not
     * be misread as a valid token either. */
    back = pc_gbi_unpack_runtime_ptr(PC_GBI_ODD_PTR_TOKEN_BASE - 4u);
    if (back != 0) { fprintf(stderr, "[GBI selftest] FAIL: below-base value returned 0x%zx, expected 0 (underflow not handled)\n", (size_t)back); fail++; }

    /* 6) Ring-buffer wraparound: fill every slot with a distinct odd value, confirm each round-trips while
     * still fresh, then push COUNT more and confirm the oldest slot now holds the NEWEST value (the
     * intended overwrite semantics) rather than silently corrupting an unrelated address. */
    {
        unsigned int first_packed = 0;
        unsigned int i;
        for (i = 0; i < PC_GBI_ODD_PTR_TOKEN_COUNT; i++) {
            uintptr_t fake = ((uintptr_t)0xA0000001u + (uintptr_t)i * 2u); /* always odd, always distinct */
            unsigned int p = pc_gbi_pack_runtime_ptr(fake, 1, "wrap_fill", __FILE__, __LINE__);
            if (i == 0) first_packed = p;
            if (pc_gbi_unpack_runtime_ptr(p) != fake) {
                fprintf(stderr, "[GBI selftest] FAIL: wrap-fill slot %u did not round-trip\n", i);
                fail++;
                break;
            }
        }
        {
            uintptr_t newest = (uintptr_t)0xB0000001u;
            unsigned int p2 = pc_gbi_pack_runtime_ptr(newest, 1, "wrap_evict", __FILE__, __LINE__);
            if (p2 != first_packed) {
                fprintf(stderr, "[GBI selftest] FAIL: ring buffer did not wrap back to slot 0 (got 0x%08x, expected 0x%08x)\n", p2, first_packed);
                fail++;
            } else if (pc_gbi_unpack_runtime_ptr(first_packed) != newest) {
                fprintf(stderr, "[GBI selftest] FAIL: evicted slot 0 did not return the newest value\n");
                fail++;
            }
        }
    }

    if (!fail) {
        pc_lowaddr_log("[SELFTEST] GBI odd-pointer token table: pack/unpack round-trip, underflow, and %u-slot wraparound all correct\n",
                        (unsigned)PC_GBI_ODD_PTR_TOKEN_COUNT);
    }
    return fail;
}
#endif
