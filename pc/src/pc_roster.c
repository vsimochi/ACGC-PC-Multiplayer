/* pc_roster.c - see pc_roster.h. Pure logic. */
#include "pc_roster.h"

#include <stdlib.h>
#include <string.h>

#define PCR_ID_SPACE 255 /* wire ids 0..254 (0xFF is "nobody") */

int pc_roster_init(PCRoster* r, int span, int host_id) {
    if (r == NULL || span < 1 || span > PCR_ID_SPACE || host_id < 0 || host_id >= PCR_ID_SPACE) {
        return 0;
    }
    memset(r, 0, sizeof(*r));
    r->pend = (PCPeerSet*)calloc((size_t)span, sizeof(PCPeerSet));
    r->gone = (PCPeerSet*)calloc((size_t)span, sizeof(PCPeerSet));
    if (r->pend == NULL || r->gone == NULL) {
        free(r->pend);
        free(r->gone);
        memset(r, 0, sizeof(*r));
        return 0;
    }
    r->span = span;
    r->track_gone = 1;
    r->host_id = host_id;
    return 1;
}

void pc_roster_free(PCRoster* r) {
    if (r == NULL) {
        return;
    }
    free(r->pend);
    free(r->gone);
    memset(r, 0, sizeof(*r));
}

void pc_roster_clear(PCRoster* r) {
    int i;
    if (r == NULL || r->pend == NULL) {
        return;
    }
    pc_peer_set_clear(&r->ready);
    for (i = 0; i < r->span; i++) {
        pc_peer_set_clear(&r->pend[i]);
        pc_peer_set_clear(&r->gone[i]);
    }
    r->cursor = 0;
}

static int valid_dest(const PCRoster* r, int id) {
    return r != NULL && r->pend != NULL && id >= 0 && id < r->span && id != r->host_id;
}

static void mark_pend(PCRoster* r, int dest, int subject) {
    if (!pc_peer_set_has(&r->pend[dest], subject)) {
        pc_peer_set_add(&r->pend[dest], subject);
        r->marked++;
    }
}

void pc_roster_join(PCRoster* r, int id) {
    int i;
    if (!valid_dest(r, id)) {
        return;
    }
    pc_peer_set_add(&r->ready, id);
    pc_peer_set_clear(&r->pend[id]); /* a new life of this id: nothing owed from the old one (its departure was queued for the others, not for it) */
    pc_peer_set_clear(&r->gone[id]);
    mark_pend(r, id, r->host_id);
    for (i = 0; i < r->span; i++) {
        if (i != id && pc_peer_set_has(&r->ready, i)) {
            mark_pend(r, id, i);
            mark_pend(r, i, id);
        }
    }
}

void pc_roster_leave(PCRoster* r, int id) {
    int i;
    if (!valid_dest(r, id)) {
        return;
    }
    pc_peer_set_del(&r->ready, id);
    pc_peer_set_clear(&r->pend[id]);
    pc_peer_set_clear(&r->gone[id]);
    for (i = 0; i < r->span; i++) {
        if (i == id) {
            continue;
        }
        pc_peer_set_del(&r->pend[i], id); /* the entry that was owed is moot */
        if (r->track_gone && pc_peer_set_has(&r->ready, i) && !pc_peer_set_has(&r->gone[i], id)) {
            pc_peer_set_add(&r->gone[i], id);
            r->gone_marked++;
        }
    }
}

void pc_roster_changed(PCRoster* r, int subject) {
    int i;
    if (r == NULL || r->pend == NULL || subject < 0 || subject >= PCR_ID_SPACE) {
        return;
    }
    if (subject != r->host_id && !pc_peer_set_has(&r->ready, subject)) {
        return; /* not a member (any more): nothing to announce */
    }
    for (i = 0; i < r->span; i++) {
        if (i != subject && pc_peer_set_has(&r->ready, i)) {
            mark_pend(r, i, subject);
        }
    }
}

void pc_roster_host_changed(PCRoster* r) {
    if (r != NULL) {
        pc_roster_changed(r, r->host_id);
    }
}

static int next_in(const PCPeerSet* s, int from, int* subject) {
    int k, i;
    if (s == NULL || pc_peer_set_empty(s)) {
        return 0;
    }
    if (from < 0 || from >= PCR_ID_SPACE) {
        from = 0;
    }
    for (k = 0; k < PCR_ID_SPACE; k++) {
        i = (from + k) % PCR_ID_SPACE;
        if (pc_peer_set_has(s, i)) {
            *subject = i;
            return 1;
        }
    }
    return 0;
}

int pc_roster_next_gone(const PCRoster* r, int dest, int from, int* subject) {
    return valid_dest(r, dest) && subject != NULL && pc_peer_set_has(&r->ready, dest) && next_in(&r->gone[dest], from, subject);
}

int pc_roster_next_pend(const PCRoster* r, int dest, int from, int* subject) {
    return valid_dest(r, dest) && subject != NULL && pc_peer_set_has(&r->ready, dest) && next_in(&r->pend[dest], from, subject);
}

void pc_roster_gone_delivered(PCRoster* r, int dest, int subject) {
    if (valid_dest(r, dest) && pc_peer_set_has(&r->gone[dest], subject)) {
        pc_peer_set_del(&r->gone[dest], subject);
        r->gone_delivered++;
    }
}

void pc_roster_pend_delivered(PCRoster* r, int dest, int subject) {
    if (valid_dest(r, dest) && pc_peer_set_has(&r->pend[dest], subject)) {
        pc_peer_set_del(&r->pend[dest], subject);
        r->delivered++;
    }
}

int pc_roster_owed(const PCRoster* r, int dest) {
    return valid_dest(r, dest) ? pc_peer_set_count(&r->gone[dest]) + pc_peer_set_count(&r->pend[dest]) : 0;
}

int pc_roster_refresh_step(PCRoster* r) {
    int k, i, subject = -1;
    if (r == NULL || r->pend == NULL) {
        return -1;
    }
    /* the subjects are the host (id host_id) and the READY peers; the cursor walks the id space */
    for (k = 0; k < PCR_ID_SPACE; k++) {
        i = (r->cursor + k) % PCR_ID_SPACE;
        if (i == r->host_id || pc_peer_set_has(&r->ready, i)) {
            subject = i;
            r->cursor = (i + 1) % PCR_ID_SPACE;
            break;
        }
    }
    if (subject >= 0) {
        pc_roster_changed(r, subject);
    }
    return subject;
}
