/* pc_mp_records.c - D3-4 host-side multiplayer resident-record sidecar (see pc_mp_records.h for the format and policy).
 *
 * Self-contained: libc + (Windows) the file APIs only. No game headers, no game state. Main-thread use only (no locking). */
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

static const char s_magic[8] = { 'A', 'C', 'M', 'P', 'R', 'E', 'C', 0 };
static PCMpRecFault s_fault;
static int s_fault_on = 0;

void pc_mp_records_set_fault(const PCMpRecFault* f) {
    if (f != NULL) {
        s_fault = *f;
        s_fault_on = 1;
    } else {
        memset(&s_fault, 0, sizeof(s_fault));
        s_fault_on = 0;
    }
}

static void rlog(const char* fmt, ...) {
    va_list ap;
    printf("[NET][REC][STORE] ");
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
}

const char* pc_mp_records_strerror(int err) {
    switch (err) {
        case PC_MP_REC_OK: return "ok";
        case PC_MP_REC_ERR_IO: return "I/O error";
        case PC_MP_REC_ERR_TRUNCATED: return "truncated";
        case PC_MP_REC_ERR_MAGIC: return "bad magic";
        case PC_MP_REC_ERR_VERSION_OLD: return "unsupported OLDER version";
        case PC_MP_REC_ERR_VERSION_FUTURE: return "unsupported FUTURE version";
        case PC_MP_REC_ERR_SHAPE: return "bad shape (slot count / entry size / length / reserved)";
        case PC_MP_REC_ERR_CRC: return "file checksum mismatch";
        case PC_MP_REC_ERR_FIELD: return "entry field out of range";
        case PC_MP_REC_ERR_BACKUP_CRC: return "backup blob checksum mismatch";
        case PC_MP_REC_ERR_FAULT_CRASH: return "simulated crash (test)";
        default: return "unknown error";
    }
}

uint32_t pc_mp_records_crc32(const void* data, size_t n) {
    const uint8_t* p = (const uint8_t*)data;
    uint32_t c = 0xFFFFFFFFu;
    size_t i;
    int k;
    for (i = 0; i < n; i++) {
        c ^= p[i];
        for (k = 0; k < 8; k++) {
            c = (c >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(c & 1u)));
        }
    }
    return ~c;
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

int pc_mp_records_serialize(const PCMpRecFile* f, uint8_t* out, size_t cap) {
    int i;
    uint8_t* p;
    if (f == NULL || out == NULL || cap < PC_MP_REC_FILE_SIZE) {
        return PC_MP_REC_ERR_SHAPE;
    }
    memset(out, 0, PC_MP_REC_FILE_SIZE);
    memcpy(out, s_magic, 8);
    put32(out + 8, PC_MP_REC_VERSION);
    put32(out + 12, 0);
    put32(out + 16, PC_MP_REC_SLOTS);
    put32(out + 20, PC_MP_REC_ENTRY_SIZE);
    put32(out + 24, f->generation);
    put32(out + 28, 0);
    for (i = 0; i < PC_MP_REC_SLOTS; i++) {
        const PCMpRecEntry* e = &f->e[i];
        p = out + PC_MP_REC_HEADER_SIZE + (size_t)i * PC_MP_REC_ENTRY_SIZE;
        if (!e->present) {
            continue; /* absent entries are all-zero */
        }
        p[0] = 1;
        p[1] = e->backup_present ? 1 : 0;
        memcpy(p + 4, e->pid, PC_MP_REC_PID_SIZE);
        put32(p + 24, e->epoch);
        put32(p + 28, e->rev);
        put32(p + 32, e->digest_at_save);
        if (e->backup_present) {
            put32(p + 36, pc_mp_records_crc32(e->backup, PC_MP_REC_PRIVATE_SIZE));
            memcpy(p + 40, e->backup, PC_MP_REC_PRIVATE_SIZE);
        }
    }
    put32(out + PC_MP_REC_FILE_SIZE - 4, pc_mp_records_crc32(out, PC_MP_REC_FILE_SIZE - 4));
    return PC_MP_REC_OK;
}

