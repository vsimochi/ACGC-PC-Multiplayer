/* pc_net.h - minimal PC-native networking layer (Winsock/UDP).
 *
 * This is a PC-only module (decomp-independent: no game header is included here). It was first
 * proven standalone by pc/tools/net_spike/ and is driven in-game by pc_net_game.c.
 *
 * Design summary
 * ---------------
 *   - Transport is a single UDP socket. One process is either idle, "hosting" (can accept
 *     multiple peers), or a "client" (connects to exactly one host). Call pc_net_host_start()
 *     or pc_net_client_connect() at most once between pc_net_init()/pc_net_shutdown().
 *   - UDP has no real connection state, so a tiny HELLO / HELLO_ACK handshake plus periodic
 *     heartbeats let each side detect "peer connected" and "peer disconnected (timed out or
 *     said goodbye)" without inventing anything heavier. A client's HELLO carries a random
 *     32-bit nonce; a HELLO from an already-connected address with a DIFFERENT nonce means the
 *     remote process restarted, and is reported as DISCONNECTED (old logical peer) followed by
 *     CONNECTED (fresh logical peer, possibly the same slot index).
 *   - Legacy (protocol v1) builds have no reliable transport and would hang against this one, so
 *     they are refused at the handshake: a HELLO without a nonce (size 0) is never accepted (the
 *     host allocates no slot, raises no event, and answers with a DISCONNECT), and a size-0
 *     HELLO_ACK to a nonce HELLO (a v1 host) makes the client give up (see
 *     pc_net_client_connect()).
 *   - Two message kinds exist:
 *       PC_NET_UNRELIABLE: one plain UDP datagram, fire-and-forget. May be lost, duplicated or
 *         reordered by the network. Never waits on anything (movement lives here).
 *       PC_NET_RELIABLE: reliable + ordered, per peer, per direction. Every reliable payload
 *         sent to a peer is delivered to that peer's game layer exactly once and in send order,
 *         or the link is torn down (a normal PC_NET_EVENT_PEER_DISCONNECTED on both sides) after
 *         a bounded retransmit budget. See "Reliability" below.
 *     Reliable and unreliable traffic are NOT ordered relative to each other.
 *   - No dynamic allocation anywhere: a fixed-size peer table, fixed per-peer reliable send /
 *     receive windows and a fixed-size event ring are the module's only state, all owned
 *     internally (never exposed).
 *   - This header never includes <windows.h>/winsock. No Windows-specific type appears here.
 *   - No thread is created by this module and nothing ever blocks or sleeps. Call pc_net_poll()
 *     from the thread that drives networking (the main/game thread).
 *
 * Reliability (PC_NET_RELIABLE)
 * -----------------------------
 *   - Each reliable payload gets a per-peer u32 sequence number (first one of every new
 *     connection, each direction, is 0) and is transmitted IMMEDIATELY inside pc_net_send(), so
 *     a reliable message sent right before pc_net_disconnect() still gets one transmission.
 *   - The receiver acknowledges with a cumulative "next expected seq" plus a 32-bit selective-ack
 *     bitfield, coalesced to at most one ACK per peer per pc_net_poll() (duplicates re-trigger an
 *     ACK so a lost ACK is repaired by the sender's retransmit).
 *   - The sender retransmits every unacknowledged payload on a per-payload timer (initial RTO
 *     derived from a smoothed RTT estimate, never below 150 ms; doubles per retransmit, capped at
 *     1000 ms), at most 8 retransmits per peer per poll. A payload that is still unacknowledged
 *     after 10 retransmits (roughly 9 s at the default RTO) means the reliable contract is
 *     broken: the peer is disconnected (DISCONNECT notice sent, local
 *     PC_NET_EVENT_PEER_DISCONNECTED queued).
 *   - The receiver buffers out-of-order payloads (window PC_NET_RELIABLE_WINDOW), drops
 *     duplicates, and hands payloads to the event queue strictly in sequence order. An
 *     already-acknowledged payload is never lost to event-queue pressure: if the queue has no
 *     room it stays buffered and is delivered on a later poll. Only unreliable data events are
 *     ever dropped for queue pressure.
 *   - At most PC_NET_RELIABLE_WINDOW reliable payloads may be queued-or-unacknowledged per peer.
 *     When that window is full, pc_net_send(..., PC_NET_RELIABLE, ...) returns 0 and queues
 *     nothing; use pc_net_reliable_backlog() to pace large bursts.
 *   - All per-peer sequence / ack / retransmit / reorder state is reset whenever a slot is
 *     allocated or freed (HELLO accept, nonce change, timeout, DISCONNECT, pc_net_disconnect(),
 *     retransmit-budget exhaustion, client connect, init, shutdown).
 *   - Heartbeat (500 ms idle) / timeout (5000 ms of silence) semantics are unchanged; any valid
 *     packet from the peer (including RDATA and ACK) counts as liveness.
 */
#ifndef PC_NET_H
#define PC_NET_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_NET_MAX_PEERS   8     /* fixed peer table size (host role); a client only ever uses 1 */
#define PC_NET_MAX_PAYLOAD 1024  /* comfortably under a safe UDP/Ethernet MTU, no fragmentation */

