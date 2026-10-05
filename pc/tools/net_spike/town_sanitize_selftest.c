/* town_sanitize_selftest.c - native unit test of pc_town_sanitize.c (M-G). Prints "PASS: ..." / "FAIL: ..." lines and "RESULT passed=N failed=M".
 * The expectations are written here WITHOUT the layout table of the module (own offsets, own checksum implementation), plus a tiling check of the exported table. */
#include "pc_town_sanitize.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_pass = 0, g_fail = 0;
#define CHECK(desc, cond) do { if (cond) { g_pass++; printf("PASS: %s\n", desc); } else { g_fail++; printf("FAIL: %s\n", desc); } } while (0)

#define N PC_TS_GCI_SIZE
#define S 0x26040u
#define B 0x4C040u
#define O 0x40u

static uint32_t g_rng = 12345u;
static uint8_t rnd(void) { g_rng = g_rng * 1664525u + 1013904223u; return (uint8_t)(g_rng >> 24); }

static uint8_t tpl_priv[4][0x2440];
static uint8_t tpl_mail[0x12A];
static uint8_t tpl_aram_mail[0xBAC0], tpl_aram_orig[0xCCA0], tpl_aram_diary[0xBA20];
static PCTownSanitizeTpl tpl;

static void make_tpl(void) {
    int i;
    size_t k;
    for (i = 0; i < 4; i++) {
        for (k = 0; k < sizeof(tpl_priv[i]); k++) tpl_priv[i][k] = (uint8_t)(0x11 * (i + 1) + (k * 7u) + 3u);
        tpl.private_be[i] = tpl_priv[i];
    }
    for (k = 0; k < sizeof(tpl_mail); k++) tpl_mail[k] = (uint8_t)(0xC0 ^ (k * 5u));
    for (k = 0; k < sizeof(tpl_aram_mail); k++) tpl_aram_mail[k] = (uint8_t)(0x21 + k * 3u);
    for (k = 0; k < sizeof(tpl_aram_orig); k++) tpl_aram_orig[k] = (uint8_t)(0x55 + k * 11u);
    for (k = 0; k < sizeof(tpl_aram_diary); k++) tpl_aram_diary[k] = (uint8_t)(0x77 + k * 13u);
    tpl.mail_be = tpl_mail;
    tpl.aram_mail_be = tpl_aram_mail;
    tpl.aram_orig_be = tpl_aram_orig;
    tpl.aram_diary_be = tpl_aram_diary;
}

static void make_input(uint8_t* in) {
    size_t k;
    int i;
    for (k = 0; k < N; k++) in[k] = rnd();
    memcpy(in, "GAFE01", 6);
    in[S + 0x912A] = 0x12; /* land id (BE) 0x1234 */
    in[S + 0x912B] = 0x34;
    for (i = 0; i < 4; i++) { /* residents 0, 1, 3 exist; slot 2 does not */
        in[S + 0x20 + i * 0x2440 + 0x1086] = (i == 2) ? 0 : 1;
    }
}

/* independent BE checksum: negated 16-bit sum, computed by subtracting */
static uint16_t cks(const uint8_t* d, size_t len) {
    uint16_t c = 0;
    size_t i;
    for (i = 0; i < len; i += 2) c = (uint16_t)(c - (uint16_t)((d[i] << 8) | d[i + 1]));
    return c;
}

static int in_ranges(uint32_t x, const uint32_t (*r)[2], int n) {
    int i;
    for (i = 0; i < n; i++) if (x >= r[i][0] && x < r[i][1]) return 1;
    return 0;
}

