/* pc_mp_members.c - host-side resident credential sidecar (save/mp/members.dat). See pc_mp_members.h for the format and policy. Self-contained: libc +
 * (Windows) the file APIs only, plus the CRC32 of pc_mp_records.c. Main-thread use only. The file helpers deliberately mirror the proven ones of
 * pc_mp_guests.c / pc_mp_records.c rather than modifying those shipped, tested modules. This module NEVER prints a token. */
#include "pc_mp_members.h"
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

static const char s_mbr_magic[8] = { 'A', 'C', 'M', 'P', 'M', 'B', 'R', 0 };

static void mlog(const char* fmt, ...) {
    va_list ap;
    printf("[NET][RESIDENT][STORE] ");
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
}

const char* pc_mp_members_strerror(int err) {
    switch (err) {
        case PC_MP_MBR_OK: return "ok";
        case PC_MP_MBR_ERR_IO: return "I/O error";
        case PC_MP_MBR_ERR_TRUNCATED: return "truncated";
        case PC_MP_MBR_ERR_MAGIC: return "bad magic";
        case PC_MP_MBR_ERR_VERSION_OLD: return "unsupported OLDER version";
        case PC_MP_MBR_ERR_VERSION_FUTURE: return "unsupported FUTURE version";
        case PC_MP_MBR_ERR_SHAPE: return "bad shape (slot count / entry size / length / reserved)";
        case PC_MP_MBR_ERR_CRC: return "file checksum mismatch";
        case PC_MP_MBR_ERR_FIELD: return "entry field out of range / inconsistent";
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

/* ---------------- file helpers (mirror pc_mp_guests.c) ---------------- */

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
        return PC_MP_MBR_ERR_IO;
    }
    n = fread(buf, 1, cap, fp);
    if (ferror(fp)) {
        fclose(fp);
        return PC_MP_MBR_ERR_IO;
    }
    fclose(fp);
    *len = n;
    return PC_MP_MBR_OK;
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
        mlog("ERROR: could not move unreadable file '%s' aside (it is left in place, NOT deleted)\n", file);
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

/* ================= serialize / parse ================= */

int pc_mp_members_serialize(const PCMpMemberFile* f, uint8_t* out, size_t cap) {
    int i;
    if (f == NULL || out == NULL || cap < PC_MP_MEMBERS_FILE_SIZE) {
        return PC_MP_MBR_ERR_SHAPE;
    }
    memset(out, 0, PC_MP_MEMBERS_FILE_SIZE);
    memcpy(out, s_mbr_magic, 8);
    put32(out + 8, PC_MP_MEMBERS_VERSION);
    put32(out + 12, 0);
    put32(out + 16, PC_MP_MEMBERS_SLOTS);
    put32(out + 20, PC_MP_MEMBERS_ENTRY_SIZE);
    put32(out + 24, f->generation);
    put32(out + 28, 0);
    for (i = 0; i < PC_MP_MEMBERS_SLOTS; i++) {
        const PCMpMemberEntry* e = &f->e[i];
        uint8_t* p = out + PC_MP_MEMBERS_HEADER_SIZE + (size_t)i * PC_MP_MEMBERS_ENTRY_SIZE;
        if (!e->present) {
            continue; /* absent entries are all-zero */
        }
        p[0] = 1;
        p[1] = e->confirmed ? 1 : 0;
        p[2] = e->res_slot;
        p[3] = e->kind;
        memcpy(p + 4, e->land_name, 8);
        p[12] = (uint8_t)(e->land_id & 0xFFu);
        p[13] = (uint8_t)(e->land_id >> 8);
        put32(p + 16, e->terrain_hash);
        memcpy(p + 20, e->pid, PC_MP_MEMBERS_PID_SIZE);
        memcpy(p + 40, e->token, PC_MP_MEMBERS_TOKEN_SIZE);
        put32(p + 56, e->age);
        memcpy(p + 60, e->aux_pid, PC_MP_MEMBERS_PID_SIZE);
    }
    put32(out + PC_MP_MEMBERS_FILE_SIZE - 4, pc_mp_records_crc32(out, PC_MP_MEMBERS_FILE_SIZE - 4));
    return PC_MP_MBR_OK;
}

int pc_mp_members_parse(const uint8_t* buf, size_t len, PCMpMemberFile* out) {
    uint32_t ver;
    int i, j;
    static PCMpMemberFile tmp;
    if (buf == NULL || out == NULL) {
        return PC_MP_MBR_ERR_SHAPE;
    }
    if (len < PC_MP_MEMBERS_HEADER_SIZE) {
        return PC_MP_MBR_ERR_TRUNCATED;
    }
    if (memcmp(buf, s_mbr_magic, 8) != 0) {
        return PC_MP_MBR_ERR_MAGIC;
    }
    ver = get32(buf + 8);
    if (ver < PC_MP_MEMBERS_VERSION) {
        return PC_MP_MBR_ERR_VERSION_OLD;
    }
    if (ver > PC_MP_MEMBERS_VERSION) {
        return PC_MP_MBR_ERR_VERSION_FUTURE;
    }
    if (get32(buf + 12) != 0 || get32(buf + 16) != PC_MP_MEMBERS_SLOTS || get32(buf + 20) != PC_MP_MEMBERS_ENTRY_SIZE || get32(buf + 28) != 0) {
        return PC_MP_MBR_ERR_SHAPE;
    }
    if (len < PC_MP_MEMBERS_FILE_SIZE) {
        return PC_MP_MBR_ERR_TRUNCATED;
    }
    if (len != PC_MP_MEMBERS_FILE_SIZE) {
        return PC_MP_MBR_ERR_SHAPE;
    }
    if (get32(buf + PC_MP_MEMBERS_FILE_SIZE - 4) != pc_mp_records_crc32(buf, PC_MP_MEMBERS_FILE_SIZE - 4)) {
        return PC_MP_MBR_ERR_CRC;
    }
    memset(&tmp, 0, sizeof(tmp));
    tmp.generation = get32(buf + 24);
    for (i = 0; i < PC_MP_MEMBERS_SLOTS; i++) {
        const uint8_t* p = buf + PC_MP_MEMBERS_HEADER_SIZE + (size_t)i * PC_MP_MEMBERS_ENTRY_SIZE;
        PCMpMemberEntry* e = &tmp.e[i];
        if (p[0] > 1 || p[1] > 1) {
            return PC_MP_MBR_ERR_FIELD;
        }
        if (p[0] == 0) {
            if (!all_zero(p, PC_MP_MEMBERS_ENTRY_SIZE)) {
                return PC_MP_MBR_ERR_FIELD;
            }
            continue;
        }
        e->present = 1;
        e->confirmed = p[1];
        e->res_slot = p[2];
        e->kind = p[3];
        memcpy(e->land_name, p + 4, 8);
        e->land_id = (uint16_t)(p[12] | ((uint16_t)p[13] << 8));
        e->terrain_hash = get32(p + 16);
        memcpy(e->pid, p + 20, PC_MP_MEMBERS_PID_SIZE);
        memcpy(e->token, p + 40, PC_MP_MEMBERS_TOKEN_SIZE);
        e->age = get32(p + 56);
        memcpy(e->aux_pid, p + 60, PC_MP_MEMBERS_PID_SIZE);
        if ((e->kind != PC_MP_MEMBER_KIND_RESIDENT_TOKEN && e->kind != PC_MP_MEMBER_KIND_PROMOTION_HANDOFF) || e->res_slot >= PC_MP_MEMBERS_RES_SLOTS ||
            p[14] != 0 || p[15] != 0 || all_zero(e->land_name, 8)) {
            return PC_MP_MBR_ERR_FIELD;
        }
        if (all_zero(e->pid, PC_MP_MEMBERS_PID_SIZE) || all_zero(e->token, PC_MP_MEMBERS_TOKEN_SIZE)) {
            return PC_MP_MBR_ERR_FIELD;
        }
        if ((e->kind == PC_MP_MEMBER_KIND_RESIDENT_TOKEN) != all_zero(e->aux_pid, PC_MP_MEMBERS_PID_SIZE)) {
            return PC_MP_MBR_ERR_FIELD; /* aux_pid: zero for a resident token, set for a handoff */
        }
        for (j = 0; j < i; j++) {
            const PCMpMemberEntry* o = &tmp.e[j];
            if (o->present &&
                ((o->kind == e->kind && memcmp(o->pid, e->pid, PC_MP_MEMBERS_PID_SIZE) == 0 && memcmp(o->land_name, e->land_name, 8) == 0 &&
                  o->land_id == e->land_id && o->terrain_hash == e->terrain_hash) ||
                 memcmp(o->token, e->token, PC_MP_MEMBERS_TOKEN_SIZE) == 0)) {
                return PC_MP_MBR_ERR_FIELD; /* two entries may not share a (town, pid, kind) or a token */
            }
        }
    }
    *out = tmp;
    return PC_MP_MBR_OK;
}

static int read_and_parse(const char* path, PCMpMemberFile* out) {
    static uint8_t buf[PC_MP_MEMBERS_FILE_SIZE + 1];
    size_t n = 0;
    int r = read_file(path, buf, sizeof(buf), &n);
    if (r != PC_MP_MBR_OK) {
        return r;
    }
    return pc_mp_members_parse(buf, n, out);
}

PCMpMemberLoadMode pc_mp_members_load(const char* path, PCMpMemberFile* out, PCMpMemberLoadInfo* info) {
    PCMpMemberLoadInfo local;
    static PCMpMemberFile f;
    char gp[320], moved[320];
    int g, any_existed = 0, chosen = -1;
    PCMpMemberLoadInfo* inf = info != NULL ? info : &local;

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
        if (inf->err[g] == PC_MP_MBR_OK) {
            if (chosen < 0) {
                chosen = g;
                *out = f;
            }
        } else {
            mlog("'%s' is UNREADABLE: %s\n", gp, pc_mp_members_strerror(inf->err[g]));
        }
    }
    if (!any_existed) {
        char first[320];
        int nc;
        first[0] = 0;
        nc = count_corrupt_siblings(path, first, sizeof(first));
        if (nc > 0) {
            memset(out, 0, sizeof(*out));
            inf->mode = PC_MP_MBR_LOAD_UNTRUSTED;
            snprintf(inf->first_corrupt_path, sizeof(inf->first_corrupt_path), "%s", first);
            mlog("*** no valid members generation, but %d preserved *.corrupt-* file(s) exist (e.g. '%s'): UNTRUSTED mode, NOT 'missing'. To deliberately reset "
                 "remove members.dat, its .bak files AND the *.corrupt-* files together ***\n", nc, first);
            return inf->mode;
        }
        mlog("no members file at '%s' (and no .bak generation, no preserved corrupt files): first run, no resident credentials known\n", path);
        inf->mode = PC_MP_MBR_LOAD_MISSING;
        return inf->mode;
    }
    for (g = 0; g < 3; g++) {
        if (inf->err[g] > PC_MP_MBR_OK) {
            gen_path(path, g, gp, sizeof(gp));
            if (move_aside(gp, moved, sizeof(moved))) {
                inf->moved_aside++;
                if (inf->first_corrupt_path[0] == 0) {
                    snprintf(inf->first_corrupt_path, sizeof(inf->first_corrupt_path), "%s", moved);
                }
                mlog("unreadable '%s' preserved as '%s'\n", gp, moved);
            }
        }
    }
    if (chosen < 0) {
        memset(out, 0, sizeof(*out));
        inf->mode = PC_MP_MBR_LOAD_UNTRUSTED;
        mlog("*** ALL members generations are unreadable: UNTRUSTED mode -- no credential is trusted or minted; the bad file(s) were preserved ***\n");
        return inf->mode;
    }
    inf->gen_used = chosen;
    if (chosen == 0) {
        inf->mode = PC_MP_MBR_LOAD_OK;
        mlog("loaded '%s' (generation counter %u)\n", path, (unsigned)out->generation);
    } else {
        inf->mode = PC_MP_MBR_LOAD_OK_BACKUP;
        mlog("*** members.dat unusable: RECOVERED from generation .bak%d (generation counter %u) ***\n", chosen, (unsigned)out->generation);
    }
    return inf->mode;
}

