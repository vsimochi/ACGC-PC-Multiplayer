/* pc_outdoor_furniture.c - OUTDOOR FURNITURE (C1): the pure logic (marker table, sidecar, message 98 / service 10 shapes, kind 22 shape, geometry, rate limit,
 * notice). No game state: the native selftest (pc_ofurn_selftest) compiles this file alone. See pc_outdoor_furniture.h. */
#ifdef _WIN32
#include <windows.h>
#endif
#include "pc_outdoor_furniture.h"

#include <math.h>
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

/* ---- C2 footprints (the indoor engine's cells, see the header) ---- */
int pc_ofurn_fp_from_shape(int aftr_shape) {
    if (aftr_shape >= 0 && aftr_shape <= 3) {
        return PC_OFURN_FP_2X1; /* TYPEB_90 / 180 / 270 / 0 (the profiles hold TYPEB_0; the others are its rotations) */
    }
    if (aftr_shape == 4) {
        return PC_OFURN_FP_1X1; /* TYPEA */
    }
    if (aftr_shape == 5) {
        return PC_OFURN_FP_2X2; /* TYPEC */
    }
    return -1;
}

void pc_ofurn_dir_delta(int dir, int* dx, int* dz) {
    static const int ddx[4] = { 0, 1, 0, -1 }, ddz[4] = { 1, 0, -1, 0 };
    *dx = ddx[dir & 3];
    *dz = ddz[dir & 3];
}

int pc_ofurn_fp_cells(int fp, int rot, int ox, int oz, int* xs, int* zs) {
    /* 2x1: the second cell of rot 0..3 = TYPEB_0 (+x), TYPEB_90 (-z), TYPEB_180 (-x), TYPEB_270 (+z) (aMR_GetTypeBPlaceInfo) = direction (rot + 1) & 3 */
    xs[0] = ox;
    zs[0] = oz;
    switch (fp) {
        case PC_OFURN_FP_1X1:
            return 1;
        case PC_OFURN_FP_2X1: {
            int dx, dz;
            pc_ofurn_dir_delta((rot + 1) & 3, &dx, &dz);
            xs[1] = ox + dx;
            zs[1] = oz + dz;
            return 2;
        }
        case PC_OFURN_FP_2X2:
            xs[1] = ox + 1;
            zs[1] = oz;
            xs[2] = ox;
            zs[2] = oz + 1;
            xs[3] = ox + 1;
            zs[3] = oz + 1;
            return 4;
        default:
            return 0;
    }
}

int pc_ofurn_marker_cells(const PCOfurnMarker* m, int* xs, int* zs) {
    return pc_ofurn_fp_cells((int)m->shape, (int)m->rot, (int)m->ut_x, (int)m->ut_z, xs, zs);
}

int pc_ofurn_fp_covers(int fp, int rot, int ox, int oz, int x, int z) {
    int xs[PC_OFURN_FP_MAX_CELLS], zs[PC_OFURN_FP_MAX_CELLS], n, i;
    n = pc_ofurn_fp_cells(fp, rot, ox, oz, xs, zs);
    for (i = 0; i < n; i++) {
        if (xs[i] == x && zs[i] == z) {
            return 1;
        }
    }
    return 0;
}

int pc_ofurn_fp_in_town(int fp, int rot, int ox, int oz) {
    int xs[PC_OFURN_FP_MAX_CELLS], zs[PC_OFURN_FP_MAX_CELLS], n, i;
    n = pc_ofurn_fp_cells(fp, rot, ox, oz, xs, zs);
    if (n == 0) {
        return 0;
    }
    for (i = 0; i < n; i++) {
        if (pc_ofurn_acre_of(xs[i], zs[i]) < 0) {
            return 0;
        }
    }
    return 1;
}

int pc_ofurn_find_cell(const PCOfurnTable* t, int x, int z) {
    int i;
    for (i = 0; i < t->n; i++) {
        const PCOfurnMarker* m = &t->m[i];
        const int dx = x - (int)m->ut_x, dz = z - (int)m->ut_z;
        if (dx >= -1 && dx <= 1 && dz >= -1 && dz <= 1 && pc_ofurn_fp_covers((int)m->shape, (int)m->rot, (int)m->ut_x, (int)m->ut_z, x, z)) {
            return i;
        }
    }
    return -1;
}

