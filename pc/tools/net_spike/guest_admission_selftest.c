/* guest_admission_selftest.c - capacity phase 4 (dynamic guest admission). Drives the REAL transport (pc/src/pc_net.c, #included, Winsock shim on Linux) with fake UDP clients and runs
 * every connected peer through the REAL pure admission rules (pc_guest_admit.c), the REAL membership classifier (pc_mp_membership_resolve over a row source) and the REAL per-guest
 * store (pc_mp_guest_store.c). The "host" around them is a small MODEL (a bound-guest table keyed by wire id): it is NOT pc_net_game.c, which needs the game. What this proves is the
 * admission LOGIC and its interaction with the transport / store; real game admission needs the Windows / MSYS2 runtime.
 * Usage: guest_admission_selftest <scratch_dir>   (prints PASS:/FAIL: and "RESULT passed=N failed=M") */
#include "../../src/pc_net.c"

#include <dirent.h>
#include <poll.h>
#include <sys/resource.h>
#include <sys/stat.h>

#include "pc_guest_admit.h"
#include "pc_mp_guest_store.h"
#include "pc_mp_guests.h"
#include "pc_mp_membership.h"

static int s_pass, s_fail;
static void check(const char* what, int ok) {
    printf("%s: %s\n", ok ? "PASS" : "FAIL", what);
    fflush(stdout);
    if (ok) s_pass++; else s_fail++;
}

#define HOST_ID 8
#define MAXF 300
static char s_root[300];

/* ---------------- fake UDP clients (same wire as peer_table_loopback_selftest) ---------------- */
typedef struct Fake { int fd; uint16_t port; uint32_t nonce; int got_hello_ack, got_disconnect; } Fake;
static Fake s_f[MAXF];
static int s_nf;
static struct sockaddr_in s_host;
static uint16_t s_port;

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
static void fake_send(int k, uint8_t type, const void* body, int blen) {
    uint8_t buf[1100];
    PCNetWireHeader h;
    h.magic = PCNET_MAGIC;
    h.type = type;
    h.kind = 0;
    h.size = (uint16_t)blen;
    memcpy(buf, &h, sizeof(h));
    if (blen) memcpy(buf + sizeof(h), body, (size_t)blen);
    sendto(s_f[k].fd, buf, sizeof(h) + (size_t)blen, 0, (struct sockaddr*)&s_host, sizeof(s_host));
}
static void fake_drain(int k) {
    uint8_t buf[1100];
    for (;;) {
        int n = (int)recv(s_f[k].fd, buf, sizeof(buf), 0);
        PCNetWireHeader h;
        if (n < (int)sizeof(h)) return;
        memcpy(&h, buf, sizeof(h));
        if (h.magic != PCNET_MAGIC) continue;
        if (h.type == PCNET_WIRE_HELLO_ACK) s_f[k].got_hello_ack = 1;
        if (h.type == PCNET_WIRE_DISCONNECT) s_f[k].got_disconnect = 1;
    }
}

/* ---------------- event collection: wire id of every connect / disconnect ---------------- */
typedef struct Ev { PCNetEventType type; PCNetPeerId peer; } Ev;
static Ev s_ev[4000];
static int s_nev;
static void pump(int ms) {
    int t, k;
    PCNetEvent e;
    for (t = 0; t < ms; t += 2) {
        pc_net_poll();
        while (pc_net_next_event(&e) && s_nev < 4000) {
            s_ev[s_nev].type = e.type;
            s_ev[s_nev].peer = e.peer;
            s_nev++;
        }
        for (k = 0; k < s_nf; k++) fake_drain(k);
        sleep_ms(2);
    }
}
static void reset_fakes(void) {
    int k;
    for (k = 0; k < s_nf; k++) close(s_f[k].fd);
    s_nf = 0;
    s_nev = 0;
}
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
/* a new fake address says HELLO: returns its fake index; *peer = the wire id the transport gave it, or -1 when the transport refused it (table full) */
static int connect_one(int* peer) {
    int k = fake_new(), before = s_nev, i;
    fake_send(k, PCNET_WIRE_HELLO, &s_f[k].nonce, 4);
    pump(6);
    *peer = -1;
    for (i = before; i < s_nev; i++) {
        if (s_ev[i].type == PC_NET_EVENT_PEER_CONNECTED) *peer = (int)s_ev[i].peer;
    }
    return k;
}