int pc_mp_records_parse(const uint8_t* buf, size_t len, PCMpRecFile* out) {
    uint32_t ver;
    int i;
    PCMpRecFile tmp;
    if (buf == NULL || out == NULL) {
        return PC_MP_REC_ERR_SHAPE;
    }
    if (len < PC_MP_REC_HEADER_SIZE) {
        return PC_MP_REC_ERR_TRUNCATED;
    }
    if (memcmp(buf, s_magic, 8) != 0) {
        return PC_MP_REC_ERR_MAGIC;
    }
    ver = get32(buf + 8);
    if (ver < PC_MP_REC_VERSION) {
        return PC_MP_REC_ERR_VERSION_OLD;
    }
    if (ver > PC_MP_REC_VERSION) {
        return PC_MP_REC_ERR_VERSION_FUTURE;
    }
    if (get32(buf + 12) != 0 || get32(buf + 16) != PC_MP_REC_SLOTS || get32(buf + 20) != PC_MP_REC_ENTRY_SIZE ||
        get32(buf + 28) != 0) {
        return PC_MP_REC_ERR_SHAPE;
    }
    if (len < PC_MP_REC_FILE_SIZE) {
        return PC_MP_REC_ERR_TRUNCATED;
    }
    if (len != PC_MP_REC_FILE_SIZE) {
        return PC_MP_REC_ERR_SHAPE;
    }
    if (get32(buf + PC_MP_REC_FILE_SIZE - 4) != pc_mp_records_crc32(buf, PC_MP_REC_FILE_SIZE - 4)) {
        return PC_MP_REC_ERR_CRC;
    }
    memset(&tmp, 0, sizeof(tmp));
    tmp.generation = get32(buf + 24);
    for (i = 0; i < PC_MP_REC_SLOTS; i++) {
        const uint8_t* p = buf + PC_MP_REC_HEADER_SIZE + (size_t)i * PC_MP_REC_ENTRY_SIZE;
        PCMpRecEntry* e = &tmp.e[i];
        if (p[0] > 1 || p[1] > 1 || p[2] != 0 || p[3] != 0) {
            return PC_MP_REC_ERR_FIELD;
        }
        if (p[0] == 0) {
            if (!all_zero(p, PC_MP_REC_ENTRY_SIZE)) {
                return PC_MP_REC_ERR_FIELD;
            }
            continue;
        }
        e->present = 1;
        e->backup_present = p[1];
        memcpy(e->pid, p + 4, PC_MP_REC_PID_SIZE);
        e->epoch = get32(p + 24);
        e->rev = get32(p + 28);
        e->digest_at_save = get32(p + 32);
        if (all_zero(e->pid, PC_MP_REC_PID_SIZE) || e->epoch == 0 || e->rev > PC_MP_REC_MAX_REV) {
            return PC_MP_REC_ERR_FIELD;
        }
        if (e->backup_present) {
            memcpy(e->backup, p + 40, PC_MP_REC_PRIVATE_SIZE);
            if (get32(p + 36) != pc_mp_records_crc32(e->backup, PC_MP_REC_PRIVATE_SIZE)) {
                return PC_MP_REC_ERR_BACKUP_CRC;
            }
            if (all_zero(e->backup, PC_MP_REC_PID_SIZE)) {
                return PC_MP_REC_ERR_FIELD;
            }
        } else if (get32(p + 36) != 0 || !all_zero(p + 40, PC_MP_REC_PRIVATE_SIZE)) {
            return PC_MP_REC_ERR_FIELD;
        }
    }
    *out = tmp;
    return PC_MP_REC_OK;
}

/* ---- file helpers ---- */

static int file_exists(const char* path) {
    struct stat st;
    return stat(path, &st) == 0;
}

