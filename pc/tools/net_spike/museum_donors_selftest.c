/* museum_donors_selftest.c - native unit test of pc/src/pc_museum_donors.c (worker B: the museum_donors.dat host sidecar).
 * Build + run (from the repo root, git-bash, PATH has /c/msys64/ucrt64/bin appended):
 *   gcc -std=gnu11 -Wall -Wextra -Ipc/include pc/tools/net_spike/museum_donors_selftest.c pc/src/pc_museum_donors.c pc/src/pc_tabfile.c pc/src/pc_keytab.c -o <scratch>/mdon_selftest.exe
 *   <scratch>/mdon_selftest.exe <scratch dir>
 * Covers: set / get / replace / clear, range checks, save + load round trip (atomic tmp -> rename, no .tmp left), a missing file (empty, MISSING), another town's file
 * (ignored, other_town = 1), a corrupt / truncated file (BAD, the table untouched), prune of orphans (only entries whose exhibit is still a guest nibble survive),
 * the 120-exhibit capacity and the record size of the on-disk format (24 B). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pc_museum_donors.h"
#include "pc_tabfile.h"

static int s_fail = 0, s_pass = 0;
#define CHECK(c)                                                    \
    do {                                                            \
        if (!(c)) {                                                 \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);     \
            s_fail++;                                               \
        } else {                                                    \
            s_pass++;                                               \
        }                                                           \
    } while (0)

static void key(uint8_t* k, const char* name, uint16_t pid) {
    memset(k, 0, PC_MDON_KEY_LEN);
    memcpy(k, name, strlen(name) < 8 ? strlen(name) : 8);
    memcpy(k + 8, "HOMETWN ", 8);
    k[16] = (uint8_t)(pid & 0xFF);
    k[17] = (uint8_t)(pid >> 8);
    k[18] = 0x01;
    k[19] = 0x5B;
}

static int s_nib[PC_MDON_EXHIBITS];
static int is_guest(int e, void* ctx) {
    (void)ctx;
    return s_nib[e] == 5;
}

int main(int argc, char** argv) {
    char path[512], tmp[520];
    const char* dir = argc > 1 ? argv[1] : ".";
    PCMuseumDonors d, d2;
    uint8_t ka[PC_MDON_KEY_LEN], kb[PC_MDON_KEY_LEN], out[PC_MDON_KEY_LEN];
    uint32_t town = pc_mdon_town_hash("TESTTOWN", 8, 0x1234), other_town_hash = pc_mdon_town_hash("ELSEWHER", 8, 0x1234);
    int other = 0, i, rc;
    FILE* f;

    snprintf(path, sizeof(path), "%s/museum_donors.dat", dir);
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    remove(path);
    CHECK(sizeof(PCMuseumDonorRec) == 24);
    CHECK(town != other_town_hash && town != 0u);
    key(ka, "GUESTA", 0x4A01);
    key(kb, "GUESTB", 0x4A02);

    CHECK(pc_mdon_init(&d));
    CHECK(pc_mdon_count(&d) == 0);
    CHECK(!pc_mdon_get(&d, 3, out));
    CHECK(pc_mdon_set(&d, 3, ka));
    CHECK(pc_mdon_set(&d, 119, kb));
    CHECK(!pc_mdon_set(&d, 120, ka));
    CHECK(!pc_mdon_set(&d, -1, ka));
    CHECK(pc_mdon_get(&d, 3, out) && memcmp(out, ka, PC_MDON_KEY_LEN) == 0);
    CHECK(pc_mdon_set(&d, 3, kb) && pc_mdon_get(&d, 3, out) && memcmp(out, kb, PC_MDON_KEY_LEN) == 0); /* replace */
    CHECK(pc_mdon_count(&d) == 2);
    CHECK(pc_mdon_clear(&d, 3) && !pc_mdon_get(&d, 3, out) && pc_mdon_count(&d) == 1);
    CHECK(pc_mdon_set(&d, 3, ka));

    /* missing file */
    CHECK(pc_mdon_init(&d2));
    rc = pc_mdon_load(&d2, path, town, &other);
    CHECK(rc == PC_TABFILE_MISSING && pc_mdon_count(&d2) == 0 && other == 0);

    /* round trip */
    CHECK(pc_mdon_save(&d, path, town));
    f = fopen(tmp, "rb");
    CHECK(f == NULL);
    if (f) fclose(f);
    rc = pc_mdon_load(&d2, path, town, &other);
    CHECK(rc == PC_TABFILE_OK && other == 0 && pc_mdon_count(&d2) == 2);
    CHECK(pc_mdon_get(&d2, 3, out) && memcmp(out, ka, PC_MDON_KEY_LEN) == 0);
    CHECK(pc_mdon_get(&d2, 119, out) && memcmp(out, kb, PC_MDON_KEY_LEN) == 0);

    /* another town's file: nothing applies */
    rc = pc_mdon_load(&d2, path, other_town_hash, &other);
    CHECK(rc == PC_TABFILE_OK && other == 1 && pc_mdon_count(&d2) == 0);

    /* corrupt file: BAD, the table untouched */
    CHECK(pc_mdon_set(&d2, 7, ka));
    f = fopen(path, "r+b");
    CHECK(f != NULL);
    if (f) {
        fseek(f, 20, SEEK_SET);
        fputc(0x5A, f);
        fclose(f);
    }
    rc = pc_mdon_load(&d2, path, town, &other);
    CHECK(rc == PC_TABFILE_BAD && pc_mdon_count(&d2) == 1 && pc_mdon_get(&d2, 7, out));
    /* truncated file */
    f = fopen(path, "wb");
    if (f) {
        fwrite("MDON", 1, 4, f);
        fclose(f);
    }
    rc = pc_mdon_load(&d2, path, town, &other);
    CHECK(rc == PC_TABFILE_BAD && pc_mdon_count(&d2) == 1);

    /* prune: only exhibits whose nibble is still 5 keep their attribution */
    for (i = 0; i < (int)PC_MDON_EXHIBITS; i++) {
        s_nib[i] = 0;
    }
    s_nib[3] = 5;    /* still a guest exhibit */
    s_nib[119] = 2;  /* re-credited to resident slot 1 (promotion) -> orphan */
    CHECK(pc_mdon_prune(&d, is_guest, NULL) == 1);
    CHECK(pc_mdon_get(&d, 3, out) && !pc_mdon_get(&d, 119, out));

    /* capacity: every exhibit */
    for (i = 0; i < (int)PC_MDON_EXHIBITS; i++) {
        CHECK(pc_mdon_set(&d, i, (i & 1) ? ka : kb));
    }
    CHECK(pc_mdon_count(&d) == (int)PC_MDON_EXHIBITS);
    CHECK(pc_mdon_save(&d, path, town));
    rc = pc_mdon_load(&d2, path, town, &other);
    CHECK(rc == PC_TABFILE_OK && pc_mdon_count(&d2) == (int)PC_MDON_EXHIBITS);
    CHECK(pc_mdon_get(&d2, 64, out) && memcmp(out, kb, PC_MDON_KEY_LEN) == 0);

    pc_mdon_free(&d);
    pc_mdon_free(&d2);
    remove(path);
    printf("%d passed, %d failed\n", s_pass, s_fail);
    return s_fail ? 1 : 0;
}
