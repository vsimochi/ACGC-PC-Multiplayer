/* pc_tabfile.h - capacity phase 7: durable file of a PCKeyTab (pure libc, native tests in tools/net_spike/guest_state_selftest.c). Used for work_jobs.dat.
 *   v3 (current): u32 magic, u32 version(3), u32 aux, u32 record count N, then N records (elem bytes), then u32 check (order-sensitive FNV over header + records)
 *   v2 (legacy):  u32 magic, u32 version(2), u32 aux, u32 body size, then a FIXED body of `legacy_count` records, then u32 check (FNV(header) ^ FNV(body))
 * A legacy file is read as it is and rewritten as v3 by the next save. Records are written atomically (tmp -> rename). Records with the first byte of `used_off` == 0 are skipped. */
#ifndef PC_TABFILE_H
#define PC_TABFILE_H

#include <stdint.h>

#include "pc_keytab.h"

#ifdef __cplusplus
extern "C" {
#endif

enum { PC_TABFILE_BAD = -1, PC_TABFILE_MISSING = 0, PC_TABFILE_OK = 1 };

uint32_t pc_tabfile_fnv(const void* p, size_t n);
int pc_tabfile_save(const char* path, uint32_t magic, uint32_t version, uint32_t aux, const PCKeyTab* t); /* 1 = written + renamed, 0 = failed (the old file is untouched) */
/* Loads into `t` (replacing its content). legacy_version / legacy_count = 0 disables the v2 reader. *aux_out / *version_out (optional) are set on success. */
int pc_tabfile_load(const char* path, uint32_t magic, uint32_t version, uint32_t legacy_version, int legacy_count, size_t used_off, PCKeyTab* t, uint32_t* aux_out, uint32_t* version_out, int* kept_out);

#ifdef __cplusplus
}
#endif
#endif
