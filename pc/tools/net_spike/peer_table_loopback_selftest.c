/* peer_table_loopback_selftest.c - drives the REAL transport (pc/src/pc_net.c, #included so the test can look at its tables) over 127.0.0.1 with FAKE clients that speak the wire
 * format from plain UDP sockets: capacity phase 2 (dynamic peer table). Built natively on Linux with the Winsock shim in tools/net_spike/winshim (-D_WIN32 -Iwinshim); the Windows
 * game build uses the real Winsock. This is a transport test, NOT a game test: no identity handshake, no game state.
 * Covers: capacity 8 behaves exactly like the old fixed table (ids 0..7, the 9th refused); capacity 20 skips the host's id 8; the maximum capacity 254 (ids 0..254 minus 8, never 0xFF)
 * with the event queue sized for it; refusal of a NEW address when full (DISCONNECT notice + stat) and reuse of freed ids; reliable / unreliable / broadcast / heartbeat for ids above
 * 8; nonce restart; client mode; bind failure frees the tables; restart with another capacity.
 * Usage: peer_table_loopback_selftest   (prints PASS:/FAIL: and "RESULT passed=N failed=M") */
#include "../../src/pc_net.c"

#include <poll.h>

static int s_pass, s_fail;
static void check(const char* what, int ok) {
    printf("%s: %s\n", ok ? "PASS" : "FAIL", what);
    fflush(stdout);
    if (ok) s_pass++; else s_fail++;
}

#define HOST_ID 8
#define MAXF 300

typedef struct Fake {
    int      fd;
    uint16_t port;
    uint32_t nonce;
    int      got_hello_ack, got_disconnect, heartbeats, rdata, rdata_size;
    uint32_t rdata_seq;
    int      data, data_size;
    uint8_t  data_first;
} Fake;
static Fake s_f[MAXF];
static int s_nf;
static struct sockaddr_in s_host;

static void sleep_ms(int ms) {
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
}

static int fake_new(void) {
    struct sockaddr_in a;
    socklen_t l = sizeof(a);
    Fake* f = &s_f[s_nf];
    memset(f, 0, sizeof(*f));
    f->fd = socket(AF_INET, SOCK_DGRAM, 0);
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (f->fd < 0 || bind(f->fd, (struct sockaddr*)&a, sizeof(a)) != 0) return -1;
    getsockname(f->fd, (struct sockaddr*)&a, &l);
    f->port = ntohs(a.sin_port);
    fcntl(f->fd, F_SETFL, fcntl(f->fd, F_GETFL, 0) | O_NONBLOCK);
    f->nonce = 0x1000u + (uint32_t)s_nf;
    return s_nf++;
}

static void fake_send(int k, uint8_t type, uint8_t kind, const void* body, int blen) {
    uint8_t buf[1100];
    PCNetWireHeader h;
    h.magic = PCNET_MAGIC;
    h.type = type;
    h.kind = kind;
    h.size = (uint16_t)blen;
    memcpy(buf, &h, sizeof(h));
    if (blen) memcpy(buf + sizeof(h), body, (size_t)blen);
    sendto(s_f[k].fd, buf, sizeof(h) + (size_t)blen, 0, (struct sockaddr*)&s_host, sizeof(s_host));
}
static void fake_hello(int k) { fake_send(k, PCNET_WIRE_HELLO, 0, &s_f[k].nonce, 4); }

static void fake_drain(int k) {
    uint8_t buf[1100];
    for (;;) {
        int n = (int)recv(s_f[k].fd, buf, sizeof(buf), 0);
        PCNetWireHeader h;
        if (n < (int)sizeof(h)) return;
        memcpy(&h, buf, sizeof(h));
        if (h.magic != PCNET_MAGIC) continue;
        switch (h.type) {
            case PCNET_WIRE_HELLO_ACK: s_f[k].got_hello_ack = 1; break;
            case PCNET_WIRE_DISCONNECT: s_f[k].got_disconnect = 1; break;
            case PCNET_WIRE_HEARTBEAT: s_f[k].heartbeats++; break;
            case PCNET_WIRE_RDATA:
                s_f[k].rdata++;
                memcpy(&s_f[k].rdata_seq, buf + sizeof(h), 4);
                s_f[k].rdata_size = h.size;
                break;
            case PCNET_WIRE_DATA:
                s_f[k].data++;
                s_f[k].data_size = h.size;
                s_f[k].data_first = h.size ? buf[sizeof(h)] : 0;
                break;
            default: break;
        }
    }
}