int pc_ofurn_fp_overlaps(const PCOfurnTable* t, int fp, int rot, int ox, int oz, int skip_idx) {
    int xs[PC_OFURN_FP_MAX_CELLS], zs[PC_OFURN_FP_MAX_CELLS], n, i, k;
    n = pc_ofurn_fp_cells(fp, rot, ox, oz, xs, zs);
    for (i = 0; i < n; i++) {
        for (k = 0; k < t->n; k++) {
            const PCOfurnMarker* m = &t->m[k];
            if (k != skip_idx && pc_ofurn_fp_covers((int)m->shape, (int)m->rot, (int)m->ut_x, (int)m->ut_z, xs[i], zs[i])) {
                return 1;
            }
        }
    }
    return 0;
}

void pc_ofurn_fp_model_pos(int fp, int ox, int oz, float* wx, float* wz) {
    *wx = (float)ox * 40.0f + 20.0f;
    *wz = (float)oz * 40.0f + 20.0f;
    if (fp == PC_OFURN_FP_2X2) {
        *wx += 20.0f; /* aMR_UnitNumber2Position: TYPEC sits on the shared corner of its 4 cells */
        *wz += 20.0f;
    }
}

int pc_ofurn_fp_reach_ok(int fp, int rot, int ox, int oz, int pux, int puz) {
    int xs[PC_OFURN_FP_MAX_CELLS], zs[PC_OFURN_FP_MAX_CELLS], n, i, adjacent = 0;
    n = pc_ofurn_fp_cells(fp, rot, ox, oz, xs, zs);
    for (i = 0; i < n; i++) {
        if (xs[i] == pux && zs[i] == puz) {
            return 0; /* never standing on the piece */
        }
        if (pc_ofurn_reach_ok(pux, puz, xs[i], zs[i])) {
            adjacent = 1;
        }
    }
    return adjacent;
}

int pc_ofurn_move_geometry(int fp, int rot, int ox, int oz, int dir, int pull, int pux, int puz, int* nox, int* noz, int* fx, int* fz) {
    int dx, dz, side_x, side_z;
    pc_ofurn_dir_delta(dir, &dx, &dz);
    *nox = ox + dx;
    *noz = oz + dz;
    /* the player's side: PUSH stands behind the piece (the tile against the move: player + dir is a cell), PULL in front (player - dir is a cell) and steps to
     * player + dir */
    side_x = pull ? pux - dx : pux + dx;
    side_z = pull ? puz - dz : puz + dz;
    if (!pc_ofurn_fp_covers(fp, rot, ox, oz, side_x, side_z) || pc_ofurn_fp_covers(fp, rot, ox, oz, pux, puz)) {
        return 0;
    }
    /* both ways the player ends one tile further along dir (PUSH: into the tile the piece leaves, PULL: one step back). That tile is never a new cell: the shifted
     * footprint covers player + dir only if the old one covered the player's own tile, which was refused above. */
    *fx = pux + dx;
    *fz = puz + dz;
    return pc_ofurn_fp_in_town(fp, rot, *nox, *noz);
}