int pc_mp_members_save(const char* path, const PCMpMemberFile* f) {
    static uint8_t buf[PC_MP_MEMBERS_FILE_SIZE];
    static PCMpMemberFile verify;
    char tmp[320], b1[320], b2[320], moved[320];
    FILE* fp;
    size_t want, wrote;
    int r;

    r = pc_mp_members_serialize(f, buf, sizeof(buf));
    if (r == PC_MP_MBR_OK) {
        r = pc_mp_members_parse(buf, sizeof(buf), &verify); /* self-check: never write something we could not read back */
    }
    if (r != PC_MP_MBR_OK) {
        mlog("save REFUSED: serialized image failed self-validation (%s)\n", pc_mp_members_strerror(r));
        return r;
    }
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    gen_path(path, 1, b1, sizeof(b1));
    gen_path(path, 2, b2, sizeof(b2));
    ensure_parent_dirs(path);

    fp = fopen(tmp, "wb");
    if (fp == NULL) {
        mlog("save FAILED: cannot open '%s' (errno %d); previous generation untouched\n", tmp, errno);
        return PC_MP_MBR_ERR_IO;
    }
    want = sizeof(buf);
    wrote = fwrite(buf, 1, want, fp);
    if (wrote != want || commit_file(fp) != 0) {
        fclose(fp);
        remove(tmp);
        mlog("save FAILED: write/flush error (disk full?); tmp removed, previous generation untouched\n");
        return PC_MP_MBR_ERR_IO;
    }
    if (fclose(fp) != 0) {
        remove(tmp);
        mlog("save FAILED: close error; tmp removed, previous generation untouched\n");
        return PC_MP_MBR_ERR_IO;
    }
    if (file_exists(path)) {
        static PCMpMemberFile cur;
        int cr = read_and_parse(path, &cur);
        if (cr == PC_MP_MBR_OK) {
            if (file_exists(b2)) {
                remove(b2);
            }
            if (file_exists(b1)) {
                if (rename_over(b1, b2) != 0) {
                    mlog("WARNING: could not rotate .bak1 -> .bak2\n");
                }
            }
            if (rename_over(path, b1) != 0) {
                mlog("save FAILED: could not rotate '%s' -> '%s'; tmp removed, previous file untouched\n", path, b1);
                remove(tmp);
                return PC_MP_MBR_ERR_IO;
            }
        } else {
            mlog("current members file is unreadable (%s) at save time: preserving it aside instead of rotating it over a good generation\n",
                 pc_mp_members_strerror(cr));
            (void)move_aside(path, moved, sizeof(moved));
            if (file_exists(path)) {
                remove(tmp);
                return PC_MP_MBR_ERR_IO;
            }
        }
    }
    if (rename_over(tmp, path) != 0) {
        mlog("save FAILED: replace '%s' -> '%s' failed, recovering .bak1...\n", tmp, path);
        if (!file_exists(path) && file_exists(b1)) {
            if (rename_over(b1, path) != 0) {
                mlog("ERROR: could not restore .bak1 (the file remains readable as .bak1 on the next load)\n");
            } else {
                mlog("restored previous generation from .bak1\n");
            }
        }
        remove(tmp);
        return PC_MP_MBR_ERR_IO;
    }
    return PC_MP_MBR_OK;
}

