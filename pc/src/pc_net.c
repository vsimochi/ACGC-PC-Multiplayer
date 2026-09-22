/* pc_net.c - minimal PC-native networking layer (Winsock/UDP). Stage 0: standalone foundation.
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
 *     shows up as a new HELLO, i.e. a new logical peer, exactly like a fresh connection).
 *   - Connect/disconnect detection: HELLO/HELLO_ACK establishes a peer; either a received
 *     PCNET_WIRE_DISCONNECT or PCNET_TIMEOUT_MS of silence (no packet of any kind received)
 *     ends it. A periodic PCNET_WIRE_HEARTBEAT keeps idle connections (no game data flowing)
 *     from looking dead.
 *   - No dynamic allocation anywhere in this file.
 */
#include "pc_net.h"

/* Stage 0/1 scope is explicitly Windows/Winsock only (see pc_net.h). On any other platform this
 * whole module compiles down to a safe "networking is simply unavailable" stub: every entry
 * point still exists and behaves per its documented contract (init fails, nothing else can be
 * called meaningfully afterwards), so pc_net_game.c and anything above it needs no platform
 * checks of its own -- a failed pc_net_init() already means "continue single-player." */
#ifndef _WIN32

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
int pc_net_is_host(void) { return 0; }
int pc_net_is_connected(void) { return 0; }
int pc_net_peer_count(void) { return 0; }
void pc_net_disconnect(PCNetPeerId peer) { (void)peer; }

#else /* _WIN32 */

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <string.h>

#define PCNET_MAGIC                0x41434E50u /* 'ACNP', ASCII - identifies this protocol on the wire */
#define PCNET_HEARTBEAT_INTERVAL_MS 500u
#define PCNET_TIMEOUT_MS            5000u

typedef enum PCNetWireType {
    PCNET_WIRE_HELLO      = 0, /* client -> host: "I'd like to connect" (resent until acked) */
    PCNET_WIRE_HELLO_ACK  = 1, /* host -> client: "you're in" */
    PCNET_WIRE_HEARTBEAT  = 2, /* either direction: "still here" (keeps an idle peer from timing out) */
    PCNET_WIRE_DISCONNECT = 3, /* either direction: "I'm leaving" (fast path; timeout is the fallback) */
    PCNET_WIRE_DATA       = 4, /* either direction: payload, see PCNetMsgKind in the kind field */
} PCNetWireType;

typedef struct PCNetWireHeader {
    uint32_t magic;
    uint8_t  type; /* PCNetWireType */
    uint8_t  kind; /* PCNetMsgKind, meaningful only when type == PCNET_WIRE_DATA */
    uint16_t size; /* payload byte count following this header, meaningful only for DATA */
} PCNetWireHeader;
/* Every field above is already naturally aligned (4/1/1/2 bytes at offsets 0/4/5/6), so this
 * struct is exactly 8 bytes with no compiler-inserted padding on any ABI this project targets;
 * asserted below rather than relying on that going unverified. */
_Static_assert(sizeof(PCNetWireHeader) == 8, "PCNetWireHeader must be exactly 8 bytes (wire format)");

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
} PCNetPeerSlot;

#define PC_NET_EVENT_QUEUE_CAP 64

static int            s_wsa_started = 0;
static SOCKET         s_socket = INVALID_SOCKET;
static int            s_is_host = 0;
static PCNetPeerSlot  s_peers[PC_NET_MAX_PEERS];

static PCNetEvent s_event_queue[PC_NET_EVENT_QUEUE_CAP];
static int        s_event_head  = 0; /* next slot to pop */
static int        s_event_tail  = 0; /* next slot to push */
static int        s_event_count = 0;

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

static void pcnet_push_event(const PCNetEvent* ev) {
    if (s_event_count >= PC_NET_EVENT_QUEUE_CAP) return; /* queue full: drop rather than grow/block */
    s_event_queue[s_event_tail] = *ev;
    s_event_tail = (s_event_tail + 1) % PC_NET_EVENT_QUEUE_CAP;
    s_event_count++;
}

static int pcnet_create_socket(void) {
    u_long mode = 1;
    s_socket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s_socket == INVALID_SOCKET) return 0;
    if (ioctlsocket(s_socket, FIONBIO, &mode) != 0) {
        closesocket(s_socket);
        s_socket = INVALID_SOCKET;
        return 0;
    }
    return 1;
}

