/* pc_net.c - minimal PC-native networking layer (Winsock/UDP).
 *
 * See pc_net.h for the full design summary. Implementation notes:
 *
 *   - One UDP socket total, always non-blocking. The same socket is used whether this process
 *     is hosting (bound to a port, tracks up to PC_NET_MAX_PEERS peers by address) or is a
 *     client (unbound, remembers exactly one peer -- the host -- in slot 0).
 *   - Every packet starts with an 8-byte PCNetWireHeader (magic + type + kind + size). The
 *     magic value guards against acting on stray/garbage UDP traffic reaching the port.
 *   - Peer identity is "whichever fixed-size table slot this remote address maps to" -- there
 *     is no notion of a peer surviving a change of IP/port (a reconnect from the same machine
 *     shows up as a new HELLO, i.e. a new logical peer, exactly like a fresh connection). A
 *     same-address HELLO carrying a different nonce is treated as a remote restart.
 *   - Connect/disconnect detection: HELLO/HELLO_ACK establishes a peer; either a received
 *     PCNET_WIRE_DISCONNECT, PCNET_TIMEOUT_MS of silence (no packet of any kind received), or
 *     exhaustion of the reliable retransmit budget ends it. A periodic PCNET_WIRE_HEARTBEAT
 *     keeps idle connections (no game data flowing) from looking dead.
 *   - No dynamic allocation anywhere in this file. No threads. Nothing blocks.
 *
 * Wire format (all fields little-endian; this module only targets little-endian x86/x64):
 *
 *   PCNetWireHeader (8 bytes): u32 magic 'ACNP' | u8 type | u8 kind | u16 size
 *
 *   type 0 HELLO      client->host. size 4: u32 nonce. A size-0 HELLO comes only from a legacy
 *                     (protocol v1, no reliable transport) client and is REFUSED, see below.
 *   type 1 HELLO_ACK  host->client. size 4: echoes the HELLO's nonce. A size-0 HELLO_ACK in
 *                     reply to a nonce HELLO comes only from a legacy v1 host and is REFUSED.
 *   type 2 HEARTBEAT  size 0.
 *   type 3 DISCONNECT size 0.
 *   type 4 DATA       kind 0 (UNRELIABLE), size = payload bytes, + payload. Unchanged from
 *                     Stage 0. Receive: kind 0 -> PC_NET_EVENT_DATA (kind UNRELIABLE);
 *                     kind 1 (legacy placeholder "reliable") is never sent any more and is
 *                     DROPPED on receive (it still counts as liveness; counted in
 *                     PCNetStats.legacy_reliable_dropped); any other kind is dropped.
 *   type 5 RDATA      kind 1, size = payload bytes only, + u32 seq + payload
 *                     (datagram = 12 + size bytes, max 1036).
 *   type 6 ACK        kind 0, size 8, + u32 next_expected + u32 sack_bits.
 *                     Every seq < next_expected has been received; bit i of sack_bits set
 *                     means seq (next_expected + 1 + i) has been received out of order.
 *
 * Reliable channel internals (per peer, per direction; see pc_net.h "Reliability"):
 *
 *   Send:    s_tx[peer][seq & (W-1)] holds a copy of every payload in [tx_base, tx_next).
 *            tx_next - tx_base <= W. An entry is freed once tx_base passes it (cumulative ack,
 *            or SACK followed by the cumulative ack catching up).
 *   Receive: s_rx[peer][seq & (W-1)] holds payloads in [rx_deliver, rx_deliver + W).
 *            rx_next_expected = first seq not yet received (what the ACK reports);
 *            rx_deliver       = next seq to hand to the event queue (<= rx_next_expected).
 *            Payloads in [rx_deliver, rx_next_expected) are received+acked but waiting for
 *            event-queue room; payloads above rx_next_expected are out-of-order (SACKed).
 *
 * Legacy (v1) peers: a v1 build has no RDATA/ACK, so a v1<->v2 pair would connect and then hang
 * (IDENTITY dropped/ignored on each side). The handshake therefore refuses them promptly:
 *   - v2 host, HELLO without a nonce (size != 4): no slot is allocated, no event is raised; the
 *     host answers with a DISCONNECT wire message (a v1 client's PENDING slot 0 turns that into a
 *     PEER_DISCONNECTED) and logs "[pc_net] legacy client (no nonce) at <addr>; refusing"
 *     (rate-limited: once per address per PCNET_LEGACY_LOG_INTERVAL_MS).
 *   - v2 client, size-0 HELLO_ACK while PENDING: the client never enters CONNECTED, logs
 *     "[pc_net] host speaks legacy transport (protocol v1); refusing", sends DISCONNECT, frees
 *     slot 0 and queues ONE PC_NET_EVENT_PEER_DISCONNECTED (peer 0) WITHOUT a preceding
 *     PC_NET_EVENT_PEER_CONNECTED, so a game layer waiting in "connecting" learns the attempt
 *     failed instead of hanging. The socket stays open; the client is simply not connected.
 * Both are counted in PCNetStats.legacy_peers_refused.
 *
 * Fault injection (TEST-ONLY seam: read once at pc_net_init(), present in every build, OFF unless
 * the environment variable is set; all counters deterministic, process-global, counting outgoing
 * transmissions):
 *   PC_NET_FAULT_DROP_RDATA_EVERY=N     every Nth outgoing RDATA transmission (original or
 *                                       retransmit, any peer) is silently not sent.
 *   PC_NET_FAULT_DUP_RDATA_EVERY=N      every Nth outgoing RDATA transmission is sent twice.
 *   PC_NET_FAULT_REORDER_RDATA_EVERY=N  every Nth outgoing RDATA transmission is held back and
 *                                       sent right after the next RDATA transmission (or at
 *                                       the end of the current/next pc_net_poll()).
 *   PC_NET_FAULT_DROP_ACK_EVERY=N       every Nth outgoing ACK is silently not sent.
 *   N = 0 or unset disables that fault; N = 1 means "every one". Drop is evaluated first, then
 *   reorder, then duplicate, on the same counter value. A one-line notice is printed to stderr
 *   when any fault is enabled.
 */
#include "pc_net.h"

/* Stage 0/1 scope is explicitly Windows/Winsock only (see pc_net.h). On any other platform this
 * whole module compiles down to a safe "networking is simply unavailable" stub: every entry
 * point still exists and behaves per its documented contract (init fails, nothing else can be
 * called meaningfully afterwards), so pc_net_game.c and anything above it needs no platform
 * checks of its own -- a failed pc_net_init() already means "continue single-player." */
#ifndef _WIN32

#include <string.h>

int pc_net_init(void) { return 0; }
void pc_net_shutdown(void) {}
int pc_net_host_start(uint16_t port) { (void)port; return 0; }
int pc_net_client_connect(const char* host_ip, uint16_t port) { (void)host_ip; (void)port; return 0; }
void pc_net_poll(void) {}
int pc_net_next_event(PCNetEvent* out) { (void)out; return 0; }
int pc_net_send(PCNetPeerId peer, PCNetMsgKind kind, const void* data, uint16_t size) {
    (void)peer; (void)kind; (void)data; (void)size;
    return 0;
}
int pc_net_reliable_backlog(PCNetPeerId peer) { (void)peer; return -1; }
void pc_net_get_stats(PCNetStats* out) { if (out != NULL) memset(out, 0, sizeof(*out)); }
int pc_net_is_host(void) { return 0; }
int pc_net_is_connected(void) { return 0; }
int pc_net_peer_count(void) { return 0; }
void pc_net_disconnect(PCNetPeerId peer) { (void)peer; }
int pc_net_peer_idle_ms(PCNetPeerId peer) { (void)peer; return -1; }
uint32_t pc_net_peer_ip(PCNetPeerId peer) { (void)peer; return 0; }
void pc_net_evict(PCNetPeerId peer) { (void)peer; }

