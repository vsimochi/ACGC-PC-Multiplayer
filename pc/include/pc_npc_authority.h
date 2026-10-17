/* pc_npc_authority.h - villager authority range and stale fallback: the PURE per-slot rules (no game headers, natively tested by
 * tools/net_spike/npc_authority_selftest.c).
 *
 * Why: a READY client runs no movement AI for a regular villager; it applies the host's NPC_MOVE stream (ac_npc_move.c_inc). The host only has a villager ACTOR (and therefore a
 * stream) near its own player (the NPC manager's 3x3 scope + Actor_delete_check), so a villager that the host does not simulate -- every villager two or more acres away from the
 * dedicated server's hidden observer -- used to be held by the client at its spawn / last pose FOREVER, and could not be pushed (the client's own push is never integrated).
 *
 * The rule (client side, per animal slot):
 *   HOST mode (consumer, the default): the client mirrors the host stream. It goes LOCAL when the stream is stale:
 *       a sample was received and the newest one is older than stale_ms, or
 *       no sample was ever received,
 *     but never inside the grace window (grace_ms after the slot's actor was created / after the session started), so a slow join or a freshly spawned villager does not flap.
 *   LOCAL mode: the villager runs the vanilla single-player AI + physics on this client (pushing works). It goes back to HOST mode only after fresh_needed CONSECUTIVE fresh samples
 *     (each within fresh_gap_ms of the previous one) -- hysteresis, so a single straggler packet cannot flip it.
 *   The caller may veto the HOST -> LOCAL switch (must_hold: the villager is hidden in its house, or another player owns its conversation); it never forces LOCAL -> HOST.
 * Times are a monotonic millisecond clock (wraps are handled with unsigned differences). */
#ifndef PC_NPC_AUTHORITY_H
#define PC_NPC_AUTHORITY_H

#include <stdint.h>
#include <string.h>

#define PC_NPC_AUTH_STALE_MS 2500u       /* no NPC_MOVE for this long -> the host is not simulating the villager */
#define PC_NPC_AUTH_GRACE_MS 4000u       /* after actor creation / session start: wait this long for a first sample before going local */
#define PC_NPC_AUTH_FRESH_SAMPLES 5u     /* consecutive fresh samples (about 250 ms of a 20 Hz stream) before LOCAL -> HOST */
#define PC_NPC_AUTH_FRESH_GAP_MS 400u    /* two samples further apart than this do not count as consecutive */

enum {
    PC_NPC_AUTH_NOCHANGE = 0,
    PC_NPC_AUTH_TO_LOCAL = 1,
    PC_NPC_AUTH_TO_HOST = 2
};

typedef struct PCNpcAuthCfg {
    uint32_t stale_ms;
    uint32_t grace_ms;
    uint32_t fresh_needed;
    uint32_t fresh_gap_ms;
} PCNpcAuthCfg;

typedef struct PCNpcAuthSlot {
    uint32_t last_recv_ms;  /* receive time of the newest accepted sample (valid iff have) */
    uint32_t grace_from_ms; /* start of the current grace window (actor created / session start) */
    uint32_t local_since_ms;
    uint32_t fresh_run;     /* consecutive fresh samples counted since the last LOCAL switch */
    uint32_t transitions;   /* diagnostics */
    uint8_t  have;          /* at least one sample since the last reset / forget */
    uint8_t  local;         /* 1 = LOCAL mode (vanilla AI on this client) */
    uint8_t  _pad[2];
} PCNpcAuthSlot;

static inline void pc_npc_auth_default_cfg(PCNpcAuthCfg* c) {
    c->stale_ms = PC_NPC_AUTH_STALE_MS;
    c->grace_ms = PC_NPC_AUTH_GRACE_MS;
    c->fresh_needed = PC_NPC_AUTH_FRESH_SAMPLES;
    c->fresh_gap_ms = PC_NPC_AUTH_FRESH_GAP_MS;
}

/* session start / reset: HOST mode, nothing received, grace from now */
static inline void pc_npc_auth_reset(PCNpcAuthSlot* s, uint32_t now) {
    memset(s, 0, sizeof(*s));
    s->grace_from_ms = now;
}

/* the receive ring was discarded (slot reuse / stale ring on actor creation): forget the samples, keep the mode */
static inline void pc_npc_auth_forget(PCNpcAuthSlot* s) {
    s->have = 0;
    s->fresh_run = 0;
    s->last_recv_ms = 0;
}

static inline void pc_npc_auth_on_sample(PCNpcAuthSlot* s, uint32_t now, const PCNpcAuthCfg* c) {
    if (s->have && (uint32_t)(now - s->last_recv_ms) <= c->fresh_gap_ms) {
        s->fresh_run++;
    } else {
        s->fresh_run = 1;
    }
    s->have = 1;
    s->last_recv_ms = now;
}

/* the client's actor for this slot was (re)created: a new grace window starts in HOST mode. Returns 1 iff the newest sample is older than the stale limit, i.e. the caller must clear the
 * receive ring (never snap a brand-new actor to an hours-old pose). */
static inline int pc_npc_auth_on_actor_created(PCNpcAuthSlot* s, uint32_t now, const PCNpcAuthCfg* c) {
    int clear = s->have && (uint32_t)(now - s->last_recv_ms) >= c->stale_ms;
    if (clear) {
        pc_npc_auth_forget(s);
    }
    s->local = 0;
    s->grace_from_ms = now;
    return clear;
}

