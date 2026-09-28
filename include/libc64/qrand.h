#ifndef LQRAND_H
#define LQRAND_H

#include "types.h"

/* This is the ONE global RNG stream shared by the entire game (villagers, fish, bugs, weather,
 * the Stalk Market, shop restock, daily growth, event NPC selection, ...), consumed in
 * frame/call-order-dependent fashion by every unrelated system -- see init_rnd() in sys_math.c for
 * where it is seeded. It is unchanged and still used everywhere by design: see
 * libc64/qrand_domains.h for independent, additive per-domain streams intended for future
 * world-authoritative decisions, which do NOT touch this stream or its callers. */
u32 qrand(void);
void sqrand(u32);
f32 fqrand(void);
f32 fqrand2(void);

#endif