#else /* _WIN32 */

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* The wire structs below are memcpy'd straight to/from the network in host byte order. */
#if defined(__BYTE_ORDER__) && defined(__ORDER_LITTLE_ENDIAN__)
_Static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "pc_net wire format assumes a little-endian host");
#endif

#define PCNET_MAGIC                 0x41434E50u /* 'ACNP', ASCII - identifies this protocol on the wire */
#define PCNET_HEARTBEAT_INTERVAL_MS 500u
#define PCNET_TIMEOUT_MS            5000u

/* Reliable-channel tuning. */
#define PCNET_RTO_MIN_MS               150u /* initial retransmit timeout floor (and default with no RTT sample) */
#define PCNET_RTO_MAX_MS               1000u /* backoff cap */
#define PCNET_RTO_SRTT_PAD_MS          50u  /* initial RTO = clamp(2*srtt + pad, MIN, MAX) */
#define PCNET_MAX_RETRANSMITS          10u  /* per payload; one more expiry after this => disconnect */
#define PCNET_MAX_RETRANSMITS_PER_POLL 8    /* per peer per pc_net_poll(), oldest first */
#define PCNET_SACK_BITS                32u
#define PCNET_RECV_ERROR_BUDGET        64   /* transient recvfrom errors tolerated per poll drain */
#define PCNET_SOCKET_BUFFER_BYTES      (256 * 1024)
#define PCNET_LEGACY_LOG_INTERVAL_MS   10000u /* per-address rate limit for the legacy-client refusal log */
#define PCNET_LEGACY_LOG_SLOTS         8

#define PCNET_WINDOW      ((uint32_t)PC_NET_RELIABLE_WINDOW)
#define PCNET_WINDOW_MASK (PCNET_WINDOW - 1u)
_Static_assert(PC_NET_RELIABLE_WINDOW >= 64, "PC_NET_RELIABLE_WINDOW must be >= 64 (transport contract)");
_Static_assert((PC_NET_RELIABLE_WINDOW & (PC_NET_RELIABLE_WINDOW - 1)) == 0,
               "PC_NET_RELIABLE_WINDOW must be a power of two (seq & mask indexing across u32 wrap)");
_Static_assert(PC_NET_RELIABLE_WINDOW <= 32768, "PC_NET_RELIABLE_WINDOW must stay far below 2^31 (seq comparisons)");
_Static_assert(PC_NET_MAX_PAYLOAD <= 0xFFFF, "payload size must fit the u16 header size field");

typedef enum PCNetWireType {
    PCNET_WIRE_HELLO      = 0, /* client -> host: "I'd like to connect" (resent until acked); u32 nonce */
    PCNET_WIRE_HELLO_ACK  = 1, /* host -> client: "you're in"; echoes the HELLO's nonce */
    PCNET_WIRE_HEARTBEAT  = 2, /* either direction: "still here" (keeps an idle peer from timing out) */
    PCNET_WIRE_DISCONNECT = 3, /* either direction: "I'm leaving" (fast path; timeout is the fallback) */
    PCNET_WIRE_DATA       = 4, /* either direction: unreliable payload (kind must be PC_NET_UNRELIABLE) */
    PCNET_WIRE_RDATA      = 5, /* either direction: reliable payload: u32 seq + payload */
    PCNET_WIRE_ACK        = 6, /* either direction: PCNetWireAck for the reliable stream */
} PCNetWireType;

typedef struct PCNetWireHeader {
    uint32_t magic;
    uint8_t  type; /* PCNetWireType */
    uint8_t  kind; /* PCNetMsgKind for DATA/RDATA; 0 otherwise */
    uint16_t size; /* bytes following the header (DATA/RDATA: payload bytes only, excluding RDATA's seq) */
} PCNetWireHeader;
/* Every field above is already naturally aligned (4/1/1/2 bytes at offsets 0/4/5/6), so this
 * struct is exactly 8 bytes with no compiler-inserted padding on any ABI this project targets;
 * asserted below rather than relying on that going unverified. */
_Static_assert(sizeof(PCNetWireHeader) == 8, "PCNetWireHeader must be exactly 8 bytes (wire format)");

typedef struct PCNetWireAck {
    uint32_t next_expected; /* every seq < this has been received */
    uint32_t sack_bits;     /* bit i: seq next_expected + 1 + i received out of order */
} PCNetWireAck;
_Static_assert(sizeof(PCNetWireAck) == 8, "PCNetWireAck must be exactly 8 bytes (wire format)");

#define PCNET_WIRE_SEQ_BYTES   4
#define PCNET_WIRE_NONCE_BYTES 4
#define PCNET_MAX_DATAGRAM     ((int)sizeof(PCNetWireHeader) + PCNET_WIRE_SEQ_BYTES + PC_NET_MAX_PAYLOAD)
_Static_assert(PCNET_MAX_DATAGRAM == 1036, "RDATA datagram ceiling changed unexpectedly");

typedef enum PCNetPeerState {
    PCNET_PEER_FREE = 0,
    PCNET_PEER_PENDING,   /* client only: HELLO sent, waiting for HELLO_ACK */
    PCNET_PEER_CONNECTED,
} PCNetPeerState;

typedef struct PCNetPeerSlot {
    PCNetPeerState      state;
    struct sockaddr_in  addr;
    uint32_t            last_recv_tick; /* GetTickCount() of the last packet received from this peer */
    uint32_t            last_send_tick; /* GetTickCount() of the last packet we sent to this peer */
    uint32_t            nonce;          /* host: remote's HELLO nonce; client slot 0: our own nonce */
    /* reliable send side */
    uint32_t            tx_next_seq;    /* seq the next pc_net_send(RELIABLE) will use */
    uint32_t            tx_base_seq;    /* oldest seq not yet cumulatively acknowledged */
    uint32_t            srtt_ms;        /* smoothed RTT (Karn samples only); 0 = no sample yet */
    /* reliable receive side */
    uint32_t            rx_next_expected; /* first seq not yet received (reported in ACKs) */
    uint32_t            rx_deliver_seq;   /* next seq to hand to the event queue */
    int                 ack_pending;      /* an ACK should be sent at the end of this poll */
} PCNetPeerSlot;

typedef struct PCNetTxEntry {
    uint32_t first_send_tick;
    uint32_t last_send_tick;
    uint32_t rto_ms;
    uint16_t size;
    uint8_t  in_use;
    uint8_t  acked;       /* cumulatively or selectively acknowledged; freed when tx_base passes it */
    uint8_t  retransmits;
    uint8_t  pad[3];
    uint8_t  data[PC_NET_MAX_PAYLOAD];
} PCNetTxEntry;

typedef struct PCNetRxEntry {
    uint16_t size;
    uint8_t  present;
    uint8_t  pad;
    uint8_t  data[PC_NET_MAX_PAYLOAD];
} PCNetRxEntry;

/* Event ring. Admission is tiered so that queue pressure can only ever cost unreliable data:
 *   control events (CONNECTED/DISCONNECTED) may use every slot;
 *   reliable deliveries may use all but PCNET_EVENT_CONTROL_RESERVE slots (otherwise they stay
 *     buffered in s_rx and are retried every poll -- never dropped);
 *   unreliable data may additionally not use the PCNET_EVENT_RELIABLE_RESERVE slots, and is
 *     dropped (counted) when it doesn't fit. */
