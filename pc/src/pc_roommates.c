/* pc_roommates.c - ROOMMATES / MOVE-IN, pure logic (see pc_roommates.h). No game state: the native selftest compiles this file alone. */
#ifdef _WIN32
#include <windows.h>
#endif
#include "pc_roommates.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef char pc_room_assert_entry[(sizeof(PCRoomWireEntry) == 32) ? 1 : -1];
typedef char pc_room_assert_msg[(sizeof(PCRoomMsg) == PC_ROOM_MSG_SIZE) ? 1 : -1];

void pc_room_init(PCRoomTable* t) {
    memset(t, 0, sizeof(*t));
}

const char* pc_room_state_name(int state) {
    return state == PC_ROOM_ST_PENDING ? "pending" : state == PC_ROOM_ST_ROOMMATE ? "roommate" : state == PC_ROOM_ST_DENIED ? "denied" : state == PC_ROOM_ST_REMOVED ? "removed" : "?";
}

const char* pc_room_result_text(int r) {
    switch (r) {
        case PC_ROOM_R_OK: return "Done";
        case PC_ROOM_R_BAD: return "That didn't work";
        case PC_ROOM_R_NOT_GYROID: return "Stand next to the gyroid";
        case PC_ROOM_R_NOT_OWNER: return "That isn't your gyroid";
        case PC_ROOM_R_NO_HOUSE: return "This gyroid has no house holder";
        case PC_ROOM_R_PENDING_ELSEWHERE: return "You already asked someone else";
        case PC_ROOM_R_ROOMMATE_ELSEWHERE: return "You already live in another house";
        case PC_ROOM_R_ALREADY: return "Already done";
        case PC_ROOM_R_COOLDOWN: return "Please wait a while before asking again";
        case PC_ROOM_R_HOUSE_FULL: return "That house is full";
        case PC_ROOM_R_LIST_FULL: return "Too many requests are waiting there";
        case PC_ROOM_R_NOT_FOUND: return "That request is no longer there";
        case PC_ROOM_R_BUSY: return "The server can't change roommates now";
        case PC_ROOM_R_OFF: return "Roommates are off on this server";
        case PC_ROOM_R_NOT_GUEST: return "Residents have a house of their own";
        case PC_ROOM_R_RATE: return "Slow down a little";
        case PC_ROOM_R_FAULT: return "The server could not save that";
        default: return "That didn't work";
    }
}

int pc_room_find(const PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN]) {
    int i;
    for (i = 0; i < t->n; i++) {
        if (memcmp(t->e[i].owner_key, owner, PC_ROOM_KEY_LEN) == 0 && memcmp(t->e[i].guest_key, guest, PC_ROOM_KEY_LEN) == 0) {
            return i;
        }
    }
    return -1;
}

int pc_room_find_state(const PCRoomTable* t, const uint8_t guest[PC_ROOM_KEY_LEN], int state) {
    int i;
    for (i = 0; i < t->n; i++) {
        if ((int)t->e[i].state == state && memcmp(t->e[i].guest_key, guest, PC_ROOM_KEY_LEN) == 0) {
            return i;
        }
    }
    return -1;
}

int pc_room_count(const PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], int state) {
    int i, n = 0;
    for (i = 0; i < t->n; i++) {
        if ((int)t->e[i].state == state && memcmp(t->e[i].owner_key, owner, PC_ROOM_KEY_LEN) == 0) {
            n++;
        }
    }
    return n;
}

void pc_room_delete_at(PCRoomTable* t, int i) {
    if (i < 0 || i >= t->n) {
        return;
    }
    memmove(&t->e[i], &t->e[i + 1], (size_t)(t->n - i - 1) * sizeof(t->e[0]));
    t->n--;
    memset(&t->e[t->n], 0, sizeof(t->e[0]));
    t->gen++;
}

static int room_tomb_active(const PCRoomEntry* e, uint32_t now) {
    if (e->state != PC_ROOM_ST_DENIED && e->state != PC_ROOM_ST_REMOVED) {
        return 0;
    }
    if (e->time > now + 86400u) {
        return 0; /* the clock went back by more than a day: expired */
    }
    return now < e->time || now - e->time < PC_ROOM_DENY_COOLDOWN_S;
}

