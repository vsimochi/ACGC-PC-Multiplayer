/* pc_outdoor_furniture.c - OUTDOOR FURNITURE (C1): the pure logic (marker table, sidecar, message 98 / service 10 shapes, kind 22 shape, geometry, rate limit,
 * notice). No game state: the native selftest (pc_ofurn_selftest) compiles this file alone. See pc_outdoor_furniture.h. */
#ifdef _WIN32
#include <windows.h>
#endif
#include "pc_outdoor_furniture.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef char pc_ofurn_marker_size_check[(sizeof(PCOfurnMarker) == 52) ? 1 : -1];
typedef char pc_ofurn_msg_size_check[(sizeof(PCOfurnMsg) == 268) ? 1 : -1];

void pc_ofurn_table_init(PCOfurnTable* t) {
    t->n = 0;
    t->gen = 0;
    memset(t->m, 0, sizeof(t->m));
}

int pc_ofurn_find(const PCOfurnTable* t, int ut_x, int ut_z) {
    int i;
    for (i = 0; i < t->n; i++) {
        if ((int)t->m[i].ut_x == ut_x && (int)t->m[i].ut_z == ut_z) {
            return i;
        }
    }
    return -1;
}

int pc_ofurn_add(PCOfurnTable* t, const PCOfurnMarker* m) {
    const int i = pc_ofurn_find(t, m->ut_x, m->ut_z);
    if (i >= 0) {
        t->m[i] = *m;
        t->gen++;
        return 1;
    }
    if (t->n >= PC_OFURN_TABLE_CAP) {
        return 0;
    }
    t->m[t->n++] = *m;
    t->gen++;
    return 1;
}

int pc_ofurn_remove_at(PCOfurnTable* t, int idx) {
    if (idx < 0 || idx >= t->n) {
        return 0;
    }
    t->m[idx] = t->m[t->n - 1];
    memset(&t->m[t->n - 1], 0, sizeof(t->m[0]));
    t->n--;
    t->gen++;
    return 1;
}

int pc_ofurn_acre_of(int ut_x, int ut_z) {
    const int bx = ut_x / 16, bz = ut_z / 16;
    if (ut_x < 0 || ut_z < 0 || bx < 1 || bx > 5 || bz < 1 || bz > 6) {
        return -1;
    }
    return (bz - 1) * 5 + (bx - 1);
}

int pc_ofurn_count_acre(const PCOfurnTable* t, int acre) {
    int i, n = 0;
    for (i = 0; i < t->n; i++) {
        if (pc_ofurn_acre_of(t->m[i].ut_x, t->m[i].ut_z) == acre) {
            n++;
        }
    }
    return n;
}

uint16_t pc_ofurn_tile_value(const PCOfurnMarker* m) {
    return (uint16_t)((m->base & 0xFFFCu) | (m->rot & 3u));
}

int pc_ofurn_is_ftr(uint16_t item) {
    const unsigned type = (unsigned)(item >> 12);
    return type == 1u || type == 3u;
}

uint16_t pc_ofurn_base(uint16_t item) {
    return (uint16_t)(item & 0xFFFCu);
}

int pc_ofurn_caps_check(const PCOfurnTable* t, int acre, int max_town, int max_acre) {
    if (max_town > PC_OFURN_TABLE_CAP) {
        max_town = PC_OFURN_TABLE_CAP;
    }
    if (t->n >= max_town) {
        return 1;
    }
    if (pc_ofurn_count_acre(t, acre) >= max_acre) {
        return 2;
    }
    return 0;
}

static uint32_t ofurn_fnv(const void* p, size_t n, uint32_t h) {
    const unsigned char* b = (const unsigned char*)p;
    size_t i;
    for (i = 0; i < n; i++) {
        h = (h ^ b[i]) * 16777619u;
    }
    return h;
}

