/* pc_roster.h - capacity phase 5: the host's ROSTER BOOK (pure logic, native tests in tools/net_spike/interest_roster_selftest.c).
 *
 * The roster is "who exists, and what does everyone know about them" (appearance, scene). It used to be sent in full to every READY peer every 3 s (N x N reliable messages) and
 * relayed best-effort at the moment of a change (a full reliable window silently dropped it). The book makes it a set of DELTAS with retry:
 *   pend[dest]   the subjects whose current entry `dest` still has to be sent (a join / a change marks the subject for every other READY dest; a join marks every READY subject for
 *                the joiner). The pump sends them in order and calls delivered() only when pc_net_send() accepted the message; a full window just leaves the bit set for the next frame.
 *   gone[dest]   the departures `dest` still has to be told about (a CLEARED notice). A departure is delivered BEFORE any later entry for the same id (ids are reused), so a rejoining
 *                peer never meets a stale entry of its predecessor.
 *   refresh      the loss-mitigation resend of the old design, as ONE subject per period (marks that subject for every dest): N messages per period instead of N x N.
 * Subjects are wire ids 0..254; the host's own wire id is a subject too (it is never a destination). */
#ifndef PC_ROSTER_H
#define PC_ROSTER_H

#include <stdint.h>

#include "pc_peer_table.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct PCRoster {
    int        span;     /* destinations are ids 0..span-1 */
    int        host_id;  /* the host's wire id (a subject, never a destination) */
    PCPeerSet  ready;    /* READY peers (never the host) */
    PCPeerSet* pend;     /* [span] */
    PCPeerSet* gone;     /* [span] */
    int        track_gone; /* 1 (default) = departures are owed to the others (the scene roster); 0 = they are not (the appearance roster: a departure is the scene's CLEARED) */
    int        cursor;   /* refresh position (a subject id) */
    uint32_t   marked, delivered, gone_marked, gone_delivered;
} PCRoster;

int  pc_roster_init(PCRoster* r, int span, int host_id); /* 1 = ok; allocates; 0 = out of memory / bad span (nothing allocated) */
void pc_roster_free(PCRoster* r);
void pc_roster_clear(PCRoster* r);                       /* host restart: forget everything */

/* `id` reached READY: it must be sent every other READY subject and the host; every other READY dest must be sent it (once its entry exists). */
void pc_roster_join(PCRoster* r, int id);
/* `id` left: forget everything pending for it; every other READY dest owes a departure. */
void pc_roster_leave(PCRoster* r, int id);
/* subject's entry changed (a new appearance / scene): every READY dest except the subject itself owes it. The subject may be the host. */
void pc_roster_changed(PCRoster* r, int subject);
/* The host's own entry changed while nobody asked (alias of changed(host_id)). */
void pc_roster_host_changed(PCRoster* r);

/* The pump: the next owed departure / entry of `dest` at or after `from` (a subject id; wraps once). 1 = found. */
int  pc_roster_next_gone(const PCRoster* r, int dest, int from, int* subject);
int  pc_roster_next_pend(const PCRoster* r, int dest, int from, int* subject);
void pc_roster_gone_delivered(PCRoster* r, int dest, int subject);
void pc_roster_pend_delivered(PCRoster* r, int dest, int subject);
int  pc_roster_owed(const PCRoster* r, int dest); /* departures + entries still owed to dest */

/* One refresh step (call once per period): marks the next READY subject (or the host) for every other READY dest. Returns the subject marked, -1 = nobody to refresh. */
int  pc_roster_refresh_step(PCRoster* r);

#ifdef __cplusplus
}
#endif
#endif
