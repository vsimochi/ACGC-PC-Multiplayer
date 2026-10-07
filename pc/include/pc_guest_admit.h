/* pc_guest_admit.h - capacity phase 4: the PURE rules of guest admission (no game state, native tests in tools/net_spike/guest_admission_selftest.c).
 *
 * Five different things bound how many guests a host serves; they are kept apart on purpose:
 *   transport capacity   pc_net_peer_capacity()   (max_peers, 1..254): simultaneous UDP peers, residents + guests + a connecting client; owns the wire ids (host id 8 is never assigned)
 *   configured admission max_guests (1..PC_GUEST_ADMIT_LIMIT, default 4): most guests BOUND at once. Operator policy, checked at every guest bind (new key or known key)
 *   guest store capacity the on-demand per-guest store (pc_mp_guest_store.h) -- bounded by guest_memory_mb only; stored guests may be far more than are bound
 *   resident slots       PLAYER_NUM (4), fixed, never consumed by a guest
 *   puppet capacity      the renderer's remote-player slots (a later phase): NOT a reason to refuse a guest
 * A guest holds exactly one transport peer, so the guests that can really be admitted are min(max_guests, transport capacity - residents' reserve); the gate says which of the two ran out. */
#ifndef PC_GUEST_ADMIT_H
#define PC_GUEST_ADMIT_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The largest max_guests: one guest per usable uint8 wire id (0..254 without the host's id 8; 0xFF is "nobody"). Equals pc_peer_capacity_max(PC_NET_RESERVED_PEER_ID). */
int pc_guest_admit_limit(void);
/* Strict decimal parse of a max_guests value: only digits (optional surrounding spaces), no sign / suffix / overflow, 1..pc_guest_admit_limit(). 1 = ok (*out set), 0 = rejected (*out untouched). */
int pc_guest_admit_parse(const char* s, int* out);
/* A configured value forced into 1..pc_guest_admit_limit(). */
int pc_guest_admit_clamp(int configured);
/* Guests that can actually be admitted at the same time: the configured cap limited by the transport capacity. */
int pc_guest_admit_effective_cap(int configured, int transport_capacity);

enum { PC_GUEST_ADMIT_OK = 0, PC_GUEST_ADMIT_GUEST_LIMIT = 1, PC_GUEST_ADMIT_TRANSPORT_FULL = 2 };
typedef struct PCGuestAdmitIn {
    int configured; /* max_guests as configured */
    int bound;      /* guests currently bound (not counting the one being admitted) */
    int occupied;   /* transport peers in use (including the one being admitted) */
    int capacity;   /* transport capacity */
    int reserve;    /* transport peers still needed by resident records that are not connected */
} PCGuestAdmitIn;
/* THE admission decision for one guest bind. A non-OK result fills `why` (the host log line); the client only ever sees the existing REJECT(SERVER_FULL). */
int pc_guest_admit_decide(const PCGuestAdmitIn* in, char* why, size_t why_cap);

#ifdef __cplusplus
}
#endif
#endif
