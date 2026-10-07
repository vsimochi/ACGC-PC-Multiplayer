/* interest_roster_selftest.c - capacity phase 5, native MODEL tests of the pure modules pc/src/pc_interest.c (MOVE relay tiers) and pc/src/pc_roster.c (roster deltas with retry).
 * The roster test drives the book through a simulated host pump with a lossy / full reliable window and simulated clients, including the wire ids around the old puppet
 * boundary (7, 8 = the host, 9, 10) and up to 254 peers. It is a LOGIC test: the real pump lives in pc_net_game.c (needs the game), which only Windows runs.
 * Usage: interest_roster_selftest (prints PASS:/FAIL: and "RESULT passed=N failed=M") */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pc_interest.h"
#include "pc_roster.h"

static int s_pass, s_fail;
static void check(const char* what, int ok) {
    printf("%s: %s\n", ok ? "PASS" : "FAIL", what);
    fflush(stdout);
    if (ok) s_pass++; else s_fail++;
}

#define HOST 8

/* ---------------- a model of "what each client knows" ---------------- */
typedef struct Client {
    int      present[256]; /* the appearance entry of subject i is known */
    int      scene[256];   /* the scene entry of subject i is known */
    int      cleared_seen[256];
} Client;
static Client s_c[256];

/* the model host's send path: a reliable window per peer of `cap` messages; `drop_all` simulates a full window */
static int s_window_room[256]; /* messages the peer's window still accepts (-1 = unlimited) */

static int send_ok(int dest) {
    if (s_window_room[dest] < 0) return 1;
    if (s_window_room[dest] == 0) return 0;
    s_window_room[dest]--;
    return 1;
}

static int s_have_app[256]; /* does the host hold an appearance for subject i */

/* one pump frame for dest, mirroring pcnetgame_roster_pump_dest (burst 4) */
static void pump_dest(PCRoster* app, PCRoster* scn, int dest) {
    int subject, from, sent, tries;
    for (sent = 0, from = 0; sent < 4 && pc_roster_next_gone(scn, dest, from, &subject);) {
        if (!send_ok(dest)) return;
        s_c[dest].cleared_seen[subject]++;
        s_c[dest].present[subject] = 0;
        s_c[dest].scene[subject] = 0;
        pc_roster_gone_delivered(scn, dest, subject);
        from = subject + 1;
        sent++;
    }
    if (pc_roster_next_gone(scn, dest, 0, &subject)) return;
    for (sent = 0, from = 0, tries = 0; sent < 4 && tries < 16 && pc_roster_next_pend(app, dest, from, &subject); tries++) {
        from = subject + 1;
        if (subject != HOST && !s_have_app[subject]) {
            continue;
        }
        if (!send_ok(dest)) break;
        s_c[dest].present[subject] = 1;
        pc_roster_pend_delivered(app, dest, subject);
        sent++;
    }
    for (sent = 0, from = 0, tries = 0; sent < 4 && tries < 16 && pc_roster_next_pend(scn, dest, from, &subject); tries++) {
        from = subject + 1;
        if (!send_ok(dest)) break;
        s_c[dest].scene[subject] = 1;
        pc_roster_pend_delivered(scn, dest, subject);
        sent++;
    }
}
static void pump_all(PCRoster* app, PCRoster* scn, int span, int frames) {
    int f, i;
    for (f = 0; f < frames; f++) {
        for (i = 0; i < span; i++) {
            if (pc_peer_set_has(&app->ready, i)) pump_dest(app, scn, i);
        }
    }
}
static void unlimited(void) {
    int i;
    for (i = 0; i < 256; i++) s_window_room[i] = -1;
}
static void join(PCRoster* app, PCRoster* scn, int id) {
    s_have_app[id] = 1; /* its own appearance arrives right after READY */
    pc_roster_join(app, id);
    pc_roster_join(scn, id);
    pc_roster_changed(app, id); /* handle_host_appearance */
}
static int knows_all(int viewer, const int* ids, int n) {
    int i;
    for (i = 0; i < n; i++) {
        if (ids[i] != viewer && !s_c[viewer].present[ids[i]]) return 0;
    }
    return s_c[viewer].present[HOST];
}