#define PC_NET_EVENT_QUEUE_CAP        128
#define PCNET_EVENT_CONTROL_RESERVE   (2 * PC_NET_MAX_PEERS)
#define PCNET_EVENT_RELIABLE_RESERVE  32
#define PCNET_EVENT_LIMIT_CONTROL     PC_NET_EVENT_QUEUE_CAP
#define PCNET_EVENT_LIMIT_RELIABLE    (PC_NET_EVENT_QUEUE_CAP - PCNET_EVENT_CONTROL_RESERVE)
#define PCNET_EVENT_LIMIT_UNRELIABLE  (PCNET_EVENT_LIMIT_RELIABLE - PCNET_EVENT_RELIABLE_RESERVE)
_Static_assert(PCNET_EVENT_LIMIT_UNRELIABLE > 0, "event queue tiers leave no room for unreliable data");

typedef struct PCNetFaultState {
    int      enabled;
    uint32_t drop_rdata_every;
    uint32_t dup_rdata_every;
    uint32_t reorder_rdata_every;
    uint32_t drop_ack_every;
    uint32_t rdata_counter;
    uint32_t ack_counter;
    int      held_len;  /* > 0: a reordered RDATA is waiting in held_buf */
    int      held_peer;
    struct sockaddr_in held_addr;
    uint8_t  held_buf[PCNET_MAX_DATAGRAM];
} PCNetFaultState;

static int            s_wsa_started = 0;
static SOCKET         s_socket = INVALID_SOCKET;
static int            s_is_host = 0;
static PCNetPeerSlot  s_peers[PC_NET_MAX_PEERS];
static PCNetTxEntry   s_tx[PC_NET_MAX_PEERS][PC_NET_RELIABLE_WINDOW];
static PCNetRxEntry   s_rx[PC_NET_MAX_PEERS][PC_NET_RELIABLE_WINDOW];
static PCNetStats     s_stats;
static PCNetFaultState s_fault;
static uint32_t       s_nonce_counter = 0;

/* Rate limiter for the "[pc_net] legacy client" log line (bounded, fixed; round-robin eviction). */
typedef struct PCNetLegacyLogEntry {
    struct sockaddr_in addr;
    uint32_t           last_log_tick;
    int                used;
} PCNetLegacyLogEntry;
static PCNetLegacyLogEntry s_legacy_log[PCNET_LEGACY_LOG_SLOTS];
static int                 s_legacy_log_next = 0;

static PCNetEvent s_event_queue[PC_NET_EVENT_QUEUE_CAP];
static int        s_event_head  = 0; /* next slot to pop */
static int        s_event_tail  = 0; /* next slot to push */
static int        s_event_count = 0;

/* ------------------------------------------------------------------------------------------ */
/* small helpers                                                                               */

static int pcnet_addr_eq(const struct sockaddr_in* a, const struct sockaddr_in* b) {
    return a->sin_addr.s_addr == b->sin_addr.s_addr && a->sin_port == b->sin_port;
}

static PCNetPeerId pcnet_find_peer(const struct sockaddr_in* from) {
    int i;
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_peers[i].state != PCNET_PEER_FREE && pcnet_addr_eq(&s_peers[i].addr, from)) return (PCNetPeerId)i;
    }
    return PC_NET_INVALID_PEER;
}

static PCNetPeerId pcnet_find_free_slot(void) {
    int i;
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_peers[i].state == PCNET_PEER_FREE) return (PCNetPeerId)i;
    }
    return PC_NET_INVALID_PEER;
}

/* Reserves the next event slot if fewer than `limit` events are queued; NULL otherwise. */
static PCNetEvent* pcnet_event_alloc(int limit) {
    PCNetEvent* ev;
    if (s_event_count >= limit) return NULL;
    ev = &s_event_queue[s_event_tail];
    s_event_tail = (s_event_tail + 1) % PC_NET_EVENT_QUEUE_CAP;
    s_event_count++;
    return ev;
}

static void pcnet_push_control(PCNetEventType type, PCNetPeerId peer) {
    PCNetEvent* ev = pcnet_event_alloc(PCNET_EVENT_LIMIT_CONTROL);
    if (ev == NULL) {
        s_stats.events_dropped_control++; /* only possible if the game stopped draining entirely */
        return;
    }
    ev->type = type;
    ev->peer = peer;
    ev->kind = PC_NET_UNRELIABLE;
    ev->size = 0;
}

static void pcnet_reset_events(void) {
    s_event_head = s_event_tail = s_event_count = 0;
}

static int pcnet_sendto(const struct sockaddr_in* to, const void* buf, int len) {
    return sendto(s_socket, (const char*)buf, len, 0, (const struct sockaddr*)to, (int)sizeof(*to)) != SOCKET_ERROR;
}

/* Header-only or small-payload (<= 8 bytes) control packet. */
static int pcnet_send_ctrl(const struct sockaddr_in* to, PCNetWireType type, const void* payload, uint16_t size) {
    uint8_t buf[sizeof(PCNetWireHeader) + 8];
    PCNetWireHeader hdr;
    if (size > 8) return 0;
    hdr.magic = PCNET_MAGIC;
    hdr.type = (uint8_t)type;
    hdr.kind = 0;
    hdr.size = size;
    memcpy(buf, &hdr, sizeof(hdr));
    if (size) memcpy(buf + sizeof(hdr), payload, size);
    return pcnet_sendto(to, buf, (int)sizeof(hdr) + size);
}

static uint32_t pcnet_make_nonce(void) {
    LARGE_INTEGER qpc;
    uint32_t n;
    QueryPerformanceCounter(&qpc);
    s_nonce_counter++;
    n = (uint32_t)qpc.LowPart ^ ((uint32_t)qpc.HighPart << 7) ^ ((uint32_t)GetCurrentProcessId() * 2654435761u) ^
        (GetTickCount() << 13) ^ (s_nonce_counter * 0x9E3779B9u);
    return n != 0 ? n : 1u; /* keep 0 out of real use (it is what a HELLO without a nonce would decode as) */
}

/* ------------------------------------------------------------------------------------------ */
/* fault injection (test seam; inert unless enabled by environment at pc_net_init())           */

static uint32_t pcnet_env_u32(const char* name) {
    const char* v = getenv(name);
    if (v == NULL || *v == '\0') return 0;
    return (uint32_t)strtoul(v, NULL, 10);
}

/* PC_NET_FAULT_* env vars are TEST-ONLY: read once here at init, never set in normal play. */
static void pcnet_fault_init(void) {
    memset(&s_fault, 0, sizeof(s_fault));
    s_fault.held_peer = -1;
    s_fault.drop_rdata_every = pcnet_env_u32("PC_NET_FAULT_DROP_RDATA_EVERY");
    s_fault.dup_rdata_every = pcnet_env_u32("PC_NET_FAULT_DUP_RDATA_EVERY");
    s_fault.reorder_rdata_every = pcnet_env_u32("PC_NET_FAULT_REORDER_RDATA_EVERY");
    s_fault.drop_ack_every = pcnet_env_u32("PC_NET_FAULT_DROP_ACK_EVERY");
    s_fault.enabled = s_fault.drop_rdata_every || s_fault.dup_rdata_every || s_fault.reorder_rdata_every ||
                      s_fault.drop_ack_every;
    if (s_fault.enabled) {
        fprintf(stderr,
                "[pc_net] FAULT INJECTION ENABLED: drop_rdata_every=%u dup_rdata_every=%u "
                "reorder_rdata_every=%u drop_ack_every=%u\n",
                (unsigned)s_fault.drop_rdata_every, (unsigned)s_fault.dup_rdata_every,
                (unsigned)s_fault.reorder_rdata_every, (unsigned)s_fault.drop_ack_every);
        fflush(stderr);
    }
}