int pc_ofurn_save(const char* path, const PCOfurnTable* t) {
    char tmp[600];
    uint32_t hdr[3], h;
    FILE* f;
    int ok;
    if (path == NULL || path[0] == '\0' || strlen(path) + 5 > sizeof(tmp) || t->n < 0 || t->n > PC_OFURN_TABLE_CAP) {
        return 0;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    f = fopen(tmp, "wb");
    if (f == NULL) {
        return 0;
    }
    hdr[0] = PC_OFURN_FILE_MAGIC;
    hdr[1] = PC_OFURN_FILE_VERSION;
    hdr[2] = (uint32_t)t->n;
    h = ofurn_fnv(hdr, sizeof(hdr), 2166136261u);
    h = ofurn_fnv(t->m, (size_t)t->n * sizeof(t->m[0]), h);
    ok = fwrite(hdr, sizeof(hdr), 1, f) == 1;
    ok = ok && (t->n == 0 || fwrite(t->m, sizeof(t->m[0]), (size_t)t->n, f) == (size_t)t->n);
    ok = ok && fwrite(&h, sizeof(h), 1, f) == 1 && fflush(f) == 0;
    ok = (fclose(f) == 0) && ok;
    if (!ok) {
        remove(tmp);
        return 0;
    }
#ifdef _WIN32
    if (!MoveFileExA(tmp, path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        remove(tmp);
        return 0;
    }
#else
    if (rename(tmp, path) != 0) {
        remove(tmp);
        return 0;
    }
#endif
    return 1;
}

int pc_ofurn_load(const char* path, PCOfurnTable* t, int* dropped) {
    FILE* f;
    uint32_t hdr[3], h, check;
    int i, drop = 0;
    PCOfurnMarker m;
    pc_ofurn_table_init(t);
    if (dropped != NULL) {
        *dropped = 0;
    }
    if (path == NULL || (f = fopen(path, "rb")) == NULL) {
        return PC_OFURN_FILE_MISSING;
    }
    if (fread(hdr, sizeof(hdr), 1, f) != 1 || hdr[0] != PC_OFURN_FILE_MAGIC || hdr[1] != PC_OFURN_FILE_VERSION || hdr[2] > (uint32_t)PC_OFURN_TABLE_CAP) {
        fclose(f);
        return PC_OFURN_FILE_BAD;
    }
    h = ofurn_fnv(hdr, sizeof(hdr), 2166136261u);
    for (i = 0; i < (int)hdr[2]; i++) {
        if (fread(&m, sizeof(m), 1, f) != 1) {
            fclose(f);
            pc_ofurn_table_init(t);
            return PC_OFURN_FILE_BAD;
        }
        h = ofurn_fnv(&m, sizeof(m), h);
        m.placer_name[PC_OFURN_NAME_MAX - 1] = '\0';
        if (pc_ofurn_acre_of(m.ut_x, m.ut_z) < 0 || !pc_ofurn_is_ftr(m.base) || (m.base & 3u) != 0u || m.rot > 3u || pc_ofurn_find(t, m.ut_x, m.ut_z) >= 0) {
            drop++; /* never a marker on a non-town tile / for a non-furniture id / twice on a tile */
            continue;
        }
        t->m[t->n++] = m;
    }
    if (fread(&check, sizeof(check), 1, f) != 1 || check != h) {
        fclose(f);
        pc_ofurn_table_init(t);
        return PC_OFURN_FILE_BAD;
    }
    fclose(f);
    t->gen = 1;
    if (dropped != NULL) {
        *dropped = drop;
    }
    return PC_OFURN_FILE_OK;
}

int pc_ofurn_msg_valid(const PCOfurnMsg* m) {
    int i;
    if (m->msg_type != (uint8_t)PC_OFURN_MSG_ID || m->kind < PC_OFURN_MK_BEGIN || m->kind > PC_OFURN_MK_PERMS || m->count > PC_OFURN_MSG_ENTRIES) {
        return 0;
    }
    if (m->kind == PC_OFURN_MK_BEGIN && (m->count != 0 || m->total > (uint32_t)PC_OFURN_TABLE_CAP)) {
        return 0;
    }
    if ((m->kind == PC_OFURN_MK_END || m->kind == PC_OFURN_MK_PERMS) && m->count != 0) {
        return 0;
    }
    if (m->kind == PC_OFURN_MK_PERMS && (m->flags & ~(PC_OFURN_PERM_PLACE | PC_OFURN_PERM_PICKUP | PC_OFURN_PERM_OP | PC_OFURN_PERM_LEAF_DROP)) != 0u) {
        return 0;
    }
    for (i = 0; i < (int)m->count; i++) {
        if (pc_ofurn_acre_of(m->e[i].ut_x, m->e[i].ut_z) < 0 || (m->kind != PC_OFURN_MK_DEL && !pc_ofurn_is_ftr(m->e[i].item))) {
            return 0;
        }
    }
    return 1;
}

void pc_ofurn_caps_build(uint8_t* blob, int place_on, int leaf_on, int max_town, int max_acre) {
    memset(blob, 0, PC_OFURN_CAPS_LEN);
    blob[0] = (uint8_t)PC_OFURN_CAPS_VERSION;
    blob[1] = (uint8_t)((place_on ? 1u : 0u) | (leaf_on ? 2u : 0u));
    blob[2] = (uint8_t)(max_town & 0xFF);
    blob[3] = (uint8_t)((max_town >> 8) & 0xFF);
    blob[4] = (uint8_t)(max_acre & 0xFF);
    blob[5] = (uint8_t)((max_acre >> 8) & 0xFF);
}

int pc_ofurn_caps_valid(const uint8_t* blob) {
    return blob[0] == (uint8_t)PC_OFURN_CAPS_VERSION && (blob[1] & ~3u) == 0u && blob[6] == 0u && blob[7] == 0u;
}

int pc_ofurn_shape_ok(int op, unsigned dest, unsigned slot, unsigned item, unsigned flags, unsigned aux_item, unsigned pocket_count) {
    const int ux = (int)(aux_item & 0xFFu), uz = (int)((aux_item >> 8) & 0xFFu);
    if (pc_ofurn_acre_of(ux, uz) < 0 || item > 0xFFFFu || !pc_ofurn_is_ftr((uint16_t)item)) {
        return 0;
    }
    switch (op) {
        case PC_OFURN_OP_PLACE:
            return dest == 0u && slot < pocket_count && flags <= 3u;
        case PC_OFURN_OP_ROTATE:
            return dest == 0u && slot == 0u && flags <= 3u;
        case PC_OFURN_OP_PICKUP:
            return dest == 1u && slot < pocket_count && flags == 0u;
        default:
            return 0;
    }
}

int pc_ofurn_dir4(int angle_s16) {
    return (int)((((unsigned)angle_s16 + 0x2000u) & 0xFFFFu) >> 14);
}

int pc_ofurn_rot_for_facing(int angle_s16) {
    return (pc_ofurn_dir4(angle_s16) + 2) & 3;
}

void pc_ofurn_front_tile(float px, float pz, int angle_s16, int* ut_x, int* ut_z) {
    static const int dx[4] = { 0, 1, 0, -1 }, dz[4] = { 1, 0, -1, 0 };
    const int d = pc_ofurn_dir4(angle_s16);
    const int bx = (int)(px / 40.0f), bz = (int)(pz / 40.0f);
    *ut_x = bx + dx[d];
    *ut_z = bz + dz[d];
}

int pc_ofurn_reach_ok(int player_ut_x, int player_ut_z, int ut_x, int ut_z) {
    const int dx = ut_x - player_ut_x, dz = ut_z - player_ut_z;
    /* one of the 8 neighbours (the host's copy of the position lags the client by up to a MOVE interval: a diagonal neighbour is tolerated), never the tile itself */
    return dx >= -1 && dx <= 1 && dz >= -1 && dz <= 1 && (dx != 0 || dz != 0);
}

int pc_ofurn_rate_ok(PCOfurnRate* r, uint32_t now_ms, uint32_t min_gap_ms, int per_min) {
    if (r->used && (uint32_t)(now_ms - r->last_ms) < min_gap_ms) {
        return 0;
    }
    if (!r->used || (uint32_t)(now_ms - r->win_ms) >= 60000u) {
        r->win_ms = now_ms;
        r->win_n = 0;
    }
    if (r->win_n >= per_min) {
        return 0;
    }
    r->win_n++;
    r->last_ms = now_ms;
    r->used = 1;
    return 1;
}

static char s_notice[96];
static uint32_t s_notice_ms;
static int s_notice_on;

void pc_ofurn_notice_set(const char* text, uint32_t now_ms) {
    snprintf(s_notice, sizeof(s_notice), "%s", text != NULL ? text : "");
    s_notice_ms = now_ms;
    s_notice_on = s_notice[0] != '\0';
}

const char* pc_ofurn_notice_get(uint32_t now_ms) {
    if (!s_notice_on || (uint32_t)(now_ms - s_notice_ms) >= 4000u) {
        s_notice_on = 0;
        return NULL;
    }
    return s_notice;
}

const char* pc_ofurn_reason_text(int reason) {
    switch (reason) {
        case PC_OFURN_REASON_OFF: return "Placing furniture outside is off on this server";
        case PC_OFURN_REASON_NOT_ALLOWED: return "Only residents can do that with placed furniture";
        case PC_OFURN_REASON_TOO_BIG: return "That is too big to place outside (Drop it instead)";
        case PC_OFURN_REASON_BAD_TILE: return "You can't place furniture there";
        case PC_OFURN_REASON_LIMIT: return "This town has too much furniture outside";
        case PC_OFURN_REASON_RATE: return "Slow down a little";
        case PC_OFURN_REASON_GONE: return "That furniture is no longer there";
        case 22: return "Your pockets are full"; /* NO_ROOM */
        case 9: return "You can't do that right now"; /* PRECOND */
        default: return "That didn't work - try again";
    }
}

/* ---- native selftest ---- */
static int s_fail;
static void ck(int ok, const char* what) {
    if (!ok) {
        s_fail++;
        printf("[OFURN] selftest FAIL: %s\n", what);
    }
}

static PCOfurnTable s_ta, s_tb;

int pc_ofurn_selftest(void) {
    PCOfurnMarker m;
    PCOfurnRate r;
    uint8_t blob[8];
    int i, ux, uz, dropped;
    char path[64];
    s_fail = 0;
    pc_ofurn_table_init(&s_ta);
    memset(&m, 0, sizeof(m));
    m.ut_x = 20;
    m.ut_z = 20;
    m.base = 0x1234 & 0xFFFC;
    m.rot = 2;
    m.placer_kind = PC_OFURN_PLACER_RESIDENT;
    snprintf(m.placer_name, sizeof(m.placer_name), "Alice");
    ck(pc_ofurn_add(&s_ta, &m) == 1 && s_ta.n == 1 && pc_ofurn_find(&s_ta, 20, 20) == 0, "add + find");
    ck(pc_ofurn_tile_value(&s_ta.m[0]) == 0x1236, "tile value = base | rot");
    m.rot = 1;
    ck(pc_ofurn_add(&s_ta, &m) == 1 && s_ta.n == 1 && s_ta.m[0].rot == 1, "a second marker on the same tile replaces it");
    ck(pc_ofurn_acre_of(16, 16) == 0 && pc_ofurn_acre_of(95, 111) == 29 && pc_ofurn_acre_of(15, 20) == -1 && pc_ofurn_acre_of(96, 20) == -1 &&
           pc_ofurn_acre_of(20, 112) == -1 && pc_ofurn_acre_of(-1, 20) == -1,
       "acre of a town unit (border / beach / ocean refused)");
    ck(pc_ofurn_is_ftr(0x1000) && pc_ofurn_is_ftr(0x3FFF) && !pc_ofurn_is_ftr(0x2000) && !pc_ofurn_is_ftr(0x0000) && !pc_ofurn_is_ftr(0xFFFF), "furniture id classes");
    for (i = 0; i < 9; i++) {
        m.ut_x = (uint8_t)(32 + i);
        m.ut_z = 40;
        (void)pc_ofurn_add(&s_ta, &m);
    }
    ck(pc_ofurn_count_acre(&s_ta, pc_ofurn_acre_of(32, 40)) == 9, "count per acre");
    ck(pc_ofurn_caps_check(&s_ta, pc_ofurn_acre_of(32, 40), 512, 9) == 2, "acre ceiling");
    ck(pc_ofurn_caps_check(&s_ta, pc_ofurn_acre_of(32, 40), 10, 128) == 1, "town ceiling");
    ck(pc_ofurn_caps_check(&s_ta, pc_ofurn_acre_of(32, 40), 512, 128) == 0, "under both ceilings");
    ck(pc_ofurn_remove_at(&s_ta, 0) == 1 && s_ta.n == 9 && pc_ofurn_find(&s_ta, 20, 20) < 0, "remove");
    /* sidecar round trip + corruption */
    snprintf(path, sizeof(path), "ofurn_selftest_%u.dat", (unsigned)(size_t)&s_ta & 0xFFFFu);
    ck(pc_ofurn_save(path, &s_ta) == 1, "save");
    ck(pc_ofurn_load(path, &s_tb, &dropped) == PC_OFURN_FILE_OK && s_tb.n == 9 && dropped == 0 && memcmp(s_tb.m, s_ta.m, 9 * sizeof(s_ta.m[0])) == 0, "load round trip");
    {
        FILE* f = fopen(path, "r+b");
        if (f != NULL) {
            fseek(f, 20, SEEK_SET);
            fputc(0x5A, f);
            fclose(f);
        }
        ck(pc_ofurn_load(path, &s_tb, NULL) == PC_OFURN_FILE_BAD && s_tb.n == 0, "a corrupted file is BAD (empty table, never half a table)");
    }
    remove(path);
    ck(pc_ofurn_load(path, &s_tb, NULL) == PC_OFURN_FILE_MISSING, "missing file");
    /* wire */
    {
        PCOfurnMsg w;
        memset(&w, 0, sizeof(w));
        w.msg_type = 98;
        w.kind = PC_OFURN_MK_CHUNK;
        w.count = 1;
        w.e[0].ut_x = 20;
        w.e[0].ut_z = 20;
        w.e[0].item = 0x1232;
        ck(pc_ofurn_msg_valid(&w), "chunk valid");
        w.e[0].item = 0x2000;
        ck(!pc_ofurn_msg_valid(&w), "a non-furniture item refused");
        w.kind = PC_OFURN_MK_DEL;
        ck(pc_ofurn_msg_valid(&w), "DEL carries tiles only");
        w.count = 33;
        ck(!pc_ofurn_msg_valid(&w), "count > 32 refused");
        w.kind = PC_OFURN_MK_PERMS;
        w.count = 0;
        w.flags = 0x10;
        ck(!pc_ofurn_msg_valid(&w), "unknown perm bit refused");
        w.kind = 7;
        w.flags = 0;
        ck(!pc_ofurn_msg_valid(&w), "unknown kind refused");
    }
    pc_ofurn_caps_build(blob, 1, 0, 512, 128);
    ck(pc_ofurn_caps_valid(blob) && blob[1] == 1 && (blob[2] | (blob[3] << 8)) == 512 && (blob[4] | (blob[5] << 8)) == 128, "caps round trip");
    blob[1] = 4;
    ck(!pc_ofurn_caps_valid(blob), "caps unknown flag refused");
    /* kind 22 shape */
    ck(pc_ofurn_shape_ok(PC_OFURN_OP_PLACE, 0, 3, 0x1230, 2, (20u << 8) | 20u, 15), "PLACE shape");
    ck(!pc_ofurn_shape_ok(PC_OFURN_OP_PLACE, 1, 3, 0x1230, 2, (20u << 8) | 20u, 15), "PLACE with dest POCKET refused");
    ck(!pc_ofurn_shape_ok(PC_OFURN_OP_PLACE, 0, 15, 0x1230, 2, (20u << 8) | 20u, 15), "PLACE slot 15 refused");
    ck(!pc_ofurn_shape_ok(PC_OFURN_OP_PLACE, 0, 3, 0x1230, 4, (20u << 8) | 20u, 15), "rotation 4 refused");
    ck(!pc_ofurn_shape_ok(PC_OFURN_OP_PLACE, 0, 3, 0x2230, 2, (20u << 8) | 20u, 15), "a non-furniture item refused");
    ck(!pc_ofurn_shape_ok(PC_OFURN_OP_PLACE, 0, 3, 0x1230, 2, (5u << 8) | 20u, 15), "a non-town tile refused");
    ck(pc_ofurn_shape_ok(PC_OFURN_OP_ROTATE, 0, 0, 0x1231, 3, (20u << 8) | 20u, 15) && !pc_ofurn_shape_ok(PC_OFURN_OP_ROTATE, 0, 1, 0x1231, 3, (20u << 8) | 20u, 15),
       "ROTATE shape (slot must be 0)");
    ck(pc_ofurn_shape_ok(PC_OFURN_OP_PICKUP, 1, 0, 0x1231, 0, (20u << 8) | 20u, 15) && !pc_ofurn_shape_ok(PC_OFURN_OP_PICKUP, 0, 0, 0x1231, 0, (20u << 8) | 20u, 15),
       "PICKUP shape (dest POCKET)");
    ck(!pc_ofurn_shape_ok(4, 0, 0, 0x1231, 0, (20u << 8) | 20u, 15), "unknown op refused");
    /* geometry */
    ck(pc_ofurn_dir4(0) == 0 && pc_ofurn_dir4(0x4000) == 1 && pc_ofurn_dir4((int)(short)0x8000) == 2 && pc_ofurn_dir4((int)(short)0xC000) == 3 && pc_ofurn_dir4(0x1FFF) == 0 &&
           pc_ofurn_dir4(0x2000) == 1,
       "facing quantised to 4 directions");
    ck(pc_ofurn_rot_for_facing(0) == 2 && pc_ofurn_rot_for_facing((int)(short)0x8000) == 0, "the piece faces the player");
    pc_ofurn_front_tile(20 * 40 + 20, 30 * 40 + 20, 0, &ux, &uz);
    ck(ux == 20 && uz == 31, "front tile facing south");
    pc_ofurn_front_tile(20 * 40 + 20, 30 * 40 + 20, (int)(short)0xC000, &ux, &uz);
    ck(ux == 19 && uz == 30, "front tile facing west");
    ck(pc_ofurn_reach_ok(20, 30, 20, 31) && !pc_ofurn_reach_ok(20, 30, 20, 30) && pc_ofurn_reach_ok(20, 30, 21, 31) && !pc_ofurn_reach_ok(20, 30, 20, 32) &&
           !pc_ofurn_reach_ok(20, 30, 22, 30),
       "reach: the 8 neighbours, never the own tile, never 2 tiles away");
    /* rate */
    memset(&r, 0, sizeof(r));
    ck(pc_ofurn_rate_ok(&r, 1000, 250, 3) && !pc_ofurn_rate_ok(&r, 1100, 250, 3) && pc_ofurn_rate_ok(&r, 1300, 250, 3) && pc_ofurn_rate_ok(&r, 1600, 250, 3) &&
           !pc_ofurn_rate_ok(&r, 2000, 250, 3) && pc_ofurn_rate_ok(&r, 62000, 250, 3),
       "rate limit: the gap and the rolling minute");
    pc_ofurn_notice_set("hello", 5000);
    ck(pc_ofurn_notice_get(5100) != NULL && pc_ofurn_notice_get(9100) == NULL, "notice expires");
    for (i = 55; i <= 61; i++) {
        const char* t = pc_ofurn_reason_text(i);
        size_t k;
        int ascii = t != NULL && t[0] != '\0';
        for (k = 0; ascii && t[k] != '\0'; k++) {
            ascii = (unsigned char)t[k] >= 0x20 && (unsigned char)t[k] < 0x7F;
        }
        ck(ascii && strlen(t) < 60, "reason texts are short printable ASCII (the notice font)");
    }
    printf("[OFURN] selftest: %s (%d failure(s))\n", s_fail ? "FAIL" : "PASS", s_fail);
    return s_fail;
}