static void pcnet_send_raw(const struct sockaddr_in* to, PCNetWireType type) {
    PCNetWireHeader hdr;
    hdr.magic = PCNET_MAGIC;
    hdr.type = (uint8_t)type;
    hdr.kind = 0;
    hdr.size = 0;
    sendto(s_socket, (const char*)&hdr, (int)sizeof(hdr), 0, (const struct sockaddr*)to, (int)sizeof(*to));
}

int pc_net_init(void) {
    WSADATA wsa;
    if (s_wsa_started) return 1;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return 0;
    s_wsa_started = 1;
    s_socket = INVALID_SOCKET;
    s_is_host = 0;
    memset(s_peers, 0, sizeof(s_peers));
    s_event_head = s_event_tail = s_event_count = 0;
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
    s_is_host = 0;
    memset(s_peers, 0, sizeof(s_peers));
    s_event_head = s_event_tail = s_event_count = 0;
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
    s_peers[0].state = PCNET_PEER_PENDING;
    s_peers[0].addr = addr;
    s_peers[0].last_recv_tick = GetTickCount();
    s_peers[0].last_send_tick = GetTickCount();
    pcnet_send_raw(&addr, PCNET_WIRE_HELLO);
    return 1;
}

static void pcnet_handle_packet(const struct sockaddr_in* from, const uint8_t* buf, int len) {
    PCNetWireHeader hdr;
    PCNetPeerId pid;
    PCNetEvent ev;
    uint32_t now;

    if (len < (int)sizeof(PCNetWireHeader)) return; /* too short to be one of ours; ignore */
    memcpy(&hdr, buf, sizeof(hdr));
    if (hdr.magic != PCNET_MAGIC) return; /* not our protocol (stray UDP traffic); ignore silently */

    now = GetTickCount();
    pid = pcnet_find_peer(from);

    switch ((PCNetWireType)hdr.type) {
        case PCNET_WIRE_HELLO:
            if (!s_is_host) break; /* clients never accept new peers */
            if (pid == PC_NET_INVALID_PEER) {
                pid = pcnet_find_free_slot();
                if (pid == PC_NET_INVALID_PEER) break; /* peer table full: drop the new connection attempt */
                s_peers[pid].state = PCNET_PEER_CONNECTED;
                s_peers[pid].addr = *from;
                s_peers[pid].last_recv_tick = now;
                s_peers[pid].last_send_tick = 0;
                ev.type = PC_NET_EVENT_PEER_CONNECTED;
                ev.peer = pid;
                pcnet_push_event(&ev);
            } else {
                s_peers[pid].last_recv_tick = now; /* duplicate HELLO (our ACK was likely lost); just refresh */
            }
            pcnet_send_raw(from, PCNET_WIRE_HELLO_ACK);
            break;

        case PCNET_WIRE_HELLO_ACK:
            if (s_is_host) break; /* only a client expects this */
            if (s_peers[0].state == PCNET_PEER_PENDING && pcnet_addr_eq(&s_peers[0].addr, from)) {
                s_peers[0].state = PCNET_PEER_CONNECTED;
                s_peers[0].last_recv_tick = now;
                ev.type = PC_NET_EVENT_PEER_CONNECTED;
                ev.peer = 0;
                pcnet_push_event(&ev);
            }
            break;

        case PCNET_WIRE_HEARTBEAT:
            if (pid != PC_NET_INVALID_PEER) s_peers[pid].last_recv_tick = now;
            break;

        case PCNET_WIRE_DISCONNECT:
            if (pid != PC_NET_INVALID_PEER) {
                s_peers[pid].state = PCNET_PEER_FREE;
                ev.type = PC_NET_EVENT_PEER_DISCONNECTED;
                ev.peer = pid;
                pcnet_push_event(&ev);
            }
            break;

        case PCNET_WIRE_DATA:
            if (pid == PC_NET_INVALID_PEER) break; /* data from an address we don't recognize; ignore */
            s_peers[pid].last_recv_tick = now;
            if (hdr.size > PC_NET_MAX_PAYLOAD || (int)(sizeof(hdr) + hdr.size) > len) break; /* malformed */
            ev.type = PC_NET_EVENT_DATA;
            ev.peer = pid;
            ev.kind = (PCNetMsgKind)hdr.kind;
            ev.size = hdr.size;
            memcpy(ev.data, buf + sizeof(hdr), hdr.size);
            pcnet_push_event(&ev);
            break;

        default:
            break; /* unknown/future type on the wire; ignore rather than misinterpret */
    }
}

