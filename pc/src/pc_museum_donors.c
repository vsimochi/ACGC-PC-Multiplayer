/* pc_museum_donors.c - see pc_museum_donors.h (worker B). Pure libc + pc_keytab / pc_tabfile. */
#include "pc_museum_donors.h"

#include <string.h>

#include "pc_tabfile.h"

int pc_mdon_init(PCMuseumDonors* d) {
    if (d == NULL) {
        return 0;
    }
    memset(d, 0, sizeof(*d));
    if (!pc_keytab_init(&d->t, sizeof(PCMuseumDonorRec), offsetof(PCMuseumDonorRec, exhibit), 1, (int)PC_MDON_EXHIBITS)) {
        return 0;
    }
    d->init = 1;
    return 1;
}

void pc_mdon_free(PCMuseumDonors* d) {
    if (d != NULL && d->init) {
        pc_keytab_free(&d->t);
        d->init = 0;
    }
}

uint32_t pc_mdon_town_hash(const void* land_name, size_t land_len, uint16_t land_id) {
    uint32_t h = pc_tabfile_fnv(land_name, land_len);
    h ^= (uint32_t)land_id * 0x9E3779B1u;
    return h != 0u ? h : 1u;
}

int pc_mdon_load(PCMuseumDonors* d, const char* path, uint32_t town_hash, int* other_town) {
    PCMuseumDonors tmp;
    uint32_t aux = 0;
    int rc, i;
    if (other_town != NULL) {
        *other_town = 0;
    }
    if (d == NULL || !d->init || !pc_mdon_init(&tmp)) {
        return PC_TABFILE_BAD;
    }
    rc = pc_tabfile_load(path, PC_MDON_MAGIC, PC_MDON_VERSION, 0u, 0, offsetof(PCMuseumDonorRec, used), &tmp.t, &aux, NULL, NULL);
    if (rc == PC_TABFILE_OK) {
        (void)pc_keytab_load(&d->t, NULL, 0);
        if (aux != town_hash) {
            if (other_town != NULL) {
                *other_town = 1; /* another town's file: nothing of it applies here (it is overwritten by this town's first save) */
            }
        } else {
            for (i = 0; i < pc_keytab_count(&tmp.t); i++) {
                const PCMuseumDonorRec* r = (const PCMuseumDonorRec*)pc_keytab_at(&tmp.t, i);
                if (r->exhibit < PC_MDON_EXHIBITS && r->used == 1u) {
                    (void)pc_mdon_set(d, r->exhibit, r->key);
                }
            }
        }
    } else if (rc == PC_TABFILE_MISSING) {
        (void)pc_keytab_load(&d->t, NULL, 0);
    }
    pc_mdon_free(&tmp);
    return rc;
}

int pc_mdon_save(const PCMuseumDonors* d, const char* path, uint32_t town_hash) {
    if (d == NULL || !d->init) {
        return 0;
    }
    return pc_tabfile_save(path, PC_MDON_MAGIC, PC_MDON_VERSION, town_hash, &d->t);
}

int pc_mdon_set(PCMuseumDonors* d, int exhibit, const uint8_t key[PC_MDON_KEY_LEN]) {
    uint8_t k = (uint8_t)exhibit;
    PCMuseumDonorRec* r;
    if (d == NULL || !d->init || exhibit < 0 || exhibit >= (int)PC_MDON_EXHIBITS || key == NULL) {
        return 0;
    }
    r = (PCMuseumDonorRec*)pc_keytab_get_or_create(&d->t, &k, NULL);
    if (r == NULL) {
        return 0;
    }
    r->exhibit = k;
    r->used = 1u;
    r->_r[0] = r->_r[1] = 0u;
    memcpy(r->key, key, PC_MDON_KEY_LEN);
    return 1;
}

int pc_mdon_get(const PCMuseumDonors* d, int exhibit, uint8_t key_out[PC_MDON_KEY_LEN]) {
    uint8_t k = (uint8_t)exhibit;
    const PCMuseumDonorRec* r;
    if (d == NULL || !d->init || exhibit < 0 || exhibit >= (int)PC_MDON_EXHIBITS) {
        return 0;
    }
    r = (const PCMuseumDonorRec*)pc_keytab_find(&d->t, &k);
    if (r == NULL || r->used != 1u) {
        return 0;
    }
    if (key_out != NULL) {
        memcpy(key_out, r->key, PC_MDON_KEY_LEN);
    }
    return 1;
}

int pc_mdon_clear(PCMuseumDonors* d, int exhibit) {
    uint8_t k = (uint8_t)exhibit;
    if (d == NULL || !d->init || exhibit < 0 || exhibit >= (int)PC_MDON_EXHIBITS) {
        return 0;
    }
    return pc_keytab_remove(&d->t, &k);
}

int pc_mdon_prune(PCMuseumDonors* d, int (*is_guest_nibble)(int exhibit, void* ctx), void* ctx) {
    int dropped = 0, e;
    if (d == NULL || !d->init || is_guest_nibble == NULL) {
        return 0;
    }
    for (e = 0; e < (int)PC_MDON_EXHIBITS; e++) {
        if (pc_mdon_get(d, e, NULL) && !is_guest_nibble(e, ctx)) {
            dropped += pc_mdon_clear(d, e);
        }
    }
    return dropped;
}

int pc_mdon_count(const PCMuseumDonors* d) {
    return (d != NULL && d->init) ? pc_keytab_count(&d->t) : 0;
}