int main(void) {
    static uint8_t in[N], out[N], out2[N], out3[N], tmp[N];
    static PCTownSanitizeSeg segs[PC_TS_MAX_SEGS];
    int n, i, k, m, a, ok;
    uint32_t pos;
    size_t x;
    static const uint32_t priv_keep[][2] = { {0, 0x17}, {0x1086, 0x108C}, {0x10F4, 0x10F8}, {0x2348, 0x234C} };

    make_tpl();
    make_input(in);

    n = pc_town_sanitize_layout(segs, PC_TS_MAX_SEGS);
    CHECK("layout: table is non-empty and fits", n > 0 && n < PC_TS_MAX_SEGS);
    ok = 1; pos = 0;
    for (i = 0; i < n; i++) {
        if (segs[i].off != pos || segs[i].len == 0) ok = 0;
        pos += segs[i].len;
    }
    CHECK("layout: the segments tile [0, 467008) exactly, in order, no gap / overlap / empty segment (every byte classified)", ok && pos == N);
    CHECK("layout: the layout call refuses a too-small table (returns 0)", pc_town_sanitize_layout(segs, 4) == 0);
    n = pc_town_sanitize_layout(segs, PC_TS_MAX_SEGS);

    memset(out, 0xEE, N);
    CHECK("sanitize succeeds on a valid input", pc_town_sanitize(in, N, out, &tpl) == 1);
    CHECK("marker ACMPSAN1 at file offset 0x70 and is_sanitized(out)", memcmp(out + 0x70, "ACMPSAN1", 8) == 0 && pc_town_gci_is_sanitized(out, N) && !pc_town_gci_is_sanitized(in, N));
    CHECK("is_sanitized refuses a wrong length", !pc_town_gci_is_sanitized(out, N - 1) && !pc_town_gci_is_sanitized(NULL, N));

    /* per-segment semantics from the table */
    ok = 1;
    for (i = 0; i < n; i++) {
        const PCTownSanitizeSeg* s = &segs[i];
        uint32_t j;
        for (j = 0; j < s->len; j++) {
            const uint32_t f = s->off + j;
            uint8_t want;
            int skip = 0;
            switch (s->kind) {
            case PC_TS_KEEP: want = in[f]; break;
            case PC_TS_ZERO: want = 0; break;
            case PC_TS_FILL20: want = 0x20; break;
            case PC_TS_TPL_PRIV: want = tpl_priv[s->arg][s->src_off + j]; break;
            case PC_TS_TPL_MAIL: want = tpl_mail[j % 0x12A]; break;
            case PC_TS_MARKER: want = (uint8_t)"ACMPSAN1"[j]; break;
            case PC_TS_TPL_ARAM: {
                const uint8_t* t = s->arg == 0 ? tpl_aram_mail : s->arg == 1 ? tpl_aram_orig : tpl_aram_diary;
                want = t[j];
                if (j < 2) skip = 1;                    /* checksum, checked below */
                if (s->arg == 0 && (j == 2 || j == 3)) skip = 1; /* land id, checked below */
                break;
            }
            case PC_TS_CKSUM_MAIN: skip = 1; want = 0; break;
            case PC_TS_BACKUP: want = out[S + j]; break;
            default: want = 0; ok = 0; break;
            }
            if (!skip && out[f] != want) { ok = 0; }
        }
    }
    CHECK("every segment holds what its kind says (keep == input, template == template, zero, 0x20, marker, backup == main)", ok);

    /* independent semantic checks (own offsets) */
    ok = 1;
    for (i = 0; i < 4; i++) {
        const uint32_t base = S + 0x20 + (uint32_t)i * 0x2440;
        uint32_t j;
        for (j = 0; j < 0x2440; j++) {
            const uint8_t want = in_ranges(j, priv_keep, 4) ? in[base + j] : tpl_priv[i][j];
            if (out[base + j] != want) ok = 0;
        }
    }
    CHECK("private_data[0..3]: every byte equals the template except PID/gender/face/reset_count, exists/hint/cloth, reset_code, state_flags (kept == input)", ok);
    ok = 1;
    for (i = 0; i < 4; i++) {
        const uint32_t base = S + 0x20 + (uint32_t)i * 0x2440;
        const uint8_t* p = out + base;
        if (memcmp(p, in + base, 0x17) != 0 || p[0x1086] != in[base + 0x1086] || memcmp(p + 0x1087, in + base + 0x1087, 5) != 0) ok = 0;
    }
    CHECK("private_data: names / PersonalIDs, gender, face, exists, cloth are kept (documented residual leakage)", ok);
    {
        int leaked = 0;
        for (i = 0; i < 4; i++) {
            const uint32_t base = S + 0x20 + (uint32_t)i * 0x2440;
            uint32_t j;
            /* a long sample of private bytes (pockets, wallet, mail, designs, museum, calendar) differs from the input, i.e. none of the input's pocket / mail region survives */
            for (j = 0x68; j < 0x1084; j++) if (out[base + j] == in[base + j] && tpl_priv[i][j] != in[base + j]) leaked++;
        }
        CHECK("private_data: no input byte of pockets / wallet / quests / mail survives where the template differs", leaked == 0);
    }
    ok = 1;
    for (i = 0; i < 4; i++) {
        const uint32_t hb = S + 0x9CE8 + (uint32_t)i * 0x26B0;
        uint32_t j;
        for (j = 0; j < 0x1A30; j++) if (out[hb + j] != in[hb + j]) ok = 0;
        for (m = 0; m < 10; m++) for (j = 0; j < 0x12A; j++) if (out[hb + 0x1A30 + m * 0x12A + j] != tpl_mail[j]) ok = 0;
        for (j = 0x25D4; j < 0x2674; j++) if (out[hb + j] != in[hb + j]) ok = 0;
        for (j = 0x2674; j < 0x2678; j++) if (out[hb + j] != 0) ok = 0;
        for (j = 0x2678; j < 0x26B0; j++) if (out[hb + j] != in[hb + j]) ok = 0;
    }
    CHECK("homes[0..3]: header / floors / gyroid / goki / music box kept, mailbox == template Mail_c x10, gyroid bells zero", ok);
    ok = 1;
    for (a = 0; a < 16; a++) {
        const uint32_t ab = (a < 15) ? S + 0x17438 + (uint32_t)a * 0x988 : S + 0x22540 + 0xF00; /* 15 villagers + the islander */
        uint32_t j;
        for (j = 0; j < 0x988; j++) {
            int inletter = 0, lo = 0;
            int mi;
            uint8_t want = in[ab + j];
            for (mi = 0; mi < 7; mi++) {
                const uint32_t L0 = 0x10 + (uint32_t)mi * 0x138 + 0x32;
                if (j >= L0 + 2 && j < L0 + 4) { want = 0; inletter = 1; }
                if (j >= L0 + 5 && j <= L0 + 0xFD) { want = 0x20; inletter = 1; }
                (void)lo;
            }
            (void)inletter;
            if (out[ab + j] != want) ok = 0;
        }
    }
    CHECK("animals[0..14] + islander: letter present item zero and text 0x20; memory headers / friendship / letter_info / dates kept", ok);
    ok = 1;
    {
        const uint32_t po = S + 0x20694;
        uint32_t j;
        for (j = 0; j < 8; j++) if (out[po + j] != 0) ok = 0;
        for (m = 0; m < 5; m++) for (j = 0; j < 0x12A; j++) if (out[po + 8 + m * 0x12A + j] != tpl_mail[j]) ok = 0;
        for (j = 0x5DA; j < 0x83C; j++) if (out[po + j] != in[po + j]) ok = 0;
    }
    CHECK("post_office: sums / recipient flags zero, mail[5] == template Mail_c, leaflets kept", ok);

    /* Others block */
    CHECK("Others: comment / banner / icon kept (except the marker); CARDDir kept",
          memcmp(out, in, 0x70) == 0 && memcmp(out + 0x78, in + 0x78, 0x40 + 0x1460 - 0x78) == 0);
    {
        const uint8_t* bm = out + O + 0x1460;
        const uint8_t* bo = out + O + 0x1460 + 0xBAC0;
        const uint8_t* bd = out + O + 0x1460 + 0xBAC0 + 0xCCA0;
        uint8_t c[0xBAC0];
        CHECK("ARAM mail block == template (landid BE 0x1234 at +2, checksum at +0)", memcmp(bm + 4, tpl_aram_mail + 4, 0xBAC0 - 4) == 0 && bm[2] == 0x12 && bm[3] == 0x34);
        memcpy(c, bm, 0xBAC0); c[0] = 0; c[1] = 0;
        CHECK("ARAM mail checksum equals the independent implementation", (uint16_t)((bm[0] << 8) | bm[1]) == cks(c, 0xBAC0));
        CHECK("ARAM original block == template except checksum, checksum independent",
              memcmp(bo + 2, tpl_aram_orig + 2, 0xCCA0 - 2) == 0 && (uint16_t)((bo[0] << 8) | bo[1]) == ({ uint8_t* t = malloc(0xCCA0); uint16_t r; memcpy(t, bo, 0xCCA0); t[0] = t[1] = 0; r = cks(t, 0xCCA0); free(t); r; }));
        CHECK("ARAM diary block == template except checksum, checksum independent",
              memcmp(bd + 2, tpl_aram_diary + 2, 0xBA20 - 2) == 0 && (uint16_t)((bd[0] << 8) | bd[1]) == ({ uint8_t* t = malloc(0xBA20); uint16_t r; memcpy(t, bd, 0xBA20); t[0] = t[1] = 0; r = cks(t, 0xBA20); free(t); r; }));
        for (x = O + 0x1460 + 0xBAC0 + 0xCCA0 + 0xBA20, ok = 1; x < O + 0x26000; x++) if (out[x] != 0) ok = 0;
        CHECK("rest of Others up to 0x26000 is zero", ok);
    }
    /* main checksum + backup */
    {
        static uint8_t sv[0x242A0];
        memcpy(sv, out + S, 0x242A0);
        sv[0x12] = 0; sv[0x13] = 0;
        CHECK("Save_t checksum (S+0x12) equals the independent implementation over the sanitized Save_t", (uint16_t)((out[S + 0x12] << 8) | out[S + 0x13]) == cks(sv, 0x242A0));
    }
    CHECK("backup [B, B+0x26000) == the sanitized main Save [S, S+0x26000)", memcmp(out + B, out + S, 0x26000) == 0);
    CHECK("Save padding after Save_t is zero in main", ({ int z = 1; for (x = S + 0x242A0; x < S + 0x26000; x++) if (out[x]) z = 0; z; }));

    /* everything not covered by a non-keep segment is byte-identical */
    {
        size_t diff = 0, allowed = 0;
        for (x = 0; x < N; x++) if (in[x] != out[x]) diff++;
        for (i = 0; i < n; i++) if (segs[i].kind != PC_TS_KEEP) allowed += segs[i].len;
        CHECK("the output differs from the input only inside non-keep segments", diff <= allowed);
    }

    /* determinism, idempotence */
    CHECK("deterministic: a second run gives identical bytes", pc_town_sanitize(in, N, out2, &tpl) == 1 && memcmp(out, out2, N) == 0);
    CHECK("idempotent: sanitize(sanitize(x)) == sanitize(x)", pc_town_sanitize(out, N, out3, &tpl) == 1 && memcmp(out, out3, N) == 0);

    /* refusals leave the output untouched */
    memset(tmp, 0xEE, N);
    CHECK("refuses a short input (len - 1)", pc_town_sanitize(in, N - 1, tmp, &tpl) == 0 && tmp[0] == 0xEE && tmp[N - 1] == 0xEE);
    CHECK("refuses a long input (len + 1)", pc_town_sanitize(in, N + 1, tmp, &tpl) == 0 && tmp[0] == 0xEE);
    {
        static uint8_t bad[N];
        memcpy(bad, in, N);
        bad[0] = 'X';
        CHECK("refuses a non-GAF game code", pc_town_sanitize(bad, N, tmp, &tpl) == 0 && tmp[0] == 0xEE);
        memcpy(bad, in, N);
        bad[S + 0x912A] = 0; bad[S + 0x912B] = 0;
        CHECK("refuses a zero land id (block order could not be detected)", pc_town_sanitize(bad, N, tmp, &tpl) == 0 && tmp[0] == 0xEE);
    }
    {
        PCTownSanitizeTpl t2 = tpl;
        t2.mail_be = NULL;
        CHECK("refuses an incomplete template set", pc_town_sanitize(in, N, tmp, &t2) == 0 && pc_town_sanitize(in, N, tmp, NULL) == 0 && tmp[0] == 0xEE);
    }
    CHECK("refuses NULL buffers and an overlapping output", pc_town_sanitize(NULL, N, tmp, &tpl) == 0 && pc_town_sanitize(in, N, NULL, &tpl) == 0 && pc_town_sanitize(in, N, in + 16, &tpl) == 0);

    /* find_resident */
    {
        uint8_t pid[20];
        int slot = -1;
        memcpy(pid, in + S + 0x20 + 1 * 0x2440, 20);
        CHECK("find_resident: finds slot 1 by its 20-byte PersonalID", pc_town_gci_find_resident(in, N, pid, &slot) == 1 && slot == 1);
        memcpy(pid, in + S + 0x20 + 2 * 0x2440, 20);
        CHECK("find_resident: a slot with exists == 0 is not a resident", pc_town_gci_find_resident(in, N, pid, &slot) == 0);
        memcpy(pid, in + S + 0x20 + 3 * 0x2440, 20);
        slot = -1;
        CHECK("find_resident: works on the sanitized image too (PersonalIDs are kept)", pc_town_gci_find_resident(out, N, pid, &slot) == 1 && slot == 3);
        pid[0] ^= 1;
        CHECK("find_resident: a different PersonalID is not found; bad length refused", pc_town_gci_find_resident(out, N, pid, &slot) == 0 && pc_town_gci_find_resident(out, N - 1, pid, &slot) == 0);
    }
    /* file helper */
    {
        const char* path = "ts_selftest_tmp.gci";
        FILE* f = fopen(path, "wb");
        int r1, r2;
        if (f) { fwrite(out, 1, N, f); fclose(f); }
        r1 = pc_town_gci_file_is_sanitized(path);
        f = fopen(path, "wb");
        if (f) { fwrite(in, 1, N, f); fclose(f); }
        r2 = pc_town_gci_file_is_sanitized(path);
        remove(path);
        CHECK("file helper: sanitized file -> 1, plain file -> 0, missing file -> 0", r1 == 1 && r2 == 0 && pc_town_gci_file_is_sanitized("no_such_file.gci") == 0);
    }
    (void)k;
    printf("RESULT passed=%d failed=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