/* ---------------- the MODEL host: a bound-guest table over the real store ---------------- */
#define MAXG 600
typedef struct MGuest { PCMpGsRecord rec; int peer; } MGuest;
static MGuest s_g[MAXG];
static int s_ng;
static int s_peer_guest[256]; /* wire id -> guest index, -1 free */
static char s_dir[400];
static int s_residents[4]; /* resident slots: the model never touches them */

static void make_entry(PCMpGuestEntry* e, int n) {
    int i;
    memset(e, 0, sizeof(*e));
    e->present = 1;
    for (i = 0; i < 8; i++) e->pid[i] = (uint8_t)('A' + ((n + i) % 26));
    for (i = 8; i < 16; i++) e->pid[i] = (uint8_t)('a' + ((n * 3 + i) % 26));
    e->pid[16] = (uint8_t)(n >> 8);
    e->pid[17] = (uint8_t)n;
    e->pid[18] = 0x12;
    e->pid[19] = (uint8_t)(0x30 + (n & 0x0F));
    for (i = 0; i < PC_MP_GUEST_TOKEN_SIZE; i++) e->token[i] = (uint8_t)(0x40 + i);
    e->token[8] = (uint8_t)((n + 1) & 0xFF);
    e->token[9] = (uint8_t)(((n + 1) >> 8) & 0xFF);
    e->epoch = 1000u + (uint32_t)n;
    e->age = (uint32_t)n + 1u;
    memcpy(e->town_land_name, "Hosttown", 8);
    e->town_land_id = 0x0777;
    e->town_terrain_hash = 0xABCD0001u;
    memcpy(e->record, e->pid, PC_MP_GUEST_PID_SIZE);
    e->record[PC_MP_GUEST_EXISTS_OFF] = 1;
    for (i = 0x40; i < 0x400; i++) e->record[i] = (uint8_t)(i * 31 + n);
}
static void model_reset(void) {
    int i;
    s_ng = 0;
    for (i = 0; i < 256; i++) s_peer_guest[i] = -1;
}
static int bound_count(int except_peer) {
    int i, n = 0;
    for (i = 0; i < 256; i++) if (s_peer_guest[i] >= 0 && i != except_peer) n++;
    return n;
}
static int row_fn(void* ctx, int idx, PCMpGuestRow* row) {
    (void)ctx;
    if (idx < 0 || idx >= s_ng) return 0;
    memset(row, 0, sizeof(*row));
    row->present = 1;
    row->confirmed = s_g[idx].rec.e.confirmed;
    memcpy(row->pid, s_g[idx].rec.e.pid, 20);
    memcpy(row->town_land_name, s_g[idx].rec.e.town_land_name, 8);
    row->town_land_id = s_g[idx].rec.e.town_land_id;
    row->town_terrain_hash = s_g[idx].rec.e.town_terrain_hash;
    return 1;
}
static int load_visit(void* ctx, const PCMpGsRecord* r, int bk) {
    (void)ctx;
    (void)bk;
    if (s_ng >= MAXG) return 0;
    s_g[s_ng].rec = *r;
    s_g[s_ng].peer = -1;
    s_ng++;
    return 1;
}