/* events collected from the host */
typedef struct Ev { PCNetEventType type; PCNetPeerId peer; int size; uint8_t first; } Ev;
static Ev s_ev[2000];
static int s_nev;
static void collect(void) {
    PCNetEvent e;
    while (pc_net_next_event(&e) && s_nev < 2000) {
        s_ev[s_nev].type = e.type;
        s_ev[s_nev].peer = e.peer;
        s_ev[s_nev].size = e.size;
        s_ev[s_nev].first = e.size ? e.data[0] : 0;
        s_nev++;
    }
}
static void pump(int ms, int drain_events) {
    int t, k;
    for (t = 0; t < ms; t += 2) {
        pc_net_poll();
        if (drain_events) collect();
        for (k = 0; k < s_nf; k++) fake_drain(k);
        sleep_ms(2);
    }
}
static int count_ev(PCNetEventType t, int peer) {
    int i, n = 0;
    for (i = 0; i < s_nev; i++) if (s_ev[i].type == t && (peer < 0 || s_ev[i].peer == peer)) n++;
    return n;
}
static void reset_fakes(void) {
    int k;
    for (k = 0; k < s_nf; k++) close(s_f[k].fd);
    s_nf = 0;
    s_nev = 0;
}

static uint16_t s_port;
static int host_up(int capacity) {
    memset(&s_host, 0, sizeof(s_host));
    s_host.sin_family = AF_INET;
    s_host.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    s_host.sin_port = htons(s_port);
    return pc_net_init() && pc_net_set_peer_capacity(capacity) && pc_net_host_start(s_port);
}
static void host_down(void) {
    pc_net_shutdown();
    reset_fakes();
}

/* connect `n` fakes one by one (a HELLO, then pumps until it was answered) and return how many got a slot */
static int connect_n(int n, int drain_events) {
    int i, ok = 0;
    for (i = 0; i < n; i++) {
        int k = fake_new();
        fake_hello(k);
        pump(6, drain_events);
        if (s_f[k].got_hello_ack) ok++;
    }
    return ok;
}

