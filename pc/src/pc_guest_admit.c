/* pc_guest_admit.c - see pc_guest_admit.h. Pure logic. */
#include "pc_guest_admit.h"

#include <stdio.h>

#include "pc_net.h" /* PC_NET_RESERVED_PEER_ID */
#include "pc_peer_table.h"

int pc_guest_admit_limit(void) {
    return pc_peer_capacity_max(PC_NET_RESERVED_PEER_ID);
}

int pc_guest_admit_parse(const char* s, int* out) {
    long v = 0;
    int digits = 0;
    if (s == NULL || out == NULL) {
        return 0;
    }
    while (*s == ' ' || *s == '\t') {
        s++;
    }
    for (; *s >= '0' && *s <= '9'; s++) {
        v = v * 10 + (*s - '0');
        digits++;
        if (digits > 6 || v > 1000000L) {
            return 0; /* far past any limit: never let it overflow into a valid-looking number */
        }
    }
    while (*s == ' ' || *s == '\t' || *s == '\r' || *s == '\n') {
        s++;
    }
    if (digits == 0 || *s != '\0' || v < 1 || v > pc_guest_admit_limit()) {
        return 0;
    }
    *out = (int)v;
    return 1;
}

int pc_guest_admit_clamp(int configured) {
    const int lim = pc_guest_admit_limit();
    return configured < 1 ? 1 : (configured > lim ? lim : configured);
}

int pc_guest_admit_effective_cap(int configured, int transport_capacity) {
    const int c = pc_guest_admit_clamp(configured);
    return (transport_capacity >= 0 && c > transport_capacity) ? transport_capacity : c;
}

int pc_guest_admit_decide(const PCGuestAdmitIn* in, char* why, size_t why_cap) {
    if (in == NULL) {
        return PC_GUEST_ADMIT_GUEST_LIMIT;
    }
    if (in->bound >= pc_guest_admit_clamp(in->configured)) {
        if (why != NULL && why_cap > 0) {
            snprintf(why, why_cap, "guest limit reached (%d of max_guests=%d guests are connected)", in->bound, pc_guest_admit_clamp(in->configured));
        }
        return PC_GUEST_ADMIT_GUEST_LIMIT;
    }
    if (in->occupied + in->reserve > in->capacity) {
        if (why != NULL && why_cap > 0) {
            snprintf(why, why_cap, "guest limit reached [transport full] (%d of %d transport peer slots are in use and %d more are held for residents; %d of max_guests=%d guests connected)",
                     in->occupied, in->capacity, in->reserve, in->bound, pc_guest_admit_clamp(in->configured));
        }
        return PC_GUEST_ADMIT_TRANSPORT_FULL;
    }
    return PC_GUEST_ADMIT_OK;
}
