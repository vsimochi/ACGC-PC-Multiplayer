/* pc_mp_guest_store.c - the per-guest store, see pc_mp_guest_store.h for the format and the policy. libc + the file APIs only, plus the CRC32 of pc_mp_records.c and the entry codec /
 * random bytes / legacy loader of pc_mp_guests.c. Main-thread use only. The small file helpers mirror the proven ones of pc_mp_guests.c / pc_mp_records.c on purpose. */
#include "pc_mp_guest_store.h"
#include "pc_mp_records.h"

#include <dirent.h>
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

#ifdef _WIN32
#include <direct.h>
#include <io.h>
#include <windows.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

static const char s_magic[8] = { 'A', 'C', 'M', 'P', 'G', 'S', '1', 0 };
static PCMpGuestFault s_fault;
static int s_fault_on = 0;
static PCMpGsIdSource s_id_source = NULL;

void pc_mp_gs_set_fault(const PCMpGuestFault* f) {
    if (f != NULL) {
        s_fault = *f;
        s_fault_on = 1;
    } else {
        memset(&s_fault, 0, sizeof(s_fault));
        s_fault_on = 0;
    }
}

void pc_mp_gs_set_id_source(PCMpGsIdSource fn) {
    s_id_source = fn;
}

static void glog(const char* fmt, ...) {
    va_list ap;
    printf("[NET][GUEST][STORE] ");
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
}

