/* pc_dayclaims.h - capacity phase 7: "this character already claimed X today" (pure logic, native tests in tools/net_spike/guest_state_selftest.c).
 * The host's per-guest-identity replacement for the vanilla single bit that all foreigners share (K.K.'s once-per-concert song): a claim is the character's key plus the day stamp it was
 * made on; a claim of an earlier day does not count and is swept at the next mark. Memory only; grows one entry per claiming character up to `max`. */
#ifndef PC_DAYCLAIMS_H
#define PC_DAYCLAIMS_H

#include <stdint.h>

#include "pc_keytab.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PC_DAYCLAIM_KEY_MAX 24

typedef struct PCDayClaim {
    uint8_t  key[PC_DAYCLAIM_KEY_MAX];
    uint32_t day;
} PCDayClaim;

typedef struct PCDayClaims {
    PCKeyTab tab;
    size_t   key_len;
    int      ready;
} PCDayClaims;

int  pc_dayclaims_init(PCDayClaims* c, size_t key_len, int max); /* key_len <= PC_DAYCLAIM_KEY_MAX */
void pc_dayclaims_free(PCDayClaims* c);
int  pc_dayclaims_has(const PCDayClaims* c, const void* key, uint32_t day);  /* 1 = this key claimed on exactly `day` */
int  pc_dayclaims_mark(PCDayClaims* c, const void* key, uint32_t day);       /* 1 = recorded (earlier days swept); 0 = full / out of memory (nothing changed for others) */
int  pc_dayclaims_count(const PCDayClaims* c);

#ifdef __cplusplus
}
#endif
#endif
