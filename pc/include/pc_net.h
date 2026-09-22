/* pc_net.h - minimal PC-native networking layer (Winsock/UDP). Stage 0: standalone foundation.
 *
 * This is a brand-new PC-only module. It is NOT wired into the game loop yet -- nothing in
 * src/ or pc/src/pc_main.c calls into this header. It exists to be built and exercised by a
 * standalone test harness (pc/tools/net_spike/) before any gameplay code depends on it, the
 * same way pc_lowaddr.c/pc_lowaddr_selftest.cpp were proven standalone before being trusted.
 *
 * Design summary
 * ---------------
 *   - Transport is a single UDP socket. One process is either idle, "hosting" (can accept
 *     multiple peers), or a "client" (connects to exactly one host). Call pc_net_host_start()
 *     or pc_net_client_connect() at most once between pc_net_init()/pc_net_shutdown().
 *   - UDP has no real connection state, so a tiny HELLO / HELLO_ACK handshake plus periodic
 *     heartbeats let each side detect "peer connected" and "peer disconnected (timed out or
 *     said goodbye)" without inventing anything heavier.
 *   - Two message kinds exist, PC_NET_UNRELIABLE and PC_NET_RELIABLE, so callers can already
 *     express intent (e.g. position updates vs. inventory changes). PC_NET_UNRELIABLE is a
 *     complete, real plain-UDP send. PC_NET_RELIABLE is *not* a full ack/retransmit/ordering
 *     protocol yet -- see the comment on pc_net_send() below. That is deliberately left as a
 *     documented placeholder for a later stage rather than a half-working implementation.
 *   - No dynamic allocation on the packet path: a fixed-size peer table and a fixed-size event
 *     ring buffer are the module's only state, all owned internally (never exposed).
 *   - This header never includes <windows.h>/winsock. No Windows-specific type appears here
 *     or needs to appear in any game header that might one day include this one.
 *   - No thread is created by this module. Call pc_net_poll() from whatever thread should
 *     drive networking (intended to eventually be the main/game thread, once integrated).
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

/* Message kind for pc_net_send()/PCNetEvent. See the reliability note on pc_net_send(). */
typedef enum PCNetMsgKind {
    PC_NET_UNRELIABLE = 0, /* fire-and-forget; may be lost or arrive out of order */
    PC_NET_RELIABLE   = 1, /* wire-tagged for a future ack/retransmit/order layer; see pc_net_send() */
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
 * check pc_net_is_connected()) once the host's HELLO_ACK arrives. */
int pc_net_client_connect(const char* host_ip, uint16_t port);

/* --- per-frame polling --- */

/* Drains whatever is waiting on the OS socket buffer (never blocks, never sleeps), runs the
 * handshake/heartbeat/timeout state machine, and appends any resulting events to the internal
 * queue. Call once per frame/tick from whichever thread owns networking. */
void pc_net_poll(void);

/* Pops the next queued event into *out. Returns 1 if an event was popped, 0 if the queue is
 * empty. Typical use: `while (pc_net_next_event(&ev)) { ...handle ev... }` right after
 * pc_net_poll(). */
int pc_net_next_event(PCNetEvent* out);

/* --- sending ---
 *
 * Reliability note: PC_NET_RELIABLE currently behaves identically to PC_NET_UNRELIABLE on the
 * wire (a single UDP datagram, no ack, no retransmit, no ordering guarantee) -- only the wire
 * header's kind tag differs. This is intentional for Stage 0: implementing real reliability
 * (sequence numbers, ack tracking, retransmit timers, resequencing on the receive side) is
 * substantial, dedicated work that belongs in its own later stage once the rest of the
 * transport is proven. Do not depend on PC_NET_RELIABLE actually being reliable yet.
 */

/* Sends `size` bytes (must be <= PC_NET_MAX_PAYLOAD) to `peer`, or to every connected peer if
 * `peer` is PC_NET_BROADCAST_PEER. Returns 1 if handed to the OS successfully for every
 * intended recipient, 0 on failure (unknown/disconnected peer, oversized payload, socket
 * error). Never blocks. */
int pc_net_send(PCNetPeerId peer, PCNetMsgKind kind, const void* data, uint16_t size);

/* --- peer / connection queries --- */

int pc_net_is_host(void);      /* 1 if pc_net_host_start() succeeded and hasn't been shut down */
int pc_net_is_connected(void); /* client: handshake with the host completed. host: currently listening. */
int pc_net_peer_count(void);   /* number of currently-connected peers (0 or 1 for a client) */

/* Host: sends a DISCONNECT notice to `peer` and immediately frees its local slot.
 * Client: pass PC_NET_INVALID_PEER to leave the current host. */
void pc_net_disconnect(PCNetPeerId peer);

#ifdef __cplusplus
}
#endif
#endif /* PC_NET_H */