static inline int pc_npc_auth_is_stale(const PCNpcAuthSlot* s, uint32_t now, const PCNpcAuthCfg* c) {
    if ((uint32_t)(now - s->grace_from_ms) < c->grace_ms) {
        return 0;
    }
    return !s->have || (uint32_t)(now - s->last_recv_ms) >= c->stale_ms;
}

/* advance the mode; must_hold vetoes HOST -> LOCAL only */
static inline int pc_npc_auth_update(PCNpcAuthSlot* s, uint32_t now, int must_hold, const PCNpcAuthCfg* c) {
    if (!s->local) {
        if (!must_hold && pc_npc_auth_is_stale(s, now, c)) {
            s->local = 1;
            s->fresh_run = 0;
            s->local_since_ms = now;
            s->transitions++;
            return PC_NPC_AUTH_TO_LOCAL;
        }
        return PC_NPC_AUTH_NOCHANGE;
    }
    if (s->have && (uint32_t)(now - s->last_recv_ms) < c->stale_ms && s->fresh_run >= c->fresh_needed) {
        s->local = 0;
        s->transitions++;
        return PC_NPC_AUTH_TO_HOST;
    }
    return PC_NPC_AUTH_NOCHANGE;
}

/* ---- host: the client relay lease for a villager the host does NOT simulate ----
 * A client in LOCAL mode reports its villager pose (NPC_PROXY_MOVE). The host accepts it only while it has no live actor of its own for that slot, from ONE owner at a time (the first
 * sender; the lease lapses after lease_ms of silence or when the owner leaves), and relays it as an ordinary NPC_MOVE to every OTHER ready peer, so two clients near the same far
 * villager see the same villager. Returns 1 = accept (and the caller relays), 0 = drop. */
#define PC_NPC_PROXY_LEASE_MS 1500u      /* the relay owner lapses after this much silence */
#define PC_NPC_PROXY_SEND_MS 100u        /* client: one proxy sample per slot per 100 ms (10 Hz) */
#define PC_NPC_HOST_ACTOR_ALIVE_MS 500u  /* host: its own actor counts as live while it reported within this window */

typedef struct PCNpcProxyLease {
    int      owner;   /* peer id, -1 = nobody */
    uint32_t last_ms;
    uint32_t since_ms;
} PCNpcProxyLease;

static inline void pc_npc_proxy_reset(PCNpcProxyLease* l) {
    l->owner = -1;
    l->last_ms = 0;
    l->since_ms = 0;
}

static inline int pc_npc_proxy_offer(PCNpcProxyLease* l, int peer, uint32_t now, int host_actor_live, uint32_t lease_ms) {
    if (host_actor_live) {
        l->owner = -1; /* the host simulates it again: its own stream wins */
        return 0;
    }
    if (l->owner >= 0 && l->owner != peer && (uint32_t)(now - l->last_ms) < lease_ms) {
        return 0; /* somebody else is the relay owner */
    }
    if (l->owner != peer) {
        l->owner = peer;
        l->since_ms = now;
    }
    l->last_ms = now;
    return 1;
}

/* ---- villager push by every player (relay owner or not) ----
 * A villager the host does not simulate is run by ONE client (the relay owner, LOCAL mode); every other client is a consumer of the relayed stream and its own collision
 * push is never integrated, so only the owner could push it (the user's report: "the guest can push the stationary villager, the resident cannot": the guest was the relay
 * owner). The remote players' puppets do not pair with villagers in the OC pass (pc_remote_player.c pipe flags), so the push of a REMOTE player is added explicitly
 * (pc_remote_player_host_npc_push): on the HOST for a villager it simulates (as before), and now also on a READY CLIENT for a villager it runs itself (LOCAL mode): the owner
 * integrates the non-owner's puppet push and the result reaches the pusher through the relay. Ownership never changes because of a push (no flapping, the exclusive owner
 * stays). A consumer (HOST mode) never integrates it (its pose is the stream's); single player never (no puppets). fix_on = AC_MP_NPC_PUSH_FIX (default 1). */
static inline int pc_npc_puppet_push_applies(int is_host, int is_ready_client, int slot_local, int fix_on) {
    if (is_host) {
        return 1;
    }
    return is_ready_client && slot_local && fix_on;
}

/* ---- host: the conversation hold cap ----
 * A client talk hold keeps the villager still on the host. Its BEGIN keepalive arrives every 10 s for as long as the client says it is still talking, so a stuck local talk state used
 * to keep the villager frozen (and unpushable) for everyone forever. The hold is now capped at cap_ms after it BEGAN; a capped peer cannot re-take the hold with keepalives until it
 * sends END (a new conversation). */
#define PC_NPC_TALK_HOLD_CAP_MS 120000u
#define PC_NPC_TALK_PROBE_MISSES 3u /* client: this many consecutive keepalive probes without a visible conversation -> END */

static inline int pc_npc_talk_hold_capped(uint32_t begin_ms, uint32_t now, uint32_t cap_ms) {
    return cap_ms != 0 && begin_ms != 0 && (uint32_t)(now - begin_ms) >= cap_ms;
}

#endif /* PC_NPC_AUTHORITY_H */
