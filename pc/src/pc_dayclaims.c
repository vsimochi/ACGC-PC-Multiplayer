/* pc_dayclaims.c - see pc_dayclaims.h. */
#include "pc_dayclaims.h"

#include <string.h>

int pc_dayclaims_init(PCDayClaims* c, size_t key_len, int max) {
    if (c == NULL || key_len == 0 || key_len > PC_DAYCLAIM_KEY_MAX) {
        return 0;
    }
    memset(c, 0, sizeof(*c));
    c->key_len = key_len;
    c->ready = pc_keytab_init(&c->tab, sizeof(PCDayClaim), 0, PC_DAYCLAIM_KEY_MAX, max);
    return c->ready;
}

void pc_dayclaims_free(PCDayClaims* c) {
    if (c != NULL && c->ready) {
        pc_keytab_free(&c->tab);
        c->ready = 0;
    }
}

static void padded(const PCDayClaims* c, const void* key, uint8_t out[PC_DAYCLAIM_KEY_MAX]) {
    memset(out, 0, PC_DAYCLAIM_KEY_MAX);
    memcpy(out, key, c->key_len);
}

int pc_dayclaims_has(const PCDayClaims* c, const void* key, uint32_t day) {
    uint8_t k[PC_DAYCLAIM_KEY_MAX];
    const PCDayClaim* e;
    if (c == NULL || !c->ready || key == NULL) {
        return 0;
    }
    padded(c, key, k);
    e = (const PCDayClaim*)pc_keytab_find(&c->tab, k);
    return e != NULL && e->day == day;
}

int pc_dayclaims_mark(PCDayClaims* c, const void* key, uint32_t day) {
    uint8_t k[PC_DAYCLAIM_KEY_MAX];
    PCDayClaim* e;
    int i;
    if (c == NULL || !c->ready || key == NULL) {
        return 0;
    }
    for (i = pc_keytab_count(&c->tab) - 1; i >= 0; i--) {
        const PCDayClaim* o = (const PCDayClaim*)pc_keytab_at(&c->tab, i);
        if (o != NULL && o->day != day) {
            uint8_t ok[PC_DAYCLAIM_KEY_MAX];
            memcpy(ok, o->key, sizeof(ok));
            (void)pc_keytab_remove(&c->tab, ok);
        }
    }
    padded(c, key, k);
    e = (PCDayClaim*)pc_keytab_get_or_create(&c->tab, k, NULL);
    if (e == NULL) {
        return 0;
    }
    e->day = day;
    return 1;
}

int pc_dayclaims_count(const PCDayClaims* c) {
    return (c != NULL && c->ready) ? pc_keytab_count(&c->tab) : 0;
}