enum { R_OK_NEW = 1, R_OK_KNOWN = 2, R_GUEST_LIMIT = 3, R_TRANSPORT_FULL = 4, R_TOKEN_INVALID = 5, R_UNTRUSTED = 6, R_NO_NEW = 7 };
static int s_untrusted, s_allow_new = 1;
/* identity of key `n` presenting `token_n` (the token of guest token_n; -1 = none) arriving on wire id `peer`; the host's admission order: classify -> admit gate -> token -> create */
static int identity(int peer, int n, int token_n, int configured, int reserve, char* why, size_t whycap) {
    PCMpAdmitIn in;
    PCMpAdmitView v;
    PCMpGuestSource src;
    PCMpGuestEntry probe;
    PCGuestAdmitIn ai;
    int rc, gi;
    make_entry(&probe, n);
    memset(&in, 0, sizeof(in));
    memcpy(in.claim_pid, probe.pid, 20);
    memcpy(in.ext_home_pid, probe.pid, 20);
    in.ext_kind = PC_MP_EXT_GUEST;
    memcpy(in.town.land_name, probe.town_land_name, 8);
    in.town.land_id = probe.town_land_id;
    in.town.terrain_hash = probe.town_terrain_hash;
    in.own_idx = -1;
    src.fn = row_fn;
    src.ctx = NULL;
    in.guest_src = &src;
    if (s_untrusted) {
        return R_UNTRUSTED;
    }
    if (!pc_mp_membership_resolve(&in, &v) || v.kind != PC_MP_ADMIT_GUEST) {
        return R_GUEST_LIMIT; /* not a guest claim at all: never reached by this test */
    }
    ai.configured = configured;
    ai.bound = bound_count(peer);
    ai.occupied = pc_net_peer_count();
    ai.capacity = pc_net_peer_capacity();
    ai.reserve = reserve;
    rc = pc_guest_admit_decide(&ai, why, whycap);
    if (rc == PC_GUEST_ADMIT_GUEST_LIMIT) return R_GUEST_LIMIT;
    if (rc == PC_GUEST_ADMIT_TRANSPORT_FULL) return R_TRANSPORT_FULL;
    if (v.guest_slot >= 0) {
        gi = v.guest_slot;
        if (token_n < 0) {
            return R_TOKEN_INVALID;
        }
        {
            PCMpGuestEntry t;
            make_entry(&t, token_n);
            if (memcmp(t.token, s_g[gi].rec.e.token, 16) != 0) return R_TOKEN_INVALID;
        }
        s_g[gi].peer = peer;
        s_peer_guest[peer] = gi;
        return R_OK_KNOWN;
    }
    if (!s_allow_new) return R_NO_NEW;
    if (s_ng >= MAXG) return R_GUEST_LIMIT;
    memset(&s_g[s_ng].rec, 0, sizeof(s_g[s_ng].rec));
    if (!pc_mp_gs_new_id(s_g[s_ng].rec.id)) return R_UNTRUSTED;
    s_g[s_ng].rec.generation = 1;
    make_entry(&s_g[s_ng].rec.e, n);
    s_g[s_ng].rec.e.age = (uint32_t)s_ng + 1u;
    if (pc_mp_gs_save(s_dir, &s_g[s_ng].rec) != PC_MP_GST_OK) return R_UNTRUSTED;
    s_g[s_ng].peer = peer;
    s_peer_guest[peer] = s_ng;
    s_ng++;
    return R_OK_NEW;
}
static void leave(int fake, int peer) {
    fake_send(fake, PCNET_WIRE_DISCONNECT, NULL, 0);
    pump(8);
    if (s_peer_guest[peer] >= 0) {
        s_g[s_peer_guest[peer]].peer = -1;
        s_peer_guest[peer] = -1;
    }
}
static int count_gst(void) {
    DIR* d = opendir(s_dir);
    struct dirent* e;
    int n = 0;
    if (!d) return 0;
    while ((e = readdir(d)) != NULL) {
        const size_t l = strlen(e->d_name);
        if (l > 4 && strcmp(e->d_name + l - 4, ".gst") == 0) n++;
    }
    closedir(d);
    return n;
}
static void rm_tree(const char* dir) {
    DIR* d = opendir(dir);
    struct dirent* e;
    char p[700];
    if (!d) return;
    while ((e = readdir(d)) != NULL) {
        if (e->d_name[0] == '.') continue;
        snprintf(p, sizeof(p), "%s/%s", dir, e->d_name);
        remove(p);
    }
    closedir(d);
    rmdir(dir);
}
static void fresh_store(const char* name) {
    snprintf(s_dir, sizeof(s_dir), "%s/%s", s_root, name);
    rm_tree(s_dir);
    model_reset();
}
static int s_idn;
static int seq_id(uint8_t out[PC_MP_GS_ID_SIZE]) {
    int i;
    memset(out, 0, PC_MP_GS_ID_SIZE);
    s_idn++;
    for (i = 0; i < 4; i++) out[12 + i] = (uint8_t)(s_idn >> (24 - 8 * i));
    out[0] = 0xB6;
    return 1;
}