int pc_ofurn_fp_new_cells(int fp, int rot0, int ox0, int oz0, int rot1, int ox1, int oz1, int* xs, int* zs) {
    int ax[PC_OFURN_FP_MAX_CELLS], az[PC_OFURN_FP_MAX_CELLS], n, i, k = 0;
    n = pc_ofurn_fp_cells(fp, rot1, ox1, oz1, ax, az);
    for (i = 0; i < n; i++) {
        if (!pc_ofurn_fp_covers(fp, rot0, ox0, oz0, ax[i], az[i])) {
            xs[k] = ax[i];
            zs[k] = az[i];
            k++;
        }
    }
    return k;
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

int pc_ofurn_copy_file(const char* from, const char* to) {
    FILE *a, *b;
    char buf[4096];
    size_t n;
    int ok = 1;
    if (from == NULL || to == NULL || (a = fopen(from, "rb")) == NULL) {
        return 0;
    }
    if ((b = fopen(to, "wb")) == NULL) {
        fclose(a);
        return 0;
    }
    while ((n = fread(buf, 1, sizeof(buf), a)) > 0) {
        if (fwrite(buf, 1, n, b) != n) {
            ok = 0;
            break;
        }
    }
    fclose(a);
    ok = (fclose(b) == 0) && ok;
    if (!ok) {
        remove(to);
    }
    return ok;
}

int pc_ofurn_load(const char* path, PCOfurnTable* t, int* dropped, int* version) {
    FILE* f;
    uint32_t hdr[3], h, check;
    int i, drop = 0;
    PCOfurnMarker m;
    pc_ofurn_table_init(t);
    if (dropped != NULL) {
        *dropped = 0;
    }
    if (version != NULL) {
        *version = 0;
    }
    if (path == NULL || (f = fopen(path, "rb")) == NULL) {
        return PC_OFURN_FILE_MISSING;
    }
    if (fread(hdr, sizeof(hdr), 1, f) != 1 || hdr[0] != PC_OFURN_FILE_MAGIC || (hdr[1] != PC_OFURN_FILE_VERSION && hdr[1] != PC_OFURN_FILE_VERSION_C1) ||
        hdr[2] > (uint32_t)PC_OFURN_TABLE_CAP) {
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
        if (hdr[1] == PC_OFURN_FILE_VERSION_C1) {
            m.shape = PC_OFURN_FP_1X1; /* v1 -> v2: C1 placed 1x1 pieces only (the u16 was 0) */
            m.flags = 0;
        }
        if (pc_ofurn_acre_of(m.ut_x, m.ut_z) < 0 || !pc_ofurn_is_ftr(m.base) || (m.base & 3u) != 0u || m.rot > 3u || m.shape >= PC_OFURN_FP_NUM ||
            !pc_ofurn_fp_in_town((int)m.shape, (int)m.rot, (int)m.ut_x, (int)m.ut_z) || pc_ofurn_find(t, m.ut_x, m.ut_z) >= 0 ||
            pc_ofurn_fp_overlaps(t, (int)m.shape, (int)m.rot, (int)m.ut_x, (int)m.ut_z, -1)) {
            drop++; /* never a marker on a non-town tile / for a non-furniture id / a bad shape / a footprint leaving the town / twice on a cell */
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
    if (version != NULL) {
        *version = (int)hdr[1];
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

int pc_ofurn_msg2_valid(const PCOfurnMsg* m) {
    int i;
    if (m->msg_type != (uint8_t)PC_OFURN_MSG2_ID || m->count > PC_OFURN_MSG_ENTRIES) {
        return 0;
    }
    switch (m->kind) {
        case PC_OFURN_MK_BEGIN:
            if (m->count != 0 || m->total > (uint32_t)PC_OFURN_TABLE_CAP) {
                return 0;
            }
            break;
        case PC_OFURN_MK_END:
            if (m->count != 0) {
                return 0;
            }
            break;
        case PC_OFURN_MK_CAPS:
            return m->count == 0 && (m->flags & ~PC_OFURN_C2_ALL) == 0u;
        case PC_OFURN_MK_MOVE:
            if (m->count != 2 || !pc_ofurn_is_ftr(m->e[1].item) || pc_ofurn_base(m->e[0].item) != pc_ofurn_base(m->e[1].item)) {
                return 0;
            }
            break;
        case PC_OFURN_MK_CHUNK:
        case PC_OFURN_MK_SET:
        case PC_OFURN_MK_DEL:
            break;
        default:
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
        case PC_OFURN_OP_MOVE:
            return dest == 0u && slot == 0u && flags <= (3u | PC_OFURN_MOVE_PULL);
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
        case PC_OFURN_REASON_NOT_EMPTY: return "Take the stored items out first";
        case 22: return "Your pockets are full"; /* NO_ROOM */
        case 9: return "You can't do that right now"; /* PRECOND */
        default: return "That didn't work - try again";
    }
}

/* ---- C3: the open tap + the vanilla birth / bye curves (pure) ---- */
int pc_ofurn_tap_opens(uint32_t held_ms, int stepped, int is_storage) {
    return is_storage && !stepped && held_ms < PC_OFURN_TAP_MS;
}

void pc_ofurn_birth_init(PCOfurnBirth* b) {
    memset(b, 0, sizeof(*b));
    b->mod = 0.5f;   /* aMR_SetFirstScale */
    b->step = 6000;
}

int pc_ofurn_birth_step(PCOfurnBirth* b, float dt, float* sxz, float* sy) {
    const short prev = (short)b->counter_f;
    short now;
    float s;
    if (b->done) {
        *sxz = *sy = 1.0f;
        return 0;
    }
    b->counter_f += (float)b->step * dt;
    while (b->counter_f >= 32768.0f) {
        b->counter_f -= 65536.0f;
    }
    while (b->counter_f < -32768.0f) {
        b->counter_f += 65536.0f;
    }
    b->frames += dt;
    now = (short)b->counter_f;
    s = sinf((float)now * (3.14159265f / 32768.0f)) * b->mod;
    *sxz = 1.0f - s;
    *sy = 1.0f + s;
    if (prev < 0 && now >= 0) { /* aMR_FtrBirth: the (upward) zero crossing */
        b->mod *= 0.4f;
        b->step += 1500;
    }
    if (fabsf(b->mod) < 0.02f || b->frames > 240.0f) {
        b->done = 1;
        *sxz = *sy = 1.0f;
        return 0;
    }
    return 1;
}

float pc_ofurn_bye_scale(float frames) {
    const float s = 1.0f - frames * 0.08333333333f;
    return s < 0.25f ? 0.0f : (s > 1.0f ? 1.0f : s);
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

static int ofurn_write_v1(const char* path, const PCOfurnMarker* m, int n) {
    uint32_t hdr[3], h;
    FILE* f = fopen(path, "wb");
    if (f == NULL) {
        return 0;
    }
    hdr[0] = PC_OFURN_FILE_MAGIC;
    hdr[1] = PC_OFURN_FILE_VERSION_C1;
    hdr[2] = (uint32_t)n;
    h = ofurn_fnv(hdr, sizeof(hdr), 2166136261u);
    h = ofurn_fnv(m, (size_t)n * sizeof(*m), h);
    fwrite(hdr, sizeof(hdr), 1, f);
    fwrite(m, sizeof(*m), (size_t)n, f);
    fwrite(&h, sizeof(h), 1, f);
    fclose(f);
    return 1;
}

/* C2: footprints, rotation, move geometry, the v1 -> v2 sidecar conversion, message 102 */
static int pc_ofurn_selftest_c2(void) {
    const int before = s_fail;
    int xs[4], zs[4], n, nx, nz, fx, fz, ver = 0, dropped = 0;
    float wx, wz;
    PCOfurnMarker m;
    char path[64];
    ck(pc_ofurn_fp_from_shape(4) == PC_OFURN_FP_1X1 && pc_ofurn_fp_from_shape(3) == PC_OFURN_FP_2X1 && pc_ofurn_fp_from_shape(0) == PC_OFURN_FP_2X1 &&
           pc_ofurn_fp_from_shape(5) == PC_OFURN_FP_2X2 && pc_ofurn_fp_from_shape(6) == -1 && pc_ofurn_fp_from_shape(-1) == -1,
       "C2 aFTR shape -> footprint class");
    n = pc_ofurn_fp_cells(PC_OFURN_FP_1X1, 2, 30, 40, xs, zs);
    ck(n == 1 && xs[0] == 30 && zs[0] == 40, "C2 1x1 = the origin");
    n = pc_ofurn_fp_cells(PC_OFURN_FP_2X1, 0, 30, 40, xs, zs);
    ck(n == 2 && xs[1] == 31 && zs[1] == 40, "C2 2x1 rot 0 (TYPEB_0): origin + (+x)");
    n = pc_ofurn_fp_cells(PC_OFURN_FP_2X1, 1, 30, 40, xs, zs);
    ck(n == 2 && xs[1] == 30 && zs[1] == 39, "C2 2x1 rot 1 (TYPEB_90): origin + (-z)");
    n = pc_ofurn_fp_cells(PC_OFURN_FP_2X1, 2, 30, 40, xs, zs);
    ck(n == 2 && xs[1] == 29 && zs[1] == 40, "C2 2x1 rot 2 (TYPEB_180): origin + (-x)");
    n = pc_ofurn_fp_cells(PC_OFURN_FP_2X1, 3, 30, 40, xs, zs);
    ck(n == 2 && xs[1] == 30 && zs[1] == 41, "C2 2x1 rot 3 (TYPEB_270): origin + (+z)");
    n = pc_ofurn_fp_cells(PC_OFURN_FP_2X2, 3, 30, 40, xs, zs);
    ck(n == 4 && xs[3] == 31 && zs[3] == 41 && pc_ofurn_fp_covers(PC_OFURN_FP_2X2, 0, 30, 40, 31, 40) && !pc_ofurn_fp_covers(PC_OFURN_FP_2X2, 0, 30, 40, 29, 40),
       "C2 2x2 = origin, +x, +z, +x+z for every rotation");
    ck(pc_ofurn_fp_cells(7, 0, 30, 40, xs, zs) == 0, "C2 an unknown footprint has no cells");
    pc_ofurn_fp_model_pos(PC_OFURN_FP_2X1, 30, 40, &wx, &wz);
    ck(wx == 1220.0f && wz == 1620.0f, "C2 model position of a 2x1 = the origin cell centre (aMR_UnitNumber2Position)");
    pc_ofurn_fp_model_pos(PC_OFURN_FP_2X2, 30, 40, &wx, &wz);
    ck(wx == 1240.0f && wz == 1640.0f, "C2 model position of a 2x2 = the shared corner of the 4 cells");
    ck(pc_ofurn_fp_in_town(PC_OFURN_FP_2X1, 2, 16, 20) == 0 && pc_ofurn_fp_in_town(PC_OFURN_FP_2X1, 0, 16, 20) == 1 && pc_ofurn_fp_in_town(PC_OFURN_FP_2X2, 0, 95, 20) == 0,
       "C2 a footprint leaving the town FG is refused");
    /* rotation swings the second cell of a 2x1 around the origin: the one cell to validate */
    n = pc_ofurn_fp_new_cells(PC_OFURN_FP_2X1, 0, 30, 40, 1, 30, 40, xs, zs);
    ck(n == 1 && xs[0] == 30 && zs[0] == 39, "C2 rotating a 2x1 east -> north: one new cell (x, z-1)");
    ck(pc_ofurn_fp_new_cells(PC_OFURN_FP_2X2, 0, 30, 40, 1, 30, 40, xs, zs) == 0 && pc_ofurn_fp_new_cells(PC_OFURN_FP_1X1, 0, 30, 40, 3, 30, 40, xs, zs) == 0,
       "C2 rotating a 1x1 / 2x2 covers no new cell");
    n = pc_ofurn_fp_new_cells(PC_OFURN_FP_2X1, 0, 30, 40, 0, 31, 40, xs, zs);
    ck(n == 1 && xs[0] == 32 && zs[0] == 40, "C2 moving a 2x1 along its long axis: one new cell");
    n = pc_ofurn_fp_new_cells(PC_OFURN_FP_2X2, 0, 30, 40, 0, 30, 41, xs, zs);
    ck(n == 2 && zs[0] == 42 && zs[1] == 42, "C2 moving a 2x2 south: two new cells");
    /* move geometry */
    ck(pc_ofurn_move_geometry(PC_OFURN_FP_1X1, 0, 30, 40, 0, 0, 30, 39, &nx, &nz, &fx, &fz) && nx == 30 && nz == 41 && fx == 30 && fz == 40,
       "C2 PUSH south: the player north of the piece, the piece one tile south, the player follows into its old tile");
    ck(pc_ofurn_move_geometry(PC_OFURN_FP_1X1, 0, 30, 40, 0, 1, 30, 41, &nx, &nz, &fx, &fz) && nx == 30 && nz == 41 && fx == 30 && fz == 42,
       "C2 PULL south: the player south of the piece steps back one tile, the piece follows");
    ck(!pc_ofurn_move_geometry(PC_OFURN_FP_1X1, 0, 30, 40, 0, 0, 30, 41, &nx, &nz, &fx, &fz) && !pc_ofurn_move_geometry(PC_OFURN_FP_1X1, 0, 30, 40, 0, 0, 31, 39, &nx, &nz, &fx, &fz),
       "C2 a push from the wrong side / diagonally is refused");
    ck(pc_ofurn_move_geometry(PC_OFURN_FP_2X1, 0, 30, 40, 1, 0, 29, 40, &nx, &nz, &fx, &fz) && nx == 31 && fx == 30 &&
           pc_ofurn_move_geometry(PC_OFURN_FP_2X1, 0, 30, 40, 0, 0, 31, 39, &nx, &nz, &fx, &fz) && nz == 41,
       "C2 a 2x1 pushed along its long axis and from its long side");
    ck(!pc_ofurn_move_geometry(PC_OFURN_FP_2X1, 2, 17, 40, 3, 0, 18, 40, &nx, &nz, &fx, &fz), "C2 a move out of the town is refused");
    ck(pc_ofurn_fp_reach_ok(PC_OFURN_FP_2X1, 0, 30, 40, 32, 41) && !pc_ofurn_fp_reach_ok(PC_OFURN_FP_2X1, 0, 30, 40, 31, 40) && !pc_ofurn_fp_reach_ok(PC_OFURN_FP_2X1, 0, 30, 40, 33, 40),
       "C2 reach of a 2x1: next to any cell, never on one");
    /* table: find by cell, overlap */
    pc_ofurn_table_init(&s_ta);
    memset(&m, 0, sizeof(m));
    m.ut_x = 30;
    m.ut_z = 40;
    m.base = 0x1100;
    m.shape = PC_OFURN_FP_2X2;
    (void)pc_ofurn_add(&s_ta, &m);
    m.ut_x = 40;
    m.shape = PC_OFURN_FP_2X1;
    m.rot = 1;
    (void)pc_ofurn_add(&s_ta, &m);
    ck(pc_ofurn_find_cell(&s_ta, 31, 41) == 0 && pc_ofurn_find_cell(&s_ta, 40, 39) == 1 && pc_ofurn_find_cell(&s_ta, 41, 40) == -1 && pc_ofurn_find(&s_ta, 31, 41) == -1,
       "C2 find by cell: every footprint cell names its marker (find by origin does not)");
    ck(pc_ofurn_fp_overlaps(&s_ta, PC_OFURN_FP_1X1, 0, 31, 40, -1) && !pc_ofurn_fp_overlaps(&s_ta, PC_OFURN_FP_2X1, 1, 30, 40, 0) &&
           pc_ofurn_fp_overlaps(&s_ta, PC_OFURN_FP_2X1, 0, 39, 39, -1),
       "C2 footprint overlap (the moving piece itself excluded)");
    /* sidecar: v2 round trip of multi-tile markers, a v1 file converted losslessly, overlaps / out-of-town footprints dropped */
    snprintf(path, sizeof(path), "ofurn_selftest_c2_%u.dat", (unsigned)(size_t)&s_tb & 0xFFFFu);
    ck(pc_ofurn_save(path, &s_ta) && pc_ofurn_load(path, &s_tb, &dropped, &ver) == PC_OFURN_FILE_OK && ver == 2 && s_tb.n == 2 && s_tb.m[0].shape == PC_OFURN_FP_2X2 &&
           s_tb.m[1].shape == PC_OFURN_FP_2X1 && s_tb.m[1].rot == 1 && dropped == 0,
       "C2 a v2 sidecar round-trips multi-tile markers");
    {
        PCOfurnMarker v1[3];
        memset(v1, 0, sizeof(v1));
        v1[0].ut_x = 20;
        v1[0].ut_z = 30;
        v1[0].base = 0x1234 & 0xFFFC;
        v1[0].rot = 3;
        v1[0].placer_kind = PC_OFURN_PLACER_RESIDENT;
        snprintf(v1[0].placer_name, sizeof(v1[0].placer_name), "Bob");
        v1[1] = v1[0];
        v1[1].ut_x = 21;
        v1[2] = v1[0]; /* a duplicate origin: dropped */
        ck(ofurn_write_v1(path, v1, 3) && pc_ofurn_load(path, &s_tb, &dropped, &ver) == PC_OFURN_FILE_OK && ver == 1 && s_tb.n == 2 && dropped == 1 &&
               s_tb.m[0].shape == PC_OFURN_FP_1X1 && s_tb.m[0].rot == 3 && strcmp(s_tb.m[0].placer_name, "Bob") == 0 && memcmp(&s_tb.m[0], &v1[0], sizeof(v1[0])) == 0,
           "C2 a v1 (0.3.0) sidecar loads losslessly as 1x1 markers (byte-identical records)");
        ck(pc_ofurn_copy_file(path, "ofurn_selftest_c2.v1.bak") && pc_ofurn_save(path, &s_tb) && pc_ofurn_load(path, &s_ta, NULL, &ver) == PC_OFURN_FILE_OK && ver == 2 &&
               s_ta.n == 2 && memcmp(s_ta.m, s_tb.m, 2 * sizeof(s_ta.m[0])) == 0 && pc_ofurn_load("ofurn_selftest_c2.v1.bak", &s_ta, NULL, &ver) == PC_OFURN_FILE_OK && ver == 1,
           "C2 the converted sidecar is rewritten as v2; the .v1.bak copy stays a readable v1 file");
        remove("ofurn_selftest_c2.v1.bak");
    }
    pc_ofurn_table_init(&s_ta);
    m.ut_x = 30;
    m.ut_z = 40;
    m.rot = 0;
    m.shape = PC_OFURN_FP_2X2;
    s_ta.m[s_ta.n++] = m;
    m.ut_x = 31; /* overlaps the 2x2 */
    m.shape = PC_OFURN_FP_1X1;
    s_ta.m[s_ta.n++] = m;
    m.ut_x = 95; /* a 2x2 leaving the town */
    m.shape = PC_OFURN_FP_2X2;
    s_ta.m[s_ta.n++] = m;
    m.ut_x = 50;
    m.shape = 3; /* unknown shape */
    s_ta.m[s_ta.n++] = m;
    ck(pc_ofurn_save(path, &s_ta) && pc_ofurn_load(path, &s_tb, &dropped, &ver) == PC_OFURN_FILE_OK && s_tb.n == 1 && dropped == 3,
       "C2 the loader drops overlapping footprints, footprints leaving the town and unknown shapes");
    remove(path);
    /* message 102 */
    {
        PCOfurnMsg w;
        memset(&w, 0, sizeof(w));
        w.msg_type = (uint8_t)PC_OFURN_MSG2_ID;
        w.kind = PC_OFURN_MK_CAPS;
        w.flags = PC_OFURN_C2_ALL;
        ck(pc_ofurn_msg2_valid(&w), "C2 102 CAPS valid");
        w.flags = 0x10;
        ck(!pc_ofurn_msg2_valid(&w), "C2 102 CAPS with an unknown flag refused");
        w.flags = 0;
        w.kind = PC_OFURN_MK_MOVE;
        w.count = 2;
        w.e[0].ut_x = 30;
        w.e[0].ut_z = 40;
        w.e[0].item = 0x1101;
        w.e[1].ut_x = 30;
        w.e[1].ut_z = 41;
        w.e[1].item = 0x1102;
        ck(pc_ofurn_msg2_valid(&w), "C2 102 MOVE (old origin -> new origin, same base) valid");
        w.e[1].item = 0x1201;
        ck(!pc_ofurn_msg2_valid(&w), "C2 102 MOVE of a different item refused");
        w.e[1].item = 0x1102;
        w.count = 1;
        ck(!pc_ofurn_msg2_valid(&w), "C2 102 MOVE needs exactly two entries");
        w.kind = 9;
        ck(!pc_ofurn_msg2_valid(&w), "C2 102 unknown kind refused");
        w.kind = PC_OFURN_MK_SET;
        ck(pc_ofurn_msg2_valid(&w), "C2 102 SET valid");
        w.msg_type = 98;
        ck(!pc_ofurn_msg2_valid(&w), "C2 a 98 is never taken as a 102");
        w.kind = PC_OFURN_MK_CAPS;
        w.count = 0;
        ck(!pc_ofurn_msg_valid(&w), "C2 the C1 (0.3.0) validator refuses kind 7 on 98: CAPS / MOVE are never sent on 98");
    }
    return s_fail - before;
}

int pc_ofurn_selftest(void) {
    PCOfurnMarker m;
    PCOfurnRate r;
    uint8_t blob[8];
    int i, ux, uz, dropped, ver = 0;
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
    ck(pc_ofurn_load(path, &s_tb, &dropped, &ver) == PC_OFURN_FILE_OK && s_tb.n == 9 && dropped == 0 && ver == 2 && memcmp(s_tb.m, s_ta.m, 9 * sizeof(s_ta.m[0])) == 0,
       "load round trip (written as v2)");
    {
        FILE* f = fopen(path, "r+b");
        if (f != NULL) {
            fseek(f, 20, SEEK_SET);
            fputc(0x5A, f);
            fclose(f);
        }
        ck(pc_ofurn_load(path, &s_tb, NULL, NULL) == PC_OFURN_FILE_BAD && s_tb.n == 0, "a corrupted file is BAD (empty table, never half a table)");
    }
    remove(path);
    ck(pc_ofurn_load(path, &s_tb, NULL, NULL) == PC_OFURN_FILE_MISSING, "missing file");
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
    ck(!pc_ofurn_shape_ok(5, 0, 0, 0x1231, 0, (20u << 8) | 20u, 15) && !pc_ofurn_shape_ok(0, 0, 0, 0x1231, 0, (20u << 8) | 20u, 15), "unknown op refused");
    ck(pc_ofurn_shape_ok(PC_OFURN_OP_MOVE, 0, 0, 0x1231, 3, (20u << 8) | 20u, 15) && pc_ofurn_shape_ok(PC_OFURN_OP_MOVE, 0, 0, 0x1231, 2 | PC_OFURN_MOVE_PULL, (20u << 8) | 20u, 15) &&
           !pc_ofurn_shape_ok(PC_OFURN_OP_MOVE, 0, 0, 0x1231, 8, (20u << 8) | 20u, 15) && !pc_ofurn_shape_ok(PC_OFURN_OP_MOVE, 1, 0, 0x1231, 0, (20u << 8) | 20u, 15) &&
           !pc_ofurn_shape_ok(PC_OFURN_OP_MOVE, 0, 2, 0x1231, 0, (20u << 8) | 20u, 15),
       "C2 MOVE shape (dest NONE, slot 0, flags = dir | PULL)");
    (void)pc_ofurn_selftest_c2(); /* counts into s_fail itself */
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
    /* C3: the open tap, the birth wobble, the bye shrink */
    ck(pc_ofurn_tap_opens(100, 0, 1) && !pc_ofurn_tap_opens(100, 1, 1) && !pc_ofurn_tap_opens(100, 0, 0) && !pc_ofurn_tap_opens(PC_OFURN_TAP_MS, 0, 1) &&
           pc_ofurn_tap_opens(PC_OFURN_TAP_MS - 1, 0, 1),
       "C3 tap: storage + no step + released before 14 frames (233 ms)");
    {
        PCOfurnBirth b;
        float sxz = 0.0f, sy = 0.0f, maxy = 0.0f, minxz = 2.0f;
        int n = 0;
        pc_ofurn_birth_init(&b);
        while (pc_ofurn_birth_step(&b, 1.0f, &sxz, &sy) && n < 1000) {
            n++;
            maxy = sy > maxy ? sy : maxy;
            minxz = sxz < minxz ? sxz : minxz;
            ck(fabsf((sy - 1.0f) + (sxz - 1.0f)) < 1e-4f, "C3 birth: the volume-keeping squash (sy - 1 == 1 - sxz)");
        }
        ck(n > 20 && n < 60 && sxz == 1.0f && sy == 1.0f && b.done, "C3 birth: ends at scale 1 after ~33 frames (4 wobbles at 60 Hz)");
        ck(maxy > 1.45f && maxy <= 1.5f && minxz < 0.55f, "C3 birth: the first stretch peaks at 1 + 0.5");
        pc_ofurn_birth_init(&b);
        n = 0;
        while (pc_ofurn_birth_step(&b, 4.0f, &sxz, &sy) && n < 1000) { /* 15 fps: the same curve in fewer, larger steps */
            n++;
        }
        ck(n > 3 && n < 20 && b.frames > 20.0f && b.frames < 70.0f, "C3 birth: frame-rate independent (dt-scaled)");
    }
    ck(pc_ofurn_bye_scale(0.0f) == 1.0f && pc_ofurn_bye_scale(6.0f) > 0.49f && pc_ofurn_bye_scale(6.0f) < 0.51f && pc_ofurn_bye_scale(9.1f) == 0.0f &&
           pc_ofurn_bye_scale(100.0f) == 0.0f,
       "C3 bye: -1/12 a frame, 0 below 0.25 (gone after 9 frames)");
    ck(strcmp(pc_ofurn_reason_text(PC_OFURN_REASON_NOT_EMPTY), "Take the stored items out first") == 0, "C3 reason 62 text");
    for (i = 55; i <= 62; i++) {
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