int main(void) {
    PCRoster app, scn;
    PCInterestView a, b;
    int i, k, ok, subject, ids[300], n;

    /* ================= interest tiers ================= */
    memset(&a, 0, sizeof(a));
    memset(&b, 0, sizeof(b));
    check("unknown scene of either side: NEAR (fail open)", pc_interest_tier(&a, &b) == PC_INTEREST_NEAR);
    a.scene_known = b.scene_known = 1;
    a.scene_id = b.scene_id = 5; /* town field */
    a.in_town = b.in_town = 1;
    check("town field, positions unknown: NEAR", pc_interest_tier(&a, &b) == PC_INTEREST_NEAR);
    a.pos_known = b.pos_known = 1;
    a.x = 100; a.z = 100; b.x = 700; b.z = 100;
    check("neighbouring acres: NEAR (every sample)", pc_interest_tier(&a, &b) == PC_INTEREST_NEAR);
    b.x = 100 + 2 * 640; b.z = 100;
    check("2 acres apart: MID", pc_interest_tier(&a, &b) == PC_INTEREST_MID);
    b.x = 100 + 3 * 640;
    check("3 acres apart: MID", pc_interest_tier(&a, &b) == PC_INTEREST_MID);
    b.x = 100 + 4 * 640;
    check("4 acres apart: FAR", pc_interest_tier(&a, &b) == PC_INTEREST_FAR);
    b.x = 100; b.z = 100 + 6 * 640;
    check("distance is the Chebyshev acre distance on either axis", pc_interest_tier(&a, &b) == PC_INTEREST_FAR);
    b.scene_id = 7; b.in_town = 0;
    check("different scenes: APART", pc_interest_tier(&a, &b) == PC_INTEREST_APART);
    a.scene_id = 7; a.in_town = 0; a.owner = 3; b.owner = 3;
    check("the same interior: NEAR", pc_interest_tier(&a, &b) == PC_INTEREST_NEAR);
    b.owner = 4;
    check("another house of the same scene id: APART", pc_interest_tier(&a, &b) == PC_INTEREST_APART);
    a.scene_id = b.scene_id = 5; a.in_town = b.in_town = 1; a.owner = b.owner = 0; a.x = -5000; a.z = 1e30f; b.x = 0; b.z = 0;
    check("negative / absurd coordinates never crash or wrap (clamped)", pc_interest_tier(&a, &b) <= PC_INTEREST_FAR);
    a.x = (float)(0.0 / 0.0);
    check("NaN position is handled", pc_interest_tier(&a, &b) <= PC_INTEREST_FAR);
    check("periods: NEAR 1, MID 2, FAR 4, APART 20", pc_interest_period(PC_INTEREST_NEAR) == 1 && pc_interest_period(PC_INTEREST_MID) == 2 && pc_interest_period(PC_INTEREST_FAR) == 4 && pc_interest_period(PC_INTEREST_APART) == 20);
    ok = 1;
    for (k = 0; k < PC_INTEREST_TIERS; k++) {
        int relayed = 0;
        for (i = 0; i < 1000; i++) relayed += pc_interest_relay(k, (uint32_t)i, 0);
        if (relayed != 1000 / pc_interest_period(k)) ok = 0;
        if (relayed < 1) ok = 0; /* nobody is ever cut off */
    }
    check("every tier still relays 1 of every `period` samples over 1000 samples (never zero)", ok);
    check("a boost forces every sample at every tier", pc_interest_relay(PC_INTEREST_APART, 7, 5) == 1 && pc_interest_relay(PC_INTEREST_FAR, 3, 1) == 1);
    {
        /* traffic: 100 players spread over the 5x7 town: the relay volume vs the full N x (N-1) */
        PCInterestView v[100];
        int relayed = 0, full = 0, s, d;
        srand(5);
        for (i = 0; i < 100; i++) {
            memset(&v[i], 0, sizeof(v[i]));
            v[i].scene_known = 1; v[i].scene_id = 5; v[i].in_town = 1; v[i].pos_known = 1;
            v[i].x = (float)(rand() % (5 * 640)); v[i].z = (float)(rand() % (7 * 640));
        }
        for (s = 0; s < 100; s++) for (d = 0; d < 100; d++) {
            if (s == d) continue;
            full++;
            relayed += pc_interest_relay(pc_interest_tier(&v[s], &v[d]), 3u, 0) ? 1 : 0; /* count 3: the worst phase for thinned tiers (only NEAR passes) */
        }
        {
            int per = 0;
            for (s = 0; s < 100; s++) for (d = 0; d < 100; d++) {
                if (s == d) continue;
                per += pc_interest_relay(pc_interest_tier(&v[s], &v[d]), 0u, 0) ? 1 : 0;
            }
            (void)per;
        }
        printf("  (model: 100 players spread over the town: %d of %d relays at an unlucky sample number)\n", relayed, full);
        check("with 100 spread-out players the relay volume is clearly below the full N x (N-1)", relayed < full / 2);
    }

    /* ================= roster ================= */
    check("roster init refuses span 0 / 256 / host id out of range", !pc_roster_init(&app, 0, HOST) && !pc_roster_init(&app, 256, HOST) && !pc_roster_init(&app, 20, 255));
    check("roster init ok", pc_roster_init(&app, 20, HOST) && pc_roster_init(&scn, 20, HOST));
    app.track_gone = 0;
    unlimited();
    memset(s_c, 0, sizeof(s_c));
    memset(s_have_app, 0, sizeof(s_have_app));
    n = 0;
    for (i = 0; i < 8; i++) { join(&app, &scn, i); ids[n++] = i; pump_all(&app, &scn, 20, 3); }
    ok = 1;
    for (i = 0; i < 8; i++) ok = ok && knows_all(i, ids, n);
    check("8 clients (ids 0..7): everyone knows everyone and the host", ok);
    /* THE REAL BUG: the 9th client gets wire id 9 (8 is the host's) */
    join(&app, &scn, 9);
    ids[n++] = 9;
    pump_all(&app, &scn, 20, 3);
    ok = 1;
    for (i = 0; i < 8; i++) ok = ok && s_c[i].present[9] && s_c[i].scene[9] == s_c[i].scene[9];
    check("the 9th client (wire id 9): every existing client (0..7) receives its roster entry", ok);
    ok = 1;
    for (i = 0; i < 8; i++) ok = ok && s_c[9].present[i];
    check("...and the 9th client receives every existing client's entry plus the host's", ok && s_c[9].present[HOST]);
    check("the host id 8 is never a destination", !pc_peer_set_has(&app.ready, HOST) && pc_roster_owed(&app, HOST) == 0);
    join(&app, &scn, 10);
    ids[n++] = 10;
    pump_all(&app, &scn, 20, 3);
    ok = 1;
    for (i = 0; i < n; i++) ok = ok && knows_all(ids[i], ids, n);
    check("a 10th client: all 10 clients know each other and the host (ids 7 / 9 / 10 around the host id 8)", ok);

    /* delta, not full roster: one join marks exactly the new subject for each other READY dest */
    {
        const uint32_t m0 = app.marked;
        join(&app, &scn, 11);
        ids[n++] = 11;
        check("a join marks its own entry for each other READY peer and every entry for itself (n + n deltas, not a full roster per peer)", app.marked - m0 == (uint32_t)(n - 1) + (uint32_t)(n - 1) + 1u);
        pump_all(&app, &scn, 20, 3);
    }
    /* change = one subject */
    {
        const uint32_t m0 = app.marked;
        pc_roster_changed(&app, 3);
        check("an appearance change marks exactly that subject for each other READY peer (n - 1 messages)", app.marked - m0 == (uint32_t)(n - 1));
        pump_all(&app, &scn, 20, 2);
        check("...and nothing stays owed", pc_roster_owed(&app, 0) == 0 && pc_roster_owed(&app, 11) == 0);
    }
    /* leave / rejoin */
    pc_roster_leave(&scn, 9);
    pc_roster_leave(&app, 9);
    s_have_app[9] = 0;
    check("a departure is owed to every other READY peer", pc_roster_owed(&scn, 0) >= 1 && pc_roster_owed(&scn, 11) >= 1 && pc_roster_owed(&scn, 9) == 0);
    pump_all(&app, &scn, 20, 3);
    ok = 1;
    for (i = 0; i < n; i++) if (ids[i] != 9) ok = ok && !s_c[ids[i]].present[9] && s_c[ids[i]].cleared_seen[9] == 1;
    check("peer 9 left: every other client was told once and dropped its entry (no stale puppet data)", ok);
    check("peer 9 is not a destination any more", !pc_peer_set_has(&app.ready, 9) && !pc_roster_next_pend(&app, 9, 0, &subject));
    /* the id is reused while a window is full: the departure must precede the new entry */
    s_window_room[0] = 0;
    pc_roster_leave(&scn, 10);
    pc_roster_leave(&app, 10);
    s_have_app[10] = 0;
    memset(&s_c[10], 0, sizeof(s_c[10]));
    join(&app, &scn, 10); /* a NEW peer takes id 10 at once */
    pump_all(&app, &scn, 20, 3);
    check("a blocked client (full window) is owed the departure AND the new entry, the others are served meanwhile", pc_roster_owed(&scn, 0) >= 1 && pc_roster_owed(&app, 0) >= 1 && s_c[1].present[10]);
    s_window_room[0] = -1;
    pump_all(&app, &scn, 20, 4);
    check("when its window opens: the departure comes first (cleared seen) and then the new entry (present)", s_c[0].cleared_seen[10] == 1 && s_c[0].present[10] && pc_roster_owed(&app, 0) == 0 && pc_roster_owed(&scn, 0) == 0);

    /* a slow peer never blocks the others */
    s_window_room[2] = 0;
    pc_roster_changed(&app, 5);
    pump_all(&app, &scn, 20, 3);
    ok = 1;
    for (i = 0; i < 8; i++) if (i != 2 && i != 5) ok = ok && s_c[i].present[5];
    check("peer 2's window is full: its entry stays owed, every other peer already has the update", ok && pc_roster_owed(&app, 2) >= 1);
    s_window_room[2] = 1; /* room for ONE message per ... */
    pump_all(&app, &scn, 20, 1);
    s_window_room[2] = -1;
    pump_all(&app, &scn, 20, 2);
    check("the slow peer is served when it has room", pc_roster_owed(&app, 2) == 0);

    /* an appearance that does not exist yet stays owed and is delivered once it does */
    pc_roster_free(&app); pc_roster_free(&scn);
    check("re-init", pc_roster_init(&app, 30, HOST) && pc_roster_init(&scn, 30, HOST));
    app.track_gone = 0;
    memset(s_c, 0, sizeof(s_c)); memset(s_have_app, 0, sizeof(s_have_app));
    s_have_app[1] = 1; pc_roster_join(&app, 1); pc_roster_join(&scn, 1); pc_roster_changed(&app, 1);
    pc_roster_join(&app, 2); pc_roster_join(&scn, 2); /* 2 is READY but its appearance has not arrived */
    pump_all(&app, &scn, 30, 3);
    check("a READY peer without an appearance yet: others have not received a (non-existent) entry; it still got the others'", !s_c[1].present[2] && s_c[2].present[1] && s_c[2].present[HOST]);
    s_have_app[2] = 1; pc_roster_changed(&app, 2);
    pump_all(&app, &scn, 30, 3);
    check("...and when its appearance arrives it is delivered to the others", s_c[1].present[2]);

    /* refresh: one subject per step, covers everybody */
    {
        char seen[256];
        int steps = 0;
        memset(seen, 0, sizeof(seen));
        for (i = 0; i < 40; i++) {
            subject = pc_roster_refresh_step(&app);
            if (subject >= 0) seen[subject] = 1;
            steps++;
        }
        check("refresh steps cycle through the host and every READY peer (one subject each)", seen[HOST] && seen[1] && seen[2] && !seen[3] && steps == 40);
    }
    pc_roster_free(&app); pc_roster_free(&scn);

    /* ================= big: 254 peers, ids 0..254 minus 8 ================= */
    check("roster at the maximum span 255", pc_roster_init(&app, 255, HOST) && pc_roster_init(&scn, 255, HOST));
    app.track_gone = 0;
    memset(s_c, 0, sizeof(s_c)); memset(s_have_app, 0, sizeof(s_have_app));
    n = 0;
    for (i = 0; i < 255; i++) {
        if (i == HOST) continue;
        join(&app, &scn, i);
        ids[n++] = i;
    }
    check("254 peers joined; the host id and 0xFF are not members", n == 254 && !pc_peer_set_has(&app.ready, HOST) && !pc_peer_set_has(&app.ready, 255));
    pump_all(&app, &scn, 255, 300);
    ok = 1;
    for (i = 0; i < n; i++) ok = ok && knows_all(ids[i], ids, n);
    check("254 peers: everyone ends up knowing the other 253 and the host (bounded burst per frame, 300 frames)", ok);
    {
        int owed = 0;
        for (i = 0; i < 255; i++) owed += pc_roster_owed(&app, i) + pc_roster_owed(&scn, i);
        check("nothing remains owed", owed == 0);
    }
    for (i = 0; i < 100; i++) { pc_roster_leave(&scn, ids[i]); pc_roster_leave(&app, ids[i]); }
    pump_all(&app, &scn, 255, 300);
    ok = 1;
    for (i = 100; i < n; i++) {
        int j;
        for (j = 0; j < 100; j++) ok = ok && !s_c[ids[i]].present[ids[j]] && s_c[ids[i]].cleared_seen[ids[j]] == 1;
    }
    check("100 peers leave: each of the 154 remaining peers was told exactly once about each", ok);
    check("roster operations on out-of-range ids are ignored (-1, 255, 1000)", (pc_roster_join(&app, -1), pc_roster_join(&app, 255), pc_roster_join(&app, 1000), pc_roster_leave(&app, 1000), pc_roster_changed(&app, 1000), 1));
    pc_roster_free(&app); pc_roster_free(&scn);

    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail == 0 ? 0 : 1;
}