static void pcnet_fault_flush_held(void) {
    if (s_fault.held_len > 0) {
        pcnet_sendto(&s_fault.held_addr, s_fault.held_buf, s_fault.held_len);
        s_fault.held_len = 0;
        s_fault.held_peer = -1;
    }
}

/* A held (reordered) RDATA belongs to a connection incarnation; never let it leak into the
 * next one after the slot is reset. */
static void pcnet_fault_discard_held_for(int i) {
    if (s_fault.held_len > 0 && s_fault.held_peer == i) {
        s_fault.held_len = 0;
        s_fault.held_peer = -1;
    }
}

/* Returns 1 if the datagram was (or is considered) handed to the OS now. */
static int pcnet_emit_rdata(int i, const uint8_t* buf, int len) {
    int ok;
    uint32_t c;
    if (!s_fault.enabled) return pcnet_sendto(&s_peers[i].addr, buf, len);

    c = ++s_fault.rdata_counter;
    if (s_fault.drop_rdata_every && (c % s_fault.drop_rdata_every) == 0) {
        s_stats.fault_dropped_rdata++;
        return 0;
    }
    if (s_fault.reorder_rdata_every && (c % s_fault.reorder_rdata_every) == 0 && s_fault.held_len == 0) {
        memcpy(s_fault.held_buf, buf, (size_t)len);
        s_fault.held_len = len;
        s_fault.held_peer = i;
        s_fault.held_addr = s_peers[i].addr;
        s_stats.fault_reordered_rdata++;
        return 0;
    }
    ok = pcnet_sendto(&s_peers[i].addr, buf, len);
    if (s_fault.dup_rdata_every && (c % s_fault.dup_rdata_every) == 0) {
        pcnet_sendto(&s_peers[i].addr, buf, len);
        s_stats.fault_duplicated_rdata++;
    }
    pcnet_fault_flush_held(); /* a held packet goes out right AFTER a later one => reordered */
    return ok;
}

/* ------------------------------------------------------------------------------------------ */
/* per-peer reliable state                                                                     */

static void pcnet_reset_reliable(int i) {
    uint32_t k;
    PCNetPeerSlot* p = &s_peers[i];
    p->tx_next_seq = 0;
    p->tx_base_seq = 0;
    p->srtt_ms = 0;
    p->rx_next_expected = 0;
    p->rx_deliver_seq = 0;
    p->ack_pending = 0;
    for (k = 0; k < PCNET_WINDOW; k++) {
        s_tx[i][k].in_use = 0;
        s_tx[i][k].acked = 0;
        s_tx[i][k].retransmits = 0;
        s_rx[i][k].present = 0;
    }
    pcnet_fault_discard_held_for(i);
}

/* Frees a slot and wipes everything a reused slot could otherwise inherit. */
static void pcnet_free_slot(int i) {
    s_peers[i].state = PCNET_PEER_FREE;
    s_peers[i].nonce = 0;
    pcnet_reset_reliable(i);
}

/* Remote-caused end of a logical peer (DISCONNECT received, timeout, retransmit budget, nonce
 * restart): payloads already received in order (and therefore already ACKed -- the remote
 * considers them delivered) get one last chance to reach the queue ahead of the DISCONNECTED
 * event, then the slot is wiped and DISCONNECTED is queued. */
static void pcnet_rx_deliver(int i);
static void pcnet_lose_peer(int i) {
    if (s_peers[i].state == PCNET_PEER_CONNECTED) pcnet_rx_deliver(i);
    pcnet_free_slot(i);
    pcnet_push_control(PC_NET_EVENT_PEER_DISCONNECTED, (PCNetPeerId)i);
}

/* Removes not-yet-popped DATA events from `peer` (used by pc_net_disconnect(), which queues no
 * DISCONNECTED event: without this, stale data could be popped after a NEW logical peer took
 * the same slot index). Order of all other events is preserved. */
static void pcnet_purge_peer_data_events(PCNetPeerId peer) {
    int k, n = s_event_count, rd = s_event_head, wr = s_event_head, kept = 0;
    for (k = 0; k < n; k++) {
        const PCNetEvent* ev = &s_event_queue[rd];
        if (!(ev->type == PC_NET_EVENT_DATA && ev->peer == peer)) {
            if (wr != rd) s_event_queue[wr] = *ev;
            wr = (wr + 1) % PC_NET_EVENT_QUEUE_CAP;
            kept++;
        }
        rd = (rd + 1) % PC_NET_EVENT_QUEUE_CAP;
    }
    s_event_tail = wr;
    s_event_count = kept;
}

/* Host: brings a free slot up as a brand-new logical peer and queues CONNECTED. */
static void pcnet_open_slot(int i, const struct sockaddr_in* from, uint32_t nonce, uint32_t now) {
    pcnet_reset_reliable(i);
    s_peers[i].state = PCNET_PEER_CONNECTED;
    s_peers[i].addr = *from;
    s_peers[i].nonce = nonce;
    s_peers[i].last_recv_tick = now;
    s_peers[i].last_send_tick = 0;
    pcnet_push_control(PC_NET_EVENT_PEER_CONNECTED, (PCNetPeerId)i);
}

static uint32_t pcnet_initial_rto(int i) {
    uint32_t srtt = s_peers[i].srtt_ms, rto;
    if (srtt == 0) return PCNET_RTO_MIN_MS;
    rto = 2u * srtt + PCNET_RTO_SRTT_PAD_MS;
    if (rto < PCNET_RTO_MIN_MS) rto = PCNET_RTO_MIN_MS;
    if (rto > PCNET_RTO_MAX_MS) rto = PCNET_RTO_MAX_MS;
    return rto;
}

static void pcnet_tx_transmit(int i, uint32_t seq) {
    uint8_t buf[PCNET_MAX_DATAGRAM];
    PCNetWireHeader hdr;
    PCNetTxEntry* e = &s_tx[i][seq & PCNET_WINDOW_MASK];

    hdr.magic = PCNET_MAGIC;
    hdr.type = (uint8_t)PCNET_WIRE_RDATA;
    hdr.kind = (uint8_t)PC_NET_RELIABLE;
    hdr.size = e->size;
    memcpy(buf, &hdr, sizeof(hdr));
    memcpy(buf + sizeof(hdr), &seq, PCNET_WIRE_SEQ_BYTES);
    if (e->size) memcpy(buf + sizeof(hdr) + PCNET_WIRE_SEQ_BYTES, e->data, e->size);
    if (pcnet_emit_rdata(i, buf, (int)sizeof(hdr) + PCNET_WIRE_SEQ_BYTES + e->size)) {
        s_peers[i].last_send_tick = GetTickCount();
    }
}

static int pcnet_tx_window_full(int i) {
    return (uint32_t)(s_peers[i].tx_next_seq - s_peers[i].tx_base_seq) >= PCNET_WINDOW;
}