static void put32(uint8_t* p, uint32_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static uint32_t get32(const uint8_t* p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static int all_zero(const uint8_t* p, size_t n) {
    size_t i;
    for (i = 0; i < n; i++) {
        if (p[i] != 0) {
            return 0;
        }
    }
    return 1;
}

/* ---------------- format ---------------- */

int pc_mp_gs_serialize(const PCMpGsRecord* r, uint8_t* out, size_t cap) {
    if (r == NULL || out == NULL || cap < PC_MP_GS_FILE_SIZE || !r->e.present) {
        return PC_MP_GST_ERR_SHAPE;
    }
    memset(out, 0, PC_MP_GS_FILE_SIZE);
    memcpy(out, s_magic, 8);
    put32(out + 8, PC_MP_GS_VERSION);
    put32(out + 12, 0);
    put32(out + 16, PC_MP_GUEST_ENTRY_SIZE);
    put32(out + 20, r->generation);
    memcpy(out + PC_MP_GS_HEADER_SIZE, r->id, PC_MP_GS_ID_SIZE);
    pc_mp_guests_entry_encode(&r->e, out + PC_MP_GS_HEADER_SIZE + PC_MP_GS_ID_SIZE);
    put32(out + PC_MP_GS_FILE_SIZE - 4, pc_mp_records_crc32(out, PC_MP_GS_FILE_SIZE - 4));
    return PC_MP_GST_OK;
}

int pc_mp_gs_parse(const uint8_t* buf, size_t len, PCMpGsRecord* out) {
    uint32_t ver;
    static PCMpGsRecord tmp;
    int r;
    if (buf == NULL || out == NULL) {
        return PC_MP_GST_ERR_SHAPE;
    }
    if (len < PC_MP_GS_HEADER_SIZE) {
        return PC_MP_GST_ERR_TRUNCATED;
    }
    if (memcmp(buf, s_magic, 8) != 0) {
        return PC_MP_GST_ERR_MAGIC;
    }
    ver = get32(buf + 8);
    if (ver < PC_MP_GS_VERSION) {
        return PC_MP_GST_ERR_VERSION_OLD;
    }
    if (ver > PC_MP_GS_VERSION) {
        return PC_MP_GST_ERR_VERSION_FUTURE;
    }
    if (get32(buf + 12) != 0 || get32(buf + 16) != PC_MP_GUEST_ENTRY_SIZE || get32(buf + 24) != 0 || get32(buf + 28) != 0) {
        return PC_MP_GST_ERR_SHAPE;
    }
    if (len < PC_MP_GS_FILE_SIZE) {
        return PC_MP_GST_ERR_TRUNCATED;
    }
    if (len != PC_MP_GS_FILE_SIZE) {
        return PC_MP_GST_ERR_SHAPE;
    }
    if (get32(buf + PC_MP_GS_FILE_SIZE - 4) != pc_mp_records_crc32(buf, PC_MP_GS_FILE_SIZE - 4)) {
        return PC_MP_GST_ERR_CRC;
    }
    memset(&tmp, 0, sizeof(tmp));
    tmp.generation = get32(buf + 20);
    memcpy(tmp.id, buf + PC_MP_GS_HEADER_SIZE, PC_MP_GS_ID_SIZE);
    if (all_zero(tmp.id, PC_MP_GS_ID_SIZE)) {
        return PC_MP_GST_ERR_FIELD;
    }
    r = pc_mp_guests_entry_decode(buf + PC_MP_GS_HEADER_SIZE + PC_MP_GS_ID_SIZE, &tmp.e);
    if (r != PC_MP_GST_OK) {
        return r;
    }
    if (!tmp.e.present) {
        return PC_MP_GST_ERR_FIELD; /* a guest file always holds a guest */
    }
    *out = tmp;
    return PC_MP_GST_OK;
}

uint32_t pc_mp_gs_entry_digest(const PCMpGuestEntry* e) {
    static uint8_t buf[PC_MP_GUEST_ENTRY_SIZE];
    pc_mp_guests_entry_encode(e, buf);
    put32(buf + 40, 0); /* the epoch alone is not a reason to rewrite a guest */
    return pc_mp_records_crc32(buf, PC_MP_GUEST_ENTRY_SIZE);
}

/* ---------------- ids and paths ---------------- */

void pc_mp_gs_id_hex(const uint8_t id[PC_MP_GS_ID_SIZE], char out[33]) {
    static const char hx[] = "0123456789abcdef";
    int i;
    for (i = 0; i < PC_MP_GS_ID_SIZE; i++) {
        out[i * 2] = hx[id[i] >> 4];
        out[i * 2 + 1] = hx[id[i] & 15];
    }
    out[32] = '\0';
}

int pc_mp_gs_id_parse(const char* hex, uint8_t id[PC_MP_GS_ID_SIZE]) {
    int i;
    if (hex == NULL) {
        return 0;
    }
    for (i = 0; i < 32; i++) {
        const char c = hex[i];
        const int v = (c >= '0' && c <= '9') ? c - '0' : (c >= 'a' && c <= 'f') ? c - 'a' + 10 : -1;
        if (v < 0) {
            return 0;
        }
        if ((i & 1) == 0) {
            id[i / 2] = (uint8_t)(v << 4);
        } else {
            id[i / 2] = (uint8_t)(id[i / 2] | v);
        }
    }
    return 1;
}

int pc_mp_gs_new_id(uint8_t id[PC_MP_GS_ID_SIZE]) {
    int ok = s_id_source != NULL ? s_id_source(id) : pc_mp_guests_random_bytes(id, PC_MP_GS_ID_SIZE);
    return ok && !all_zero(id, PC_MP_GS_ID_SIZE);
}

void pc_mp_gs_path(const char* dir, const uint8_t id[PC_MP_GS_ID_SIZE], int gen, char* out, size_t cap) {
    char hex[33];
    pc_mp_gs_id_hex(id, hex);
    if (gen == 0) {
        snprintf(out, cap, "%s/%s.gst", dir, hex);
    } else {
        snprintf(out, cap, "%s/%s.gst.bak%d", dir, hex, gen);
    }
}

void pc_mp_gs_dir_for_legacy(const char* legacy_path, char* out, size_t cap) {
    const size_t n = strlen(legacy_path);
    if (n > 4 && strcmp(legacy_path + n - 4, ".dat") == 0) {
        snprintf(out, cap, "%.*s", (int)(n - 4), legacy_path);
    } else {
        snprintf(out, cap, "%s-store", legacy_path);
    }
}

/* ---------------- file helpers ---------------- */

static int file_exists(const char* path) {
    struct stat st;
    return stat(path, &st) == 0;
}

static int rename_over(const char* src, const char* dst) {
#ifdef _WIN32
    return MoveFileExA(src, dst, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) ? 0 : -1;
#else
    return rename(src, dst);
#endif
}

static void make_dir(const char* p) {
#ifdef _WIN32
    _mkdir(p);
#else
    mkdir(p, 0755);
#endif
}

static void ensure_dirs(const char* path) {
    char buf[512];
    size_t i, n = strlen(path);
    if (n >= sizeof(buf)) {
        return;
    }
    memcpy(buf, path, n + 1);
    for (i = 1; i < n; i++) {
        if (buf[i] == '/' || buf[i] == '\\') {
            char c = buf[i];
            buf[i] = 0;
            make_dir(buf);
            buf[i] = c;
        }
    }
    make_dir(buf);
}

static int read_file(const char* path, uint8_t* buf, size_t cap, size_t* len) {
    FILE* fp = fopen(path, "rb");
    size_t n;
    if (fp == NULL) {
        return PC_MP_GST_ERR_IO;
    }
    n = fread(buf, 1, cap, fp);
    if (ferror(fp)) {
        fclose(fp);
        return PC_MP_GST_ERR_IO;
    }
    fclose(fp);
    *len = n;
    return PC_MP_GST_OK;
}

static int commit_file(FILE* fp) {
    if (fflush(fp) != 0) {
        return -1;
    }
#ifdef _WIN32
    return _commit(_fileno(fp));
#else
    return fsync(fileno(fp));
#endif
}

/* "<file>.<tag>-<timestamp>[-n]": a name that does not exist yet. */
static int tagged_name(const char* file, const char* tag, char* out, size_t cap) {
    char ts[32];
    time_t now = time(NULL);
    struct tm* tmv = localtime(&now);
    int n;
    if (tmv != NULL) {
        strftime(ts, sizeof(ts), "%Y%m%d-%H%M%S", tmv);
    } else {
        snprintf(ts, sizeof(ts), "%lld", (long long)now);
    }
    for (n = 0; n < 1000; n++) {
        if (n == 0) {
            snprintf(out, cap, "%s.%s-%s", file, tag, ts);
        } else {
            snprintf(out, cap, "%s.%s-%s-%d", file, tag, ts, n);
        }
        if (!file_exists(out)) {
            return 1;
        }
    }
    out[0] = 0;
    return 0;
}

static int move_aside(const char* file, const char* tag, char* new_path, size_t cap) {
    if (!tagged_name(file, tag, new_path, cap) || rename_over(file, new_path) != 0) {
        glog("ERROR: could not move '%s' aside (it is left in place, NOT deleted)\n", file);
        new_path[0] = 0;
        return 0;
    }
    return 1;
}

static int read_and_parse(const char* path, PCMpGsRecord* out) {
    static uint8_t buf[PC_MP_GS_FILE_SIZE + 1];
    size_t n = 0;
    int r = read_file(path, buf, sizeof(buf), &n);
    if (r != PC_MP_GST_OK) {
        return r;
    }
    return pc_mp_gs_parse(buf, n, out);
}

static const char* gs_strerror(int e) {
    return pc_mp_guests_strerror(e);
}

/* ---------------- save / retire / backup ---------------- */

int pc_mp_gs_save(const char* dir, const PCMpGsRecord* r) {
    static uint8_t buf[PC_MP_GS_FILE_SIZE];
    static PCMpGsRecord verify;
    char path[360], tmp[380], b1[380], b2[380], moved[420];
    FILE* fp;
    size_t want, wrote;
    int rc;

    rc = pc_mp_gs_serialize(r, buf, sizeof(buf));
    if (rc == PC_MP_GST_OK) {
        rc = pc_mp_gs_parse(buf, sizeof(buf), &verify); /* self-check: never write something we could not read back */
        if (rc == PC_MP_GST_OK && memcmp(verify.id, r->id, PC_MP_GS_ID_SIZE) != 0) {
            rc = PC_MP_GST_ERR_FIELD;
        }
    }
    if (rc != PC_MP_GST_OK) {
        glog("save REFUSED: serialized guest image failed self-validation (%s)\n", gs_strerror(rc));
        return rc;
    }
    pc_mp_gs_path(dir, r->id, 0, path, sizeof(path));
    pc_mp_gs_path(dir, r->id, 1, b1, sizeof(b1));
    pc_mp_gs_path(dir, r->id, 2, b2, sizeof(b2));
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    ensure_dirs(dir);

    fp = fopen(tmp, "wb");
    if (fp == NULL) {
        glog("save FAILED: cannot open '%s' (errno %d); previous generation untouched\n", tmp, errno);
        return PC_MP_GST_ERR_IO;
    }
    want = sizeof(buf);
    if (s_fault_on && s_fault.crash_partial_tmp >= 0) {
        wrote = fwrite(buf, 1, (size_t)s_fault.crash_partial_tmp < want ? (size_t)s_fault.crash_partial_tmp : want, fp);
        fflush(fp);
        fclose(fp);
        glog("TEST: simulated crash after %lu tmp bytes\n", (unsigned long)wrote);
        return PC_MP_GST_ERR_FAULT_CRASH;
    }
    if (s_fault_on && s_fault.fail_write_after >= 0) {
        size_t lim = (size_t)s_fault.fail_write_after < want ? (size_t)s_fault.fail_write_after : want;
        wrote = fwrite(buf, 1, lim, fp);
        fclose(fp);
        remove(tmp);
        glog("save FAILED: write error after %lu bytes (simulated); tmp removed, previous generation untouched\n", (unsigned long)wrote);
        return PC_MP_GST_ERR_IO;
    }
    wrote = fwrite(buf, 1, want, fp);
    if (wrote != want || commit_file(fp) != 0) {
        fclose(fp);
        remove(tmp);
        glog("save FAILED: write/flush error (disk full?); tmp removed, previous generation untouched\n");
        return PC_MP_GST_ERR_IO;
    }
    if (fclose(fp) != 0) {
        remove(tmp);
        glog("save FAILED: close error; tmp removed, previous generation untouched\n");
        return PC_MP_GST_ERR_IO;
    }
    if (s_fault_on && s_fault.crash_after_tmp) {
        glog("TEST: simulated crash after the tmp file was committed (before rotation)\n");
        return PC_MP_GST_ERR_FAULT_CRASH;
    }
    if (file_exists(path)) {
        static PCMpGsRecord cur;
        const int cr = read_and_parse(path, &cur);
        if (cr == PC_MP_GST_OK) {
            if (file_exists(b2)) {
                remove(b2);
            }
            if (file_exists(b1) && rename_over(b1, b2) != 0) {
                glog("WARNING: could not rotate .bak1 -> .bak2\n");
            }
            if (rename_over(path, b1) != 0) {
                glog("save FAILED: could not rotate '%s' -> '%s'; tmp removed, previous file untouched\n", path, b1);
                remove(tmp);
                return PC_MP_GST_ERR_IO;
            }
        } else {
            glog("current guest file '%s' is unreadable (%s) at save time: preserving it aside instead of rotating it over a good generation\n", path, gs_strerror(cr));
            (void)move_aside(path, "corrupt", moved, sizeof(moved));
            if (file_exists(path)) {
                remove(tmp);
                return PC_MP_GST_ERR_IO;
            }
        }
    }
    if (s_fault_on && s_fault.crash_after_rotate) {
        glog("TEST: simulated crash after the .bak rotation (no primary present)\n");
        return PC_MP_GST_ERR_FAULT_CRASH;
    }
    if ((s_fault_on && s_fault.fail_rename) || rename_over(tmp, path) != 0) {
        glog("save FAILED: replace '%s' -> '%s' failed, recovering .bak1...\n", tmp, path);
        if (!file_exists(path) && file_exists(b1)) {
            if (rename_over(b1, path) != 0) {
                glog("ERROR: could not restore .bak1 (the file remains readable as .bak1 on the next load)\n");
            } else {
                glog("restored previous generation from .bak1\n");
            }
        }
        remove(tmp);
        return PC_MP_GST_ERR_IO;
    }
    return PC_MP_GST_OK;
}

int pc_mp_gs_retire(const char* dir, const uint8_t id[PC_MP_GS_ID_SIZE], char* moved_primary, size_t cap) {
    char p[380], moved[460];
    int g, ok = 1;
    if (moved_primary != NULL && cap > 0) {
        moved_primary[0] = '\0';
    }
    for (g = 0; g < 3; g++) {
        pc_mp_gs_path(dir, id, g, p, sizeof(p));
        if (!file_exists(p)) {
            continue;
        }
        if (move_aside(p, "removed", moved, sizeof(moved))) {
            if (g == 0 && moved_primary != NULL) {
                snprintf(moved_primary, cap, "%s", moved);
            }
        } else {
            ok = 0;
        }
    }
    return ok;
}

int pc_mp_gs_backup(const char* dir, const uint8_t id[PC_MP_GS_ID_SIZE], char* out_path, size_t cap) {
    static uint8_t buf[PC_MP_GS_FILE_SIZE + 1];
    char p[380], b[460];
    size_t n = 0;
    FILE* fp;
    if (out_path != NULL && cap > 0) {
        out_path[0] = '\0';
    }
    pc_mp_gs_path(dir, id, 0, p, sizeof(p));
    if (!file_exists(p) || read_file(p, buf, sizeof(buf), &n) != PC_MP_GST_OK || n == 0 || !tagged_name(p, "bak", b, sizeof(b))) {
        return 0;
    }
    fp = fopen(b, "wb");
    if (fp == NULL) {
        return 0;
    }
    if (fwrite(buf, 1, n, fp) != n || commit_file(fp) != 0) {
        fclose(fp);
        remove(b);
        return 0;
    }
    if (fclose(fp) != 0) {
        remove(b);
        return 0;
    }
    if (out_path != NULL) {
        snprintf(out_path, cap, "%s", b);
    }
    return 1;
}

/* ---------------- load ---------------- */

typedef struct IdList {
    uint8_t (*ids)[PC_MP_GS_ID_SIZE];
    int n, cap;
} IdList;

static int idlist_add(IdList* l, const uint8_t id[PC_MP_GS_ID_SIZE]) {
    int i;
    for (i = 0; i < l->n; i++) {
        if (memcmp(l->ids[i], id, PC_MP_GS_ID_SIZE) == 0) {
            return 1;
        }
    }
    if (l->n == l->cap) {
        const int nc = l->cap == 0 ? 64 : l->cap * 2;
        void* p = realloc(l->ids, (size_t)nc * PC_MP_GS_ID_SIZE);
        if (p == NULL) {
            return 0;
        }
        l->ids = (uint8_t(*)[PC_MP_GS_ID_SIZE])p;
        l->cap = nc;
    }
    memcpy(l->ids[l->n++], id, PC_MP_GS_ID_SIZE);
    return 1;
}

static int id_cmp(const void* a, const void* b) {
    return memcmp(a, b, PC_MP_GS_ID_SIZE);
}

/* The mint order of a guest file, read without parsing it (offset of `age` inside the entry): used ONLY to order the load (oldest guest first, so slot numbers follow arrival order
 * and survive a restart); a file whose age cannot be read sorts first. */
typedef struct AgeId {
    uint32_t age;
    uint8_t  id[PC_MP_GS_ID_SIZE];
} AgeId;

static int ageid_cmp(const void* a, const void* b) {
    const AgeId* x = (const AgeId*)a;
    const AgeId* y = (const AgeId*)b;
    if (x->age != y->age) {
        return x->age < y->age ? -1 : 1;
    }
    return memcmp(x->id, y->id, PC_MP_GS_ID_SIZE);
}

static uint32_t peek_age(const char* dir, const uint8_t id[PC_MP_GS_ID_SIZE]) {
    uint8_t b[4];
    char p[380];
    int g;
    for (g = 0; g < 3; g++) {
        FILE* fp;
        size_t n;
        pc_mp_gs_path(dir, id, g, p, sizeof(p));
        fp = fopen(p, "rb");
        if (fp == NULL) {
            continue;
        }
        n = (fseek(fp, PC_MP_GS_HEADER_SIZE + PC_MP_GS_ID_SIZE + 48, SEEK_SET) == 0) ? fread(b, 1, 4, fp) : 0;
        fclose(fp);
        if (n == 4) {
            return get32(b);
        }
    }
    return 0;
}

/* One guest's identity facts, for the cross-guest duplicate check. */
typedef struct Tup {
    uint8_t token[PC_MP_GUEST_TOKEN_SIZE];
    uint8_t key[PC_MP_GUEST_PID_SIZE + PC_MP_GUEST_TOWN_NAME_SIZE + 2 + 4];
} Tup;

static void tup_fill(Tup* t, const PCMpGuestEntry* e) {
    memcpy(t->token, e->token, PC_MP_GUEST_TOKEN_SIZE);
    memcpy(t->key, e->pid, PC_MP_GUEST_PID_SIZE);
    memcpy(t->key + PC_MP_GUEST_PID_SIZE, e->town_land_name, PC_MP_GUEST_TOWN_NAME_SIZE);
    t->key[28] = (uint8_t)(e->town_land_id & 0xFFu);
    t->key[29] = (uint8_t)(e->town_land_id >> 8);
    put32(t->key + 30, e->town_terrain_hash);
}

static int tup_cmp_token(const void* a, const void* b) {
    return memcmp(((const Tup*)a)->token, ((const Tup*)b)->token, sizeof(((Tup*)0)->token));
}

static int tup_cmp_key(const void* a, const void* b) {
    return memcmp(((const Tup*)a)->key, ((const Tup*)b)->key, sizeof(((Tup*)0)->key));
}

static int count_dups(Tup* t, int n) {
    int i, d = 0;
    if (n < 2) {
        return 0;
    }
    qsort(t, (size_t)n, sizeof(*t), tup_cmp_token);
    for (i = 1; i < n; i++) {
        if (tup_cmp_token(&t[i - 1], &t[i]) == 0) {
            d++;
        }
    }
    qsort(t, (size_t)n, sizeof(*t), tup_cmp_key);
    for (i = 1; i < n; i++) {
        if (tup_cmp_key(&t[i - 1], &t[i]) == 0) {
            d++;
        }
    }
    return d;
}

/* "<32 hex>.gst" -> 0, ".gst.bak1" / ".gst.bak2" -> 1 / 2, ".gst.corrupt-*" -> 100, anything else -> -1. */
static int classify_name(const char* name, uint8_t id[PC_MP_GS_ID_SIZE]) {
    if (strlen(name) < 36 || !pc_mp_gs_id_parse(name, id) || strncmp(name + 32, ".gst", 4) != 0) {
        return -1;
    }
    if (name[36] == '\0') {
        return 0;
    }
    if (strcmp(name + 36, ".bak1") == 0) {
        return 1;
    }
    if (strcmp(name + 36, ".bak2") == 0) {
        return 2;
    }
    if (strncmp(name + 36, ".corrupt-", 9) == 0) {
        return 100;
    }
    return -1;
}

int pc_mp_gs_load(const char* dir, PCMpGsVisit visit, void* ctx, PCMpGsLoadInfo* info) {
    PCMpGsLoadInfo local;
    PCMpGsLoadInfo* inf = info != NULL ? info : &local;
    static PCMpGsRecord chosen, probe;
    IdList ids = { NULL, 0, 0 }, corrupt = { NULL, 0, 0 };
    Tup* tups = NULL;
    uint8_t (*loaded_ids)[PC_MP_GS_ID_SIZE] = NULL;
    int n_loaded = 0, i, g, ok = 1;
    DIR* d;
    struct dirent* de;

    memset(inf, 0, sizeof(*inf));
    errno = 0;
    d = opendir(dir);
    if (d == NULL) {
        if (errno == ENOENT || errno == ENOTDIR) {
            inf->dir_missing = 1;
            glog("no guest store directory '%s': first run, no guests known\n", dir);
            return 1;
        }
        inf->untrusted = 1;
        snprintf(inf->first_problem, sizeof(inf->first_problem), "cannot list '%s' (errno %d)", dir, errno);
        glog("*** cannot list the guest store directory '%s' (errno %d): UNTRUSTED mode ***\n", dir, errno);
        return 0;
    }
    while ((de = readdir(d)) != NULL) {
        uint8_t id[PC_MP_GS_ID_SIZE];
        const int k = classify_name(de->d_name, id);
        if (k >= 0 && k < 100) {
            ok = ok && idlist_add(&ids, id);
        } else if (k == 100) {
            ok = ok && idlist_add(&corrupt, id);
        }
    }
    closedir(d);
    if (!ok) {
        free(ids.ids);
        free(corrupt.ids);
        inf->untrusted = 1;
        glog("*** out of memory while listing the guest store: UNTRUSTED mode ***\n");
        return 0;
    }
    if (ids.n > 1) {
        AgeId* ao = (AgeId*)malloc((size_t)ids.n * sizeof(AgeId));
        qsort(ids.ids, (size_t)ids.n, PC_MP_GS_ID_SIZE, id_cmp);
        if (ao != NULL) { /* oldest guest first (ties: by id); without memory the plain id order is used */
            for (i = 0; i < ids.n; i++) {
                memcpy(ao[i].id, ids.ids[i], PC_MP_GS_ID_SIZE);
                ao[i].age = peek_age(dir, ids.ids[i]);
            }
            qsort(ao, (size_t)ids.n, sizeof(AgeId), ageid_cmp);
            for (i = 0; i < ids.n; i++) {
                memcpy(ids.ids[i], ao[i].id, PC_MP_GS_ID_SIZE);
            }
            free(ao);
        }
    }
    inf->files = ids.n;
    tups = (Tup*)malloc((size_t)(ids.n > 0 ? ids.n : 1) * sizeof(Tup));
    loaded_ids = (uint8_t(*)[PC_MP_GS_ID_SIZE])malloc((size_t)(ids.n > 0 ? ids.n : 1) * PC_MP_GS_ID_SIZE);
    if (tups == NULL || loaded_ids == NULL) {
        free(tups);
        free(loaded_ids);
        free(ids.ids);
        free(corrupt.ids);
        inf->untrusted = 1;
        glog("*** out of memory while loading the guest store: UNTRUSTED mode ***\n");
        return 0;
    }

    for (i = 0; i < ids.n && ok; i++) {
        int err[3] = { -1, -1, -1 }, pick = -1;
        char p[380], moved[460];
        for (g = 0; g < 3; g++) {
            pc_mp_gs_path(dir, ids.ids[i], g, p, sizeof(p));
            if (!file_exists(p)) {
                continue;
            }
            err[g] = read_and_parse(p, &probe);
            if (err[g] == PC_MP_GST_OK && memcmp(probe.id, ids.ids[i], PC_MP_GS_ID_SIZE) != 0) {
                err[g] = PC_MP_GST_ERR_FIELD; /* a file whose inner id is not its name: renamed / copied, not trusted */
            }
            if (err[g] == PC_MP_GST_OK) {
                if (pick < 0) {
                    pick = g;
                    chosen = probe;
                }
            } else {
                glog("'%s' is UNREADABLE: %s\n", p, gs_strerror(err[g]));
            }
        }
        for (g = 0; g < 3; g++) {
            if (err[g] > PC_MP_GST_OK) {
                pc_mp_gs_path(dir, ids.ids[i], g, p, sizeof(p));
                if (move_aside(p, "corrupt", moved, sizeof(moved))) {
                    inf->moved_aside++;
                    glog("unreadable '%s' preserved as '%s'\n", p, moved);
                }
            }
        }
        if (pick < 0) {
            char hex[33];
            pc_mp_gs_id_hex(ids.ids[i], hex);
            inf->unreadable++;
            inf->untrusted = 1;
            if (inf->first_problem[0] == '\0') {
                snprintf(inf->first_problem, sizeof(inf->first_problem), "guest file %s.gst has no readable generation", hex);
            }
            glog("*** guest %s has NO readable generation: UNTRUSTED mode (its token is unknown) ***\n", hex);
            continue;
        }
        if (pick > 0) {
            char hex[33];
            pc_mp_gs_id_hex(ids.ids[i], hex);
            inf->from_backup++;
            glog("*** guest %s: primary unusable, RECOVERED from generation .bak%d ***\n", hex, pick);
        }
        tup_fill(&tups[n_loaded], &chosen.e);
        memcpy(loaded_ids[n_loaded], ids.ids[i], PC_MP_GS_ID_SIZE);
        n_loaded++;
        inf->loaded++;
        if (visit != NULL && !visit(ctx, &chosen, pick)) {
            ok = 0;
        }
    }
    if (!ok) {
        inf->untrusted = 1;
    }
    for (i = 0; i < corrupt.n; i++) { /* preserved *.corrupt-* files of a guest that has no valid generation */
        int found = 0;
        for (g = 0; g < n_loaded && !found; g++) {
            found = memcmp(loaded_ids[g], corrupt.ids[i], PC_MP_GS_ID_SIZE) == 0;
        }
        if (!found) {
            inf->stray_corrupt++;
        }
    }
    if (inf->stray_corrupt > 0) {
        inf->untrusted = 1;
        glog("*** %d preserved *.corrupt-* guest file(s) without a valid guest: UNTRUSTED mode, NOT 'missing'. To deliberately reset remove them (and the guest's files) together ***\n",
             inf->stray_corrupt);
        if (inf->first_problem[0] == '\0') {
            snprintf(inf->first_problem, sizeof(inf->first_problem), "%d stray *.corrupt-* guest file(s)", inf->stray_corrupt);
        }
    }
    inf->duplicates = count_dups(tups, n_loaded);
    if (inf->duplicates > 0) {
        inf->untrusted = 1;
        glog("*** %d guest(s) share a (town, key) or a token with another guest: UNTRUSTED mode (the store is inconsistent) ***\n", inf->duplicates);
        if (inf->first_problem[0] == '\0') {
            snprintf(inf->first_problem, sizeof(inf->first_problem), "%d duplicate guest identity / token", inf->duplicates);
        }
    }
    free(tups);
    free(loaded_ids);
    free(ids.ids);
    free(corrupt.ids);
    return ok;
}

/* ---------------- migration from guests.dat v2 ---------------- */

typedef struct MigCtx {
    Tup* t;
    int n, cap;
    int oom;
} MigCtx;

static int mig_collect(void* ctx, const PCMpGsRecord* rec, int from_backup) {
    MigCtx* m = (MigCtx*)ctx;
    (void)from_backup;
    if (m->n == m->cap) {
        const int nc = m->cap == 0 ? 64 : m->cap * 2;
        Tup* p = (Tup*)realloc(m->t, (size_t)nc * sizeof(Tup));
        if (p == NULL) {
            m->oom = 1;
            return 0;
        }
        m->t = p;
        m->cap = nc;
    }
    tup_fill(&m->t[m->n++], &rec->e);
    return 1;
}

static void mig_fail(PCMpGsMigInfo* info, const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(info->error, sizeof(info->error), fmt, ap);
    va_end(ap);
    glog("*** migration FAILED: %s -- the legacy guests.dat is left untouched ***\n", info->error);
}

int pc_mp_gs_migrate_legacy(const char* legacy_path, const char* dir, PCMpGsMigInfo* info) {
    static PCMpGuestFile legacy;
    PCMpGuestLoadInfo li;
    PCMpGsMigInfo local;
    PCMpGsLoadInfo si;
    MigCtx mc = { NULL, 0, 0, 0 };
    int i, mode;
    PCMpGsMigInfo* mi = info != NULL ? info : &local;

    memset(mi, 0, sizeof(*mi));
    mode = (int)pc_mp_guests_load(legacy_path, &legacy, &li);
    if (mode == PC_MP_GST_LOAD_MISSING) {
        return PC_MP_GS_MIG_NONE;
    }
    if (mode == PC_MP_GST_LOAD_UNTRUSTED) {
        snprintf(mi->error, sizeof(mi->error), "the legacy guests file '%s' exists but is unreadable", legacy_path);
        return PC_MP_GS_MIG_LEGACY_UNTRUSTED;
    }
    if (!pc_mp_gs_load(dir, mig_collect, &mc, &si) && mc.oom) {
        free(mc.t);
        mig_fail(mi, "out of memory");
        return PC_MP_GS_MIG_FAILED;
    }
    if (si.untrusted) {
        free(mc.t);
        mig_fail(mi, "the per-guest store '%s' is itself untrusted (%s)", dir, si.first_problem);
        return PC_MP_GS_MIG_FAILED;
    }
    glog("migrating '%s' (generation %u) to the per-guest store '%s' (%d guest(s) already there)\n", legacy_path, (unsigned)legacy.generation, dir, mc.n);
    for (i = 0; i < PC_MP_GUEST_SLOTS; i++) {
        const PCMpGuestEntry* e = &legacy.e[i];
        static PCMpGsRecord rec, back;
        static uint8_t fbuf[PC_MP_GS_FILE_SIZE + 1];
        Tup t;
        char p[380];
        size_t n = 0;
        int j, same_key = 0, same_token = 0;
        if (!e->present) {
            continue;
        }
        mi->legacy_entries++;
        tup_fill(&t, e);
        for (j = 0; j < mc.n; j++) {
            same_key |= memcmp(mc.t[j].key, t.key, sizeof(t.key)) == 0;
            same_token |= memcmp(mc.t[j].token, t.token, sizeof(t.token)) == 0;
        }
        if (same_key || same_token) {
            if (same_key && same_token) {
                mi->skipped_existing++; /* the very same guest is already in the store: it stays exactly as it is */
                continue;
            }
            free(mc.t);
            mig_fail(mi, "legacy slot %d conflicts with a guest already in the store (same key / token but not the same guest)", i);
            return PC_MP_GS_MIG_FAILED;
        }
        memset(&rec, 0, sizeof(rec));
        if (!pc_mp_gs_new_id(rec.id)) {
            free(mc.t);
            mig_fail(mi, "no OS randomness for a guest id");
            return PC_MP_GS_MIG_FAILED;
        }
        rec.generation = 1;
        rec.e = *e;
        if (pc_mp_gs_save(dir, &rec) != PC_MP_GST_OK) {
            free(mc.t);
            mig_fail(mi, "could not write legacy slot %d to the store", i);
            return PC_MP_GS_MIG_FAILED;
        }
        pc_mp_gs_path(dir, rec.id, 0, p, sizeof(p));
        if (read_file(p, fbuf, sizeof(fbuf), &n) != PC_MP_GST_OK || pc_mp_gs_parse(fbuf, n, &back) != PC_MP_GST_OK || memcmp(&back.e, e, sizeof(*e)) != 0 ||
            memcmp(back.id, rec.id, PC_MP_GS_ID_SIZE) != 0) {
            free(mc.t);
            mig_fail(mi, "read-back verification of legacy slot %d failed", i);
            return PC_MP_GS_MIG_FAILED;
        }
        if (mc.n == mc.cap) {
            const int nc = mc.cap == 0 ? 64 : mc.cap * 2;
            Tup* np = (Tup*)realloc(mc.t, (size_t)nc * sizeof(Tup));
            if (np == NULL) {
                free(mc.t);
                mig_fail(mi, "out of memory");
                return PC_MP_GS_MIG_FAILED;
            }
            mc.t = np;
            mc.cap = nc;
        }
        mc.t[mc.n++] = t;
        mi->written++;
    }
    free(mc.t);
    /* every guest is safely in the store: retire the legacy file and its generations (kept, renamed: they must never be re-imported over later changes) */
    {
        char gp[360], moved[460];
        int g, ok = 1;
        for (g = 0; g < 3; g++) {
            if (g == 0) {
                snprintf(gp, sizeof(gp), "%s", legacy_path);
            } else {
                snprintf(gp, sizeof(gp), "%s.bak%d", legacy_path, g);
            }
            if (!file_exists(gp)) {
                continue;
            }
            if (!move_aside(gp, "v2-migrated", moved, sizeof(moved))) {
                ok = 0;
            } else if (g == 0) {
                snprintf(mi->retired, sizeof(mi->retired), "%s", moved);
            }
        }
        if (!ok) {
            mig_fail(mi, "the guests were copied but the legacy file could not be renamed");
            return PC_MP_GS_MIG_FAILED;
        }
    }
    glog("migration done: %d legacy guest(s), %d written, %d already present; the legacy file is now '%s'\n", mi->legacy_entries, mi->written, mi->skipped_existing, mi->retired);
    return PC_MP_GS_MIG_DONE;
}