/* Atomic replace of dst by src (dst may be absent). Windows: MoveFileEx(REPLACE_EXISTING|WRITE_THROUGH); elsewhere rename(). */
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

/* mkdir -p for the parent directory of `path` (every prefix; failures of existing dirs are ignored). */
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

/* Reads a whole file (bounded). Returns an error code; *len receives the size. */
static int read_file(const char* path, uint8_t* buf, size_t cap, size_t* len) {
    FILE* fp = fopen(path, "rb");
    size_t n;
    if (fp == NULL) {
        return PC_MP_REC_ERR_IO;
    }
    n = fread(buf, 1, cap, fp);
    if (ferror(fp)) {
        fclose(fp);
        return PC_MP_REC_ERR_IO;
    }
    fclose(fp);
    *len = n;
    return PC_MP_REC_OK;
}

static int read_and_parse(const char* path, PCMpRecFile* out) {
    static uint8_t buf[PC_MP_REC_FILE_SIZE + 1];
    size_t n = 0;
    int r = read_file(path, buf, sizeof(buf), &n);
    if (r != PC_MP_REC_OK) {
        return r;
    }
    return pc_mp_records_parse(buf, n, out);
}

static void gen_path(const char* path, int gen, char* out, size_t cap) {
    if (gen == 0) {
        snprintf(out, cap, "%s", path);
    } else {
        snprintf(out, cap, "%s.bak%d", path, gen);
    }
}

/* Never delete an unreadable file: move it to <file>.corrupt-<timestamp>[-N]. Returns 1 on success, copies the new path. */
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
        rlog("ERROR: could not move unreadable file '%s' aside (it is left in place, NOT deleted)\n", file);
        new_path[0] = 0;
        return 0;
    }
    return 1;
}

/* Number of preserved-bad-file siblings "<base>.*.corrupt-*" / "<base>.corrupt-*" next to `path` (they are never generations). */
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

PCMpRecLoadMode pc_mp_records_load(const char* path, PCMpRecFile* out, PCMpRecLoadInfo* info) {
    PCMpRecLoadInfo local;
    PCMpRecFile f;
    char gp[320], moved[320];
    int g, any_existed = 0, chosen = -1;
    PCMpRecLoadInfo* inf = info != NULL ? info : &local;

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
        if (inf->err[g] == PC_MP_REC_OK) {
            if (chosen < 0) {
                chosen = g;
                *out = f;
            }
        } else {
            rlog("'%s' is UNREADABLE: %s\n", gp, pc_mp_records_strerror(inf->err[g]));
        }
    }
    if (!any_existed) {
        char first[320];
        int nc;
        first[0] = 0;
        nc = count_corrupt_siblings(path, first, sizeof(first));
        if (nc > 0) {
            /* An earlier run preserved unreadable generations (UNTRUSTED/recovery) and no valid generation exists any more:
             * this is NOT a first run. Staying UNTRUSTED keeps stale client records from re-migrating over host progress. */
            memset(out, 0, sizeof(*out));
            inf->mode = PC_MP_REC_LOAD_UNTRUSTED;
            snprintf(inf->first_corrupt_path, sizeof(inf->first_corrupt_path), "%s", first);
            rlog("*** no valid records generation, but %d preserved *.corrupt-* file(s) exist (e.g. '%s'): entering UNTRUSTED mode, "
                 "NOT 'missing'. To deliberately reset (allow first-join migration again) remove records.dat, its .bak files AND the "
                 "*.corrupt-* files together ***\n", nc, first);
            return inf->mode;
        }
        rlog("no records file at '%s' (and no .bak generation, no preserved corrupt files): treating as a first run, all "
             "residents never-synced (rev 0)\n", path);
        inf->mode = PC_MP_REC_LOAD_MISSING;
        return inf->mode;
    }
    /* Preserve (never delete) every unreadable generation, so the next save's rotation cannot overwrite or drop them. */
    for (g = 0; g < 3; g++) {
        if (inf->err[g] > PC_MP_REC_OK) {
            gen_path(path, g, gp, sizeof(gp));
            if (move_aside(gp, moved, sizeof(moved))) {
                inf->moved_aside++;
                if (inf->first_corrupt_path[0] == 0) {
                    snprintf(inf->first_corrupt_path, sizeof(inf->first_corrupt_path), "%s", moved);
                }
                rlog("unreadable '%s' preserved as '%s'\n", gp, moved);
            }
        }
    }
    if (chosen < 0) {
        memset(out, 0, sizeof(*out));
        inf->mode = PC_MP_REC_LOAD_UNTRUSTED;
        rlog("*** ALL records generations are unreadable: entering UNTRUSTED mode -- migration is DISABLED, the host save record "
             "wins for every resident (rev 1, fresh epoch); the bad file(s) were preserved ***\n");
        return inf->mode;
    }
    inf->gen_used = chosen;
    if (chosen == 0) {
        inf->mode = PC_MP_REC_LOAD_OK;
        rlog("loaded '%s' (generation counter %u)\n", path, (unsigned)out->generation);
    } else {
        inf->mode = PC_MP_REC_LOAD_OK_BACKUP;
        rlog("*** records.dat unusable: RECOVERED from generation .bak%d (generation counter %u) ***\n", chosen,
             (unsigned)out->generation);
    }
    return inf->mode;
}

