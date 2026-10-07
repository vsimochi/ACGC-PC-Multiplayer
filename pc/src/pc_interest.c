/* pc_interest.c - see pc_interest.h. Pure logic. */
#include "pc_interest.h"

#include <math.h>
#include <stddef.h>

static int acre_of(float v) {
    if (!(v == v)) { /* NaN: unknown */
        return 0;
    }
    if (v < 0.0f) {
        v = 0.0f;
    }
    if (v > 1.0e7f) {
        v = 1.0e7f;
    }
    return (int)(v / PC_INTEREST_ACRE_UNITS);
}

int pc_interest_tier(const PCInterestView* s, const PCInterestView* d) {
    int dx, dz, dist;
    if (s == NULL || d == NULL || !s->scene_known || !d->scene_known) {
        return PC_INTEREST_NEAR; /* fail open: nothing is known, so nothing is thinned */
    }
    if (s->scene_id != d->scene_id || s->owner != d->owner || s->in_town != d->in_town) {
        return PC_INTEREST_APART;
    }
    if (!s->in_town || !s->pos_known || !d->pos_known) {
        return PC_INTEREST_NEAR; /* the same interior, or a position we do not have yet */
    }
    dx = acre_of(s->x) - acre_of(d->x);
    dz = acre_of(s->z) - acre_of(d->z);
    dx = dx < 0 ? -dx : dx;
    dz = dz < 0 ? -dz : dz;
    dist = dx > dz ? dx : dz;
    if (dist <= PC_INTEREST_NEAR_ACRES) {
        return PC_INTEREST_NEAR;
    }
    return dist <= PC_INTEREST_MID_ACRES ? PC_INTEREST_MID : PC_INTEREST_FAR;
}

int pc_interest_period(int tier) {
    switch (tier) {
        case PC_INTEREST_MID: return 2;
        case PC_INTEREST_FAR: return 4;
        case PC_INTEREST_APART: return 20;
        default: return 1;
    }
}

int pc_interest_relay(int tier, uint32_t count, int boost) {
    return boost > 0 || (count % (uint32_t)pc_interest_period(tier)) == 0u;
}
