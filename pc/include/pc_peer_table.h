/* pc_peer_table.h - the PURE (no sockets, no game headers) logic of the dynamic peer table: how many peer ids exist, how a free id is chosen, a
 * wire-id-wide peer set, and runtime-sized per-peer arrays. Used by pc_net.c (the transport's slot table), pc_net_game.c (the host's per-peer state) and tested
 * natively by tools/net_spike/peer_table_selftest.c.
 *
 * Terms (they are different things and are never conflated):
 *   wire id space   the uint8_t net_player_id every message carries: 0..254 are usable, 0xFF means "nobody" (NPC_LEASE). Fixed by the protocol.
 *   reserved id     the one id inside that range that no client may ever own: the HOST's wire id (PC_NETGAME_HOST_WIRE_ID, 8).
 *   capacity        how many simultaneous transport peers the operator allows (host startup setting). NOT a constant of the protocol.
 *   span            how many slot indices the tables need so that every possible peer id of that capacity is a valid index: a peer id IS its slot index, and the
 *                   reserved id is skipped, so span = capacity (capacity <= reserved id) or capacity + 1 (the slot of the reserved id stays an unused hole).
 * With the historical capacity 8 and reserved id 8 the span is 8 and ids 0..7 are exactly the old ones. */
#ifndef PC_PEER_TABLE_H
#define PC_PEER_TABLE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_PEER_ID_LAST      254   /* highest usable wire id */
#define PC_PEER_ID_NOBODY    0xFF  /* never a peer / player id */
#define PC_PEER_ID_SPACE     256   /* number of values a uint8_t wire id can take (size of per-wire-id arrays) */
#define PC_PEER_DEFAULT_CAPACITY 8 /* the historical table size: the default capacity, no longer a limit */

/* Largest capacity that still fits the usable wire ids once `reserved_id` is taken out (254 for a reserved id in 0..254). */
int pc_peer_capacity_max(int reserved_id);

/* Slot-table length for `capacity` peers (see "span" above); 0 when the capacity is < 1 or above pc_peer_capacity_max(). */
int pc_peer_span(int capacity, int reserved_id);

/* 1 iff `id` can be a peer's id in a table of `span` slots: 0 <= id < span, not the reserved id, <= PC_PEER_ID_LAST. */
int pc_peer_id_usable(int id, int span, int reserved_id);

/* The lowest usable id whose slot `is_free(ctx, id)` accepts, or -1 (table full). The transport calls this with its own slot state. */
int pc_peer_find_free(int span, int reserved_id, int (*is_free)(void* ctx, int id), void* ctx);

/* A set of peer ids over the WHOLE wire id space (256 bits): replaces 16-bit "bit i = peer i" masks. Ids outside 0..255 are never members. */
#define PC_PEER_SET_WORDS 8
typedef struct PCPeerSet {
    uint32_t w[PC_PEER_SET_WORDS];
} PCPeerSet;
void pc_peer_set_clear(PCPeerSet* s);
void pc_peer_set_add(PCPeerSet* s, int id);
void pc_peer_set_del(PCPeerSet* s, int id);
int  pc_peer_set_has(const PCPeerSet* s, int id);
int  pc_peer_set_empty(const PCPeerSet* s);
int  pc_peer_set_count(const PCPeerSet* s);
int  pc_peer_set_has_other(const PCPeerSet* s, int id); /* 1 iff some member is not `id` */
uint32_t pc_peer_set_low32(const PCPeerSet* s);         /* members 0..31 as bits (log output: identical to the old 16-bit mask for peers < 16) */

/* Runtime-sized per-peer arrays. Each table keeps its storage INLINE (a static array of `inline_n` elements: exactly the old fixed array) and only moves to the heap
 * when a span above `inline_n` is requested, so the default 8-peer host runs on the same static memory as before and nothing dereferences NULL before a host starts.
 * `bind` receives the pointer the game code must use from now on. */
typedef struct PCPeerTable {
    void (*bind)(void* base);
    void*  inline_buf; /* static storage, inline_n elements */
    size_t elem;       /* sizeof one per-peer element (a whole row for a 2-D table) */
    void*  heap;       /* current heap storage, NULL while inline */
} PCPeerTable;