/* Caller guarantees: slot CONNECTED, window not full, size <= PC_NET_MAX_PAYLOAD. */
static void pcnet_tx_queue(int i, const void* data, uint16_t size) {
    uint32_t now = GetTickCount();
    uint32_t seq = s_peers[i].tx_next_seq++;
    PCNetTxEntry* e = &s_tx[i][seq & PCNET_WINDOW_MASK];
    e->in_use = 1;
    e->acked = 0;
    e->retransmits = 0;
    e->size = size;
    if (size) memcpy(e->data, data, size);
    e->first_send_tick = now;
    e->last_send_tick = now;
    e->rto_ms = pcnet_initial_rto(i);
    s_stats.rdata_sent++;
    pcnet_tx_transmit(i, seq); /* immediate first transmission (contract) */
}

static void pcnet_tx_mark_acked(int i, uint32_t seq, uint32_t now) {
    PCNetTxEntry* e = &s_tx[i][seq & PCNET_WINDOW_MASK];
    if (!e->in_use || e->acked) return;
    e->acked = 1;
    if (e->retransmits == 0) { /* Karn: only unambiguous samples feed the RTT estimate */
        uint32_t sample = now - e->first_send_tick;
        uint32_t srtt = s_peers[i].srtt_ms;
        if (sample == 0) sample = 1;
        if (sample > 10000u) sample = 10000u;
        s_peers[i].srtt_ms = (srtt == 0) ? sample : (7u * srtt + sample) / 8u;
        if (s_peers[i].srtt_ms == 0) s_peers[i].srtt_ms = 1;
    }
}

static void pcnet_handle_ack(int i, const PCNetWireAck* ack, uint32_t now) {
    PCNetPeerSlot* p = &s_peers[i];
    uint32_t seq, b, inflight;

    s_stats.acks_received++;
    if ((int32_t)(ack->next_expected - p->tx_next_seq) > 0) {
        s_stats.acks_invalid++; /* acknowledges seqs we never sent (stale incarnation/garbage) */
        return;
    }
    /* cumulative part (a stale ACK with next_expected < tx_base simply marks nothing here) */
    for (seq = p->tx_base_seq; seq != p->tx_next_seq && (int32_t)(seq - ack->next_expected) < 0; seq++) {
        pcnet_tx_mark_acked(i, seq, now);
    }
    /* selective part */
    inflight = p->tx_next_seq - p->tx_base_seq;
    for (b = 0; b < PCNET_SACK_BITS; b++) {
        if ((ack->sack_bits & (1u << b)) == 0) continue;
        seq = ack->next_expected + 1u + b;
        if ((uint32_t)(seq - p->tx_base_seq) < inflight) pcnet_tx_mark_acked(i, seq, now);
    }
    /* slide the window */
    while (p->tx_base_seq != p->tx_next_seq && s_tx[i][p->tx_base_seq & PCNET_WINDOW_MASK].acked) {
        PCNetTxEntry* e = &s_tx[i][p->tx_base_seq & PCNET_WINDOW_MASK];
        e->in_use = 0;
        e->acked = 0;
        p->tx_base_seq++;
    }
}

/* Retransmits overdue payloads (oldest first, capped per poll). Returns 0 if some payload has
 * exhausted its retransmit budget (caller disconnects the peer). */
static int pcnet_tx_service(int i, uint32_t now) {
    PCNetPeerSlot* p = &s_peers[i];
    uint32_t seq;
    int sent = 0;
    for (seq = p->tx_base_seq; seq != p->tx_next_seq; seq++) {
        PCNetTxEntry* e = &s_tx[i][seq & PCNET_WINDOW_MASK];
        uint32_t rto;
        if (!e->in_use || e->acked) continue;
        if ((uint32_t)(now - e->last_send_tick) < e->rto_ms) continue;
        if (e->retransmits >= PCNET_MAX_RETRANSMITS) return 0;
        if (sent >= PCNET_MAX_RETRANSMITS_PER_POLL) continue; /* keep scanning only for budget checks */
        e->retransmits++;
        e->last_send_tick = now;
        rto = e->rto_ms * 2u;
        e->rto_ms = rto > PCNET_RTO_MAX_MS ? PCNET_RTO_MAX_MS : rto;
        s_stats.rdata_retransmits++;
        pcnet_tx_transmit(i, seq);
        sent++;
    }
    return 1;
}

/* Hands contiguous received payloads to the event queue while it has room for them. */
static void pcnet_rx_deliver(int i) {
    PCNetPeerSlot* p = &s_peers[i];
    while (p->rx_deliver_seq != p->rx_next_expected) {
        PCNetRxEntry* e = &s_rx[i][p->rx_deliver_seq & PCNET_WINDOW_MASK];
        PCNetEvent* ev = pcnet_event_alloc(PCNET_EVENT_LIMIT_RELIABLE);
        if (ev == NULL) {
            s_stats.rdata_delivery_deferred++; /* stays buffered; retried next poll */
            return;
        }
        ev->type = PC_NET_EVENT_DATA;
        ev->peer = (PCNetPeerId)i;
        ev->kind = PC_NET_RELIABLE;
        ev->size = e->size;
        if (e->size) memcpy(ev->data, e->data, e->size);
        e->present = 0;
        p->rx_deliver_seq++;
        s_stats.rdata_delivered++;
    }
}

static void pcnet_handle_rdata(int i, uint32_t seq, const uint8_t* payload, uint16_t size) {
    PCNetPeerSlot* p = &s_peers[i];
    uint32_t off = seq - p->rx_deliver_seq;
    PCNetRxEntry* e;

    s_stats.rdata_received++;
    p->ack_pending = 1; /* every RDATA (new, duplicate or out of window) earns a (coalesced) ACK */
    if ((int32_t)off < 0) {
        s_stats.rdata_duplicates++; /* already delivered */
        return;
    }
    if (off >= PCNET_WINDOW) {
        s_stats.rdata_out_of_window++; /* not buffered, not acked: the sender will retransmit */
        return;
    }
    e = &s_rx[i][seq & PCNET_WINDOW_MASK];
    if (e->present) {
        s_stats.rdata_duplicates++; /* received earlier, waiting for delivery or for a gap to fill */
        return;
    }
    e->present = 1;
    e->size = size;
    if (size) memcpy(e->data, payload, size);
    while ((uint32_t)(p->rx_next_expected - p->rx_deliver_seq) < PCNET_WINDOW &&
           s_rx[i][p->rx_next_expected & PCNET_WINDOW_MASK].present) {
        p->rx_next_expected++;
    }
    pcnet_rx_deliver(i);
}

static void pcnet_send_ack(int i) {
    PCNetPeerSlot* p = &s_peers[i];
    PCNetWireAck ack;
    uint32_t b;

    ack.next_expected = p->rx_next_expected;
    ack.sack_bits = 0;
    for (b = 0; b < PCNET_SACK_BITS; b++) {
        uint32_t seq = p->rx_next_expected + 1u + b;
        if ((uint32_t)(seq - p->rx_deliver_seq) < PCNET_WINDOW && s_rx[i][seq & PCNET_WINDOW_MASK].present) {
            ack.sack_bits |= 1u << b;
        }
    }
    p->ack_pending = 0;
    if (s_fault.enabled && s_fault.drop_ack_every && (++s_fault.ack_counter % s_fault.drop_ack_every) == 0) {
        s_stats.fault_dropped_acks++;
        return;
    }
    if (pcnet_send_ctrl(&p->addr, PCNET_WIRE_ACK, &ack, (uint16_t)sizeof(ack))) {
        p->last_send_tick = GetTickCount();
        s_stats.acks_sent++;
    }
}

