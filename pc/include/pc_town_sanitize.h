#ifndef PC_TOWN_SANITIZE_H
#define PC_TOWN_SANITIZE_H

/* M-G: SANITIZED town transfer image (pure module, no game headers; natively unit tested by town_sanitize_selftest.c).
 *
 * The host's saved GCI (467,008 B = 0x40 CARDDir + Others 0x26000 + main Save 0x26000 + backup Save 0x26000) holds every resident's private data. A transfer
 * image is derived from it so a fetcher learns no OTHER resident's private data while the game still loads it (the loader reads the main Save and the three ARAM
 * blocks only, checks no checksum). The table of what is kept / replaced is in pc_town_sanitize.c (pc_town_sanitize_layout() exports it as DATA).
 * The replacement bytes ("templates": a cleared Private_c, a cleared Mail_c, the fresh ARAM blocks) come from the game code (pc_m_card.c) as BIG-ENDIAN images,
 * so this module needs no game types. All offsets are mirrored by _Static_asserts in pc_m_card.c. */

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_TS_GCI_SIZE        467008u   /* 0x40 + 0x72000 */
#define PC_TS_HDR_SIZE        0x40u
#define PC_TS_OTHERS_OFF      0x40u     /* "O": comment / banner / icon, then the ARAM blocks */
#define PC_TS_OTHERS_SIZE     0x26000u
#define PC_TS_MAIN_OFF        0x26040u  /* "S": main Save_t */
#define PC_TS_BACK_OFF        0x4C040u  /* "B": backup copy of the main Save */
#define PC_TS_SAVE_SIZE       0x26000u  /* sizeof(Save) (Save_t is 0x242A0, the rest is padding) */
#define PC_TS_SAVE_T_SIZE     0x242A0u
#define PC_TS_ARAM_START      0x1460u   /* offset inside Others of the first ARAM block */
#define PC_TS_ARAM_MAIL_SIZE  0xBAC0u   /* ALIGN(sizeof(mCD_keep_mail_c), 32) */
#define PC_TS_ARAM_ORIG_SIZE  0xCCA0u   /* ALIGN(sizeof(mCD_keep_original_c), 32) */
#define PC_TS_ARAM_DIARY_SIZE 0xBA20u   /* ALIGN(sizeof(mCD_keep_diary_c), 32) */
#define PC_TS_PRIV_SIZE       0x2440u   /* sizeof(Private_c) */
#define PC_TS_MAIL_SIZE       0x12Au    /* sizeof(Mail_c) */
#define PC_TS_MARKER_OFF      0x70u     /* file offset of the 8-byte marker (O + 48; zero in the normal writer) */
#define PC_TS_MARKER_LEN      8u
#define PC_TS_MARKER_TEXT     "ACMPSAN1"

typedef struct PCTownSanitizeTpl {
    const uint8_t* private_be[4]; /* 4 cleared Private_c, BIG-ENDIAN GCI image, 0x2440 B each (slot i has my_org_no_table 0..7) */
    const uint8_t* mail_be;       /* one cleared Mail_c, BIG-ENDIAN, 0x12A B */
    const uint8_t* aram_mail_be;  /* fresh mail ARAM block (0xBAC0 B, BE; checksum / landid are recomputed by the sanitizer) */
    const uint8_t* aram_orig_be;  /* fresh original-designs block (0xCCA0 B) */
    const uint8_t* aram_diary_be; /* fresh diary block (0xBA20 B) */
} PCTownSanitizeTpl;

/* Segment kinds of the table (the segments tile [0, PC_TS_GCI_SIZE) exactly, in order). */
enum {
    PC_TS_KEEP = 0,      /* byte-identical to the input */
    PC_TS_ZERO,          /* zero */
    PC_TS_FILL20,        /* 0x20 (letter text) */
    PC_TS_TPL_PRIV,      /* template private_be[arg] at src_off */
    PC_TS_TPL_MAIL,      /* template mail_be (whole Mail_c) */
    PC_TS_TPL_ARAM,      /* template ARAM block arg (0 mail, 1 original, 2 diary), then landid (mail only) + checksum recomputed */
    PC_TS_MARKER,        /* "ACMPSAN1" */
    PC_TS_CKSUM_MAIN,    /* Save_t.save_check.checksum (2 B), recomputed over the sanitized main Save_t */
    PC_TS_BACKUP         /* copy of the sanitized main Save (0x26000 B) */
};

typedef struct PCTownSanitizeSeg {
    uint32_t off;      /* file offset */
    uint32_t len;
    uint8_t  kind;
    uint8_t  arg;
    uint32_t src_off;  /* TPL_PRIV: offset inside the private record */
} PCTownSanitizeSeg;

#define PC_TS_MAX_SEGS 1024
/* Fills `out` (capacity `max`) with the segment table; returns the count (0 when `max` is too small). */
int pc_town_sanitize_layout(PCTownSanitizeSeg* out, int max);

/* Sanitizes `in` (exactly PC_TS_GCI_SIZE bytes, "GAF" game code, nonzero land id) into `out` (PC_TS_GCI_SIZE bytes, must not overlap `in`). Deterministic,
 * idempotent (sanitize(sanitize(x)) == sanitize(x)). Returns 1 on success, 0 (out untouched) on a refusal. */
int pc_town_sanitize(const uint8_t* in, size_t len, uint8_t* out, const PCTownSanitizeTpl* t);

/* 1 when the image carries the marker (and has the right size). */
int pc_town_gci_is_sanitized(const uint8_t* buf, size_t len);
/* Same for a file (reads only the first 0x80 bytes); 0 when it cannot be read. */
int pc_town_gci_file_is_sanitized(const char* path);

/* The resident slot (0..3) whose private_data holds this 20-byte BIG-ENDIAN PersonalID (name 8, land 8, player id u16, land id u16) AND exists == 1; 1 on a hit. */
int pc_town_gci_find_resident(const uint8_t* buf, size_t len, const uint8_t pid_be[20], int* slot);

/* BE checksum exactly like pc_checksum_be / mFRm_GetFlatCheckSum over `len` bytes (the old checksum bytes must be zeroed by the caller). */
uint16_t pc_town_sanitize_checksum(const uint8_t* data, uint32_t len);

#ifdef __cplusplus
}
#endif

#endif /* PC_TOWN_SANITIZE_H */
