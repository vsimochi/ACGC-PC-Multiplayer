/* pc_keytab.h - capacity phase 7: a small DYNAMIC table of fixed-size records keyed by a byte string (a PersonalID, a guest key ...), pure logic.
 * Replaces the fixed arrays that were indexed by "the 4 residents + a few foreigners" or capped at a hand-picked count: one record per stable CHARACTER IDENTITY, found by hash,
 * grown on demand up to an explicit `max` (the caller ties it to a memory budget). Records are dense (0..count-1) so a table can be written to disk in one go.
 * POINTERS INTO A TABLE ARE INVALID AFTER create / remove / load (the storage may move or be compacted). */
#ifndef PC_KEYTAB_H
#define PC_KEYTAB_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct PCKeyTab {
    size_t         elem, key_off, key_len;
    unsigned char* data;
    uint32_t*      idx; /* open-addressed hash index: 0 = empty, else record index + 1 */
    int            count, cap, idx_cap, max;
    int            fail_next; /* TEST SEAM: the next N growths fail */
} PCKeyTab;

/* max = the most records ever held (> 0). 1 = ok. */
int   pc_keytab_init(PCKeyTab* t, size_t elem, size_t key_off, size_t key_len, int max);
void  pc_keytab_free(PCKeyTab* t);
void* pc_keytab_find(const PCKeyTab* t, const void* key);
/* The record of `key`, created zeroed (with the key stored) when absent. NULL = full (count == max) or out of memory; *created (optional) = 1 when it was new. */
void* pc_keytab_get_or_create(PCKeyTab* t, const void* key, int* created);
int   pc_keytab_remove(PCKeyTab* t, const void* key); /* 1 = removed (the last record moves into its place) */
void* pc_keytab_at(const PCKeyTab* t, int i);         /* dense access 0..count-1 */
int   pc_keytab_count(const PCKeyTab* t);
/* Replace the content with `n` records (raw layout, elem bytes each); a record whose key repeats an earlier one is dropped, and more than `max` are not taken. Returns how many were kept. */
int   pc_keytab_load(PCKeyTab* t, const void* recs, int n);

#ifdef __cplusplus
}
#endif
#endif