/* ---- save ---- */

/* Durably flush an existing file by path (the GCI the caller just wrote with a plain fflush): FlushFileBuffers on a handle
 * opened with GENERIC_WRITE and full sharing / fsync elsewhere. 0 = ok. */
int pc_mp_records_sync_file(const char* path) {
#ifdef _WIN32
    HANDLE h = CreateFileA(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    int ok;
    if (h == INVALID_HANDLE_VALUE) {
        return -1;
    }
    ok = FlushFileBuffers(h) ? 0 : -1;
    CloseHandle(h);
    return ok;
#else
    int fd = open(path, O_RDWR);
    int r;
    if (fd < 0) {
        return -1;
    }
    r = fsync(fd);
    close(fd);
    return r;
#endif
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

#ifndef _WIN32
static void sync_parent_dir(const char* path) {
    char dir[512];
    char* s;
    int fd;
    snprintf(dir, sizeof(dir), "%s", path);
    s = strrchr(dir, '/');
    if (s == NULL) {
        return;
    }
    *s = 0;
    fd = open(dir[0] ? dir : "/", O_RDONLY);
    if (fd >= 0) {
        (void)fsync(fd);
        close(fd);
    }
}
#endif

int pc_mp_records_save(const char* path, const PCMpRecFile* f) {
    static uint8_t buf[PC_MP_REC_FILE_SIZE];
    static PCMpRecFile verify;
    char tmp[320], b1[320], b2[320], moved[320];
    FILE* fp;
    size_t want, wrote;
    int r;

    r = pc_mp_records_serialize(f, buf, sizeof(buf));
    if (r == PC_MP_REC_OK) {
        r = pc_mp_records_parse(buf, sizeof(buf), &verify); /* self-check: never write something we could not read back */
    }
    if (r != PC_MP_REC_OK) {
        rlog("save REFUSED: serialized image failed self-validation (%s)\n", pc_mp_records_strerror(r));
        return r;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    gen_path(path, 1, b1, sizeof(b1));
    gen_path(path, 2, b2, sizeof(b2));
    ensure_parent_dirs(path);

    /* 1. tmp write + flush + commit */
    fp = fopen(tmp, "wb");
    if (fp == NULL) {
        rlog("save FAILED: cannot open '%s' (errno %d); previous generation untouched\n", tmp, errno);
        return PC_MP_REC_ERR_IO;
    }
    want = sizeof(buf);
    if (s_fault_on && s_fault.crash_partial_tmp >= 0) {
        wrote = fwrite(buf, 1, (size_t)s_fault.crash_partial_tmp < want ? (size_t)s_fault.crash_partial_tmp : want, fp);
        fflush(fp);
        fclose(fp);
        rlog("TEST: simulated crash after %lu tmp bytes\n", (unsigned long)wrote);
        return PC_MP_REC_ERR_FAULT_CRASH;
    }
    if (s_fault_on && s_fault.fail_write_after >= 0) {
        size_t lim = (size_t)s_fault.fail_write_after < want ? (size_t)s_fault.fail_write_after : want;
        wrote = fwrite(buf, 1, lim, fp);
        fclose(fp);
        remove(tmp);
        rlog("save FAILED: write error after %lu bytes (simulated); tmp removed, previous generation untouched\n",
             (unsigned long)wrote);
        return PC_MP_REC_ERR_IO;
    }
    wrote = fwrite(buf, 1, want, fp);
    if (wrote != want || commit_file(fp) != 0) {
        fclose(fp);
        remove(tmp);
        rlog("save FAILED: write/flush error (disk full?); tmp removed, previous generation untouched\n");
        return PC_MP_REC_ERR_IO;
    }
    if (fclose(fp) != 0) {
        remove(tmp);
        rlog("save FAILED: close error; tmp removed, previous generation untouched\n");
        return PC_MP_REC_ERR_IO;
    }
    if (s_fault_on && s_fault.crash_after_tmp) {
        rlog("TEST: simulated crash after the tmp file was committed (before rotation)\n");
        return PC_MP_REC_ERR_FAULT_CRASH;
    }

    /* 2. rotate: the CURRENT file only becomes .bak1 if it is itself valid; an unreadable current file is preserved aside and
     *    must never displace a good .bak1/.bak2. */
    if (file_exists(path)) {
        static PCMpRecFile cur;
        int cr = read_and_parse(path, &cur);
        if (cr == PC_MP_REC_OK) {
            if (file_exists(b2)) {
                remove(b2);
            }
            if (file_exists(b1)) {
                if (rename_over(b1, b2) != 0) {
                    rlog("WARNING: could not rotate .bak1 -> .bak2\n");
                }
            }
            if (rename_over(path, b1) != 0) {
                rlog("save FAILED: could not rotate '%s' -> '%s'; tmp removed, previous file untouched\n", path, b1);
                remove(tmp);
                return PC_MP_REC_ERR_IO;
            }
        } else {
            rlog("current records file is unreadable (%s) at save time: preserving it aside instead of rotating it over a good "
                 "generation\n", pc_mp_records_strerror(cr));
            (void)move_aside(path, moved, sizeof(moved));
            if (file_exists(path)) {
                remove(tmp);
                return PC_MP_REC_ERR_IO;
            }
        }
    }
    if (s_fault_on && s_fault.crash_after_rotate) {
        rlog("TEST: simulated crash after the .bak rotation (no records.dat present)\n");
        return PC_MP_REC_ERR_FAULT_CRASH;
    }

    /* 3. atomic replace; on failure restore .bak1 -> main (like pc_save_write_gci_to) */
    if ((s_fault_on && s_fault.fail_rename) || rename_over(tmp, path) != 0) {
        rlog("save FAILED: replace '%s' -> '%s' failed, recovering .bak1...\n", tmp, path);
        if (!file_exists(path) && file_exists(b1)) {
            if (rename_over(b1, path) != 0) {
                rlog("ERROR: could not restore .bak1 (the file remains readable as .bak1 on the next load)\n");
            } else {
                rlog("restored previous generation from .bak1\n");
            }
        }
        remove(tmp);
        return PC_MP_REC_ERR_IO;
    }
#ifndef _WIN32
    sync_parent_dir(path);
#endif
    return PC_MP_REC_OK;
}