void pc_net_poll(void) {
    uint8_t buf[sizeof(PCNetWireHeader) + PC_NET_MAX_PAYLOAD];
    struct sockaddr_in from;
    int fromlen, n, i;
    uint32_t now;

    if (s_socket == INVALID_SOCKET) return;

    /* Drain every datagram currently waiting; recvfrom on a non-blocking socket returns
     * WSAEWOULDBLOCK the instant nothing is left, so this loop always terminates promptly. */
    for (;;) {
        fromlen = (int)sizeof(from);
        n = recvfrom(s_socket, (char*)buf, (int)sizeof(buf), 0, (struct sockaddr*)&from, &fromlen);
        if (n == SOCKET_ERROR) {
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
                pcnet_send_raw(&s_peers[i].addr, PCNET_WIRE_HELLO);
                s_peers[i].last_send_tick = now;
            }
            continue;
        }

        /* PCNET_PEER_CONNECTED */
        if (now - s_peers[i].last_recv_tick > PCNET_TIMEOUT_MS) {
            PCNetEvent ev;
            s_peers[i].state = PCNET_PEER_FREE;
            ev.type = PC_NET_EVENT_PEER_DISCONNECTED;
            ev.peer = (PCNetPeerId)i;
            pcnet_push_event(&ev);
            continue;
        }
        if (now - s_peers[i].last_send_tick > PCNET_HEARTBEAT_INTERVAL_MS) {
            pcnet_send_raw(&s_peers[i].addr, PCNET_WIRE_HEARTBEAT);
            s_peers[i].last_send_tick = now;
        }
    }
}

int pc_net_next_event(PCNetEvent* out) {
    if (s_event_count == 0 || out == NULL) return 0;
    *out = s_event_queue[s_event_head];
    s_event_head = (s_event_head + 1) % PC_NET_EVENT_QUEUE_CAP;
    s_event_count--;
    return 1;
}

static int pcnet_send_to_slot(int i, PCNetMsgKind kind, const void* data, uint16_t size) {
    uint8_t buf[sizeof(PCNetWireHeader) + PC_NET_MAX_PAYLOAD];
    PCNetWireHeader hdr;
    int total;

    if (s_peers[i].state != PCNET_PEER_CONNECTED) return 0;

    hdr.magic = PCNET_MAGIC;
    hdr.type = (uint8_t)PCNET_WIRE_DATA;
    hdr.kind = (uint8_t)kind;
    hdr.size = size;
    memcpy(buf, &hdr, sizeof(hdr));
    if (size) memcpy(buf + sizeof(hdr), data, size);
    total = (int)(sizeof(hdr) + size);

    if (sendto(s_socket, (const char*)buf, total, 0, (const struct sockaddr*)&s_peers[i].addr,
               (int)sizeof(s_peers[i].addr)) == SOCKET_ERROR) {
        return 0;
    }
    s_peers[i].last_send_tick = GetTickCount();
    return 1;
}

int pc_net_send(PCNetPeerId peer, PCNetMsgKind kind, const void* data, uint16_t size) {
    if (s_socket == INVALID_SOCKET) return 0;
    if (size > PC_NET_MAX_PAYLOAD) return 0;
    if (size != 0 && data == NULL) return 0;

    if (peer == PC_NET_BROADCAST_PEER) {
        int i, ok = 1, sent_any = 0;
        for (i = 0; i < PC_NET_MAX_PEERS; i++) {
            if (s_peers[i].state != PCNET_PEER_CONNECTED) continue;
            sent_any = 1;
            if (!pcnet_send_to_slot(i, kind, data, size)) ok = 0;
        }
        return sent_any ? ok : 0;
    }

    if (peer < 0 || peer >= PC_NET_MAX_PEERS) return 0;
    return pcnet_send_to_slot((int)peer, kind, data, size);
}

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
    pcnet_send_raw(&s_peers[idx].addr, PCNET_WIRE_DISCONNECT);
    s_peers[idx].state = PCNET_PEER_FREE;
}

#endif /* _WIN32 */