/* ------------------------------------------------------------------------------------------ */
/* lifecycle                                                                                   */

static void pcnet_reset_all_state(void) {
    s_is_host = 0;
    memset(s_peers, 0, sizeof(s_peers));
    memset(s_tx, 0, sizeof(s_tx));
    memset(s_rx, 0, sizeof(s_rx));
    memset(&s_stats, 0, sizeof(s_stats));
    memset(s_legacy_log, 0, sizeof(s_legacy_log));
    s_legacy_log_next = 0;
    pcnet_reset_events();
}

static int pcnet_create_socket(void) {
    u_long mode = 1;
    int bufsz = PCNET_SOCKET_BUFFER_BYTES;
    s_socket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s_socket == INVALID_SOCKET) return 0;
    if (ioctlsocket(s_socket, FIONBIO, &mode) != 0) {
        closesocket(s_socket);
        s_socket = INVALID_SOCKET;
        return 0;
    }
    /* Headroom for reliable bursts (e.g. a world snapshot); failure is harmless (OS default). */
    setsockopt(s_socket, SOL_SOCKET, SO_RCVBUF, (const char*)&bufsz, (int)sizeof(bufsz));
    setsockopt(s_socket, SOL_SOCKET, SO_SNDBUF, (const char*)&bufsz, (int)sizeof(bufsz));
    return 1;
}

int pc_net_init(void) {
    WSADATA wsa;
    if (s_wsa_started) return 1;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return 0;
    s_wsa_started = 1;
    s_socket = INVALID_SOCKET;
    pcnet_reset_all_state();
    pcnet_fault_init();
    return 1;
}

void pc_net_shutdown(void) {
    if (s_socket != INVALID_SOCKET) {
        closesocket(s_socket);
        s_socket = INVALID_SOCKET;
    }
    if (s_wsa_started) {
        WSACleanup();
        s_wsa_started = 0;
    }
    pcnet_reset_all_state();
    s_fault.held_len = 0;
    s_fault.held_peer = -1;
}