/* Max reliable payloads queued-or-unacknowledged per peer (send side), and the receive-side
 * reorder/hold window per peer. Must be a power of two and >= 64. */
#define PC_NET_RELIABLE_WINDOW 64

/* Message kind for pc_net_send()/PCNetEvent. See "Reliability" above. */
typedef enum PCNetMsgKind {
    PC_NET_UNRELIABLE = 0, /* fire-and-forget; may be lost, duplicated or arrive out of order */
    PC_NET_RELIABLE   = 1, /* acked + retransmitted + deduplicated + ordered per peer/direction */
} PCNetMsgKind;

/* Opaque peer handle: an index into the module's internal peer table. Never a raw socket/address. */
typedef int32_t PCNetPeerId;
#define PC_NET_INVALID_PEER   ((PCNetPeerId)-1)
#define PC_NET_BROADCAST_PEER ((PCNetPeerId)-2) /* pseudo-id for pc_net_send(): all connected peers */

typedef enum PCNetEventType {
    PC_NET_EVENT_PEER_CONNECTED,
    PC_NET_EVENT_PEER_DISCONNECTED,
    PC_NET_EVENT_DATA,
} PCNetEventType;

typedef struct PCNetEvent {
    PCNetEventType type;
    PCNetPeerId    peer;
    PCNetMsgKind   kind;                     /* meaningful when type == PC_NET_EVENT_DATA */
    uint16_t       size;                     /* meaningful when type == PC_NET_EVENT_DATA */
    uint8_t        data[PC_NET_MAX_PAYLOAD]; /* meaningful when type == PC_NET_EVENT_DATA */
} PCNetEvent;

/* Transport counters (process-global, cumulative since pc_net_init(); reset by init/shutdown).
 * Diagnostics/tests only -- nothing in the game should branch on these. */
typedef struct PCNetStats {
    uint32_t rdata_sent;                 /* reliable payloads accepted by pc_net_send (per peer) */
    uint32_t rdata_retransmits;          /* timer-driven retransmissions */
    uint32_t rdata_received;             /* RDATA datagrams received from connected peers */
    uint32_t rdata_duplicates;           /* RDATA already received/delivered (dropped, re-ACKed) */
    uint32_t rdata_out_of_window;        /* RDATA beyond the receive window (dropped, not acked) */
    uint32_t rdata_delivered;            /* reliable payloads handed to the event queue */
    uint32_t rdata_delivery_deferred;    /* times in-order delivery paused because the queue was full */
    uint32_t acks_sent;
    uint32_t acks_received;
    uint32_t acks_invalid;               /* ACKs acknowledging seqs never sent (ignored) */
    uint32_t reliable_budget_disconnects;/* peers dropped for exhausting the retransmit budget */
    uint32_t nonce_restarts;             /* same-address HELLO with a new nonce (remote restart) */
    uint32_t legacy_reliable_dropped;    /* legacy DATA(type 4) kind=RELIABLE datagrams dropped */
    uint32_t legacy_peers_refused;       /* size-0 HELLO (host) / size-0 HELLO_ACK (client) refused */
    uint32_t events_dropped_unreliable;  /* unreliable data dropped for event-queue pressure */
    uint32_t events_dropped_control;     /* CONNECTED/DISCONNECTED events lost (queue completely full) */
    uint32_t fault_dropped_rdata;        /* fault injection counters (0 unless enabled, see pc_net.c) */
    uint32_t fault_duplicated_rdata;
    uint32_t fault_reordered_rdata;
    uint32_t fault_dropped_acks;
} PCNetStats;

/* --- lifecycle --- */

/* Initializes Winsock and resets all internal state. Call once before any other pc_net_*
 * function. Returns 1 on success, 0 on failure (Winsock unavailable). */
int pc_net_init(void);

/* Closes any open socket/peers and calls WSACleanup(). Safe to call even if pc_net_init()
 * was never called or already failed. After this, pc_net_init() may be called again. */
void pc_net_shutdown(void);

/* --- host / client setup ---
 * Call at most one of these after pc_net_init(). To change role, pc_net_shutdown() then
 * pc_net_init() again first.
 */

/* Binds a UDP socket to `port` on all local interfaces and starts accepting peers. Returns 1 on
 * success, 0 on failure (e.g. the port is already in use). */
int pc_net_host_start(uint16_t port);

/* Creates a UDP socket and sends the first HELLO to host_ip:port. Returns 1 if the socket was
 * created and the first HELLO was sent, 0 on failure (bad address, socket error). This does NOT
 * mean the connection is established yet -- poll for a PC_NET_EVENT_PEER_CONNECTED event (or
 * check pc_net_is_connected()) once the host's HELLO_ACK arrives.
 *
 * If the host turns out to be a legacy (protocol v1) build, the attempt is abandoned: the client
 * logs "[pc_net] host speaks legacy transport (protocol v1); refusing", sends DISCONNECT, and the
 * event queue receives exactly ONE PC_NET_EVENT_PEER_DISCONNECTED (peer 0) with NO preceding
 * PC_NET_EVENT_PEER_CONNECTED. pc_net_is_connected() stays 0 and the socket stays open (the
 * caller should pc_net_shutdown()). A host that never answers at all produces no event: HELLO
 * is simply resent every 500 ms, as before. */