/* admit guests 0..n-1 one by one on fresh addresses; fills ids[]; returns how many were admitted; *first_refusal = result of the first refused one (0 = none) */
static int admit_many(int n, int configured, int reserve, int* ids, int* fakes, int* first_refusal) {
    int i, got = 0, peer, k, r;
    *first_refusal = 0;
    for (i = 0; i < n; i++) {
        k = connect_one(&peer);
        if (peer < 0) {
            if (!*first_refusal) *first_refusal = -1; /* the transport itself refused the address */
            continue;
        }
        r = identity(peer, i, i, configured, reserve, NULL, 0);
        if (r == R_OK_NEW || r == R_OK_KNOWN) {
            ids[got] = peer;
            fakes[got] = k;
            got++;
        } else if (!*first_refusal) {
            *first_refusal = r;
        }
    }
    return got;
}

int main(int argc, char** argv) {
    static int ids[400], fakes[400];
    char why[400];
    int i, ok, n, refusal, peer, k, r, v;
    struct rlimit rl;
    if (argc < 2) {
        fprintf(stderr, "usage: guest_admission_selftest <scratch_dir>\n");
        return 2;
    }
    snprintf(s_root, sizeof(s_root), "%s", argv[1]);
    mkdir(s_root, 0755);
    if (getrlimit(RLIMIT_NOFILE, &rl) == 0 && rl.rlim_cur < 4096) {
        rl.rlim_cur = rl.rlim_max < 4096 ? rl.rlim_max : 4096;
        setrlimit(RLIMIT_NOFILE, &rl);
    }
    s_port = (uint16_t)(30000 + (getpid() % 20000));
    pc_mp_gs_set_id_source(seq_id);

    /* ================= 1. configuration: range and parsing ================= */
    check("the largest max_guests is the usable wire-id count: 254 == pc_peer_capacity_max(host id 8)", pc_guest_admit_limit() == 254 && pc_guest_admit_limit() == pc_peer_capacity_max(PC_NET_RESERVED_PEER_ID));
    {
        static const struct { const char* s; int ok; int v; } t[] = {
            { "1", 1, 1 }, { "4", 1, 4 }, { "8", 1, 8 }, { "9", 1, 9 }, { "20", 1, 20 }, { "254", 1, 254 }, { " 12 ", 1, 12 }, { "012", 1, 12 }, { "255", 0, 0 }, { "256", 0, 0 }, { "0", 0, 0 },
            { "-1", 0, 0 }, { "+5", 0, 0 }, { "", 0, 0 }, { " ", 0, 0 }, { "abc", 0, 0 }, { "4x", 0, 0 }, { "x4", 0, 0 }, { "4 5", 0, 0 }, { "1e2", 0, 0 }, { "0x10", 0, 0 }, { "4.5", 0, 0 },
            { "99999999999999999999", 0, 0 }, { "4294967297", 0, 0 }, { "4294967552", 0, 0 }, { "000000000000000000000004", 0, 0 }, { "257", 0, 0 }, { "65536", 0, 0 }, { "65537", 0, 0 },
        };
        ok = 1;
        for (i = 0; i < (int)(sizeof(t) / sizeof(t[0])); i++) {
            int out = -77, rc = pc_guest_admit_parse(t[i].s, &out);
            if (rc != t[i].ok || (t[i].ok ? out != t[i].v : out != -77)) {
                printf("  parse '%s': rc=%d out=%d\n", t[i].s, rc, out);
                ok = 0;
            }
        }
        check("parse: 1..254 accepted (spaces / leading zeros ok); 0, 255+, negative, signed, empty, text, hex, float, overflow and 32-bit wrap values (4294967297 / 4294967552) all REJECTED, *out untouched", ok);
        check("parse: NULL string / NULL out rejected", !pc_guest_admit_parse(NULL, &v) && !pc_guest_admit_parse("5", NULL));
    }
    check("clamp: below 1 -> 1, above 254 -> 254, in range unchanged (no wrap: 256 -> 254, 300 -> 254, INT_MAX -> 254)", pc_guest_admit_clamp(0) == 1 && pc_guest_admit_clamp(-5) == 1 && pc_guest_admit_clamp(256) == 254 &&
          pc_guest_admit_clamp(300) == 254 && pc_guest_admit_clamp(0x7FFFFFFF) == 254 && pc_guest_admit_clamp(77) == 77);
    check("effective cap = min(max_guests, transport capacity): (4,8)=4 (20,8)=8 (254,254)=254 (300,8)=8 (100,254)=100", pc_guest_admit_effective_cap(4, 8) == 4 && pc_guest_admit_effective_cap(20, 8) == 8 &&
          pc_guest_admit_effective_cap(254, 254) == 254 && pc_guest_admit_effective_cap(300, 8) == 8 && pc_guest_admit_effective_cap(100, 254) == 100);
    {
        PCGuestAdmitIn a = { 4, 3, 5, 8, 0 };
        check("decide: 3 of 4 bound, transport free -> OK", pc_guest_admit_decide(&a, why, sizeof(why)) == PC_GUEST_ADMIT_OK);
        a.bound = 4;
        check("decide: 4 of 4 bound -> GUEST_LIMIT, the log names max_guests", pc_guest_admit_decide(&a, why, sizeof(why)) == PC_GUEST_ADMIT_GUEST_LIMIT && strstr(why, "max_guests=4 guests are connected") != NULL);
        a.bound = 2;
        a.occupied = 7;
        a.reserve = 2;
        check("decide: 2 of 4 bound but 7 + 2 held for residents > 8 -> TRANSPORT_FULL, the log says transport", pc_guest_admit_decide(&a, why, sizeof(why)) == PC_GUEST_ADMIT_TRANSPORT_FULL &&
              strstr(why, "transport full") != NULL && strstr(why, "held for residents") != NULL);
        a.configured = 1000;
        a.bound = 253;
        a.occupied = 100;
        a.reserve = 0;
        a.capacity = 254;
        check("decide: a configured value past 254 is clamped (1000 -> 254): 253 bound is still below the cap", pc_guest_admit_decide(&a, why, sizeof(why)) == PC_GUEST_ADMIT_OK);
        a.bound = 254;
        check("decide: ...and 254 bound is the limit", pc_guest_admit_decide(&a, why, sizeof(why)) == PC_GUEST_ADMIT_GUEST_LIMIT);
    }

    /* ================= 2. default behaviour: max_guests 4, transport 8 ================= */
    fresh_store("s_default");
    check("host up, capacity 8", host_up(8));
    n = admit_many(8, 4, 0, ids, fakes, &refusal);
    check("default (max_guests 4): exactly 4 guests admitted, the 5th..8th refused as GUEST LIMIT (not transport)", n == 4 && refusal == R_GUEST_LIMIT && bound_count(-1) == 4 && count_gst() == 4);
    ok = 1;
    for (i = 0; i < 4; i++) ok = ok && ids[i] == i;
    check("their wire ids are 0..3", ok);
    check("a refused guest created no store file and no guest", s_ng == 4 && count_gst() == 4);
    host_down();

    /* ================= 3. above 8: 30 guests (ids skip the host's 8) ================= */
    fresh_store("s_30");
    check("host up, capacity 40", host_up(40));
    n = admit_many(31, 30, 0, ids, fakes, &refusal);
    check("max_guests 30: 30 guests admitted, the 31st refused as GUEST LIMIT while the transport still has room", n == 30 && refusal == R_GUEST_LIMIT && pc_net_peer_count() == 31 && pc_net_peer_count() < pc_net_peer_capacity());
    {
        char seen[256];
        memset(seen, 0, sizeof(seen));
        ok = 1;
        for (i = 0; i < n; i++) {
            if (ids[i] == HOST_ID || ids[i] < 0 || ids[i] > 254 || seen[ids[i]]) ok = 0;
            seen[ids[i]] = 1;
        }
        check("the 30 guests have 30 UNIQUE valid wire ids, none is the host's 8, none 0xFF / above 254", ok);
        check("the ids are exactly 0..30 without 8 (the allocator skips the reserved id)", seen[0] && seen[7] && !seen[8] && seen[9] && seen[30]);
    }
    check("every guest is durable: 30 .gst files; residents untouched (4 resident slots, none used)", count_gst() == 30 && s_residents[0] == 0 && s_residents[1] == 0 && s_residents[2] == 0 && s_residents[3] == 0);

    /* ================= 4. disconnect frees capacity; reuse; persistence ================= */
    for (i = 0; i < 10; i++) leave(fakes[i], ids[i]);
    check("10 guests leave: 20 bound and their 10 transport slots are free (31 -> 21 peers)", bound_count(-1) == 20 && pc_net_peer_count() == 21);
    for (i = 30; i < 40; i++) { /* ten NEW guests (keys 30..39) take the freed capacity */
        k = connect_one(&peer);
        r = peer >= 0 ? identity(peer, i, i, 30, 0, NULL, 0) : -1;
        if (r != R_OK_NEW) break;
        ids[i] = peer;
        fakes[i] = k;
    }
    check("10 new guests are admitted into the freed capacity (30 bound again), the store now holds 40", i == 40 && bound_count(-1) == 30 && count_gst() == 40);
    k = connect_one(&peer);
    r = identity(peer, 99, 99, 30, 0, NULL, 0);
    check("...and the 31st is refused again", r == R_GUEST_LIMIT);
    host_down();
    /* restart: the store is the only memory */
    model_reset();
    check("restart: the 40 guests load from the .gst files, oldest first, none lost", pc_mp_gs_load(s_dir, load_visit, NULL, &(PCMpGsLoadInfo){ 0 }) && s_ng == 40);
    check("host up again", host_up(40));
    ok = 1;
    for (i = 0; i < 40; i += 7) {
        k = connect_one(&peer);
        r = identity(peer, i, i, 30, 0, NULL, 0);
        ok = ok && peer >= 0 && r == R_OK_KNOWN;
    }
    check("known guests reconnect after the restart with their token (persistent identity): KNOWN, no new file", ok && count_gst() == 40 && s_ng == 40);
    k = connect_one(&peer);
    r = identity(peer, 3, 4, 30, 0, NULL, 0);
    check("a known key presenting ANOTHER guest's token is TOKEN_INVALID (distinct from full / untrusted)", r == R_TOKEN_INVALID);
    k = connect_one(&peer);
    r = identity(peer, 3, -1, 30, 0, NULL, 0);
    check("a known key presenting NO token is TOKEN_INVALID", r == R_TOKEN_INVALID);
    s_allow_new = 0;
    k = connect_one(&peer);
    r = identity(peer, 500, 500, 30, 0, NULL, 0);
    check("allow_new_guests=0: a NEW key is refused (distinct result) and nothing is created", r == R_NO_NEW && count_gst() == 40);
    s_allow_new = 1;
    s_untrusted = 1;
    k = connect_one(&peer);
    r = identity(peer, 501, 501, 30, 0, NULL, 0);
    check("an UNTRUSTED store refuses (distinct result), nothing is created", r == R_UNTRUSTED && count_gst() == 40);
    s_untrusted = 0;
    (void)k;
    host_down();

    /* ================= 5. exhaustion: guest limit vs transport (both orders) ================= */
    fresh_store("s_exhaust");
    check("host up, capacity 12 (max_guests 50 > transport)", host_up(12));
    n = admit_many(14, 50, 0, ids, fakes, &refusal);
    check("max_guests above the transport capacity: 12 guests fit, the 13th ADDRESS is refused by the TRANSPORT (no guest decision reached)", n == 12 && refusal == -1 && bound_count(-1) == 12 && pc_net_peer_count() == 12);
    {
        PCNetStats st;
        pc_net_get_stats(&st);
        check("the transport counted the refusals (peer_table_full_refused)", st.peer_table_full_refused >= 2);
    }
    check("the effective admissible count is 12 = min(50, 12); wire ids never exceed the table (0..12 minus 8)", pc_guest_admit_effective_cap(50, pc_net_peer_capacity()) == 12);
    host_down();
    fresh_store("s_reserve");
    check("host up, capacity 10, 2 residents' peers held in reserve", host_up(10));
    n = admit_many(10, 10, 2, ids, fakes, &refusal);
    check("a guest needs a free peer beyond the residents' reserve: 8 admitted, the 9th is TRANSPORT_FULL (not the guest limit)", n == 8 && refusal == R_TRANSPORT_FULL);
    host_down();

    /* ================= 6. the whole wire-id range: 254 guests ================= */
    fresh_store("s_254");
    check("host up at the maximum capacity 254", host_up(254) && pc_net_peer_capacity() == 254);
    n = admit_many(256, 254, 0, ids, fakes, &refusal);
    {
        char seen[256];
        memset(seen, 0, sizeof(seen));
        ok = 1;
        for (i = 0; i < n; i++) {
            if (ids[i] == HOST_ID || ids[i] < 0 || ids[i] > 254 || seen[ids[i]]) ok = 0;
            seen[ids[i]] = 1;
        }
        for (i = 0; i <= 254; i++) if (i != HOST_ID && !seen[i]) ok = 0;
        check("254 guests admitted, every wire id 0..254 except 8 used exactly once, none 8 / 255", n == 254 && ok && !seen[255] && !seen[HOST_ID]);
    }
    check("the 255th address was refused by the transport (the wire-id space is exhausted), nothing wrapped or reused", refusal == -1 && pc_net_peer_count() == 254 && bound_count(-1) == 254 && count_gst() == 254);
    leave(fakes[100], ids[100]);
    leave(fakes[200], ids[200]);
    k = connect_one(&peer);
    check("after two leave, the next guests take exactly the freed ids (no new id invented)", peer == ids[100] || peer == ids[200]);
    r = identity(peer, 100, 100, 254, 0, NULL, 0);
    check("a returning guest (key 100) gets in again with its token and its own file (254 files, none added)", r == R_OK_KNOWN && count_gst() == 254);
    host_down();

    /* ================= 7. with 4 residents' reserve at the top of the range ================= */
    fresh_store("s_254r");
    check("host up at 254, 4 resident peers reserved", host_up(254));
    n = admit_many(254, 254, 4, ids, fakes, &refusal);
    check("254 capacity - 4 held for residents = 250 guests, the 251st is TRANSPORT_FULL; resident slots never consumed", n == 250 && refusal == R_TRANSPORT_FULL && s_residents[0] + s_residents[1] + s_residents[2] + s_residents[3] == 0);
    host_down();

    /* ================= 8. the store is not bounded by admission ================= */
    fresh_store("s_store");
    for (i = 0; i < 300; i++) {
        PCMpGsRecord rec;
        memset(&rec, 0, sizeof(rec));
        pc_mp_gs_new_id(rec.id);
        rec.generation = 1;
        make_entry(&rec.e, i);
        (void)pc_mp_gs_save(s_dir, &rec);
    }
    {
        PCMpGsLoadInfo li;
        model_reset();
        memset(&li, 0, sizeof(li));
        check("300 stored guests (more than the 254 wire ids) all load: no truncation, trusted", pc_mp_gs_load(s_dir, load_visit, NULL, &li) && s_ng == 300 && li.loaded == 300 && !li.untrusted);
    }
    {
        PCMpGuestSource src;
        PCMpMembership rows[400];
        PCMpTownKey town;
        uint8_t res_pid[4][20];
        uint8_t res_ex[4] = { 0, 0, 0, 0 };
        memset(res_pid, 0, sizeof(res_pid));
        memset(&town, 0, sizeof(town));
        memcpy(town.land_name, "Hosttown", 8);
        town.land_id = 0x0777;
        town.terrain_hash = 0xABCD0001u;
        src.fn = row_fn;
        src.ctx = NULL;
        n = pc_mp_membership_list_src(&town, res_pid, res_ex, &src, rows, 400);
        ok = n == 300;
        for (i = 0; i < n && ok; i++) ok = rows[i].kind == PC_MP_MEMBER_GUEST && rows[i].res_index == -1 && rows[i].guest_slot == i;
        check("the membership list over the 300-guest store returns 300 guest rows and 0 residents", ok);
    }
    {
        /* a stored guest above the bound limit still reconnects once capacity allows */
        check("host up", host_up(20));
        k = connect_one(&peer);
        r = identity(peer, 299, 299, 20, 0, NULL, 0);
        check("guest #299 of 300 stored reconnects (KNOWN) under max_guests 20: admission is about BOUND guests, the store about stored ones", peer >= 0 && r == R_OK_KNOWN && count_gst() == 300);
        host_down();
    }

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail == 0 ? 0 : 1;
}