int pc_net_host_start(uint16_t port) {
    struct sockaddr_in addr;
    if (!s_wsa_started || s_socket != INVALID_SOCKET) return 0;
    if (!pcnet_create_socket()) return 0;

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(port);
    if (bind(s_socket, (struct sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR) {
        closesocket(s_socket);
        s_socket = INVALID_SOCKET;
        return 0;
    }
    s_is_host = 1;
    return 1;
}

int pc_net_client_connect(const char* host_ip, uint16_t port) {
    struct sockaddr_in addr;
    if (!s_wsa_started || s_socket != INVALID_SOCKET) return 0;
    if (host_ip == NULL) return 0;
    if (!pcnet_create_socket()) return 0;

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    if (inet_pton(AF_INET, host_ip, &addr.sin_addr) != 1) {
        closesocket(s_socket);
        s_socket = INVALID_SOCKET;
        return 0;
    }

    s_is_host = 0;
    pcnet_free_slot(0); /* fresh sequence/ack/retransmit/reorder state for the new connection */
    s_peers[0].state = PCNET_PEER_PENDING;
    s_peers[0].addr = addr;
    s_peers[0].nonce = pcnet_make_nonce();
    s_peers[0].last_recv_tick = GetTickCount();
    s_peers[0].last_send_tick = GetTickCount();
    pcnet_send_ctrl(&addr, PCNET_WIRE_HELLO, &s_peers[0].nonce, PCNET_WIRE_NONCE_BYTES);
    return 1;
}

/* ------------------------------------------------------------------------------------------ */
/* receive path                                                                                */

/* Logs the legacy-client refusal at most once per address per PCNET_LEGACY_LOG_INTERVAL_MS. */
static void pcnet_log_legacy_client(const struct sockaddr_in* from, uint32_t now) {
    char ip[INET_ADDRSTRLEN];
    int k;
    PCNetLegacyLogEntry* e = NULL;

    for (k = 0; k < PCNET_LEGACY_LOG_SLOTS; k++) {
        if (s_legacy_log[k].used && pcnet_addr_eq(&s_legacy_log[k].addr, from)) {
            e = &s_legacy_log[k];
            if ((uint32_t)(now - e->last_log_tick) < PCNET_LEGACY_LOG_INTERVAL_MS) return;
            break;
        }
    }
    if (e == NULL) {
        e = &s_legacy_log[s_legacy_log_next];
        s_legacy_log_next = (s_legacy_log_next + 1) % PCNET_LEGACY_LOG_SLOTS;
        e->addr = *from;
        e->used = 1;
    }
    e->last_log_tick = now;
    if (inet_ntop(AF_INET, (void*)&from->sin_addr, ip, sizeof(ip)) == NULL) strcpy(ip, "?");
    printf("[pc_net] legacy client (no nonce) at %s:%u; refusing\n", ip, (unsigned)ntohs(from->sin_port));
    fflush(stdout);
}
static void pcnet_handle_packet(const struct sockaddr_in* from, const uint8_t* buf, int len) {
    PCNetWireHeader hdr;
    PCNetPeerId pid;
    uint32_t now;

    if (len < (int)sizeof(PCNetWireHeader)) return; /* too short to be one of ours; ignore */
    memcpy(&hdr, buf, sizeof(hdr));
    if (hdr.magic != PCNET_MAGIC) return; /* not our protocol (stray UDP traffic); ignore silently */

    now = GetTickCount();
    pid = pcnet_find_peer(from);

    switch ((PCNetWireType)hdr.type) {
        case PCNET_WIRE_HELLO: {
            uint32_t nonce = 0;
            if (!s_is_host) break; /* clients never accept new peers */
            if (hdr.size < PCNET_WIRE_NONCE_BYTES || len < (int)sizeof(hdr) + PCNET_WIRE_NONCE_BYTES) {
                /* No nonce: a legacy (v1) client, which has no reliable transport and would hang
                 * against us after its IDENTITY is dropped. Refuse: no slot, no event, no HELLO_ACK;
                 * a DISCONNECT lets a v1 client (PENDING slot 0 matches our address) fail at once. */
                s_stats.legacy_peers_refused++;
                pcnet_log_legacy_client(from, now);
                pcnet_send_ctrl(from, PCNET_WIRE_DISCONNECT, NULL, 0);
                break;
            }
            memcpy(&nonce, buf + sizeof(hdr), PCNET_WIRE_NONCE_BYTES);
            if (pid == PC_NET_INVALID_PEER) {
                pid = pcnet_find_free_slot();
                if (pid == PC_NET_INVALID_PEER) break; /* peer table full: drop the new connection attempt */
                pcnet_open_slot((int)pid, from, nonce, now);
            } else if (s_peers[pid].nonce != nonce) {
                /* Same address, different nonce: the remote process restarted. The old logical
                 * peer is gone (its reliable streams can never complete); start a fresh one. */
                s_stats.nonce_restarts++;
                pcnet_lose_peer((int)pid);
                pcnet_open_slot((int)pid, from, nonce, now);
            } else {
                s_peers[pid].last_recv_tick = now; /* duplicate HELLO (our ACK was likely lost); just refresh */
            }
            pcnet_send_ctrl(from, PCNET_WIRE_HELLO_ACK, &nonce, PCNET_WIRE_NONCE_BYTES);
            break;
        }

        case PCNET_WIRE_HELLO_ACK:
            if (s_is_host) break; /* only a client expects this */
            if (s_peers[0].state == PCNET_PEER_PENDING && pcnet_addr_eq(&s_peers[0].addr, from)) {
                uint32_t echoed;
                if (hdr.size == 0) {
                    /* Our HELLO carried a nonce, so only a legacy (v1) host answers with a size-0
                     * HELLO_ACK. It has no reliable transport: our IDENTITY (RDATA) would be
                     * ignored and both sides would hang. Refuse: never CONNECTED, tell the host,
                     * and surface a single DISCONNECTED so the game layer stops waiting. */
                    s_stats.legacy_peers_refused++;
                    printf("[pc_net] host speaks legacy transport (protocol v1); refusing\n");
                    fflush(stdout);
                    pcnet_send_ctrl(&s_peers[0].addr, PCNET_WIRE_DISCONNECT, NULL, 0);
                    pcnet_free_slot(0);
                    pcnet_push_control(PC_NET_EVENT_PEER_DISCONNECTED, 0);
                    break;
                }
                if (hdr.size < PCNET_WIRE_NONCE_BYTES || len < (int)sizeof(hdr) + PCNET_WIRE_NONCE_BYTES) break; /* malformed */
                memcpy(&echoed, buf + sizeof(hdr), PCNET_WIRE_NONCE_BYTES);
                if (echoed != s_peers[0].nonce) break; /* answers some other HELLO; not ours */
                s_peers[0].state = PCNET_PEER_CONNECTED;
                s_peers[0].last_recv_tick = now;
                pcnet_push_control(PC_NET_EVENT_PEER_CONNECTED, 0);
            }
            break;

        case PCNET_WIRE_HEARTBEAT:
            if (pid != PC_NET_INVALID_PEER) s_peers[pid].last_recv_tick = now;
            break;

        case PCNET_WIRE_DISCONNECT:
            if (pid != PC_NET_INVALID_PEER) pcnet_lose_peer((int)pid); /* in-order payloads first, then DISCONNECTED */
            break;

        case PCNET_WIRE_DATA: {
            PCNetEvent* ev;
            if (pid == PC_NET_INVALID_PEER) break; /* data from an address we don't recognize; ignore */
            s_peers[pid].last_recv_tick = now;
            if (hdr.size > PC_NET_MAX_PAYLOAD || (int)(sizeof(hdr) + hdr.size) > len) break; /* malformed */
            if (hdr.kind != (uint8_t)PC_NET_UNRELIABLE) {
                /* Legacy placeholder "reliable" DATA (never sent by this version) or an unknown
                 * kind: drop -- it has no sequence number, so it cannot be delivered exactly-once
                 * and in order, and delivering it tagged RELIABLE would break that guarantee. */
                if (hdr.kind == (uint8_t)PC_NET_RELIABLE) s_stats.legacy_reliable_dropped++;
                break;
            }
            ev = pcnet_event_alloc(PCNET_EVENT_LIMIT_UNRELIABLE);
            if (ev == NULL) {
                s_stats.events_dropped_unreliable++;
                break;
            }
            ev->type = PC_NET_EVENT_DATA;
            ev->peer = pid;
            ev->kind = PC_NET_UNRELIABLE;
            ev->size = hdr.size;
            if (hdr.size) memcpy(ev->data, buf + sizeof(hdr), hdr.size);
            break;
        }

        case PCNET_WIRE_RDATA: {
            uint32_t seq;
            if (pid == PC_NET_INVALID_PEER) break;
            s_peers[pid].last_recv_tick = now;
            if (s_peers[pid].state != PCNET_PEER_CONNECTED) break; /* handshake not finished; sender retransmits */
            if (hdr.kind != (uint8_t)PC_NET_RELIABLE || hdr.size > PC_NET_MAX_PAYLOAD ||
                (int)sizeof(hdr) + PCNET_WIRE_SEQ_BYTES + (int)hdr.size > len) {
                break; /* malformed */
            }
            memcpy(&seq, buf + sizeof(hdr), PCNET_WIRE_SEQ_BYTES);
            pcnet_handle_rdata((int)pid, seq, buf + sizeof(hdr) + PCNET_WIRE_SEQ_BYTES, hdr.size);
            break;
        }

        case PCNET_WIRE_ACK: {
            PCNetWireAck ack;
            if (pid == PC_NET_INVALID_PEER) break;
            s_peers[pid].last_recv_tick = now;
            if (s_peers[pid].state != PCNET_PEER_CONNECTED) break;
            if (hdr.size < sizeof(ack) || len < (int)(sizeof(hdr) + sizeof(ack))) break; /* malformed */
            memcpy(&ack, buf + sizeof(hdr), sizeof(ack));
            pcnet_handle_ack((int)pid, &ack, now);
            break;
        }

        default:
            break; /* unknown/future type on the wire; ignore rather than misinterpret */
    }
}

void pc_net_poll(void) {
    uint8_t buf[PCNET_MAX_DATAGRAM];
    struct sockaddr_in from;
    int fromlen, n, i, errors = 0;
    uint32_t now;

    if (s_socket == INVALID_SOCKET) return;

    /* Reliable payloads held back by a full queue get first claim on room the game freed. */
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_peers[i].state == PCNET_PEER_CONNECTED) pcnet_rx_deliver(i);
    }

    /* Drain every datagram currently waiting; recvfrom on a non-blocking socket returns
     * WSAEWOULDBLOCK the instant nothing is left, so this loop always terminates promptly.
     * WSAECONNRESET (ICMP port-unreachable from an earlier send to a dead peer) and
     * WSAEMSGSIZE (oversized datagram, discarded) are per-datagram conditions: skip them,
     * bounded by PCNET_RECV_ERROR_BUDGET, rather than abandoning the rest of the drain. */
    for (;;) {
        fromlen = (int)sizeof(from);
        n = recvfrom(s_socket, (char*)buf, (int)sizeof(buf), 0, (struct sockaddr*)&from, &fromlen);
        if (n == SOCKET_ERROR) {
            int err = WSAGetLastError();
            if ((err == WSAECONNRESET || err == WSAEMSGSIZE) && ++errors < PCNET_RECV_ERROR_BUDGET) continue;
            break; /* WSAEWOULDBLOCK (no more data) or any other socket error: stop this poll cycle */
        }
        if (n == 0) continue; /* zero-length datagram; nothing to do */
        pcnet_handle_packet(&from, buf, n);
    }

    now = GetTickCount();
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_peers[i].state == PCNET_PEER_FREE) continue;

        if (s_peers[i].state == PCNET_PEER_PENDING) {
            /* Not connected yet, so there is nothing to time out -- just keep resending HELLO
             * in case the original HELLO or the host's HELLO_ACK was lost in transit. */
            if (now - s_peers[i].last_send_tick > PCNET_HEARTBEAT_INTERVAL_MS) {
                pcnet_send_ctrl(&s_peers[i].addr, PCNET_WIRE_HELLO, &s_peers[i].nonce, PCNET_WIRE_NONCE_BYTES);
                s_peers[i].last_send_tick = now;
            }
            continue;
        }

        /* PCNET_PEER_CONNECTED */
        if (now - s_peers[i].last_recv_tick > PCNET_TIMEOUT_MS) {
            pcnet_lose_peer(i);
            continue;
        }
        if (!pcnet_tx_service(i, now)) {
            /* Retransmit budget exhausted: the reliable contract can no longer be honoured, so
             * the link is dead. Tell the peer (best effort) and report a normal disconnect. */
            s_stats.reliable_budget_disconnects++;
            pcnet_send_ctrl(&s_peers[i].addr, PCNET_WIRE_DISCONNECT, NULL, 0);
            pcnet_lose_peer(i);
            continue;
        }
        if (s_peers[i].ack_pending) pcnet_send_ack(i); /* at most one coalesced ACK per peer per poll */
        if (now - s_peers[i].last_send_tick > PCNET_HEARTBEAT_INTERVAL_MS) {
            pcnet_send_ctrl(&s_peers[i].addr, PCNET_WIRE_HEARTBEAT, NULL, 0);
            s_peers[i].last_send_tick = now;
        }
    }

    if (s_fault.enabled) pcnet_fault_flush_held();
}

