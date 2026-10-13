/* pc_museum_donors.h - worker B: host sidecar museum_donors.dat (pure libc, native selftest pc/tools/net_spike/museum_donors_selftest.c).
 *
 * Which GUEST donated a museum exhibit. The town save keeps a 4-bit donor nibble per exhibit (Save_t.museum_display): 1..4 = resident slot + 1, 5 = "deleted player", which is
 * also what every guest donation carries (no other value exists; 6..15 fail the save check). This table maps exhibit -> the guest KEY (its home PersonalID: name, land name,
 * player id, land id; 20 raw bytes). An entry is only MEANINGFUL while that exhibit's nibble is 5 (the caller checks it on every read); entries of exhibits whose nibble is
 * not 5 any more are orphans and are dropped by pc_mdon_prune() (a host crash between the sidecar write and the town save, a promoted guest, a vanilla deletion).
 * File: a pc_tabfile (magic "MDON", version 1, aux = the town hash: a file of another town is ignored), written atomically (tmp -> MoveFileEx / rename). A missing file =
 * no attribution; a corrupt file is refused (PC_TABFILE_BAD: the caller keeps it untouched and does not write over it). */
#ifndef PC_MUSEUM_DONORS_H
#define PC_MUSEUM_DONORS_H

#include <stddef.h>
#include <stdint.h>

#include "pc_keytab.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PC_MDON_EXHIBITS 120u   /* fossil 25 + art 15 + insect 40 + fish 40 */
#define PC_MDON_KEY_LEN  20u    /* PersonalID_c raw bytes */
#define PC_MDON_MAGIC    0x4E4F444Du /* "MDON" */
#define PC_MDON_VERSION  1u

typedef struct PCMuseumDonorRec {
    uint8_t exhibit; /* 0..119 (the table key) */
    uint8_t used;    /* 1 */
    uint8_t _r[2];
    uint8_t key[PC_MDON_KEY_LEN];
} PCMuseumDonorRec; /* 24 B on disk */

typedef struct PCMuseumDonors {
    PCKeyTab t;
    int      init;
} PCMuseumDonors;

int  pc_mdon_init(PCMuseumDonors* d);
void pc_mdon_free(PCMuseumDonors* d);
/* PC_TABFILE_OK / _MISSING (empty table) / _BAD (table untouched, do not overwrite the file). A file of another town (aux != town_hash) loads as empty (*other_town = 1). */
int  pc_mdon_load(PCMuseumDonors* d, const char* path, uint32_t town_hash, int* other_town);
int  pc_mdon_save(const PCMuseumDonors* d, const char* path, uint32_t town_hash); /* 1 = written atomically */
int  pc_mdon_set(PCMuseumDonors* d, int exhibit, const uint8_t key[PC_MDON_KEY_LEN]); /* 1 = stored (replaces) */
int  pc_mdon_get(const PCMuseumDonors* d, int exhibit, uint8_t key_out[PC_MDON_KEY_LEN]);
int  pc_mdon_clear(PCMuseumDonors* d, int exhibit);
/* Drops every entry whose exhibit is not attributable any more (is_guest_nibble(exhibit, ctx) == 0). Returns how many were dropped. */
int  pc_mdon_prune(PCMuseumDonors* d, int (*is_guest_nibble)(int exhibit, void* ctx), void* ctx);
int  pc_mdon_count(const PCMuseumDonors* d);
uint32_t pc_mdon_town_hash(const void* land_name, size_t land_len, uint16_t land_id);

#ifdef __cplusplus
}
#endif
#endif
