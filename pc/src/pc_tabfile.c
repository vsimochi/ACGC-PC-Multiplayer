/* pc_tabfile.c - see pc_tabfile.h. */
#include "pc_tabfile.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

uint32_t pc_tabfile_fnv(const void* p, size_t n) {
    const unsigned char* b = (const unsigned char*)p;
    uint32_t h = 2166136261u;
    size_t i;
    for (i = 0; i < n; i++) {
        h = (h ^ b[i]) * 16777619u;
    }
    return h;
}

int pc_tabfile_save(const char* path, uint32_t magic, uint32_t version, uint32_t aux, const PCKeyTab* t) {
    char tmp[512];
    uint32_t hdr[4], crc;
    FILE* f;
    int i, ok = 1;
    if (path == NULL || path[0] == 0 || t == NULL || strlen(path) + 5 > sizeof(tmp)) {
        return 0;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    f = fopen(tmp, "wb");
    if (f == NULL) {
        return 0;
    }
    hdr[0] = magic;
    hdr[1] = version;
    hdr[2] = aux;
    hdr[3] = (uint32_t)pc_keytab_count(t);
    crc = pc_tabfile_fnv(hdr, sizeof(hdr));
    ok = fwrite(hdr, sizeof(hdr), 1, f) == 1;
    for (i = 0; ok && i < (int)hdr[3]; i++) {
        const void* r = pc_keytab_at(t, i);
        ok = fwrite(r, t->elem, 1, f) == 1;
        crc ^= pc_tabfile_fnv(r, t->elem) + (uint32_t)i * 0x9E3779B1u;
    }
    ok = ok && fwrite(&crc, sizeof(crc), 1, f) == 1 && fflush(f) == 0;
    ok = (fclose(f) == 0) && ok;
    if (!ok) {
        remove(tmp);
        return 0;
    }
    remove(path);
    if (rename(tmp, path) != 0) {
        remove(tmp);
        return 0;
    }
    return 1;
}

int pc_tabfile_load(const char* path, uint32_t magic, uint32_t version, uint32_t legacy_version, int legacy_count, size_t used_off, PCKeyTab* t, uint32_t* aux_out, uint32_t* version_out, int* kept_out) {
    uint32_t hdr[4], crc, want;
    FILE* f;
    unsigned char* recs = NULL;
    int n = -1, i, kept = 0, rc = PC_TABFILE_BAD;
    if (kept_out != NULL) {
        *kept_out = 0;
    }
    if (path == NULL || t == NULL || (f = fopen(path, "rb")) == NULL) {
        return PC_TABFILE_MISSING;
    }
    if (fread(hdr, sizeof(hdr), 1, f) == 1 && hdr[0] == magic) {
        if (hdr[1] == version) {
            n = hdr[3] <= (uint32_t)t->max ? (int)hdr[3] : -1;
        } else if (legacy_version != 0u && hdr[1] == legacy_version && legacy_count > 0 && hdr[3] == (uint32_t)legacy_count * (uint32_t)t->elem) {
            n = legacy_count;
        }
        if (n >= 0) {
            recs = (unsigned char*)calloc((size_t)(n > 0 ? n : 1), t->elem);
        }
        if (recs != NULL && fread(recs, t->elem, (size_t)n, f) == (size_t)n && fread(&crc, sizeof(crc), 1, f) == 1) {
            if (hdr[1] == version) {
                want = pc_tabfile_fnv(hdr, sizeof(hdr));
                for (i = 0; i < n; i++) {
                    want ^= pc_tabfile_fnv(recs + (size_t)i * t->elem, t->elem) + (uint32_t)i * 0x9E3779B1u;
                }
            } else {
                want = pc_tabfile_fnv(hdr, sizeof(hdr)) ^ pc_tabfile_fnv(recs, (size_t)n * t->elem);
            }
            if (crc == want) {
                /* only now is the table touched: a bad file never half-replaces it */
                (void)pc_keytab_load(t, NULL, 0);
                for (i = 0; i < n; i++) {
                    const unsigned char* r = recs + (size_t)i * t->elem;
                    if (r[used_off] != 0) {
                        void* dst = pc_keytab_get_or_create(t, r + t->key_off, NULL);
                        if (dst != NULL) {
                            memcpy(dst, r, t->elem);
                            kept++;
                        }
                    }
                }
                if (aux_out != NULL) {
                    *aux_out = hdr[2];
                }
                if (version_out != NULL) {
                    *version_out = hdr[1];
                }
                rc = PC_TABFILE_OK;
            }
        }
    }
    free(recs);
    fclose(f);
    if (kept_out != NULL) {
        *kept_out = kept;
    }
    return rc;
}
