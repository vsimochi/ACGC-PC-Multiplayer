/* pc_mp_guests.c - host-side guest table sidecar (save/mp/guests.dat) and the client token file (save/mp/guest_token.dat).
 * See pc_mp_guests.h for the formats and policy. Self-contained: libc + (Windows) the file APIs only, plus the CRC32 of
 * pc_mp_records.c. Main-thread use only. The file helpers (rename_over / move_aside / count_corrupt_siblings / commit) deliberately
 * mirror the proven ones of pc_mp_records.c rather than modifying that shipped, tested module. */
#include "pc_mp_guests.h"
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

static const char s_gst_magic[8] = { 'A', 'C', 'M', 'P', 'G', 'S', 'T', 0 };
static const char s_gtk_magic[8] = { 'A', 'C', 'M', 'P', 'G', 'T', 'K', 0 };
static PCMpGuestFault s_fault;
static int s_fault_on = 0;

void pc_mp_guests_set_fault(const PCMpGuestFault* f) {
    if (f != NULL) {
        s_fault = *f;
        s_fault_on = 1;
    } else {
        memset(&s_fault, 0, sizeof(s_fault));
        s_fault_on = 0;
    }
}

static void glog(const char* fmt, ...) {
    va_list ap;
    printf("[NET][GUEST][STORE] ");
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
}

const char* pc_mp_guests_strerror(int err) {
    switch (err) {
        case PC_MP_GST_OK: return "ok";
        case PC_MP_GST_ERR_IO: return "I/O error";
        case PC_MP_GST_ERR_TRUNCATED: return "truncated";
        case PC_MP_GST_ERR_MAGIC: return "bad magic";
        case PC_MP_GST_ERR_VERSION_OLD: return "unsupported OLDER version";
        case PC_MP_GST_ERR_VERSION_FUTURE: return "unsupported FUTURE version";
        case PC_MP_GST_ERR_SHAPE: return "bad shape (slot count / entry size / length / reserved)";
        case PC_MP_GST_ERR_CRC: return "file checksum mismatch";
        case PC_MP_GST_ERR_FIELD: return "entry field out of range / inconsistent";
        case PC_MP_GST_ERR_RECORD_CRC: return "guest record checksum mismatch";
        case PC_MP_GST_ERR_FAULT_CRASH: return "simulated crash (test)";
        default: return "unknown error";
    }
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

/* ---------------- OS random ---------------- */

int pc_mp_guests_random_bytes(uint8_t* out, size_t n) {
#ifdef _WIN32
    typedef BOOLEAN(WINAPI * RtlGenRandomFn)(PVOID, ULONG);
    static RtlGenRandomFn fn = NULL;
    static int tried = 0;
    if (!tried) {
        HMODULE h = LoadLibraryA("advapi32.dll");
        tried = 1;
        if (h != NULL) {
            fn = (RtlGenRandomFn)(void*)GetProcAddress(h, "SystemFunction036");
        }
    }
    if (fn == NULL || n > 0x10000) {
        return 0;
    }
    return fn(out, (ULONG)n) ? 1 : 0;
#else
    FILE* fp = fopen("/dev/urandom", "rb");
    size_t got;
    if (fp == NULL) {
        return 0;
    }
    got = fread(out, 1, n, fp);
    fclose(fp);
    return got == n;
#endif
}

/* ---------------- file helpers (mirror pc_mp_records.c) ---------------- */

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

static void ensure_parent_dirs(const char* path) {
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

static void gen_path(const char* path, int gen, char* out, size_t cap) {
    if (gen == 0) {
        snprintf(out, cap, "%s", path);
    } else {
        snprintf(out, cap, "%s.bak%d", path, gen);
    }
}

static int move_aside(const char* file, char* new_path, size_t cap) {
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
            snprintf(new_path, cap, "%s.corrupt-%s", file, ts);
        } else {
            snprintf(new_path, cap, "%s.corrupt-%s-%d", file, ts, n);
        }
        if (!file_exists(new_path)) {
            break;
        }
    }
    if (n >= 1000 || rename_over(file, new_path) != 0) {
        glog("ERROR: could not move unreadable file '%s' aside (it is left in place, NOT deleted)\n", file);
        new_path[0] = 0;
        return 0;
    }
    return 1;
}