int pc_room_request(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN], const uint8_t* owner_name, const uint8_t* guest_name,
                    uint32_t now) {
    const int here = pc_room_find(t, owner, guest);
    int i;
    PCRoomEntry* e;
    if (memcmp(owner, guest, PC_ROOM_KEY_LEN) == 0) {
        return PC_ROOM_R_BAD;
    }
    if (here >= 0 && (t->e[here].state == PC_ROOM_ST_PENDING || t->e[here].state == PC_ROOM_ST_ROOMMATE)) {
        return PC_ROOM_R_ALREADY;
    }
    if (pc_room_find_state(t, guest, PC_ROOM_ST_ROOMMATE) >= 0) {
        return PC_ROOM_R_ROOMMATE_ELSEWHERE;
    }
    if (pc_room_find_state(t, guest, PC_ROOM_ST_PENDING) >= 0) {
        return PC_ROOM_R_PENDING_ELSEWHERE;
    }
    if (here >= 0 && room_tomb_active(&t->e[here], now)) {
        return PC_ROOM_R_COOLDOWN;
    }
    if (pc_room_count(t, owner, PC_ROOM_ST_ROOMMATE) >= PC_ROOM_MAX_ROOMMATES) {
        return PC_ROOM_R_HOUSE_FULL;
    }
    if (pc_room_count(t, owner, PC_ROOM_ST_PENDING) >= PC_ROOM_MAX_PENDING) {
        return PC_ROOM_R_LIST_FULL;
    }
    if (here >= 0) {
        e = &t->e[here]; /* an expired tombstone of this pair is reused (its notice, if any, is superseded) */
    } else {
        if (t->n >= PC_ROOM_MAX) {
            (void)pc_room_gc(t, now);
        }
        if (t->n >= PC_ROOM_MAX) {
            return PC_ROOM_R_LIST_FULL;
        }
        e = &t->e[t->n++];
    }
    memset(e, 0, sizeof(*e));
    e->state = PC_ROOM_ST_PENDING;
    e->onote = PC_ROOM_ON_REQUEST;
    e->time = now;
    memcpy(e->owner_key, owner, PC_ROOM_KEY_LEN);
    memcpy(e->guest_key, guest, PC_ROOM_KEY_LEN);
    for (i = 0; i < PC_ROOM_NAME_LEN; i++) {
        e->owner_name[i] = owner_name != NULL ? owner_name[i] : (uint8_t)' ';
        e->guest_name[i] = guest_name != NULL ? guest_name[i] : (uint8_t)' ';
    }
    t->gen++;
    return PC_ROOM_R_OK;
}

static int room_pair_in(PCRoomTable* t, const uint8_t* owner, const uint8_t* guest, int state) {
    const int i = pc_room_find(t, owner, guest);
    return (i >= 0 && (int)t->e[i].state == state) ? i : -1;
}

int pc_room_cancel(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN]) {
    const int i = room_pair_in(t, owner, guest, PC_ROOM_ST_PENDING);
    if (i < 0) {
        return PC_ROOM_R_NOT_FOUND;
    }
    pc_room_delete_at(t, i);
    return PC_ROOM_R_OK;
}

int pc_room_accept(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN], uint32_t now) {
    const int i = room_pair_in(t, owner, guest, PC_ROOM_ST_PENDING);
    if (i < 0) {
        return PC_ROOM_R_NOT_FOUND;
    }
    if (pc_room_find_state(t, guest, PC_ROOM_ST_ROOMMATE) >= 0) {
        return PC_ROOM_R_ROOMMATE_ELSEWHERE; /* cannot happen through the request rules; a hand-edited file */
    }
    if (pc_room_count(t, owner, PC_ROOM_ST_ROOMMATE) >= PC_ROOM_MAX_ROOMMATES) {
        return PC_ROOM_R_HOUSE_FULL;
    }
    t->e[i].state = PC_ROOM_ST_ROOMMATE;
    t->e[i].gnote = PC_ROOM_GN_ACCEPTED;
    t->e[i].onote = PC_ROOM_ON_NONE;
    t->e[i].time = now;
    t->gen++;
    return PC_ROOM_R_OK;
}

static int room_to_tomb(PCRoomTable* t, int i, int state, int gnote, int onote, uint32_t now) {
    t->e[i].state = (uint8_t)state;
    t->e[i].gnote = (uint8_t)gnote;
    t->e[i].onote = (uint8_t)onote;
    t->e[i].time = now;
    t->gen++;
    return PC_ROOM_R_OK;
}

int pc_room_deny(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN], uint32_t now) {
    const int i = room_pair_in(t, owner, guest, PC_ROOM_ST_PENDING);
    return i < 0 ? PC_ROOM_R_NOT_FOUND : room_to_tomb(t, i, PC_ROOM_ST_DENIED, PC_ROOM_GN_DENIED, PC_ROOM_ON_NONE, now);
}

