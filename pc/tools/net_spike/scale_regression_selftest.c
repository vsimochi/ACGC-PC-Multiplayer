/* scale_regression_selftest.c - capacity phases 5 + 6, native MODEL test of the real bug "the 9th guest cannot be seen" and of the dynamic puppet slots that fix it.
 * Real code under test: pc/src/pc_puppet_pool.c (puppet slot pool), pc/src/pc_roster.c (roster deltas), pc/src/pc_interest.c (MOVE relay tiers). Modelled (NOT the game): the host's
 * send loop and the clients' receive handlers, which in the game are pc_net_game.c + pc_remote_player.c (they need the game and run on Windows only). The model mirrors those handlers:
 *   client on APPEARANCE(id) -> acquire slot(id), store appearance;  on MOVE(id) -> acquire slot(id), count sample;  on CLEARED(id) -> release slot(id)
 * "Visible" = a slot exists with an appearance and at least one movement sample. LEGACY = the pre-phase-6 table of 9 slots (ids 0..8): every other id was rejected.
 * Usage: scale_regression_selftest (prints PASS:/FAIL: and "RESULT passed=N failed=M") */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pc_interest.h"
#include "pc_puppet_pool.h"
#include "pc_roster.h"

static int s_pass, s_fail;
static void check(const char* what, int ok) {
    printf("%s: %s\n", ok ? "PASS" : "FAIL", what);
    fflush(stdout);
    if (ok) s_pass++; else s_fail++;
}

#define HOST 8
#define MAXC 255

typedef struct Slot {
    int      has_app, has_scene;
    unsigned moves;
    unsigned char marker[64]; /* a payload that must never survive a release */
} Slot;

typedef struct Client {
    int         live;
    int         legacy;       /* 1 = the pre-phase-6 9-slot table */
    PCPuppetPool pool;
    Slot        legacy_slot[9];
    unsigned    rejected;     /* messages for ids the process could not represent */
} Client;
static Client s_cl[MAXC];

static Slot* cslot(Client* c, int id, int create) {
    if (c->legacy) {
        if (id < 0 || id > 8) { c->rejected++; return NULL; }
        return &c->legacy_slot[id];
    }
    if (create) {
        Slot* s = (Slot*)pc_puppet_pool_acquire(&c->pool, id);
        if (s == NULL) c->rejected++;
        return s;
    }
    return (Slot*)pc_puppet_pool_find(&c->pool, id);
}

/* ---------------- the model host ---------------- */
static PCRoster s_app, s_scn;
static int s_x[MAXC], s_z[MAXC], s_scene[MAXC], s_town[MAXC], s_have_app[MAXC];
static unsigned s_move_count[MAXC];