int pc_net_client_connect(const char* host_ip, uint16_t port);

/* --- per-frame polling --- */

/* Drains whatever is waiting on the OS socket buffer (never blocks, never sleeps), runs the
 * handshake/heartbeat/timeout state machine, delivers in-order reliable payloads, retransmits
 * overdue reliable payloads, sends coalesced ACKs, and appends any resulting events to the
 * internal queue. Call once per frame/tick from whichever thread owns networking. */
void pc_net_poll(void);

/* Pops the next queued event into *out. Returns 1 if an event was popped, 0 if the queue is
 * empty. Typical use: `while (pc_net_next_event(&ev)) { ...handle ev... }` right after
 * pc_net_poll(). Draining the queue every poll keeps reliable delivery flowing; a consumer that
 * stops draining eventually stalls its peers' reliable streams (and, past the retransmit budget,
 * gets them disconnected). */
int pc_net_next_event(PCNetEvent* out);

/* --- sending --- */

/* Sends `size` bytes (must be <= PC_NET_MAX_PAYLOAD) to `peer`, or to every connected peer if
 * `peer` is PC_NET_BROADCAST_PEER. Never blocks.
 *
 * PC_NET_UNRELIABLE: returns 1 if the datagram was handed to the OS for every intended
 *   recipient, 0 otherwise (unknown/disconnected peer, oversized payload, socket error).
 *   Wire bytes are exactly the Stage 0 format (8-byte header + payload).
 *
 * PC_NET_RELIABLE: returns 1 if the payload was accepted for reliable delivery (queued in the
 *   peer's send window and transmitted once immediately -- an OS-level send failure of that first
 *   transmission is repaired by the retransmit timer, not reported). Returns 0 and queues nothing
 *   if the peer is unknown/not connected, the payload is oversized, or the peer's window
 *   (PC_NET_RELIABLE_WINDOW) is full.
 *   Broadcast is ALL-OR-NOTHING: if any connected peer's window is full, nothing is queued to
 *   anyone and 0 is returned (so retrying the same broadcast can never double-deliver). Returns
 *   0 if no peer is connected. One slow peer therefore blocks reliable broadcasts to everyone;
 *   callers sending large bursts should send per peer and check pc_net_reliable_backlog().
 */
int pc_net_send(PCNetPeerId peer, PCNetMsgKind kind, const void* data, uint16_t size);

/* Number of reliable payloads currently queued or unacknowledged to `peer` (0 ..
 * PC_NET_RELIABLE_WINDOW); -1 for an invalid, broadcast, or not-connected peer. A reliable
 * send to `peer` succeeds iff this is < PC_NET_RELIABLE_WINDOW. */
int pc_net_reliable_backlog(PCNetPeerId peer);

/* Copies the cumulative transport counters into *out (zeroed on non-Windows builds). */
void pc_net_get_stats(PCNetStats* out);

/* --- peer / connection queries --- */

int pc_net_is_host(void);      /* 1 if pc_net_host_start() succeeded and hasn't been shut down */
int pc_net_is_connected(void); /* client: handshake with the host completed. host: currently listening. */
int pc_net_peer_count(void);   /* number of currently-connected peers (0 or 1 for a client) */

/* Host: milliseconds since the last packet of any kind (DATA, ACK, HEARTBEAT, ...) arrived from the CONNECTED
 * `peer`, measured at call time; -1 if `peer` is not a connected host-side peer (and always -1 on a client).
 * Read-only liveness query -- a healthy remote sends at least a heartbeat every 500 ms. No wire change. */
int pc_net_peer_idle_ms(PCNetPeerId peer);
/* Guests: the connected peer's IPv4 address (network byte order), 0 if unknown. Read-only. */
uint32_t pc_net_peer_ip(PCNetPeerId peer);

/* Host: remote-caused-style removal of a peer the game has judged unresponsive (M9 identity Stage 1B). Sends a
 * best-effort DISCONNECT notice, then ends the peer exactly like a timeout/DISCONNECT would: payloads the peer
 * already had ACKed are delivered first, the slot is wiped, and a PC_NET_EVENT_PEER_DISCONNECTED is queued so the
 * game runs its normal teardown once. Unlike pc_net_disconnect() it queues an event and does not purge
 * already-ACKed payloads. No effect on a non-connected peer or on a client. */
void pc_net_evict(PCNetPeerId peer);

/* Host: sends a DISCONNECT notice to `peer` and immediately frees its local slot (all reliable
 * state for it is discarded, any of its DATA events not yet popped are removed from the queue,
 * and no local DISCONNECTED event is queued -- the caller already knows).
 * Client: pass PC_NET_INVALID_PEER to leave the current host. */
void pc_net_disconnect(PCNetPeerId peer);

#ifdef __cplusplus
}
#endif
#endif /* PC_NET_H */