static int count_corrupt_siblings(const char* path, char* first, size_t cap) {
    char dir[320], base[160];
    const char* slash = strrchr(path, '/');
    const char* bslash = strrchr(path, '\\');
    DIR* d;
    struct dirent* e;
    size_t bl;
    int n = 0;
    if (bslash != NULL && (slash == NULL || bslash > slash)) {
        slash = bslash;
    }
    if (slash == NULL) {
        snprintf(dir, sizeof(dir), ".");
        snprintf(base, sizeof(base), "%s", path);
    } else {
        snprintf(dir, sizeof(dir), "%.*s", (int)(slash - path), path);
        snprintf(base, sizeof(base), "%s", slash + 1);
    }
    bl = strlen(base);
    d = opendir(dir);
    if (d == NULL) {
        return 0;
    }
    while ((e = readdir(d)) != NULL) {
        if (strncmp(e->d_name, base, bl) == 0 && e->d_name[bl] == '.' && strstr(e->d_name + bl, ".corrupt-") != NULL) {
            if (n == 0 && first != NULL) {
                snprintf(first, cap, "%.150s/%.150s", dir, e->d_name);
            }
            n++;
        }
    }
    closedir(d);
    return n;
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

/* ================= HOST: guests.dat ================= */

int pc_mp_guests_serialize(const PCMpGuestFile* f, uint8_t* out, size_t cap) {
    int i;
    uint8_t* p;
    if (f == NULL || out == NULL || cap < PC_MP_GUEST_FILE_SIZE) {
        return PC_MP_GST_ERR_SHAPE;
    }
    memset(out, 0, PC_MP_GUEST_FILE_SIZE);
    memcpy(out, s_gst_magic, 8);
    put32(out + 8, PC_MP_GUEST_VERSION);
    put32(out + 12, 0);
    put32(out + 16, PC_MP_GUEST_SLOTS);
    put32(out + 20, PC_MP_GUEST_ENTRY_SIZE);
    put32(out + 24, f->generation);
    put32(out + 28, 0);
    for (i = 0; i < PC_MP_GUEST_SLOTS; i++) {
        const PCMpGuestEntry* e = &f->e[i];
        p = out + PC_MP_GUEST_HEADER_SIZE + (size_t)i * PC_MP_GUEST_ENTRY_SIZE;
        if (!e->present) {
            continue; /* absent entries are all-zero */
        }
        p[0] = 1;
        p[1] = e->confirmed ? 1 : 0;
        memcpy(p + 4, e->pid, PC_MP_GUEST_PID_SIZE);
        memcpy(p + 24, e->token, PC_MP_GUEST_TOKEN_SIZE);
        put32(p + 40, e->epoch);
        put32(p + 44, e->rev);
        put32(p + 48, e->age);
        put32(p + 52, pc_mp_records_crc32(e->record, PC_MP_GUEST_PRIVATE_SIZE));
        memcpy(p + 56, e->town_land_name, PC_MP_GUEST_TOWN_NAME_SIZE);
        p[64] = (uint8_t)(e->town_land_id & 0xFFu);
        p[65] = (uint8_t)(e->town_land_id >> 8);
        put32(p + 68, e->town_terrain_hash);
        memcpy(p + 72, e->record, PC_MP_GUEST_PRIVATE_SIZE);
    }
    put32(out + PC_MP_GUEST_FILE_SIZE - 4, pc_mp_records_crc32(out, PC_MP_GUEST_FILE_SIZE - 4));
    return PC_MP_GST_OK;
}

int pc_mp_guests_parse(const uint8_t* buf, size_t len, PCMpGuestFile* out) {
    uint32_t ver;
    int i, j;
    static PCMpGuestFile tmp;
    if (buf == NULL || out == NULL) {
        return PC_MP_GST_ERR_SHAPE;
    }
    if (len < PC_MP_GUEST_HEADER_SIZE) {
        return PC_MP_GST_ERR_TRUNCATED;
    }
    if (memcmp(buf, s_gst_magic, 8) != 0) {
        return PC_MP_GST_ERR_MAGIC;
    }
    ver = get32(buf + 8);
    if (ver < PC_MP_GUEST_VERSION) {
        return PC_MP_GST_ERR_VERSION_OLD;
    }
    if (ver > PC_MP_GUEST_VERSION) {
        return PC_MP_GST_ERR_VERSION_FUTURE;
    }
    if (get32(buf + 12) != 0 || get32(buf + 16) != PC_MP_GUEST_SLOTS || get32(buf + 20) != PC_MP_GUEST_ENTRY_SIZE ||
        get32(buf + 28) != 0) {
        return PC_MP_GST_ERR_SHAPE;
    }
    if (len < PC_MP_GUEST_FILE_SIZE) {
        return PC_MP_GST_ERR_TRUNCATED;
    }
    if (len != PC_MP_GUEST_FILE_SIZE) {
        return PC_MP_GST_ERR_SHAPE;
    }
    if (get32(buf + PC_MP_GUEST_FILE_SIZE - 4) != pc_mp_records_crc32(buf, PC_MP_GUEST_FILE_SIZE - 4)) {
        return PC_MP_GST_ERR_CRC;
    }
    memset(&tmp, 0, sizeof(tmp));
    tmp.generation = get32(buf + 24);
    for (i = 0; i < PC_MP_GUEST_SLOTS; i++) {
        const uint8_t* p = buf + PC_MP_GUEST_HEADER_SIZE + (size_t)i * PC_MP_GUEST_ENTRY_SIZE;
        PCMpGuestEntry* e = &tmp.e[i];
        if (p[0] > 1 || p[1] > 1 || p[2] != 0 || p[3] != 0) {
            return PC_MP_GST_ERR_FIELD;
        }
        if (p[0] == 0) {
            if (!all_zero(p, PC_MP_GUEST_ENTRY_SIZE)) {
                return PC_MP_GST_ERR_FIELD;
            }
            continue;
        }
        e->present = 1;
        e->confirmed = p[1];
        memcpy(e->pid, p + 4, PC_MP_GUEST_PID_SIZE);
        memcpy(e->token, p + 24, PC_MP_GUEST_TOKEN_SIZE);
        e->epoch = get32(p + 40);
        e->rev = get32(p + 44);
        e->age = get32(p + 48);
        memcpy(e->town_land_name, p + 56, PC_MP_GUEST_TOWN_NAME_SIZE);
        e->town_land_id = (uint16_t)(p[64] | ((uint16_t)p[65] << 8));
        e->town_terrain_hash = get32(p + 68);
        if (p[66] != 0 || p[67] != 0 || all_zero(e->town_land_name, PC_MP_GUEST_TOWN_NAME_SIZE)) {
            return PC_MP_GST_ERR_FIELD;
        }
        if (all_zero(e->pid, PC_MP_GUEST_PID_SIZE) || all_zero(e->token, PC_MP_GUEST_TOKEN_SIZE) || e->epoch == 0 ||
            e->rev > PC_MP_GUEST_MAX_REV) {
            return PC_MP_GST_ERR_FIELD;
        }
        memcpy(e->record, p + 72, PC_MP_GUEST_PRIVATE_SIZE);
        if (get32(p + 52) != pc_mp_records_crc32(e->record, PC_MP_GUEST_PRIVATE_SIZE)) {
            return PC_MP_GST_ERR_RECORD_CRC;
        }
        if (memcmp(e->record, e->pid, PC_MP_GUEST_PID_SIZE) != 0 || e->record[PC_MP_GUEST_EXISTS_OFF] != 1) {
            return PC_MP_GST_ERR_FIELD; /* the record must belong to the key and exist */
        }
        for (j = 0; j < i; j++) {
            if (tmp.e[j].present &&
                ((memcmp(tmp.e[j].pid, e->pid, PC_MP_GUEST_PID_SIZE) == 0 &&
                  memcmp(tmp.e[j].town_land_name, e->town_land_name, PC_MP_GUEST_TOWN_NAME_SIZE) == 0 &&
                  tmp.e[j].town_land_id == e->town_land_id && tmp.e[j].town_terrain_hash == e->town_terrain_hash) ||
                 memcmp(tmp.e[j].token, e->token, PC_MP_GUEST_TOKEN_SIZE) == 0)) {
                return PC_MP_GST_ERR_FIELD; /* two entries may not share a (town, key) or a token */
            }
        }
    }
    *out = tmp;
    return PC_MP_GST_OK;
}

static int read_and_parse(const char* path, PCMpGuestFile* out) {
    static uint8_t buf[PC_MP_GUEST_FILE_SIZE + 1];
    size_t n = 0;
    int r = read_file(path, buf, sizeof(buf), &n);
    if (r != PC_MP_GST_OK) {
        return r;
    }
    return pc_mp_guests_parse(buf, n, out);
}

PCMpGuestLoadMode pc_mp_guests_load(const char* path, PCMpGuestFile* out, PCMpGuestLoadInfo* info) {
    PCMpGuestLoadInfo local;
    static PCMpGuestFile f;
    char gp[320], moved[320];
    int g, any_existed = 0, chosen = -1;
    PCMpGuestLoadInfo* inf = info != NULL ? info : &local;

    memset(inf, 0, sizeof(*inf));
    memset(out, 0, sizeof(*out));
    inf->err[0] = inf->err[1] = inf->err[2] = -1;

    for (g = 0; g < 3; g++) {
        gen_path(path, g, gp, sizeof(gp));
        if (!file_exists(gp)) {
            continue;
        }
        any_existed = 1;
        inf->err[g] = read_and_parse(gp, &f);
        if (inf->err[g] == PC_MP_GST_OK) {
            if (chosen < 0) {
                chosen = g;
                *out = f;
            }
        } else {
            glog("'%s' is UNREADABLE: %s\n", gp, pc_mp_guests_strerror(inf->err[g]));
        }
    }
    if (!any_existed) {
        char first[320];
        int nc;
        first[0] = 0;
        nc = count_corrupt_siblings(path, first, sizeof(first));
        if (nc > 0) {
            memset(out, 0, sizeof(*out));
            inf->mode = PC_MP_GST_LOAD_UNTRUSTED;
            snprintf(inf->first_corrupt_path, sizeof(inf->first_corrupt_path), "%s", first);
            glog("*** no valid guests generation, but %d preserved *.corrupt-* file(s) exist (e.g. '%s'): UNTRUSTED mode, NOT "
                 "'missing'. To deliberately reset (allow guests to join again) remove guests.dat, its .bak files AND the "
                 "*.corrupt-* files together ***\n", nc, first);
            return inf->mode;
        }
        glog("no guests file at '%s' (and no .bak generation, no preserved corrupt files): first run, no guests known\n", path);
        inf->mode = PC_MP_GST_LOAD_MISSING;
        return inf->mode;
    }
    for (g = 0; g < 3; g++) {
        if (inf->err[g] > PC_MP_GST_OK) {
            gen_path(path, g, gp, sizeof(gp));
            if (move_aside(gp, moved, sizeof(moved))) {
                inf->moved_aside++;
                if (inf->first_corrupt_path[0] == 0) {
                    snprintf(inf->first_corrupt_path, sizeof(inf->first_corrupt_path), "%s", moved);
                }
                glog("unreadable '%s' preserved as '%s'\n", gp, moved);
            }
        }
    }
    if (chosen < 0) {
        memset(out, 0, sizeof(*out));
        inf->mode = PC_MP_GST_LOAD_UNTRUSTED;
        glog("*** ALL guests generations are unreadable: UNTRUSTED mode -- new guests are REFUSED (their tokens could not be "
             "told apart from a squatter's); the bad file(s) were preserved ***\n");
        return inf->mode;
    }
    inf->gen_used = chosen;
    if (chosen == 0) {
        inf->mode = PC_MP_GST_LOAD_OK;
        glog("loaded '%s' (generation counter %u)\n", path, (unsigned)out->generation);
    } else {
        inf->mode = PC_MP_GST_LOAD_OK_BACKUP;
        glog("*** guests.dat unusable: RECOVERED from generation .bak%d (generation counter %u) ***\n", chosen,
             (unsigned)out->generation);
    }
    return inf->mode;
}

int pc_mp_guests_save(const char* path, const PCMpGuestFile* f) {
    static uint8_t buf[PC_MP_GUEST_FILE_SIZE];
    static PCMpGuestFile verify;
    char tmp[320], b1[320], b2[320], moved[320];
    FILE* fp;
    size_t want, wrote;
    int r;

    r = pc_mp_guests_serialize(f, buf, sizeof(buf));
    if (r == PC_MP_GST_OK) {
        r = pc_mp_guests_parse(buf, sizeof(buf), &verify); /* self-check: never write something we could not read back */
    }
    if (r != PC_MP_GST_OK) {
        glog("save REFUSED: serialized image failed self-validation (%s)\n", pc_mp_guests_strerror(r));
        return r;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    gen_path(path, 1, b1, sizeof(b1));
    gen_path(path, 2, b2, sizeof(b2));
    ensure_parent_dirs(path);

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
        glog("save FAILED: write error after %lu bytes (simulated); tmp removed, previous generation untouched\n",
             (unsigned long)wrote);
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
        static PCMpGuestFile cur;
        int cr = read_and_parse(path, &cur);
        if (cr == PC_MP_GST_OK) {
            if (file_exists(b2)) {
                remove(b2);
            }
            if (file_exists(b1)) {
                if (rename_over(b1, b2) != 0) {
                    glog("WARNING: could not rotate .bak1 -> .bak2\n");
                }
            }
            if (rename_over(path, b1) != 0) {
                glog("save FAILED: could not rotate '%s' -> '%s'; tmp removed, previous file untouched\n", path, b1);
                remove(tmp);
                return PC_MP_GST_ERR_IO;
            }
        } else {
            glog("current guests file is unreadable (%s) at save time: preserving it aside instead of rotating it over a good "
                 "generation\n", pc_mp_guests_strerror(cr));
            (void)move_aside(path, moved, sizeof(moved));
            if (file_exists(path)) {
                remove(tmp);
                return PC_MP_GST_ERR_IO;
            }
        }
    }
    if (s_fault_on && s_fault.crash_after_rotate) {
        glog("TEST: simulated crash after the .bak rotation (no guests.dat present)\n");
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

/* ================= CLIENT: guest_token.dat ================= */

int pc_mp_gtoken_serialize(const PCMpGtkFile* f, uint8_t* out, size_t cap) {
    int i;
    if (f == NULL || out == NULL || cap < PC_MP_GTK_FILE_SIZE) {
        return PC_MP_GST_ERR_SHAPE;
    }
    memset(out, 0, PC_MP_GTK_FILE_SIZE);
    memcpy(out, s_gtk_magic, 8);
    put32(out + 8, PC_MP_GTK_VERSION);
    put32(out + 12, 0);
    put32(out + 16, PC_MP_GTK_SLOTS);
    put32(out + 20, PC_MP_GTK_ENTRY_SIZE);
    put32(out + 24, f->generation);
    put32(out + 28, 0);
    for (i = 0; i < PC_MP_GTK_SLOTS; i++) {
        const PCMpGtkEntry* e = &f->e[i];
        uint8_t* p = out + PC_MP_GTK_HEADER_SIZE + (size_t)i * PC_MP_GTK_ENTRY_SIZE;
        if (!e->present) {
            continue;
        }
        p[0] = 1;
        memcpy(p + 4, e->host_land_name, 8);
        p[12] = (uint8_t)e->host_land_id;
        p[13] = (uint8_t)(e->host_land_id >> 8);
        put32(p + 16, e->host_terrain_hash);
        memcpy(p + 28, e->home_pid, PC_MP_GUEST_PID_SIZE);
        memcpy(p + 48, e->token, PC_MP_GUEST_TOKEN_SIZE);
    }
    put32(out + PC_MP_GTK_FILE_SIZE - 4, pc_mp_records_crc32(out, PC_MP_GTK_FILE_SIZE - 4));
    return PC_MP_GST_OK;
}

int pc_mp_gtoken_parse(const uint8_t* buf, size_t len, PCMpGtkFile* out) {
    uint32_t ver;
    int i;
    PCMpGtkFile tmp;
    if (buf == NULL || out == NULL) {
        return PC_MP_GST_ERR_SHAPE;
    }
    if (len < PC_MP_GTK_HEADER_SIZE) {
        return PC_MP_GST_ERR_TRUNCATED;
    }
    if (memcmp(buf, s_gtk_magic, 8) != 0) {
        return PC_MP_GST_ERR_MAGIC;
    }
    ver = get32(buf + 8);
    if (ver < PC_MP_GTK_VERSION) {
        return PC_MP_GST_ERR_VERSION_OLD;
    }
    if (ver > PC_MP_GTK_VERSION) {
        return PC_MP_GST_ERR_VERSION_FUTURE;
    }
    if (get32(buf + 12) != 0 || get32(buf + 16) != PC_MP_GTK_SLOTS || get32(buf + 20) != PC_MP_GTK_ENTRY_SIZE ||
        get32(buf + 28) != 0) {
        return PC_MP_GST_ERR_SHAPE;
    }
    if (len < PC_MP_GTK_FILE_SIZE) {
        return PC_MP_GST_ERR_TRUNCATED;
    }
    if (len != PC_MP_GTK_FILE_SIZE) {
        return PC_MP_GST_ERR_SHAPE;
    }
    if (get32(buf + PC_MP_GTK_FILE_SIZE - 4) != pc_mp_records_crc32(buf, PC_MP_GTK_FILE_SIZE - 4)) {
        return PC_MP_GST_ERR_CRC;
    }
    memset(&tmp, 0, sizeof(tmp));
    tmp.generation = get32(buf + 24);
    for (i = 0; i < PC_MP_GTK_SLOTS; i++) {
        const uint8_t* p = buf + PC_MP_GTK_HEADER_SIZE + (size_t)i * PC_MP_GTK_ENTRY_SIZE;
        PCMpGtkEntry* e = &tmp.e[i];
        if (p[0] > 1 || p[1] != 0 || p[2] != 0 || p[3] != 0 || p[14] != 0 || p[15] != 0 || !all_zero(p + 20, 8)) {
            return PC_MP_GST_ERR_FIELD;
        }
        if (p[0] == 0) {
            if (!all_zero(p, PC_MP_GTK_ENTRY_SIZE)) {
                return PC_MP_GST_ERR_FIELD;
            }
            continue;
        }
        e->present = 1;
        memcpy(e->host_land_name, p + 4, 8);
        e->host_land_id = (uint16_t)(p[12] | (p[13] << 8));
        e->host_terrain_hash = get32(p + 16);
        memcpy(e->home_pid, p + 28, PC_MP_GUEST_PID_SIZE);
        memcpy(e->token, p + 48, PC_MP_GUEST_TOKEN_SIZE);
        if (all_zero(e->home_pid, PC_MP_GUEST_PID_SIZE) || all_zero(e->token, PC_MP_GUEST_TOKEN_SIZE)) {
            return PC_MP_GST_ERR_FIELD;
        }
    }
    *out = tmp;
    return PC_MP_GST_OK;
}

static int gtk_read_and_parse(const char* path, PCMpGtkFile* out) {
    static uint8_t buf[PC_MP_GTK_FILE_SIZE + 1];
    size_t n = 0;
    int r = read_file(path, buf, sizeof(buf), &n);
    if (r != PC_MP_GST_OK) {
        return r;
    }
    return pc_mp_gtoken_parse(buf, n, out);
}

int pc_mp_gtoken_load(const char* path, PCMpGtkFile* out, int* unreadable) {
    char b1[320], moved[320];
    int r0 = -1, r1 = -1;
    PCMpGtkFile f;
    *unreadable = 0;
    memset(out, 0, sizeof(*out));
    gen_path(path, 1, b1, sizeof(b1));
    if (file_exists(path)) {
        r0 = gtk_read_and_parse(path, &f);
        if (r0 == PC_MP_GST_OK) {
            *out = f;
            return 1;
        }
    }
    if (file_exists(b1)) {
        r1 = gtk_read_and_parse(b1, &f);
        if (r1 == PC_MP_GST_OK) {
            if (r0 > PC_MP_GST_OK && move_aside(path, moved, sizeof(moved))) {
                glog("guest token file '%s' unreadable (%s): preserved as '%s'; RECOVERED from .bak1\n", path,
                     pc_mp_guests_strerror(r0), moved);
            }
            *out = f;
            return 1;
        }
    }
    if (r0 < 0 && r1 < 0) {
        return 1; /* no file at all: a first run, no tokens */
    }
    if (r0 > PC_MP_GST_OK && move_aside(path, moved, sizeof(moved))) {
        glog("guest token file '%s' is UNREADABLE (%s): preserved as '%s'. The saved guest tokens are unknown; a host that "
             "already knows this guest will refuse it. To start over remove the preserved file and ask the host owner to remove "
             "this guest from the host's save/mp/guests.dat\n", path, pc_mp_guests_strerror(r0), moved);
    }
    if (r1 > PC_MP_GST_OK && move_aside(b1, moved, sizeof(moved))) {
        glog("guest token backup '%s' unreadable (%s): preserved as '%s'\n", b1, pc_mp_guests_strerror(r1), moved);
    }
    *unreadable = 1;
    return 0;
}

int pc_mp_gtoken_save(const char* path, const PCMpGtkFile* f) {
    static uint8_t buf[PC_MP_GTK_FILE_SIZE];
    static PCMpGtkFile verify;
    char tmp[320], b1[320];
    FILE* fp;
    int r;
    r = pc_mp_gtoken_serialize(f, buf, sizeof(buf));
    if (r == PC_MP_GST_OK) {
        r = pc_mp_gtoken_parse(buf, sizeof(buf), &verify);
    }
    if (r != PC_MP_GST_OK) {
        glog("token save REFUSED: image failed self-validation (%s)\n", pc_mp_guests_strerror(r));
        return r;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    gen_path(path, 1, b1, sizeof(b1));
    ensure_parent_dirs(path);
    fp = fopen(tmp, "wb");
    if (fp == NULL) {
        glog("token save FAILED: cannot open '%s' (errno %d)\n", tmp, errno);
        return PC_MP_GST_ERR_IO;
    }
    if (fwrite(buf, 1, sizeof(buf), fp) != sizeof(buf) || commit_file(fp) != 0) {
        fclose(fp);
        remove(tmp);
        glog("token save FAILED: write/flush error; previous file untouched\n");
        return PC_MP_GST_ERR_IO;
    }
    if (fclose(fp) != 0) {
        remove(tmp);
        return PC_MP_GST_ERR_IO;
    }
    if (file_exists(path)) {
        static PCMpGtkFile cur;
        if (gtk_read_and_parse(path, &cur) == PC_MP_GST_OK) {
            if (rename_over(path, b1) != 0) {
                glog("token save FAILED: could not rotate '%s' -> '%s'\n", path, b1);
                remove(tmp);
                return PC_MP_GST_ERR_IO;
            }
        } else {
            char moved[320];
            (void)move_aside(path, moved, sizeof(moved));
            if (file_exists(path)) {
                remove(tmp);
                return PC_MP_GST_ERR_IO;
            }
        }
    }
    if (rename_over(tmp, path) != 0) {
        glog("token save FAILED: replace failed, recovering .bak1\n");
        if (!file_exists(path) && file_exists(b1)) {
            (void)rename_over(b1, path);
        }
        remove(tmp);
        return PC_MP_GST_ERR_IO;
    }
    return PC_MP_GST_OK;
}

int pc_mp_gtoken_find(const PCMpGtkFile* f, const uint8_t* host_land_name, uint16_t host_land_id, uint32_t host_terrain_hash,
                      const uint8_t* home_pid) {
    int i;
    for (i = 0; i < PC_MP_GTK_SLOTS; i++) {
        const PCMpGtkEntry* e = &f->e[i];
        if (e->present && e->host_land_id == host_land_id && e->host_terrain_hash == host_terrain_hash &&
            memcmp(e->host_land_name, host_land_name, 8) == 0 && memcmp(e->home_pid, home_pid, PC_MP_GUEST_PID_SIZE) == 0) {
            return i;
        }
    }
    return -1;
}

int pc_mp_gtoken_put(PCMpGtkFile* f, const PCMpGtkEntry* e) {
    int i = pc_mp_gtoken_find(f, e->host_land_name, e->host_land_id, e->host_terrain_hash, e->home_pid);
    if (i < 0) {
        for (i = 0; i < PC_MP_GTK_SLOTS; i++) {
            if (!f->e[i].present) {
                break;
            }
        }
        if (i >= PC_MP_GTK_SLOTS) { /* full: recycle the oldest-inserted slot (0), shift the rest down */
            memmove(&f->e[0], &f->e[1], sizeof(f->e[0]) * (PC_MP_GTK_SLOTS - 1));
            i = PC_MP_GTK_SLOTS - 1;
        }
    }
    f->e[i] = *e;
    f->e[i].present = 1;
    f->generation++;
    return i;
}

/* Guests G1: see pc_mp_guests.h (the guest NAME rule shared by the client's early check and the host's authority check). */
int pc_mp_guests_name_valid(const uint8_t* name) {
    int k, content = 0;
    if (name == NULL) {
        return 0;
    }
    for (k = 0; k < PC_MP_GUEST_NAME_LEN; k++) {
        const uint8_t ch = name[k];
        if (ch == 0u || ch == 127u || ch == 128u || ch == 205u || ch > 222u) {
            return 0;
        }
        if (ch != 32u && ch != 210u && ch != 211u) {
            content = 1;
        }
    }
    return content;
}

/* Guests G1.1: see pc_mp_guests.h (the reserved observer name; exact 8-byte comparison, shared by the client's early check and the host's authority check). */
int pc_mp_guests_name_reserved(const uint8_t* name) {
    if (name == NULL) {
        return 0;
    }
    return memcmp(name, PC_MP_GUEST_RESERVED_NAME, PC_MP_GUEST_NAME_LEN) == 0;
}

/* ================= Guests G2.1: the fresh-character predicate (see pc_mp_guests.h) ================= */

/* Offsets into the canonical BE Private_c image; pc_net_game.c static-asserts every one of them against offsetof(Private_c, ...). */
static uint16_t fr_be16(const uint8_t* b, size_t o) {
    return (uint16_t)(((unsigned)b[o] << 8) | b[o + 1]);
}

static uint32_t fr_be32(const uint8_t* b, size_t o) {
    return ((uint32_t)b[o] << 24) | ((uint32_t)b[o + 1] << 16) | ((uint32_t)b[o + 2] << 8) | b[o + 3];
}

static int fr_all_zero(const uint8_t* b, size_t o, size_t n) {
    size_t k;
    for (k = 0; k < n; k++) {
        if (b[o + k] != 0) {
            return 0;
        }
    }
    return 1;
}

int pc_mp_guest_record_fresh_check(const uint8_t* r, size_t len, unsigned* detail) {
    size_t i, o;
    unsigned bits = 0;
    unsigned dummy;
    if (detail == NULL) {
        detail = &dummy;
    }
    *detail = 0;
#define FRESH_FAIL(reason, off) do { *detail = (unsigned)(off); return (reason); } while (0)
    if (r == NULL || len != PC_MP_GUEST_PRIVATE_SIZE) {
        FRESH_FAIL(PC_MP_FRESH_BAD_SHAPE, 0);
    }
    /* appearance: the guest's own choice, range-checked only */
    if (r[0x14] > 1u || r[0x15] > 7u || r[0x16] != 0u) {
        FRESH_FAIL(PC_MP_FRESH_BAD_APPEARANCE, 0x14);
    }
    {
        const uint16_t idx = fr_be16(r, 0x1088), item = fr_be16(r, 0x108A);
        if (item < 0x2400u || item > 0x240Fu || idx != (uint16_t)(item - 0x2400u)) {
            FRESH_FAIL(PC_MP_FRESH_BAD_APPEARANCE, 0x1088);
        }
    }
    /* economy */
    for (i = 0; i < 15; i++) {
        if (fr_be16(r, 0x68 + i * 2) != 0u) {
            FRESH_FAIL(PC_MP_FRESH_BAD_POCKET, 0x68 + i * 2);
        }
    }
    if (fr_be32(r, 0x88) != 0u) {
        FRESH_FAIL(PC_MP_FRESH_BAD_POCKET, 0x88);
    }
    if (fr_be32(r, 0x8C) != 0u) {
        FRESH_FAIL(PC_MP_FRESH_BAD_WALLET, 0x8C);
    }
    if (fr_be32(r, 0x122C) != 0u) {
        FRESH_FAIL(PC_MP_FRESH_BAD_BANK, 0x122C);
    }
    if (fr_be32(r, 0x90) != (uint32_t)PC_MP_FRESH_LOAN) {
        FRESH_FAIL(PC_MP_FRESH_BAD_LOAN, 0x90);
    }
    if (r[0x86] != 0u || r[0x87] != 0u) {
        FRESH_FAIL(PC_MP_FRESH_BAD_LOTTO, 0x86);
    }
    if (fr_be16(r, 0x4A4) != 0u) {
        FRESH_FAIL(PC_MP_FRESH_BAD_EQUIPMENT, 0x4A4);
    }
    for (i = 0; i < 10; i++) { /* Mail_c: 0x12A bytes, `present` at +0x2C */
        const uint16_t pr = fr_be16(r, 0x4E0 + i * 0x12A + 0x2C);
        if (pr != 0u && pr != 0xFFFFu) {
            FRESH_FAIL(PC_MP_FRESH_BAD_MAIL_GIFT, 0x4E0 + i * 0x12A + 0x2C);
        }
    }
    if (!fr_all_zero(r, 0x10A8, 0x14)) {
        FRESH_FAIL(PC_MP_FRESH_BAD_CATALOG_ORDER, 0x10A8);
    }
    /* catalog progress: the arrival init sets exactly 3 item-collect bits; anything above that is progress */
    for (o = 0x1108; o < 0x11DC; o++) {
        uint8_t v = r[o];
        while (v != 0) {
            bits += (unsigned)(v & 1u);
            v = (uint8_t)(v >> 1);
        }
    }
    if (bits > (unsigned)PC_MP_FRESH_CATALOG_MAX_BITS) {
        FRESH_FAIL(PC_MP_FRESH_BAD_CATALOG, 0x1108);
    }
    /* quests: every delivery (15 x 0x28 at 0x94) and errand (5 x 0x58 at 0x2EC) slot is the vanilla "none" marker (type 3 = 0xC0 in the BE bitfield byte) */
    for (i = 0; i < 15; i++) {
        if (r[0x94 + i * 0x28] != 0xC0u) {
            FRESH_FAIL(PC_MP_FRESH_BAD_QUEST, 0x94 + i * 0x28);
        }
    }
    for (i = 0; i < 5; i++) {
        if (r[0x2EC + i * 0x58] != 0xC0u) {
            FRESH_FAIL(PC_MP_FRESH_BAD_QUEST, 0x2EC + i * 0x58);
        }
    }
    /* other progress */
    if (r[0x1087] != 0u) {
        FRESH_FAIL(PC_MP_FRESH_BAD_PROGRESS, 0x1087);                       /* hint_count */
    }
    if (!fr_all_zero(r, 0x109A, 0x0A)) {
        FRESH_FAIL(PC_MP_FRESH_BAD_PROGRESS, 0x109A);                       /* destiny */
    }
    if (!fr_all_zero(r, 0x10BC, 0x18) || !fr_all_zero(r, 0x10D4, 0x08)) {
        FRESH_FAIL(PC_MP_FRESH_BAD_PROGRESS, 0x10BC);                       /* unk_10A8, aircheck */
    }
    if (r[0x1102] != 0u || fr_be16(r, 0x1104) != 0u) {
        FRESH_FAIL(PC_MP_FRESH_BAD_PROGRESS, 0x1102);                       /* complete fish/insect flags, celebrated birthday */
    }
    for (i = 0; i < 8; i++) {                                               /* maps: land name spaces, land id 0 */
        size_t m = 0x11DC + i * 10;
        size_t k;
        for (k = 0; k < 8; k++) {
            if (r[m + k] != 0x20u) {
                FRESH_FAIL(PC_MP_FRESH_BAD_PROGRESS, m);
            }
        }
        if (fr_be16(r, m + 8) != 0u) {
            FRESH_FAIL(PC_MP_FRESH_BAD_PROGRESS, m + 8);
        }
    }
    if (fr_be32(r, 0x2348) != 1u) {
        FRESH_FAIL(PC_MP_FRESH_BAD_PROGRESS, 0x2348);                       /* state_flags (includes the post office gift bits) */
    }
    if (!fr_all_zero(r, 0x23B4, 4) || fr_be16(r, 0x23D8) != 0u || r[0x23DA] != 0u || !fr_all_zero(r, 0x23DC, 4) || !fr_all_zero(r, 0x23E0, 0x32)) {
        FRESH_FAIL(PC_MP_FRESH_BAD_PROGRESS, 0x23B4);                       /* tortimer, birthday present npc, golden items, e-Card data */
    }
#undef FRESH_FAIL
    return PC_MP_FRESH_OK;
}

const char* pc_mp_guest_fresh_reason_str(int reason) {
    switch (reason) {
        case PC_MP_FRESH_OK: return "fresh";
        case PC_MP_FRESH_BAD_SHAPE: return "wrong record size";
        case PC_MP_FRESH_BAD_APPEARANCE: return "gender / face / shirt out of range";
        case PC_MP_FRESH_BAD_POCKET: return "a pocket slot is not empty (or an item condition is set)";
        case PC_MP_FRESH_BAD_WALLET: return "wallet is not 0";
        case PC_MP_FRESH_BAD_BANK: return "bank account is not 0";
        case PC_MP_FRESH_BAD_LOAN: return "loan is not the vanilla 100";
        case PC_MP_FRESH_BAD_LOTTO: return "lottery ticket state is not empty";
        case PC_MP_FRESH_BAD_EQUIPMENT: return "equipment is not empty";
        case PC_MP_FRESH_BAD_MAIL_GIFT: return "a letter carries a gift";
        case PC_MP_FRESH_BAD_CATALOG_ORDER: return "a catalog order is not empty";
        case PC_MP_FRESH_BAD_CATALOG: return "catalog progress beyond the 3 starter bits";
        case PC_MP_FRESH_BAD_QUEST: return "a delivery / errand quest is active";
        case PC_MP_FRESH_BAD_PROGRESS: return "game progress is not the vanilla empty default";
        default: return "unknown";
    }
}