static void host_start(int span, int legacy_clients) {
    int i;
    pc_roster_free(&s_app);
    pc_roster_free(&s_scn);
    pc_roster_init(&s_app, span, HOST);
    pc_roster_init(&s_scn, span, HOST);
    s_app.track_gone = 0;
    memset(s_x, 0, sizeof(s_x)); memset(s_z, 0, sizeof(s_z)); memset(s_scene, 0, sizeof(s_scene)); memset(s_town, 0, sizeof(s_town));
    memset(s_have_app, 0, sizeof(s_have_app)); memset(s_move_count, 0, sizeof(s_move_count));
    for (i = 0; i < MAXC; i++) {
        pc_puppet_pool_release_all(&s_cl[i].pool);
        memset(&s_cl[i], 0, sizeof(s_cl[i]));
        pc_puppet_pool_init(&s_cl[i].pool, sizeof(Slot));
        s_cl[i].legacy = legacy_clients;
    }
}
static void host_join(int id) {
    s_cl[id].live = 1;
    s_have_app[id] = 1;
    s_scene[id] = 5; s_town[id] = 1;
    pc_roster_join(&s_app, id);
    pc_roster_join(&s_scn, id);
    pc_roster_changed(&s_app, id);
    pc_roster_changed(&s_scn, id);
}
static void host_leave(int id) {
    s_cl[id].live = 0;
    s_have_app[id] = 0;
    pc_roster_leave(&s_scn, id);
    pc_roster_leave(&s_app, id);
}
/* reliable delivery of roster messages to clients (the window is never full here) */
static void deliver(int span, int frames) {
    int f, d, subject, from, tries;
    for (f = 0; f < frames; f++) {
        for (d = 0; d < span; d++) {
            Client* c = &s_cl[d];
            if (!pc_peer_set_has(&s_app.ready, d)) continue;
            for (from = 0; pc_roster_next_gone(&s_scn, d, from, &subject); from = subject + 1) {
                Slot* sl;
                (void)sl;
                if (!c->legacy) pc_puppet_pool_release(&c->pool, subject);
                pc_roster_gone_delivered(&s_scn, d, subject);
            }
            for (tries = 0, from = 0; tries < 8 && pc_roster_next_pend(&s_app, d, from, &subject); tries++) {
                from = subject + 1;
                if (subject != HOST && !s_have_app[subject]) continue;
                if (subject != d) {
                    Slot* sl = cslot(c, subject, 1);
                    if (sl != NULL) { sl->has_app = 1; sl->marker[0] = 0xAB; }
                }
                pc_roster_pend_delivered(&s_app, d, subject);
            }
            for (tries = 0, from = 0; tries < 8 && pc_roster_next_pend(&s_scn, d, from, &subject); tries++) {
                from = subject + 1;
                if (subject != d) {
                    Slot* sl = cslot(c, subject, 1);
                    if (sl != NULL) sl->has_scene = 1;
                }
                pc_roster_pend_delivered(&s_scn, d, subject);
            }
        }
    }
}
static void view(int id, PCInterestView* v) {
    memset(v, 0, sizeof(*v));
    v->scene_known = 1; v->scene_id = (uint8_t)s_scene[id]; v->in_town = (uint8_t)s_town[id]; v->pos_known = 1; v->x = (float)s_x[id]; v->z = (float)s_z[id];
}
/* every live client sends one MOVE sample; the host relays it with the interest tiers (interest = 0: to everybody) */
static unsigned s_relayed, s_full;
static void move_round(int span, int interest) {
    int s, d;
    for (s = 0; s < span; s++) {
        PCInterestView sv, dv;
        const unsigned count = s_move_count[s]++;
        if (!s_cl[s].live) continue;
        view(s, &sv);
        for (d = 0; d < span; d++) {
            int tier = PC_INTEREST_NEAR;
            if (d == s || !s_cl[d].live) continue;
            s_full++;
            if (interest) { view(d, &dv); tier = pc_interest_tier(&sv, &dv); }
            if (pc_interest_relay(tier, count, 0)) {
                Slot* sl = cslot(&s_cl[d], s, 1);
                s_relayed++;
                if (sl != NULL) sl->moves++;
            }
        }
        { /* the host's own sample to everybody (id 8) */
            Slot* sl = cslot(&s_cl[s], HOST, 1);
            if (sl != NULL && (count % 1u) == 0u) sl->moves++;
        }
    }
}
static int visible(Client* c, int id) {
    Slot* sl = cslot(c, id, 0);
    return sl != NULL && sl->has_app && sl->moves > 0;
}