int pc_room_remove(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN], uint32_t now) {
    const int i = room_pair_in(t, owner, guest, PC_ROOM_ST_ROOMMATE);
    return i < 0 ? PC_ROOM_R_NOT_FOUND : room_to_tomb(t, i, PC_ROOM_ST_REMOVED, PC_ROOM_GN_REMOVED, PC_ROOM_ON_NONE, now);
}

int pc_room_leave(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], const uint8_t guest[PC_ROOM_KEY_LEN], uint32_t now) {
    const int i = room_pair_in(t, owner, guest, PC_ROOM_ST_ROOMMATE);
    return i < 0 ? PC_ROOM_R_NOT_FOUND : room_to_tomb(t, i, PC_ROOM_ST_REMOVED, PC_ROOM_GN_NONE, PC_ROOM_ON_LEFT, now);
}

int pc_room_owner_gone(PCRoomTable* t, const uint8_t owner[PC_ROOM_KEY_LEN], uint32_t now) {
    int i, n = 0;
    for (i = 0; i < t->n; i++) {
        PCRoomEntry* e = &t->e[i];
        if (memcmp(e->owner_key, owner, PC_ROOM_KEY_LEN) != 0) {
            continue;
        }
        if (e->state == PC_ROOM_ST_ROOMMATE) {
            (void)room_to_tomb(t, i, PC_ROOM_ST_REMOVED, PC_ROOM_GN_HOUSE_GONE, PC_ROOM_ON_NONE, now);
            n++;
        } else if (e->state == PC_ROOM_ST_PENDING) {
            (void)room_to_tomb(t, i, PC_ROOM_ST_DENIED, PC_ROOM_GN_HOUSE_GONE, PC_ROOM_ON_NONE, now);
            n++;
        } else if (e->onote != PC_ROOM_ON_NONE) {
            e->onote = PC_ROOM_ON_NONE; /* nobody to tell any more */
            t->gen++;
            n++;
        }
    }
    return n;
}

int pc_room_drop_guest(PCRoomTable* t, const uint8_t guest[PC_ROOM_KEY_LEN]) {
    int i, n = 0;
    for (i = t->n - 1; i >= 0; i--) {
        if (memcmp(t->e[i].guest_key, guest, PC_ROOM_KEY_LEN) == 0) {
            pc_room_delete_at(t, i);
            n++;
        }
    }
    return n;
}

int pc_room_gc(PCRoomTable* t, uint32_t now) {
    int i, n = 0;
    for (i = t->n - 1; i >= 0; i--) {
        const PCRoomEntry* e = &t->e[i];
        if ((e->state == PC_ROOM_ST_DENIED || e->state == PC_ROOM_ST_REMOVED) && e->gnote == PC_ROOM_GN_NONE && e->onote == PC_ROOM_ON_NONE && !room_tomb_active(e, now)) {
            pc_room_delete_at(t, i);
            n++;
        }
    }
    return n;
}