/* Declares a runtime-sized per-peer table `NAME` (element type TYPE) with INLINE_N inline elements, and the bind function a PCPeerTable entry needs. PC_PEER_TABLE2 is the same for a
 * table of rows (TYPE[DIM] per peer). PC_PEER_TABLE_ENTRY(NAME) is its PCPeerTable initialiser. Code keeps using NAME[peer] / &NAME[peer] / sizeof(NAME[peer]) unchanged. */
#define PC_PEER_TABLE(TYPE, NAME, INLINE_N)                                  \
    static TYPE NAME##_inl[INLINE_N];                                        \
    static TYPE* NAME = NAME##_inl;                                          \
    static void NAME##_bind(void* base) { NAME = (TYPE*)base; }
#define PC_PEER_TABLE2(TYPE, NAME, INLINE_N, DIM)                            \
    static TYPE NAME##_inl[INLINE_N][DIM];                                   \
    static TYPE (*NAME)[DIM] = NAME##_inl;                                   \
    static void NAME##_bind(void* base) { NAME = (TYPE(*)[DIM])base; }
#define PC_PEER_TABLE_ENTRY(NAME) { NAME##_bind, NAME##_inl, sizeof(NAME##_inl[0]), NULL }

/* All-or-nothing: every table is given `span` zeroed elements (heap when span > inline_n, else the zeroed inline buffer). 1 = done, 0 = an allocation failed and NOTHING changed. */
int  pc_peer_tables_resize(PCPeerTable* t, int n, int inline_n, int span);
/* Back to the zeroed inline storage (frees any heap). */
void pc_peer_tables_release(PCPeerTable* t, int n, int inline_n);
/* Growable tables (the guest store: one element per guest, and the record slots that follow the 4 residents in the shared slot index space). Same inline-first idea as above,
 * but the count GROWS at run time and the contents are KEPT: pc_grow_tables_ensure() reallocates every table to base + want elements, copies the base + have in use, rebinds
 * the pointers and frees the old heap block. All-or-nothing. POINTERS INTO A TABLE ARE INVALID AFTER A GROWTH: take element addresses only after the call. */
typedef struct PCGrowTable {
    void (*bind)(void* base);
    void*  inline_buf; /* static storage, base + inline_n elements */
    size_t elem;       /* sizeof one element (a row for a 2-D table) */
    int    base;       /* elements that exist whatever the count (e.g. the 4 resident record slots) */
    void*  heap;       /* current heap block, NULL while inline */
    void*  cur;        /* the storage currently bound (set by the helper; initialise to inline_buf) */
} PCGrowTable;
#define PC_GROW_TABLE(TYPE, NAME, BASE, INLINE_N)                            \
    static TYPE NAME##_inl[(BASE) + (INLINE_N)];                             \
    static TYPE* NAME = NAME##_inl;                                          \
    static void NAME##_bind(void* b) { NAME = (TYPE*)b; }
#define PC_GROW_TABLE2(TYPE, NAME, BASE, INLINE_N, DIM)                      \
    static TYPE NAME##_inl[(BASE) + (INLINE_N)][DIM];                        \
    static TYPE (*NAME)[DIM] = NAME##_inl;                                   \
    static void NAME##_bind(void* b) { NAME = (TYPE(*)[DIM])b; }
#define PC_GROW_TABLE_ENTRY(NAME, BASE) { NAME##_bind, NAME##_inl, sizeof(NAME##_inl[0]), (BASE), NULL, NAME##_inl }
/* want <= have: nothing to do (1). Otherwise every table gets base + want elements (the new ones zeroed). 0 = an allocation failed and NOTHING changed. */
int pc_grow_tables_ensure(PCGrowTable* t, int n, int have, int want);

/* TEST SEAM: replaces calloc / free (NULL = the C library, the only production value). */
void pc_peer_tables_set_allocator(void* (*alloc)(size_t count, size_t size), void (*release)(void*));

#ifdef __cplusplus
}
#endif
#endif