/* ================= table helpers ================= */

int pc_mp_members_find(const PCMpMemberFile* f, int kind, const uint8_t land_name[8], uint16_t land_id, uint32_t terrain_hash,
                       const uint8_t pid[PC_MP_MEMBERS_PID_SIZE]) {
    int i;
    for (i = 0; i < PC_MP_MEMBERS_SLOTS; i++) {
        const PCMpMemberEntry* e = &f->e[i];
        if (e->present && e->kind == (uint8_t)kind && e->land_id == land_id && e->terrain_hash == terrain_hash && memcmp(e->land_name, land_name, 8) == 0 &&
            memcmp(e->pid, pid, PC_MP_MEMBERS_PID_SIZE) == 0) {
            return i;
        }
    }
    return -1;
}

int pc_mp_members_free_slot(const PCMpMemberFile* f) {
    int i;
    for (i = 0; i < PC_MP_MEMBERS_SLOTS; i++) {
        if (!f->e[i].present) {
            return i;
        }
    }
    return -1;
}

uint32_t pc_mp_members_max_age(const PCMpMemberFile* f) {
    uint32_t m = 0;
    int i;
    for (i = 0; i < PC_MP_MEMBERS_SLOTS; i++) {
        if (f->e[i].present && (int32_t)(f->e[i].age - m) > 0) {
            m = f->e[i].age;
        }
    }
    return m;
}
