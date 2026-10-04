#ifndef PC_LOG_H
#define PC_LOG_H

/**
 * pc_log - category-based diagnostic logging for the PC port (TARGET_PC only; plain C, no heavy includes, usable from
 * src/ decomp C files under #ifdef TARGET_PC).
 *
 *   PC_LOG(PCL_NET, "peer %d connected", id);                    one line, printed only when the NETWORK bit is enabled
 *   PC_LOG_RL(PCL_TXN, 8, 64, "commit %u", n);                   rate limited per call site: the first 8 lines, then every 64th
 *
 * COST WHEN DISABLED: one load of g_pc_log_mask, one AND and one (predicted, not-taken) branch. The arguments are NOT evaluated and
 * nothing is formatted (cheaper than today's "printf into NUL"). Enabled lines are formatted by pc_log_write(), which adds a "[CAT] "
 * prefix (and, with -logtime, a "[t+SECONDS fFRAME] " stamp). ONLY lines written through these macros get that prefix: the ~700
 * pre-existing printf/OSReport call sites are untouched and byte-identical (the net_spike tests grep their exact text).
 *
 * MASK / SWITCHES (parsed in pc_main.c via pc_log_cli_arg(); see also AnimalCrossing --debug-list):
 *   -debug                          GENERAL only (does NOT turn on the verbose subsystems)
 *   -debugnetwork -debugplayers -debugvillagers (or -debugvillager) -debugbuildings -debugitems -debugwildlife -debugsave
 *   -debugevents -debugtransactions -debugrecords -debugguests -debugtownsvc -debugmail -debugshop     (also --debug-<cat>)
 *   --debug=a,b,c                   comma list of category names (case-insensitive); an unknown name is an error (exit 2)
 *   -debugall                       GENERAL + every category (LEGACY stays tied to --verbose)
 *   -debug-list / --debug-list      print the categories and exit 0 before any game init
 *   -logtime                        stamp each PC_LOG line
 *   -logfile PATH                   also append PC_LOG lines to PATH (refused inside a directory named "save")
 *   env PC_LOG=a,b,c                ORed in once at startup (same names; "all"; or a 0x hex mask)
 *   --verbose / -v                  LEGACY | GENERAL: every pre-existing `if (g_pc_verbose) printf` behaves exactly as before
 * Any debug flag (-debug..., --debug...) or PC_LOG request keeps stdout/stderr (no redirect to NUL) and makes them unbuffered, like --verbose.
 * -logfile alone writes only the file; stdout stays redirected exactly as before.
 *
 * CATEGORY -> MODULE MAP (where the new sample PC_LOG lines live; existing lines keep their legacy prefixes):
 *   GENERAL       pc_main.c (startup summary)
 *   NETWORK       pc_net_game.c: host listen, world ready, peer connect/disconnect, client transport-connect
 *   PLAYERS       pc_m_card.c bootstrap-resident bind; pc_net_game.c peer leaving a READY session
 *   VILLAGERS     pc_net_game.c host villager roster at world ready, arrival / departure broadcasts
 *   BUILDINGS     src/game/m_field_info.c mFI_SetFGStructureKeep (field structure replaced / removed)
 *   ITEMS         pc_net_game.c host pickup reservation
 *   WILDLIFE      pc_wildlife_authority.c host spawn trigger
 *   SAVE          pc_m_card.c pc_save_write_authoritative (periodic / early / shutdown save result)
 *   EVENTS        pc_net_game.c event-state blob log
 *   TRANSACTIONS  pc_net_game.c host TXN_COMMIT applied
 *   RECORDS       pc_net_game.c host resident-record exchange messages
 *   GUESTS        pc_net_game.c guest slot confirmed
 *   TOWNSVC       pc_net_game.c host town-service (museum / police) transactions
 *   MAIL          pc_net_game.c host mail transactions
 *   SHOP          pc_net_game.c host shop buy / sell commits
 *   LEGACY        == --verbose (not selectable by name; gates nothing new by itself)
 */

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PCL_GENERAL ((uint32_t)1u << 0)
#define PCL_NET     ((uint32_t)1u << 1)
#define PCL_PLAYERS ((uint32_t)1u << 2)
#define PCL_VILLAGERS ((uint32_t)1u << 3)
#define PCL_BUILDINGS ((uint32_t)1u << 4)
#define PCL_ITEMS   ((uint32_t)1u << 5)
#define PCL_WILDLIFE ((uint32_t)1u << 6)
#define PCL_SAVE    ((uint32_t)1u << 7)
#define PCL_EVENTS  ((uint32_t)1u << 8)
#define PCL_TXN     ((uint32_t)1u << 9)
#define PCL_RECORDS ((uint32_t)1u << 10)
#define PCL_GUESTS  ((uint32_t)1u << 11)
#define PCL_TOWNSVC ((uint32_t)1u << 12)
#define PCL_MAIL    ((uint32_t)1u << 13)
#define PCL_SHOP    ((uint32_t)1u << 14)
#define PCL_LEGACY  ((uint32_t)1u << 31) /* == today's --verbose */
#define PCL_ALL_CATEGORIES ((uint32_t)0x7FFFu) /* GENERAL + the 14 named categories (not LEGACY) */

extern uint32_t g_pc_log_mask;

#if defined(__GNUC__)
#define PC_LOG_UNLIKELY(x) __builtin_expect(!!(x), 0)
#define PC_LOG_FORMAT_ATTR __attribute__((format(printf, 2, 3)))
#else
#define PC_LOG_UNLIKELY(x) (x)
#define PC_LOG_FORMAT_ATTR
#endif

void pc_log_write(uint32_t cat, const char* fmt, ...) PC_LOG_FORMAT_ATTR;

#define PC_LOG(cat, ...) \
    do { if (PC_LOG_UNLIKELY(g_pc_log_mask & (uint32_t)(cat))) pc_log_write((uint32_t)(cat), __VA_ARGS__); } while (0)

#define PC_LOG_RL(cat, burst, every, ...) \
    do { \
        if (PC_LOG_UNLIKELY(g_pc_log_mask & (uint32_t)(cat))) { \
            static uint32_t pcl_n_; \
            uint32_t pcl_c_ = pcl_n_++; \
            if (pcl_c_ < (uint32_t)(burst) || (pcl_c_ % (uint32_t)(every)) == 0u) pc_log_write((uint32_t)(cat), __VA_ARGS__); \
        } \
    } while (0)

/* ---- CLI / startup (pc_main.c only) ---- */
/* Handles one argv entry. Returns 0 = not a log flag; 1 = recognised and consumed; 2 = a log flag with a bad value (a message was written
 * to stderr, the caller exits 2); 3 = -debug-list handled (the caller exits 0). -logfile consumes the NEXT argument: pass it as `next`
 * (NULL when absent) and *consumed_next is set to 1. */
int  pc_log_cli_arg(const char* arg, const char* next, int* consumed_next);
/* After argument parsing: ORs in env PC_LOG and the legacy env aliases, opens -logfile. Returns 0 on success, nonzero = exit 2 (message
 * already printed). *want_console is set to 1 when a debug request (flag or PC_LOG) needs stdout/stderr kept and unbuffered. */
int  pc_log_finish_cli(int* want_console);
void pc_log_print_categories(void);
/* Parses one category name (case-insensitive, singular/plural / short aliases). Returns the bit, or 0 if unknown. */
uint32_t pc_log_parse_category(const char* name);

#ifdef __cplusplus
}
#endif

#endif /* PC_LOG_H */