int pc_net_next_event(PCNetEvent* out) {
    if (s_event_count == 0 || out == NULL) return 0;
    *out = s_event_queue[s_event_head];
    s_event_head = (s_event_head + 1) % PC_NET_EVENT_QUEUE_CAP;
    s_event_count--;
    return 1;
}

/* ------------------------------------------------------------------------------------------ */
/* send path                                                                                   */

static int pcnet_send_unreliable_to_slot(int i, const void* data, uint16_t size) {
    uint8_t buf[sizeof(PCNetWireHeader) + PC_NET_MAX_PAYLOAD];
    PCNetWireHeader hdr;
    int total;

    if (s_peers[i].state != PCNET_PEER_CONNECTED) return 0;

    hdr.magic = PCNET_MAGIC;
    hdr.type = (uint8_t)PCNET_WIRE_DATA;
    hdr.kind = (uint8_t)PC_NET_UNRELIABLE;
    hdr.size = size;
    memcpy(buf, &hdr, sizeof(hdr));
    if (size) memcpy(buf + sizeof(hdr), data, size);
    total = (int)(sizeof(hdr) + size);

    if (!pcnet_sendto(&s_peers[i].addr, buf, total)) return 0;
    s_peers[i].last_send_tick = GetTickCount();
    return 1;
}

int pc_net_send(PCNetPeerId peer, PCNetMsgKind kind, const void* data, uint16_t size) {
    int i;
    if (s_socket == INVALID_SOCKET) return 0;
    if (size > PC_NET_MAX_PAYLOAD) return 0;
    if (size != 0 && data == NULL) return 0;
    if (kind != PC_NET_UNRELIABLE && kind != PC_NET_RELIABLE) return 0;

    if (kind == PC_NET_RELIABLE) {
        if (peer == PC_NET_BROADCAST_PEER) {
            /* All-or-nothing: verify every connected peer has window room before queueing to any. */
            int any = 0;
            for (i = 0; i < PC_NET_MAX_PEERS; i++) {
                if (s_peers[i].state != PCNET_PEER_CONNECTED) continue;
                any = 1;
                if (pcnet_tx_window_full(i)) return 0;
            }
            if (!any) return 0;
            for (i = 0; i < PC_NET_MAX_PEERS; i++) {
                if (s_peers[i].state == PCNET_PEER_CONNECTED) pcnet_tx_queue(i, data, size);
            }
            return 1;
        }
        if (peer < 0 || peer >= PC_NET_MAX_PEERS) return 0;
        if (s_peers[peer].state != PCNET_PEER_CONNECTED || pcnet_tx_window_full((int)peer)) return 0;
        pcnet_tx_queue((int)peer, data, size);
        return 1;
    }

    if (peer == PC_NET_BROADCAST_PEER) {
        int ok = 1, sent_any = 0;
        for (i = 0; i < PC_NET_MAX_PEERS; i++) {
            if (s_peers[i].state != PCNET_PEER_CONNECTED) continue;
            sent_any = 1;
            if (!pcnet_send_unreliable_to_slot(i, data, size)) ok = 0;
        }
        return sent_any ? ok : 0;
    }

    if (peer < 0 || peer >= PC_NET_MAX_PEERS) return 0;
    return pcnet_send_unreliable_to_slot((int)peer, data, size);
}

int pc_net_reliable_backlog(PCNetPeerId peer) {
    if (s_socket == INVALID_SOCKET) return -1;
    if (peer < 0 || peer >= PC_NET_MAX_PEERS) return -1;
    if (s_peers[peer].state != PCNET_PEER_CONNECTED) return -1;
    return (int)(s_peers[peer].tx_next_seq - s_peers[peer].tx_base_seq);
}

void pc_net_get_stats(PCNetStats* out) {
    if (out != NULL) *out = s_stats;
}

/* ------------------------------------------------------------------------------------------ */
/* queries / teardown                                                                          */

int pc_net_is_host(void) {
    return s_socket != INVALID_SOCKET && s_is_host;
}

int pc_net_is_connected(void) {
    if (s_socket == INVALID_SOCKET) return 0;
    if (s_is_host) return 1; /* "up and listening"; a host is never itself "connected" to anyone */
    return s_peers[0].state == PCNET_PEER_CONNECTED;
}

int pc_net_peer_count(void) {
    int i, n = 0;
    for (i = 0; i < PC_NET_MAX_PEERS; i++) {
        if (s_peers[i].state == PCNET_PEER_CONNECTED) n++;
    }
    return n;
}

int pc_net_peer_idle_ms(PCNetPeerId peer) {
    uint32_t idle;
    if (s_socket == INVALID_SOCKET || !s_is_host) return -1;
    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_peers[peer].state != PCNET_PEER_CONNECTED) return -1;
    idle = (uint32_t)GetTickCount() - s_peers[peer].last_recv_tick;
    return idle > 0x7FFFFFFFu ? 0 : (int)idle; /* a tick newer than `now` (wrap/ordering) reads as 0 */
}

/* Guests: the peer's IPv4 address (network byte order) for per-address admission limits; 0 = unknown / not a connected host-side peer. Read-only. */
uint32_t pc_net_peer_ip(PCNetPeerId peer) {
    if (s_socket == INVALID_SOCKET || !s_is_host) return 0;
    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_peers[peer].state != PCNET_PEER_CONNECTED) return 0;
    return (uint32_t)s_peers[peer].addr.sin_addr.s_addr;
}

void pc_net_evict(PCNetPeerId peer) {
    if (s_socket == INVALID_SOCKET || !s_is_host) return;
    if (peer < 0 || peer >= PC_NET_MAX_PEERS || s_peers[peer].state != PCNET_PEER_CONNECTED) return;
    if (s_fault.enabled) pcnet_fault_flush_held();
    pcnet_send_ctrl(&s_peers[peer].addr, PCNET_WIRE_DISCONNECT, NULL, 0); /* best effort: a falsely evicted live peer learns at once */
    pcnet_lose_peer((int)peer); /* delivers already-ACKed payloads, frees the slot, queues PEER_DISCONNECTED */
}

void pc_net_disconnect(PCNetPeerId peer) {
    int idx;
    if (s_socket == INVALID_SOCKET) return;

    if (!s_is_host) {
        idx = 0; /* the only slot a client ever uses; PC_NET_INVALID_PEER (or anything) means "leave" */
    } else {
        if (peer < 0 || peer >= PC_NET_MAX_PEERS) return;
        idx = (int)peer;
    }
    if (s_peers[idx].state == PCNET_PEER_FREE) return;
    if (s_fault.enabled) pcnet_fault_flush_held(); /* keep "one transmission before goodbye" under test faults */
    pcnet_send_ctrl(&s_peers[idx].addr, PCNET_WIRE_DISCONNECT, NULL, 0);
    pcnet_free_slot(idx);
    pcnet_purge_peer_data_events((PCNetPeerId)idx);
}

#endif /* _WIN32 */