/* ---- text form ---- */
static int room_hexval(char c) {
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

/* n bytes of hex at *pp followed by a blank / end; advances *pp past the token and its blanks. 1 = ok */
static int room_hex_tok(const char** pp, uint8_t* out, int n) {
    const char* v = *pp;
    int i;
    for (i = 0; i < n; i++) {
        const int hi = room_hexval(v[2 * i]);
        const int lo = hi < 0 ? -1 : room_hexval(v[2 * i + 1]);
        if (hi < 0 || lo < 0) {
            return 0;
        }
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    v += 2 * n;
    if (*v != '\0' && *v != ' ' && *v != '\t') {
        return 0;
    }
    while (*v == ' ' || *v == '\t') {
        v++;
    }
    *pp = v;
    return 1;
}

static int room_num_tok(const char** pp, uint32_t* out, uint32_t max) {
    const char* v = *pp;
    uint32_t x = 0;
    int n = 0;
    while (*v >= '0' && *v <= '9') {
        const uint32_t d = (uint32_t)(*v - '0');
        if (x > (0xFFFFFFFFu - d) / 10u) {
            return 0;
        }
        x = x * 10u + d;
        v++;
        n++;
    }
    if (n == 0 || n > 10 || x > max || (*v != '\0' && *v != ' ' && *v != '\t')) {
        return 0;
    }
    while (*v == ' ' || *v == '\t') {
        v++;
    }
    *out = x;
    *pp = v;
    return 1;
}

/* 1 = an entry was added, 0 = nothing to add, -1 = malformed / refused */
static int room_parse_line(PCRoomTable* t, char* s) {
    char* p = s;
    char* eq;
    const char* v;
    PCRoomEntry e;
    uint32_t st, tm, gn, on;
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    if (*p == '\0' || *p == '#' || *p == ';') {
        return 0;
    }
    eq = strchr(p, '=');
    if (eq == NULL) {
        return -1;
    }
    *eq = '\0';
    {
        char* x = eq;
        while (x > p && (x[-1] == ' ' || x[-1] == '\t')) {
            *--x = '\0';
        }
    }
    v = eq + 1;
    while (*v == ' ' || *v == '\t') {
        v++;
    }
    if (strcmp(p, "version") == 0) {
        return 0;
    }
    if (strcmp(p, "room") != 0) {
        return -1;
    }
    memset(&e, 0, sizeof(e));
    if (!room_num_tok(&v, &st, 4u) || st < 1u || !room_hex_tok(&v, e.owner_key, PC_ROOM_KEY_LEN) || !room_hex_tok(&v, e.guest_key, PC_ROOM_KEY_LEN) ||
        !room_num_tok(&v, &tm, 0xFFFFFFFFu) || !room_num_tok(&v, &gn, 4u) || !room_num_tok(&v, &on, 2u) || !room_hex_tok(&v, e.owner_name, PC_ROOM_NAME_LEN) ||
        !room_hex_tok(&v, e.guest_name, PC_ROOM_NAME_LEN)) {
        return -1; /* extra trailing fields of a later version are fine (ignored) */
    }
    e.state = (uint8_t)st;
    e.time = tm;
    e.gnote = (uint8_t)gn;
    e.onote = (uint8_t)on;
    if (memcmp(e.owner_key, e.guest_key, PC_ROOM_KEY_LEN) == 0 || pc_room_find(t, e.owner_key, e.guest_key) >= 0) {
        return -1; /* self relation / duplicate pair: the first line wins */
    }
    if ((e.state == PC_ROOM_ST_ROOMMATE && pc_room_find_state(t, e.guest_key, PC_ROOM_ST_ROOMMATE) >= 0) ||
        (e.state == PC_ROOM_ST_PENDING && pc_room_find_state(t, e.guest_key, PC_ROOM_ST_PENDING) >= 0)) {
        return -1; /* one house / one open request per guest */
    }
    if ((e.state == PC_ROOM_ST_ROOMMATE && pc_room_count(t, e.owner_key, PC_ROOM_ST_ROOMMATE) >= PC_ROOM_MAX_ROOMMATES) ||
        (e.state == PC_ROOM_ST_PENDING && pc_room_count(t, e.owner_key, PC_ROOM_ST_PENDING) >= PC_ROOM_MAX_PENDING) || t->n >= PC_ROOM_MAX) {
        return -1;
    }
    t->e[t->n++] = e;
    return 1;
}

int pc_room_parse(PCRoomTable* t, const char* text, size_t len, int* bad_lines) {
    char line[512];
    size_t i = 0;
    int bad = 0;
    pc_room_init(t);
    while (i < len) {
        size_t n = 0;
        int too_long = 0;
        while (i < len && text[i] != '\n') {
            if (n + 1 < sizeof(line)) {
                line[n++] = text[i];
            } else {
                too_long = 1;
            }
            i++;
        }
        if (i < len) {
            i++;
        }
        while (n > 0 && (line[n - 1] == '\r' || line[n - 1] == ' ' || line[n - 1] == '\t')) {
            n--;
        }
        line[n] = '\0';
        if (too_long || room_parse_line(t, line) < 0) {
            bad++;
        }
    }
    t->gen = 1;
    if (bad_lines != NULL) {
        *bad_lines = bad;
    }
    return t->n;
}

static void room_hex(char* out, const uint8_t* b, int n) {
    static const char d[] = "0123456789abcdef";
    int k;
    for (k = 0; k < n; k++) {
        out[2 * k] = d[b[k] >> 4];
        out[2 * k + 1] = d[b[k] & 15];
    }
    out[2 * n] = '\0';
}

int pc_room_format(const PCRoomTable* t, char* out, size_t cap) {
    size_t used;
    int i, w;
    w = snprintf(out, cap,
                 "# roommates.ini - who shares whose house on this server (guests that moved in with a resident). Written by the server; change it in the game\n"
                 "# (the house holder's gyroid) or with the console command `roommates`.\n"
                 "# room = <state 1 pending|2 roommate|3 denied|4 removed> <holder key> <guest key> <unix time> <guest note> <holder note> <holder name hex> <guest name hex>\n"
                 "version = %d\n",
                 PC_ROOM_VERSION);
    if (w < 0 || (size_t)w >= cap) {
        return -1;
    }
    used = (size_t)w;
    for (i = 0; i < t->n; i++) {
        const PCRoomEntry* e = &t->e[i];
        char ok[2 * PC_ROOM_KEY_LEN + 1], gk[2 * PC_ROOM_KEY_LEN + 1], on[2 * PC_ROOM_NAME_LEN + 1], gname[2 * PC_ROOM_NAME_LEN + 1];
        room_hex(ok, e->owner_key, PC_ROOM_KEY_LEN);
        room_hex(gk, e->guest_key, PC_ROOM_KEY_LEN);
        room_hex(on, e->owner_name, PC_ROOM_NAME_LEN);
        room_hex(gname, e->guest_name, PC_ROOM_NAME_LEN);
        w = snprintf(out + used, cap - used, "room = %u %s %s %u %u %u %s %s\n", (unsigned)e->state, ok, gk, (unsigned)e->time, (unsigned)e->gnote, (unsigned)e->onote, on,
                     gname);
        if (w < 0 || (size_t)w >= cap - used) {
            return -1;
        }
        used += (size_t)w;
    }
    return (int)used;
}

static int room_read_file(const char* path, PCRoomTable* t, int* bad_lines) {
    FILE* f;
    char* buf;
    long sz;
    size_t got;
    if (path == NULL || (f = fopen(path, "rb")) == NULL) {
        return PC_ROOM_FILE_MISSING;
    }
    if (fseek(f, 0, SEEK_END) != 0 || (sz = ftell(f)) < 0 || sz > 256 * 1024 || fseek(f, 0, SEEK_SET) != 0) {
        fclose(f);
        return PC_ROOM_FILE_BAD;
    }
    buf = (char*)malloc((size_t)sz + 1);
    if (buf == NULL) {
        fclose(f);
        return PC_ROOM_FILE_BAD;
    }
    got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    if (got != (size_t)sz) {
        free(buf);
        return PC_ROOM_FILE_BAD;
    }
    (void)pc_room_parse(t, buf, got, bad_lines);
    free(buf);
    return PC_ROOM_FILE_OK;
}

int pc_room_load(const char* path, PCRoomTable* t, int* bad_lines, int* from_bak) {
    char bak[600];
    int rc;
    pc_room_init(t);
    if (bad_lines != NULL) {
        *bad_lines = 0;
    }
    if (from_bak != NULL) {
        *from_bak = 0;
    }
    rc = room_read_file(path, t, bad_lines);
    if (rc != PC_ROOM_FILE_MISSING || path == NULL || strlen(path) + 5 > sizeof(bak)) {
        return rc;
    }
    snprintf(bak, sizeof(bak), "%s.bak", path); /* a crash between the two renames of a save leaves only the .bak */
    rc = room_read_file(bak, t, bad_lines);
    if (rc == PC_ROOM_FILE_OK && from_bak != NULL) {
        *from_bak = 1;
    }
    return rc;
}

static int room_move(const char* from, const char* to) {
#ifdef _WIN32
    return MoveFileExA(from, to, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) ? 1 : 0;
#else
    return rename(from, to) == 0;
#endif
}

int pc_room_save(const char* path, const PCRoomTable* t) {
    char tmp[600], bak[600];
    char* buf;
    int n, ok;
    FILE* f;
    const size_t cap = 1024 + (size_t)PC_ROOM_MAX * 200;
    if (path == NULL || path[0] == '\0' || strlen(path) + 5 > sizeof(tmp)) {
        return 0;
    }
    buf = (char*)malloc(cap);
    if (buf == NULL) {
        return 0;
    }
    n = pc_room_format(t, buf, cap);
    if (n < 0) {
        free(buf);
        return 0;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    snprintf(bak, sizeof(bak), "%s.bak", path);
    f = fopen(tmp, "wb");
    if (f == NULL) {
        free(buf);
        return 0;
    }
    ok = fwrite(buf, 1, (size_t)n, f) == (size_t)n && fflush(f) == 0;
    ok = (fclose(f) == 0) && ok;
    free(buf);
    if (!ok) {
        remove(tmp);
        return 0;
    }
    {
        FILE* old = fopen(path, "rb");
        if (old != NULL) {
            fclose(old);
            (void)room_move(path, bak); /* the previous generation (also the crash fallback of pc_room_load) */
        }
    }
    if (!room_move(tmp, path)) {
        (void)room_move(bak, path);
        remove(tmp);
        return 0;
    }
    return 1;
}

/* ---- message 104 ---- */
int pc_room_msg_valid(const PCRoomMsg* m, int from_client) {
    int i;
    if (m->msg_type != PC_ROOM_MSG_ID || m->flags != 0) {
        return 0;
    }
    if (from_client) {
        const int needs_one = m->op == PC_ROOM_OP_ACCEPT || m->op == PC_ROOM_OP_DENY || m->op == PC_ROOM_OP_REMOVE;
        static const uint8_t zero[sizeof(m->owner_name) + sizeof(m->owner_key)] = {0};
        if (m->op < PC_ROOM_OP_QUERY || m->op > PC_ROOM_OP_CLIENT_MAX || m->result != 0 || m->role != 0 || m->house != 0 || m->aux != 0 ||
            m->count != (uint8_t)(needs_one ? 1 : 0) || memcmp(m->owner_name, zero, sizeof(m->owner_name)) != 0 || memcmp(m->owner_key, zero, sizeof(m->owner_key)) != 0) {
            return 0;
        }
        if (m->ut_x < 16 || m->ut_x > 95 || m->ut_z < 16 || m->ut_z > 111 || m->item == 0) {
            return 0;
        }
        for (i = 0; i < PC_ROOM_MSG_ENTRIES; i++) {
            const PCRoomWireEntry* e = &m->e[i];
            if (i < (int)m->count) {
                if (e->state != 0 || e->online != 0 || e->rsv[0] != 0 || e->rsv[1] != 0) {
                    return 0;
                }
            } else {
                static const uint8_t z[sizeof(PCRoomWireEntry)] = {0};
                if (memcmp(e, z, sizeof(z)) != 0) {
                    return 0;
                }
            }
        }
        return 1;
    }
    if (m->op < PC_ROOM_OP_INFO || m->op > PC_ROOM_OP_NOTICE || m->count > PC_ROOM_MSG_ENTRIES || m->result > PC_ROOM_R_MAX || m->role > PC_ROOM_ROLE_MAX) {
        return 0;
    }
    if (m->house != 0xFF && m->house > 3) {
        return 0;
    }
    for (i = 0; i < (int)m->count; i++) {
        const PCRoomWireEntry* e = &m->e[i];
        if ((e->state != PC_ROOM_ST_PENDING && e->state != PC_ROOM_ST_ROOMMATE && m->op != PC_ROOM_OP_NOTICE) || e->online > 1 || e->rsv[0] != 0 || e->rsv[1] != 0) {
            return 0;
        }
    }
    return 1;
}

/* ---- native selftest ---- */
static int s_room_fail;
static void room_ck(int ok, const char* what) {
    if (!ok) {
        s_room_fail++;
        printf("[ROOM] selftest FAIL: %s\n", what);
    }
}

static void room_key(uint8_t* k, int seed) {
    int i;
    for (i = 0; i < PC_ROOM_KEY_LEN; i++) {
        k[i] = (uint8_t)(seed * 31 + i * 7 + 1);
    }
}

int pc_room_selftest(void) {
    PCRoomTable t, u;
    uint8_t o1[PC_ROOM_KEY_LEN], o2[PC_ROOM_KEY_LEN], g[6][PC_ROOM_KEY_LEN];
    const uint8_t on[PC_ROOM_NAME_LEN] = {'O', 'w', 'n', 'e', 'r', ' ', ' ', ' '}, gn[PC_ROOM_NAME_LEN] = {'G', 'u', 'e', 's', 't', ' ', ' ', ' '};
    static char txt[32768];
    int i, n, bad;
    const uint32_t T0 = 1700000000u;
    s_room_fail = 0;
    room_key(o1, 1);
    room_key(o2, 2);
    for (i = 0; i < 6; i++) {
        room_key(g[i], 10 + i);
    }
    pc_room_init(&t);
    room_ck(pc_room_request(&t, o1, o1, on, on, T0) == PC_ROOM_R_BAD, "self request refused");
    room_ck(pc_room_request(&t, o1, g[0], on, gn, T0) == PC_ROOM_R_OK && t.n == 1 && t.e[0].onote == PC_ROOM_ON_REQUEST, "request -> pending, holder note");
    room_ck(pc_room_request(&t, o1, g[0], on, gn, T0) == PC_ROOM_R_ALREADY, "the same request twice = ALREADY");
    room_ck(pc_room_request(&t, o2, g[0], on, gn, T0) == PC_ROOM_R_PENDING_ELSEWHERE, "one open request per guest");
    room_ck(pc_room_cancel(&t, o1, g[0]) == PC_ROOM_R_OK && t.n == 0, "cancel deletes it");
    room_ck(pc_room_cancel(&t, o1, g[0]) == PC_ROOM_R_NOT_FOUND, "cancel twice");
    room_ck(pc_room_request(&t, o1, g[0], on, gn, T0) == PC_ROOM_R_OK, "request again");
    room_ck(pc_room_accept(&t, o1, g[0], T0 + 1) == PC_ROOM_R_OK && t.e[0].state == PC_ROOM_ST_ROOMMATE && t.e[0].gnote == PC_ROOM_GN_ACCEPTED && t.e[0].onote == 0,
            "accept -> roommate, guest note");
    room_ck(pc_room_accept(&t, o1, g[0], T0 + 1) == PC_ROOM_R_NOT_FOUND, "accept twice");
    room_ck(pc_room_request(&t, o2, g[0], on, gn, T0) == PC_ROOM_R_ROOMMATE_ELSEWHERE, "a roommate cannot ask elsewhere");
    room_ck(pc_room_request(&t, o1, g[0], on, gn, T0) == PC_ROOM_R_ALREADY, "a roommate asking the same holder = ALREADY");
    /* house full / list full */
    for (i = 1; i < 4; i++) {
        (void)pc_room_request(&t, o1, g[i], on, gn, T0);
        (void)pc_room_accept(&t, o1, g[i], T0);
    }
    room_ck(pc_room_count(&t, o1, PC_ROOM_ST_ROOMMATE) == PC_ROOM_MAX_ROOMMATES, "4 roommates");
    room_ck(pc_room_request(&t, o1, g[4], on, gn, T0) == PC_ROOM_R_HOUSE_FULL, "a full house refuses requests");
    room_ck(pc_room_remove(&t, o1, g[3], T0 + 5) == PC_ROOM_R_OK && t.e[3].state == PC_ROOM_ST_REMOVED && t.e[3].gnote == PC_ROOM_GN_REMOVED, "remove -> tombstone + note");
    room_ck(pc_room_request(&t, o1, g[3], on, gn, T0 + 10) == PC_ROOM_R_COOLDOWN, "removed guest: cooldown at the same holder");
    room_ck(pc_room_request(&t, o2, g[3], on, gn, T0 + 10) == PC_ROOM_R_OK, "removed guest may ask another holder at once");
    room_ck(pc_room_cancel(&t, o2, g[3]) == PC_ROOM_R_OK, "cancel");
    room_ck(pc_room_request(&t, o1, g[4], on, gn, T0) == PC_ROOM_R_OK && pc_room_deny(&t, o1, g[4], T0 + 20) == PC_ROOM_R_OK, "request + deny");
    room_ck(pc_room_request(&t, o1, g[4], on, gn, T0 + 30) == PC_ROOM_R_COOLDOWN, "denied: cooldown");
    room_ck(pc_room_gc(&t, T0 + 10000) == 0, "gc keeps tombstones that still carry a guest note");
    t.e[pc_room_find(&t, o1, g[4])].gnote = 0;
    room_ck(pc_room_gc(&t, T0 + 30) == 0, "gc keeps an active cooldown");
    room_ck(pc_room_gc(&t, T0 + 20 + PC_ROOM_DENY_COOLDOWN_S) == 1 && pc_room_find(&t, o1, g[4]) < 0, "gc drops an expired tombstone without notes");
    room_ck(pc_room_request(&t, o1, g[4], on, gn, T0 + 30 + PC_ROOM_DENY_COOLDOWN_S) == PC_ROOM_R_OK, "after the cooldown the guest may ask again");
    room_ck(pc_room_leave(&t, o1, g[2], T0 + 40) == PC_ROOM_R_OK && t.e[pc_room_find(&t, o1, g[2])].onote == PC_ROOM_ON_LEFT, "leave -> holder note LEFT");
    /* round trip */
    n = pc_room_format(&t, txt, sizeof(txt));
    room_ck(n > 0, "format");
    pc_room_parse(&u, txt, (size_t)n, &bad);
    room_ck(bad == 0 && u.n == t.n && memcmp(u.e, t.e, sizeof(t.e[0]) * (size_t)t.n) == 0, "round trip is lossless");
    room_ck(pc_room_owner_gone(&t, o1, T0 + 50) >= 3 && pc_room_count(&t, o1, PC_ROOM_ST_ROOMMATE) == 0 && pc_room_count(&t, o1, PC_ROOM_ST_PENDING) == 0,
            "owner gone: every roommate / request ended");
    room_ck(t.e[pc_room_find(&t, o1, g[0])].gnote == PC_ROOM_GN_HOUSE_GONE, "owner gone: the guest is told");
    room_ck(pc_room_drop_guest(&t, g[0]) == 1 && pc_room_find(&t, o1, g[0]) < 0, "drop guest");
    {
        char bad_txt[2048];
        char a[41], b2[41];
        room_hex(a, o1, PC_ROOM_KEY_LEN);
        room_hex(b2, g[5], PC_ROOM_KEY_LEN);
        snprintf(bad_txt, sizeof(bad_txt),
                 "garbage\r\nversion = 7\r\nunknown = 1\r\nroom = 9 %s %s 1 0 0 0000000000000000 0000000000000000\r\n"
                 "room = 2 %s %s 100 1 0 4f776e6572202020 4775657374202020 extra fields of v2\r\n"
                 "room = 1 %s %s 100 0 1 4f776e6572202020 4775657374202020\r\n"
                 "room = 2 %s %s 100 0 0 4f776e6572202020 4775657374202020\r\n"
                 "room = 2 %s %s 100 0 0 4f776e65722020 4775657374202020\r\n",
                 a, b2, a, b2, a, b2, b2, b2, b2, a);
        pc_room_parse(&u, bad_txt, strlen(bad_txt), &bad);
        room_ck(u.n == 1 && u.e[0].state == PC_ROOM_ST_ROOMMATE && u.e[0].time == 100u && u.e[0].gnote == 1, "tolerant parser: one valid (v2 extra fields ignored)");
        room_ck(bad == 6, "tolerant parser: 6 bad lines (garbage, unknown key, bad state, duplicate pair, self relation, short name)");
    }
    /* message validator */
    {
        PCRoomMsg m;
        memset(&m, 0, sizeof(m));
        m.msg_type = PC_ROOM_MSG_ID;
        m.op = PC_ROOM_OP_QUERY;
        m.ut_x = 40;
        m.ut_z = 50;
        m.item = 0x15B0;
        room_ck(pc_room_msg_valid(&m, 1), "client QUERY valid");
        room_ck(!pc_room_msg_valid(&m, 0), "a client op is not a host message");
        m.count = 1;
        room_ck(!pc_room_msg_valid(&m, 1), "QUERY with an entry refused");
        m.op = PC_ROOM_OP_ACCEPT;
        memcpy(m.e[0].key, g[0], PC_ROOM_KEY_LEN);
        room_ck(pc_room_msg_valid(&m, 1), "ACCEPT with one entry valid");
        m.e[1].name[0] = 1;
        room_ck(!pc_room_msg_valid(&m, 1), "a used unused entry refused");
        m.e[1].name[0] = 0;
        m.ut_x = 5;
        room_ck(!pc_room_msg_valid(&m, 1), "a tile out of the town refused");
        m.ut_x = 40;
        m.op = 9;
        room_ck(!pc_room_msg_valid(&m, 1), "unknown client op refused");
        memset(&m, 0, sizeof(m));
        m.msg_type = PC_ROOM_MSG_ID;
        m.op = PC_ROOM_OP_INFO;
        m.role = PC_ROOM_ROLE_OWNER;
        m.house = 0xFF;
        m.count = 2;
        m.e[0].state = PC_ROOM_ST_PENDING;
        m.e[1].state = PC_ROOM_ST_ROOMMATE;
        room_ck(pc_room_msg_valid(&m, 0), "host INFO valid");
        m.e[1].state = PC_ROOM_ST_DENIED;
        room_ck(!pc_room_msg_valid(&m, 0), "INFO entry of a tombstone state refused");
        m.e[1].state = PC_ROOM_ST_ROOMMATE;
        m.house = 4;
        room_ck(!pc_room_msg_valid(&m, 0), "house 4 refused");
        m.house = 2;
        m.role = 7;
        room_ck(!pc_room_msg_valid(&m, 0), "unknown role refused");
        m.role = 0;
        m.flags = 1;
        room_ck(!pc_room_msg_valid(&m, 0), "flags must be 0");
    }
    for (i = 0; i <= PC_ROOM_R_MAX; i++) {
        const char* s = pc_room_result_text(i);
        const char* c;
        for (c = s; *c != '\0'; c++) {
            const char ch = *c;
            room_ck((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == ' ' || ch == '\'' || ch == '.', "result text charset");
        }
    }
    printf("[ROOM] selftest: %s (%d failure(s))\n", s_room_fail ? "FAIL" : "PASS", s_room_fail);
    return s_room_fail;
}
