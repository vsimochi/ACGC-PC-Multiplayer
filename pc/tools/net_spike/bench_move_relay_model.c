/* bench_move_relay_model.c - WS4: the LOOKUP / DECISION part of the host's MOVE relay at 8..254 peers, with the REAL pc_interest.c and pc_puppet_pool.c.
 *
 * pcnetgame_handle_host_move() (pc_net_game.c) does, per incoming MOVE: one interest view of the sender, then for every other READY peer i: pcnetgame_interest_view(i) (an array lookup
 * slot[id] in the puppet pool, a copy of the scene and of the newest position snapshot), pc_interest_tier(), pc_interest_relay() and, when relayed, a 48-byte message copy + pc_net_send().
 * This program reproduces exactly that loop around the real pure-logic pieces (slot memory is as large as a real PCRemotePlayerSlot so the cache behaviour is not flattered); the
 * "send" is only a memcpy into a ring, because the real transport cost is measured in the game itself (bench_move_relay_real.py, "pc_net_send us/send").
 * Layouts: near (everybody in one acre: everything relayed), spread (peers spread over the 5x6 town: distant receivers thinned), mixed (a quarter indoors).
 * Build: gcc -O2 -I../../include bench_move_relay_model.c ../../src/pc_interest.c ../../src/pc_puppet_pool.c -o bench_move_relay_model.exe   (from tools/net_spike)
 * Usage: bench_move_relay_model.exe  -> a table, one line per (layout, peers). */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
static double now_s(void) {
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)f.QuadPart;
}
#else
#include <time.h>
static double now_s(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + (double)t.tv_nsec * 1e-9;
}
#endif
#include "pc_interest.h"
#include "pc_puppet_pool.h"

typedef struct Snap {
    float x, y, z;
    int16_t a;
    double t;
} Snap;
typedef struct Slot {
    int    peer_id;
    int    scene_valid;
    uint8_t scene_id, in_town;
    uint16_t owner;
    int    snapshot_head, snapshot_count;
    Snap   snapshots[8];
    unsigned char rest[3000]; /* the rest of a PCRemotePlayerSlot (appearance buffers, puppet state ...): a slot is a few KB */
} Slot;

typedef struct Msg {
    uint8_t type, id;
    float   x, y, z, speed;
    uint32_t frame;
    unsigned char pad[24];
} Msg; /* ~48 bytes like PCNetMoveMsg */

static PCPuppetPool pool;
static unsigned char ring[1 << 16];
static size_t ring_pos;

static void view(int id, PCInterestView* v) {
    const Slot* s = (const Slot*)pc_puppet_pool_find(&pool, id);
    memset(v, 0, sizeof(*v));
    if (s == NULL) {
        return;
    }
    if (s->scene_valid) {
        v->scene_known = 1;
        v->scene_id = s->scene_id;
        v->owner = s->owner;
        v->in_town = s->in_town;
    }
    if (s->snapshot_count != 0) {
        const int newest = (s->snapshot_head - 1 + 8) % 8;
        v->pos_known = 1;
        v->x = s->snapshots[newest].x;
        v->z = s->snapshots[newest].z;
    }
}

static void relay_one(int peer, int n, uint32_t count, unsigned long long* relayed, unsigned long long* thinned) {
    PCInterestView sv, dv;
    Msg m;
    int i;
    memset(&m, 0, sizeof(m));
    view(peer, &sv);
    for (i = 0; i < n; i++) {
        int tier;
        if (i == peer) {
            continue;
        }
        view(i, &dv);
        tier = pc_interest_tier(&sv, &dv);
        if (pc_interest_relay(tier, count, 0)) {
            Msg out = m;
            out.id = (uint8_t)peer;
            memcpy(ring + (ring_pos & 0xFFC0u), &out, sizeof(out));
            ring_pos += 64;
            (*relayed)++;
        } else {
            (*thinned)++;
        }
    }
}

static void setup(int n, int layout) {
    int i;
    pc_puppet_pool_init(&pool, sizeof(Slot));
    for (i = 0; i < n; i++) {
        Slot* s = (Slot*)pc_puppet_pool_acquire(&pool, i);
        s->peer_id = i;
        s->scene_valid = 1;
        s->scene_id = 0;
        s->in_town = (layout == 2 && (i % 4) == 3) ? 0 : 1;
        s->owner = s->in_town ? 0 : (uint16_t)i; /* the town has no house owner; interiors belong to their owner */
        s->snapshot_head = 1;
        s->snapshot_count = 1;
        s->snapshots[0].x = (layout == 0) ? 1000.0f + (float)(i % 8) * 40.0f : 700.0f + (float)((i * 397) % 2600);
        s->snapshots[0].z = (layout == 0) ? 1500.0f : 700.0f + (float)((i * 811) % 3200);
    }
}

int main(void) {
    static const int counts[] = { 8, 16, 32, 64, 128, 254 };
    static const char* names[] = { "near", "spread", "mixed" };
    int l, c;
    printf("%-7s %5s %12s %12s %12s %12s %16s\n", "layout", "peers", "ns/iteration", "us/move", "relayed%", "us/round", "core share @20Hz");
    for (l = 0; l < 3; l++) {
        for (c = 0; c < (int)(sizeof(counts) / sizeof(counts[0])); c++) {
            const int n = counts[c];
            unsigned long long relayed = 0, thinned = 0, iters = 0;
            int rounds = (n <= 32) ? 20000 : (n <= 128 ? 4000 : 1500), r, p;
            double t0, t1;
            setup(n, l);
            t0 = now_s();
            for (r = 0; r < rounds; r++) {
                for (p = 0; p < n; p++) {
                    relay_one(p, n, (uint32_t)r, &relayed, &thinned);
                    iters += (unsigned long long)(n - 1);
                }
            }
            t1 = now_s();
            {
                const double secs = t1 - t0;
                const double moves = (double)rounds * (double)n;
                const double us_move = secs * 1e6 / moves;
                /* a round = every peer sends one MOVE = 20 rounds per second */
                printf("%-7s %5d %12.1f %12.3f %11.1f%% %12.1f %15.2f%%\n", names[l], n, secs * 1e9 / (double)iters, us_move, 100.0 * (double)relayed / (double)(relayed + thinned), us_move * n,
                       100.0 * (us_move * n * 20.0) / 1e6);
            }
        }
    }
    return 0;
}
