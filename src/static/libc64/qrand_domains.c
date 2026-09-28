#include "libc64/qrand_domains.h"

/* Same multiplier/increment as qrand.c's qrand()/fqrand()/fqrand2() -- see the header comment for
 * why this file exists and what it deliberately does not change. */
#define QRAND_D_MULT 0x19660Du
#define QRAND_D_INC 0x3C6EF35Fu

/* One independent LCG state word per domain, including the reserved-but-unnamed headroom slots.
 * All start at 1, matching qrand.c's own `static u32 __qrand_idum = 1;` default -- see the header
 * comment on what that means for un-seeded domains. */
static u32 l_domain_idum[QRAND_DOMAIN_CAPACITY] = {
  1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1
};

/* Defends the fixed-size backing array against an out-of-range domain value (e.g. a stray cast)
 * the same way a real out-of-bounds index would be a bug elsewhere in this decomp -- clamps to
 * domain 0 rather than reading/writing out of bounds. */
static QRandDomain qrand_domain_clamp(QRandDomain domain) {
  if ((u32)domain >= (u32)QRAND_DOMAIN_CAPACITY) {
    return (QRandDomain)0;
  }

  return domain;
}

void sqrand_d(QRandDomain domain, u32 seed) {
  l_domain_idum[qrand_domain_clamp(domain)] = seed;
}

u32 qrand_d(QRandDomain domain) {
  domain = qrand_domain_clamp(domain);
  l_domain_idum[domain] = l_domain_idum[domain] * QRAND_D_MULT + QRAND_D_INC;
  return l_domain_idum[domain];
}

f32 fqrand_d(QRandDomain domain) {
  u32 itemp;

  domain = qrand_domain_clamp(domain);
  l_domain_idum[domain] = l_domain_idum[domain] * QRAND_D_MULT + QRAND_D_INC;
  itemp = (l_domain_idum[domain] >> 9) | 0x3F800000;

  /* Same [1.0, 2.0) bit-pattern trick as qrand.c's fqrand(); the project builds with
   * -fno-strict-aliasing specifically so this pattern (already used by the original game's own
   * fqrand()/fqrand2()) is well-defined for this compiler, not just "works in practice". */
  return *(f32*)&itemp - 1.0f;
}

f32 fqrand2_d(QRandDomain domain) {
  u32 itemp;

  domain = qrand_domain_clamp(domain);
  l_domain_idum[domain] = l_domain_idum[domain] * QRAND_D_MULT + QRAND_D_INC;
  itemp = (l_domain_idum[domain] >> 9) | 0x3F800000;

  return *(f32*)&itemp - 1.5f;
}

u32 qrand_domain_hash_town(u16 land_id, const u8* land_name, int name_len) {
  /* FNV-1a, 32-bit (offset basis 0x811C9DC5, prime 0x01000193). */
  u32 hash = 0x811C9DC5u;
  int i;

  hash = (hash ^ (u32)(land_id & 0xFF)) * 0x01000193u;
  hash = (hash ^ (u32)((land_id >> 8) & 0xFF)) * 0x01000193u;

  if (land_name != NULL) {
    for (i = 0; i < name_len; i++) {
      hash = (hash ^ (u32)land_name[i]) * 0x01000193u;
    }
  }

  return hash;
}

u32 qrand_domain_day_epoch(u16 year, u8 month, u8 day) {
  /* Deliberately not real calendar math -- see header comment. */
  return (u32)year * 372u + (u32)month * 31u + (u32)day;
}

u32 qrand_domain_make_seed(u32 town_hash, u32 day_epoch, QRandDomain domain) {
  u32 seed = town_hash ^ (day_epoch * 0x01000193u) ^ ((u32)domain * 0x9E3779B1u);

  /* One LCG step used purely as an avalanche/mix here, not as a stream -- see header comment. */
  seed = seed * QRAND_D_MULT + QRAND_D_INC;

  return seed;
}