int main(void) {
    int i, k, ok;
    s_port = (uint16_t)(30000 + (getpid() % 20000));

    /* ============ capacity 8: exactly the old fixed table ============ */
    check("capacity API: 0, 255 and negative are refused before any socket", pc_net_init() && !pc_net_set_peer_capacity(0) && !pc_net_set_peer_capacity(255) && !pc_net_set_peer_capacity(-1) && pc_net_set_peer_capacity(8));
    pc_net_shutdown();
    check("host starts at capacity 8", host_up(8));
    check("capacity 8: span 8, capacity 8, no hole", pc_net_peer_capacity() == 8 && pc_net_peer_span() == 8 && s_peer_span == 8);
    check("capacity 8: the event queue is the old 128 / 112 / 80", s_event_cap == 128 && s_event_limit_reliable == 112 && s_event_limit_unreliable == 80);
    check("a capacity change is refused while the socket is open", !pc_net_set_peer_capacity(20));
    check("8 fakes connect and each gets a HELLO_ACK", connect_n(8, 1) == 8);
    ok = 1;
    for (i = 0; i < 8; i++) ok = ok && s_ev[i].type == PC_NET_EVENT_PEER_CONNECTED && s_ev[i].peer == i;
    check("capacity 8: CONNECTED events carry ids 0..7 in order (identical to the old table)", ok && s_nev == 8 && pc_net_peer_count() == 8);
    k = fake_new();
    fake_hello(k);
    pump(20, 1);
    check("the 9th address is refused: a DISCONNECT notice, no HELLO_ACK, no event, no slot", s_f[k].got_disconnect && !s_f[k].got_hello_ack && s_nev == 8 && pc_net_peer_count() == 8);
    {
        PCNetStats st;
        pc_net_get_stats(&st);
        check("peer_table_full_refused counts it", st.peer_table_full_refused >= 1);
    }
    fake_send(2, PCNET_WIRE_DISCONNECT, 0, NULL, 0);
    pump(20, 1);
    check("a fake leaves: PEER_DISCONNECTED for exactly id 2, slot freed", count_ev(PC_NET_EVENT_PEER_DISCONNECTED, 2) == 1 && pc_net_peer_count() == 7);
    s_f[k].got_disconnect = 0;
    fake_hello(k);
    pump(20, 1);
    check("the refused address retries and takes the FREED id 2 (no leaked slot)", s_f[k].got_hello_ack && count_ev(PC_NET_EVENT_PEER_CONNECTED, 2) == 2 && pc_net_peer_count() == 8);
    /* reliable to id 5 */
    {
        const uint8_t msg[3] = { 0xAA, 0xBB, 0xCC };
        PCNetWireAck ack;
        check("reliable send to id 5 is accepted", pc_net_send(5, PC_NET_RELIABLE, msg, 3) == 1);
        pump(30, 1);
        check("fake 5 got that RDATA (seq 0, 3 bytes) and the backlog is 1 until it ACKs", s_f[5].rdata == 1 && s_f[5].rdata_seq == 0 && s_f[5].rdata_size == 3 && pc_net_reliable_backlog(5) == 1);
        ack.next_expected = 1;
        ack.sack_bits = 0;
        fake_send(5, PCNET_WIRE_ACK, 0, &ack, 8);
        pump(20, 1);
        check("after the ACK the backlog is 0", pc_net_reliable_backlog(5) == 0);
    }
    for (i = 0; i < s_nf; i++) s_f[i].data = 0;
    check("an unreliable broadcast reaches every connected fake", pc_net_send(PC_NET_BROADCAST_PEER, PC_NET_UNRELIABLE, "x", 1) == 1 && (pump(20, 1), 1));
    ok = 1; /* fake 2 left and fake 8 took its id: the 8 connected fakes are every index but 2 */
    for (i = 0; i < s_nf; i++) if (i != 2) ok = ok && s_f[i].data == 1 && s_f[i].data_first == 'x';
    check("...each of the 8 got exactly one (the departed fake none)", ok && s_f[2].data == 0 && s_nf == 9);
    {
        const uint8_t dat = 0x5A;
        int before = s_nev;
        fake_send(6, PCNET_WIRE_DATA, PC_NET_UNRELIABLE, &dat, 1);
        pump(20, 1);
        check("an unreliable DATA from fake 6 is delivered as an event from peer 6", s_nev == before + 1 && s_ev[before].type == PC_NET_EVENT_DATA && s_ev[before].peer == 6 && s_ev[before].first == 0x5A);
    }
    for (i = 0; i < s_nf; i++) s_f[i].heartbeats = 0;
    pump(700, 1);
    ok = 1;
    for (i = 0; i < s_nf; i++) if (i != 2) ok = ok && s_f[i].heartbeats >= 1;
    check("after 700 ms of silence the host heartbeats every connected peer", ok);
    {
        int before = s_nev;
        s_f[3].nonce = 0x7777u;
        fake_hello(3);
        pump(20, 1);
        check("a HELLO with a new nonce from peer 3's address replaces it in place: DISCONNECTED(3) then CONNECTED(3), no other slot used",
              s_nev == before + 2 && s_ev[before].type == PC_NET_EVENT_PEER_DISCONNECTED && s_ev[before].peer == 3 && s_ev[before + 1].type == PC_NET_EVENT_PEER_CONNECTED && s_ev[before + 1].peer == 3 && pc_net_peer_count() == 8);
    }
    pc_net_disconnect(7);
    check("pc_net_disconnect frees the slot immediately (host side)", pc_net_peer_count() == 7 && pc_net_reliable_backlog(7) == -1);
    check("out-of-range ids are refused by every API", pc_net_send(8, PC_NET_RELIABLE, "x", 1) == 0 && pc_net_send(300, PC_NET_UNRELIABLE, "x", 1) == 0 && pc_net_reliable_backlog(-1) == -1 && pc_net_peer_idle_ms(99) == -1 && pc_net_peer_ip(99) == 0);
    host_down();

    /* ============ capacity 20: ids 0..7 then 9..20 ============ */
    check("host starts at capacity 20", host_up(20) && pc_net_peer_capacity() == 20 && pc_net_peer_span() == 21);
    check("capacity 20: event queue grows with the capacity (2 x peers + 112), unreliable room stays 80", s_event_cap == 2 * 20 + 112 && s_event_limit_unreliable == 80);
    check("20 fakes connect", connect_n(20, 1) == 20);
    ok = 1;
    for (i = 0; i < 8; i++) ok = ok && s_ev[i].peer == i;
    for (i = 8; i < 20; i++) ok = ok && s_ev[i].peer == i + 1;
    check("ids are 0..7 then 9..20: the host's id 8 is NEVER assigned", ok && count_ev(PC_NET_EVENT_PEER_CONNECTED, HOST_ID) == 0 && s_peers[HOST_ID].state == PCNET_PEER_FREE);
    k = fake_new();
    fake_hello(k);
    pump(20, 1);
    check("the 21st address is refused with a DISCONNECT notice", s_f[k].got_disconnect && !s_f[k].got_hello_ack && pc_net_peer_count() == 20);
    {
        const uint8_t msg[2] = { 1, 2 };
        PCNetWireAck ack;
        ok = pc_net_send(9, PC_NET_RELIABLE, msg, 2) && pc_net_send(20, PC_NET_RELIABLE, msg, 2);
        pump(30, 1);
        check("reliable sends to ids 9 and 20 (above the old table) arrive with their own sequence space", ok && s_f[9 - 1].rdata == 1 && s_f[20 - 1].rdata == 1 && s_f[9 - 1].rdata_seq == 0 && s_f[20 - 1].rdata_seq == 0);
        ack.next_expected = 1;
        ack.sack_bits = 0;
        fake_send(20 - 1, PCNET_WIRE_ACK, 0, &ack, 8);
        pump(20, 1);
        check("an ACK from id 20 clears only id 20's backlog", pc_net_reliable_backlog(20) == 0 && pc_net_reliable_backlog(9) == 1);
    }
    {
        const uint8_t dat = 0x33;
        int before = s_nev;
        fake_send(15 - 1, PCNET_WIRE_DATA, PC_NET_UNRELIABLE, &dat, 1); /* fake index 14 holds id 15 */
        pump(20, 1);
        check("DATA from the fake holding id 15 arrives as an event from peer 15", s_nev == before + 1 && s_ev[before].peer == 15 && s_ev[before].first == 0x33);
    }
    {
        int before = s_nev;
        fake_send(8, PCNET_WIRE_DISCONNECT, 0, NULL, 0); /* fake index 8 holds id 9 */
        pump(20, 1);
        check("id 9 leaves", s_nev == before + 1 && s_ev[before].type == PC_NET_EVENT_PEER_DISCONNECTED && s_ev[before].peer == 9);
        k = fake_new();
        fake_hello(k);
        pump(20, 1);
        check("a new address gets id 9 (not the host's 8): the freed id is reused", s_f[k].got_hello_ack && count_ev(PC_NET_EVENT_PEER_CONNECTED, 9) == 2 && count_ev(PC_NET_EVENT_PEER_CONNECTED, HOST_ID) == 0);
    }
    {
        PCNetStats st;
        pc_net_get_stats(&st);
        check("no control event was dropped", st.events_dropped_control == 0);
    }
    host_down();

    /* ============ maximum capacity 254 ============ */
    check("host starts at the maximum capacity 254 (span 255)", host_up(254) && pc_net_peer_capacity() == 254 && pc_net_peer_span() == 255);
    check("capacity 254: event queue 2 x 254 + 112 entries", s_event_cap == 2 * 254 + 112 && s_event_limit_unreliable == 80 && s_event_limit_reliable == s_event_cap - 508);
    /* connect all 254 WITHOUT draining events: the control events of every peer must fit */
    check("254 fakes connect (events NOT drained meanwhile)", connect_n(254, 0) == 254 && pc_net_peer_count() == 254);
    for (i = 0; i < 10; i++) {
        int kk = fake_new();
        fake_hello(kk);
    }
    pump(60, 0);
    ok = 1;
    for (i = 254; i < 264; i++) ok = ok && s_f[i].got_disconnect && !s_f[i].got_hello_ack;
    {
        PCNetStats st;
        pc_net_get_stats(&st);
        check("10 more addresses are all refused (DISCONNECT notice each), nothing corrupted", ok && pc_net_peer_count() == 254 && st.peer_table_full_refused >= 10);
        check("not one CONNECTED event was lost with 254 undrained (the old fixed 128-entry queue could not hold them)", st.events_dropped_control == 0);
    }
    collect();
    {
        unsigned char seen[256];
        int max_id = -1, n_conn = 0;
        memset(seen, 0, sizeof(seen));
        for (i = 0; i < s_nev; i++) {
            if (s_ev[i].type != PC_NET_EVENT_PEER_CONNECTED) continue;
            n_conn++;
            if (s_ev[i].peer > max_id) max_id = s_ev[i].peer;
            if (s_ev[i].peer >= 0 && s_ev[i].peer < 256) seen[s_ev[i].peer]++;
        }
        ok = 1;
        for (i = 0; i < 255; i++) ok = ok && seen[i] == (i == HOST_ID ? 0 : 1);
        check("254 CONNECTED events: every id 0..254 except 8 exactly once, highest id 254, 255 never", n_conn == 254 && ok && max_id == 254 && !seen[255]);
    }
    {
        const uint8_t msg[1] = { 9 };
        check("reliable send to the highest id (254) works, a send to 255 / 8 is refused", pc_net_send(254, PC_NET_RELIABLE, msg, 1) == 1 && pc_net_send(255, PC_NET_RELIABLE, msg, 1) == 0 && pc_net_send(HOST_ID, PC_NET_RELIABLE, msg, 1) == 0);
        pump(30, 1);
        ok = 1;
        for (i = 0; i < 254; i++) ok = ok && ((s_f[i].rdata == 1) == (i == 253)); /* fake index 253 holds id 254 */
        check("only the fake holding id 254 received it", ok);
    }
    for (i = 0; i < s_nf; i++) s_f[i].data = 0;
    ok = pc_net_send(PC_NET_BROADCAST_PEER, PC_NET_UNRELIABLE, "y", 1) == 1;
    pump(60, 1);
    for (i = 0; i < 254; i++) ok = ok && s_f[i].data == 1;
    for (i = 254; i < s_nf; i++) ok = ok && s_f[i].data == 0;
    check("...each connected fake got exactly one and the 10 refused got none", ok);
    for (i = 0; i < 254; i += 2) fake_send(i, PCNET_WIRE_DISCONNECT, 0, NULL, 0);
    pump(100, 1);
    check("127 peers leave at once: 127 left, every slot freed", pc_net_peer_count() == 127);
    ok = connect_n(0, 1) == 0;
    k = fake_new();
    fake_hello(k);
    pump(20, 1);
    check("a refused address now gets the lowest freed id (0)", s_f[k].got_hello_ack && count_ev(PC_NET_EVENT_PEER_CONNECTED, 0) == 2 && ok);
    host_down();

    /* ============ bind failure frees the tables; restart with another capacity ============ */
    {
        int blocker = socket(AF_INET, SOCK_DGRAM, 0);
        struct sockaddr_in a;
        memset(&a, 0, sizeof(a));
        a.sin_family = AF_INET;
        a.sin_addr.s_addr = htonl(INADDR_ANY);
        a.sin_port = htons((uint16_t)(s_port + 1));
        check("setup: a blocker socket holds the port", bind(blocker, (struct sockaddr*)&a, sizeof(a)) == 0);
        check("host_start fails on a busy port", pc_net_init() && pc_net_set_peer_capacity(30) && !pc_net_host_start((uint16_t)(s_port + 1)));
        check("...and leaves NO tables behind (span 0, nothing allocated)", s_peers == NULL && s_tx == NULL && s_rx == NULL && s_event_queue == NULL && s_peer_span == 0 && pc_net_peer_span() == 0 && pc_net_peer_capacity() == 0);
        close(blocker);
        pc_net_shutdown();
    }
    check("restart at capacity 3", host_up(3) && pc_net_peer_span() == 3 && s_event_cap == 128);
    check("capacity 3: 3 connect, the 4th is refused", connect_n(3, 1) == 3 && (k = fake_new(), fake_hello(k), pump(20, 1), s_f[k].got_disconnect && !s_f[k].got_hello_ack));
    host_down();
    check("after shutdown nothing is open or allocated", pc_net_peer_span() == 0 && pc_net_peer_capacity() == 0 && s_peers == NULL && s_event_queue == NULL && pc_net_peer_count() == 0);

    /* ============ client mode: one slot, the old event tiers ============ */
    {
        struct sockaddr_in a, from;
        socklen_t l = sizeof(a);
        int fl = (int)sizeof(from), n, tries;
        uint8_t buf[64];
        PCNetWireHeader hh;
        uint32_t nonce = 0;
        PCNetEvent e;
        int got_connected = 0;
        int srv = socket(AF_INET, SOCK_DGRAM, 0);
        memset(&a, 0, sizeof(a));
        a.sin_family = AF_INET;
        a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        bind(srv, (struct sockaddr*)&a, sizeof(a));
        getsockname(srv, (struct sockaddr*)&a, &l);
        fcntl(srv, F_SETFL, fcntl(srv, F_GETFL, 0) | O_NONBLOCK);
        check("client connects (allocates exactly one slot)", pc_net_init() && pc_net_client_connect("127.0.0.1", ntohs(a.sin_port)) && pc_net_peer_span() == 1 && pc_net_peer_capacity() == 1);
        check("client: the event queue is the old 128 / 112 / 80 and slot 0 has its windows", s_event_cap == 128 && s_event_limit_reliable == 112 && s_event_limit_unreliable == 80 && s_tx[0] != NULL && s_rx[0] != NULL);
        n = -1;
        for (tries = 0; tries < 50 && n < (int)sizeof(hh); tries++) {
            sleep_ms(4);
            n = (int)recvfrom(srv, (char*)buf, (int)sizeof(buf), 0, (struct sockaddr*)&from, &fl);
        }
        memcpy(&hh, buf, sizeof(hh));
        check("the client sent a HELLO with a nonce", n >= (int)sizeof(hh) + 4 && hh.magic == PCNET_MAGIC && hh.type == PCNET_WIRE_HELLO && hh.size == 4);
        memcpy(&nonce, buf + sizeof(hh), 4);
        hh.type = PCNET_WIRE_HELLO_ACK;
        memcpy(buf, &hh, sizeof(hh));
        sendto(srv, buf, sizeof(hh) + 4, 0, (struct sockaddr*)&from, sizeof(from));
        for (tries = 0; tries < 25 && !got_connected; tries++) {
            sleep_ms(4);
            pc_net_poll();
            while (pc_net_next_event(&e)) got_connected |= e.type == PC_NET_EVENT_PEER_CONNECTED && e.peer == 0;
        }
        check("the HELLO_ACK connects the client (PEER_CONNECTED peer 0)", got_connected && pc_net_is_connected() && pc_net_peer_count() == 1);
        check("a client cannot be asked for a host capacity", pc_net_peer_capacity() == 1);
        pc_net_shutdown();
        close(srv);
    }
    check("client shutdown frees everything", s_peers == NULL && s_tx == NULL && s_event_queue == NULL && s_peer_span == 0);

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail == 0 ? 0 : 1;
}