int main(void) {
    int i, ok, n, id;
    PCPuppetPool pool;

    /* ================= the pool ================= */
    pc_puppet_pool_init(&pool, sizeof(Slot));
    check("valid puppet ids are exactly 0..254 (the host's 8 included, 0xFF / negative / 1000 invalid)", pc_puppet_id_valid(0) && pc_puppet_id_valid(8) && pc_puppet_id_valid(9) && pc_puppet_id_valid(254) && !pc_puppet_id_valid(255) && !pc_puppet_id_valid(-1) && !pc_puppet_id_valid(1000));
    check("a reader never allocates: find() of an id without a slot is NULL and the pool stays empty", pc_puppet_pool_find(&pool, 9) == NULL && pool.used == 0);
    {
        Slot* s9 = (Slot*)pc_puppet_pool_acquire(&pool, 9);
        check("acquire(9) (the first id the old table rejected) succeeds", s9 != NULL && pc_puppet_pool_find(&pool, 9) == s9 && pool.used == 1);
        check("a slot is zeroed and 32-byte aligned (the appearance buffers are DMA targets)", s9 != NULL && ((size_t)s9 % 32) == 0 && s9->has_app == 0 && s9->moves == 0);
        s9->has_app = 1; s9->moves = 77; memset(s9->marker, 0xEE, sizeof(s9->marker));
        check("acquire of an existing id returns the same slot and keeps its content", pc_puppet_pool_acquire(&pool, 9) == s9 && s9->moves == 77);
        pc_puppet_pool_release(&pool, 9);
        check("release frees it; the id is reusable and the new slot is zeroed (no stale appearance / state)", pool.used == 0 && pc_puppet_pool_find(&pool, 9) == NULL);
        s9 = (Slot*)pc_puppet_pool_acquire(&pool, 9);
        check("reuse of id 9: zeroed again", s9 != NULL && s9->has_app == 0 && s9->moves == 0 && s9->marker[0] == 0 && s9->marker[63] == 0);
    }
    ok = 1;
    for (id = 0; id <= 254; id++) {
        Slot* s = (Slot*)pc_puppet_pool_acquire(&pool, id);
        ok = ok && s != NULL && ((size_t)s % 32) == 0;
    }
    check("all 255 wire ids 0..254 can hold a slot at once (ids 8 and 9..254 included)", ok && pool.used == 255 && pool.peak == 255);
    check("id 255, -1 and 1000 are refused (counted), nothing is allocated for them", pc_puppet_pool_acquire(&pool, 255) == NULL && pc_puppet_pool_acquire(&pool, -1) == NULL && pc_puppet_pool_acquire(&pool, 1000) == NULL && pool.used == 255 && pool.alloc_failures >= 3);
    n = 0;
    for (id = pc_puppet_pool_next(&pool, 0); id >= 0; id = pc_puppet_pool_next(&pool, id + 1)) n++;
    check("iteration visits exactly the allocated ids", n == 255);
    pc_puppet_pool_release_all(&pool);
    check("release_all frees everything", pool.used == 0 && pc_puppet_pool_next(&pool, 0) == -1);
    pool.fail_next = 1;
    check("an allocation failure is reported (NULL, counted) and changes nothing; the next one works", pc_puppet_pool_acquire(&pool, 20) == NULL && pool.used == 0 && pc_puppet_pool_acquire(&pool, 20) != NULL && pool.used == 1);
    pc_puppet_pool_release_all(&pool);
    check("actor headroom: (150 now of 200, reserve 48) = 2; at or past the reserve = 0, never negative", pc_puppet_actor_headroom(150, 200, 48) == 2 && pc_puppet_actor_headroom(152, 200, 48) == 0 && pc_puppet_actor_headroom(199, 200, 48) == 0 && pc_puppet_actor_headroom(0, 200, 48) == 152);

    /* ================= THE REAL BUG: 8 existing clients + a 9th guest ================= */
    for (n = 0; n < 2; n++) {
        const int legacy = (n == 0);
        host_start(20, legacy);
        for (i = 0; i < 8; i++) { host_join(i); deliver(20, 3); }
        host_join(9); /* the 9th client: wire id 9 (8 is the host's) */
        deliver(20, 4);
        move_round(20, 0);
        move_round(20, 0);
        ok = 1;
        for (i = 0; i < 8; i++) ok = ok && visible(&s_cl[i], 9);
        if (legacy) {
            check("LEGACY 9-slot table (the old code): existing clients 0..7 can NOT see client 9 -- the real bug", !ok);
            check("LEGACY: client 9 itself sees everybody (its own ids are all < 9), exactly the observed symptom", visible(&s_cl[9], 0) && visible(&s_cl[9], 7) && visible(&s_cl[9], HOST));
            check("LEGACY: the 9th client's entries were rejected by 8 clients (counted)", s_cl[0].rejected > 0);
        } else {
            check("DYNAMIC slots: every existing client (0..7) has a puppet slot for client 9 with its appearance, scene and movement", ok);
            ok = 1;
            for (i = 0; i < 8; i++) ok = ok && visible(&s_cl[9], i);
            check("DYNAMIC: client 9 receives and represents every existing client", ok && visible(&s_cl[9], HOST));
            check("DYNAMIC: nothing was rejected anywhere", s_cl[0].rejected == 0 && s_cl[9].rejected == 0);
            check("DYNAMIC: client 9 holds slots for ids 0..7 and the host (9), and none for itself", s_cl[9].pool.used == 9 && pc_puppet_pool_find(&s_cl[9].pool, 9) == NULL);
            check("DYNAMIC: client 0 holds slots for 1..7, 9 and the host (9 slots)", s_cl[0].pool.used == 9 && pc_puppet_pool_find(&s_cl[0].pool, 9) != NULL);
        }
    }
    /* ids 10, 11 and above ... up to the whole range */
    host_start(255, 0);
    for (id = 0; id <= 254; id++) {
        if (id == HOST) continue;
        host_join(id);
    }
    deliver(255, 300);
    move_round(255, 0);
    ok = 1;
    for (i = 0; i <= 254 && ok; i++) {
        int j;
        if (i == HOST) continue;
        for (j = 0; j <= 254; j++) {
            if (j == HOST || j == i) continue;
            if (!visible(&s_cl[i], j)) { ok = 0; printf("  client %d cannot see %d\n", i, j); break; }
        }
    }
    check("254 clients (ids 0..254 except 8): every client has a visible puppet for each of the other 253 and the host (the whole wire-id range, no id rejected)", ok);
    for (i = 0; i <= 254; i++) {
        if (i != HOST) { ok = ok && s_cl[i].rejected == 0 && visible(&s_cl[i], HOST); }
    }
    check("...and nothing was rejected on any client", ok);

    /* ================= leave / rejoin / cleanup ================= */
    for (i = 0; i < 100; i++) if (i != HOST) host_leave(i);
    deliver(255, 300);
    ok = 1;
    for (i = 100; i <= 254; i++) {
        int j;
        for (j = 0; j < 100; j++) if (j != HOST) ok = ok && pc_puppet_pool_find(&s_cl[i].pool, j) == NULL;
    }
    check("99 peers leave: every remaining client released their slots (no stale puppet / appearance / scene)", ok);
    check("a remaining client's pool shrank to the peers still there (154 others + the host)", s_cl[150].pool.used == 155);
    host_join(9);
    deliver(255, 20);
    move_round(255, 0);
    {
        Slot* sl = (Slot*)pc_puppet_pool_find(&s_cl[200].pool, 9);
        check("id 9 is reused by a NEW peer: the slot starts clean (one appearance, fresh sample count) -- nothing of the previous owner survives", sl != NULL && sl->has_app && sl->moves == 1 && sl->marker[1] == 0);
    }

    /* ================= interest-managed MOVE traffic keeps everybody visible ================= */
    host_start(61, 0);
    srand(11);
    for (id = 0; id < 60; id++) {
        const int pid = id < HOST ? id : id + 1;
        host_join(pid);
        s_x[pid] = rand() % (5 * 640);
        s_z[pid] = rand() % (7 * 640);
    }
    deliver(61, 30);
    s_relayed = s_full = 0;
    for (i = 0; i < 60; i++) move_round(61, 1);
    printf("  (model: 60 players spread over the town, 60 rounds: %u relayed of %u full-rate)\n", s_relayed, s_full);
    ok = 1;
    for (i = 0; i <= 60; i++) {
        int j;
        if (i == HOST) continue;
        for (j = 0; j <= 60; j++) if (j != HOST && j != i && !visible(&s_cl[i], j)) { if (ok) { Slot* q = cslot(&s_cl[i], j, 0); printf("  client %d does not see %d (slot %s app=%d moves=%u)\n", i, j, q ? "yes" : "none", q ? q->has_app : 0, q ? q->moves : 0); } ok = 0; }
    }
    check("with interest management every player still has a visible puppet for every other player (nobody is culled to zero)", ok);
    check("...while the relayed MOVE volume is well below the full N x (N - 1)", s_relayed < s_full * 7 / 10);
    {
        /* a player inside a house is still seen alive (1 Hz) and at full rate once it comes out */
        const unsigned before = ((Slot*)pc_puppet_pool_find(&s_cl[0].pool, 20))->moves;
        s_scene[20] = 7; s_town[20] = 0;
        for (i = 0; i < 40; i++) move_round(61, 1);
        {
            const unsigned inside = ((Slot*)pc_puppet_pool_find(&s_cl[0].pool, 20))->moves - before;
            check("a player in another scene still reaches an outdoor client at ~1 Hz (2 samples in 40 rounds), never zero", inside >= 2 && inside <= 4);
        }
    }

    /* ================= exhaustion ================= */
    host_start(20, 0);
    s_cl[3].pool.fail_next = 1000;
    for (i = 0; i < 5; i++) host_join(i);
    deliver(20, 5);
    check("a client that cannot allocate slots counts the failures and ignores those players; the others are unaffected", s_cl[3].pool.used == 0 && s_cl[3].rejected > 0 && s_cl[0].pool.used == 5);

    pc_roster_free(&s_app);
    pc_roster_free(&s_scn);
    for (i = 0; i < MAXC; i++) pc_puppet_pool_release_all(&s_cl[i].pool);
    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail == 0 ? 0 : 1;
}
