/* pc_town_sanitize.c - M-G: sanitized town transfer image (pure module: no game headers, see pc_town_sanitize.h).
 *
 * THE TABLE (file offsets; S = 0x26040 main Save_t, O = 0x40 Others block). Everything not listed is KEEP (byte-identical to the host's GCI).
 *   [0, 0x40)                          CARDDir                       keep
 *   [O, O+0x1460)                      comment / banner / icon       keep, except the 8-byte marker "ACMPSAN1" at O+48
 *   [O+0x1460 ...)                     ARAM blocks mail / original / diary (GC order)   -> templates (fresh block), landid (mail) + checksum recomputed
 *   rest of Others up to O+0x26000     zero
 *   S+0x12                             Save_t.save_check.checksum    recomputed over the sanitized Save_t
 *   private_data[i] (S+0x20+i*0x2440, ALL 4 slots, the own record returns through PUSH_FULL):
 *        keep   0x00-0x16 (PersonalID, gender, face, reset_count), 0x1086-0x108B (exists, hint_count, cloth), 0x10F4-0x10F7 (reset_code), 0x2348-0x234B (state_flags)
 *        every other byte -> the cleared Private_c template of slot i
 *   homes[h] (S+0x9CE8+h*0x26B0):      keep header + floors [0, 0x1A30) and gyroid items / message, goki, music box;
 *        mailbox [0x1A30, 0x25D4) (10 x Mail_c) -> cleared Mail_c; gyroid bells [0x2674, 0x2678) zero
 *   animals[a] (S+0x17438+a*0x988) and the islander Animal_c (S+0x22540+0xF00): per memory (+0x10+m*0x138) letter at +0x32: present item (+2..+3) zero,
 *        text bytes (+5..+0xFD) 0x20; the memory header, friendship and the letter_info flags stay
 *   post_office (S+0x20694):           [0, 8) sums / recipient flags zero; mail[5] at +8 -> cleared Mail_c; leaflets keep
 *   [S+0x242A0, S+0x26000)             Save padding zero
 *   backup Save [B, B+0x26000)         copy of the sanitized main Save
 */
#include "pc_town_sanitize.h"

#include <stdio.h>
#include <string.h>

#define PRIV_STRIDE   0x2440u
#define HOME_BASE     0x9CE8u
#define HOME_STRIDE   0x26B0u
#define ANIMAL_BASE   0x17438u
#define ANIMAL_STRIDE 0x988u
#define ISLAND_ANIMAL (0x22540u + 0xF00u)
#define POST_OFFICE   0x20694u
#define LAND_ID_OFF   (0x9120u + 0xAu) /* Save_t.land_info.id (BE u16) */

typedef struct SegBuilder {
    PCTownSanitizeSeg* out;
    int max;
    int n;
    uint32_t pos; /* everything below is already covered */
    int fail;
} SegBuilder;

static void sb_add(SegBuilder* b, uint32_t off, uint32_t len, uint8_t kind, uint8_t arg, uint32_t src_off) {
    PCTownSanitizeSeg* s;
    if (b->fail || len == 0) {
        return;
    }
    if (off < b->pos) { /* ops must be added in increasing offset order */
        b->fail = 1;
        return;
    }
    if (off > b->pos) {
        if (b->n >= b->max) { b->fail = 1; return; }
        s = &b->out[b->n++];
        s->off = b->pos; s->len = off - b->pos; s->kind = PC_TS_KEEP; s->arg = 0; s->src_off = 0;
    }
    if (b->n >= b->max) { b->fail = 1; return; }
    s = &b->out[b->n++];
    s->off = off; s->len = len; s->kind = kind; s->arg = arg; s->src_off = src_off;
    b->pos = off + len;
}

static void sb_end(SegBuilder* b, uint32_t end) {
    if (!b->fail && b->pos < end) {
        PCTownSanitizeSeg* s;
        if (b->n >= b->max) { b->fail = 1; return; }
        s = &b->out[b->n++];
        s->off = b->pos; s->len = end - b->pos; s->kind = PC_TS_KEEP; s->arg = 0; s->src_off = 0;
        b->pos = end;
    }
}

static void sb_animal(SegBuilder* b, uint32_t base) {
    int m;
    for (m = 0; m < 7; m++) {
        const uint32_t letter = base + 0x10u + (uint32_t)m * 0x138u + 0x32u;
        sb_add(b, letter + 2u, 2u, PC_TS_ZERO, 0, 0);               /* attached present item */
        sb_add(b, letter + 5u, 0xFDu - 5u + 1u, PC_TS_FILL20, 0, 0); /* header / body / footer / pad */
    }
}

int pc_town_sanitize_layout(PCTownSanitizeSeg* out, int max) {
    SegBuilder b;
    int i, h, a;
    uint32_t aram;

    b.out = out; b.max = max; b.n = 0; b.pos = 0; b.fail = 0;
    sb_add(&b, PC_TS_MARKER_OFF, PC_TS_MARKER_LEN, PC_TS_MARKER, 0, 0);
    aram = PC_TS_OTHERS_OFF + PC_TS_ARAM_START;
    sb_add(&b, aram, PC_TS_ARAM_MAIL_SIZE, PC_TS_TPL_ARAM, 0, 0);
    aram += PC_TS_ARAM_MAIL_SIZE;
    sb_add(&b, aram, PC_TS_ARAM_ORIG_SIZE, PC_TS_TPL_ARAM, 1, 0);
    aram += PC_TS_ARAM_ORIG_SIZE;
    sb_add(&b, aram, PC_TS_ARAM_DIARY_SIZE, PC_TS_TPL_ARAM, 2, 0);
    aram += PC_TS_ARAM_DIARY_SIZE;
    sb_add(&b, aram, PC_TS_OTHERS_OFF + PC_TS_OTHERS_SIZE - aram, PC_TS_ZERO, 0, 0);

    sb_add(&b, PC_TS_MAIN_OFF + 0x12u, 2u, PC_TS_CKSUM_MAIN, 0, 0);
    for (i = 0; i < 4; i++) {
        const uint32_t p = PC_TS_MAIN_OFF + 0x20u + (uint32_t)i * PRIV_STRIDE;
        sb_add(&b, p + 0x17u,  0x1086u - 0x17u,  PC_TS_TPL_PRIV, (uint8_t)i, 0x17u);
        sb_add(&b, p + 0x108Cu, 0x10F4u - 0x108Cu, PC_TS_TPL_PRIV, (uint8_t)i, 0x108Cu);
        sb_add(&b, p + 0x10F8u, 0x2348u - 0x10F8u, PC_TS_TPL_PRIV, (uint8_t)i, 0x10F8u);
        sb_add(&b, p + 0x234Cu, PRIV_STRIDE - 0x234Cu, PC_TS_TPL_PRIV, (uint8_t)i, 0x234Cu);
    }
    for (h = 0; h < 4; h++) {
        const uint32_t hb = PC_TS_MAIN_OFF + HOME_BASE + (uint32_t)h * HOME_STRIDE;
        for (i = 0; i < 10; i++) {
            sb_add(&b, hb + 0x1A30u + (uint32_t)i * PC_TS_MAIL_SIZE, PC_TS_MAIL_SIZE, PC_TS_TPL_MAIL, 0, 0);
        }
        sb_add(&b, hb + 0x2674u, 4u, PC_TS_ZERO, 0, 0);
    }
    for (a = 0; a < 15; a++) {
        sb_animal(&b, PC_TS_MAIN_OFF + ANIMAL_BASE + (uint32_t)a * ANIMAL_STRIDE);
    }
    sb_add(&b, PC_TS_MAIN_OFF + POST_OFFICE, 8u, PC_TS_ZERO, 0, 0);
    for (i = 0; i < 5; i++) {
        sb_add(&b, PC_TS_MAIN_OFF + POST_OFFICE + 8u + (uint32_t)i * PC_TS_MAIL_SIZE, PC_TS_MAIL_SIZE, PC_TS_TPL_MAIL, 0, 0);
    }
    sb_animal(&b, PC_TS_MAIN_OFF + ISLAND_ANIMAL);
    sb_add(&b, PC_TS_MAIN_OFF + PC_TS_SAVE_T_SIZE, PC_TS_SAVE_SIZE - PC_TS_SAVE_T_SIZE, PC_TS_ZERO, 0, 0);
    sb_add(&b, PC_TS_BACK_OFF, PC_TS_SAVE_SIZE, PC_TS_BACKUP, 0, 0);
    sb_end(&b, PC_TS_GCI_SIZE);
    return b.fail ? 0 : b.n;
}

uint16_t pc_town_sanitize_checksum(const uint8_t* data, uint32_t len) {
    uint32_t i, sum = 0;
    for (i = 0; i + 1u < len; i += 2u) {
        sum += ((uint32_t)data[i] << 8) | data[i + 1u];
    }
    return (uint16_t)((~sum + 1u) & 0xFFFFu);
}

static int tpl_complete(const PCTownSanitizeTpl* t) {
    int i;
    if (t == NULL || t->mail_be == NULL || t->aram_mail_be == NULL || t->aram_orig_be == NULL || t->aram_diary_be == NULL) {
        return 0;
    }
    for (i = 0; i < 4; i++) {
        if (t->private_be[i] == NULL) {
            return 0;
        }
    }
    return 1;
}

int pc_town_sanitize(const uint8_t* in, size_t len, uint8_t* out, const PCTownSanitizeTpl* t) {
    PCTownSanitizeSeg segs[PC_TS_MAX_SEGS];
    int n, k;
    uint8_t land[2];

    if (in == NULL || out == NULL || len != PC_TS_GCI_SIZE || memcmp(in, "GAF", 3) != 0 || !tpl_complete(t)) {
        return 0;
    }
    if ((in < out ? (size_t)(out - in) : (size_t)(in - out)) < PC_TS_GCI_SIZE) {
        return 0; /* overlapping buffers are not supported */
    }
    land[0] = in[PC_TS_MAIN_OFF + LAND_ID_OFF];
    land[1] = in[PC_TS_MAIN_OFF + LAND_ID_OFF + 1u];
    if (land[0] == 0 && land[1] == 0) {
        return 0; /* the loader detects the ARAM block order through the land id: a town without one cannot be transferred */
    }
    n = pc_town_sanitize_layout(segs, PC_TS_MAX_SEGS);
    if (n <= 0) {
        return 0;
    }
    for (k = 0; k < n; k++) {
        const PCTownSanitizeSeg* s = &segs[k];
        uint8_t* d = out + s->off;
        switch (s->kind) {
        case PC_TS_KEEP:   memcpy(d, in + s->off, s->len); break;
        case PC_TS_ZERO:   memset(d, 0, s->len); break;
        case PC_TS_FILL20: memset(d, 0x20, s->len); break;
        case PC_TS_TPL_PRIV: memcpy(d, t->private_be[s->arg] + s->src_off, s->len); break;
        case PC_TS_TPL_MAIL: memcpy(d, t->mail_be, s->len); break;
        case PC_TS_MARKER: memcpy(d, PC_TS_MARKER_TEXT, s->len); break;
        case PC_TS_CKSUM_MAIN: d[0] = 0; d[1] = 0; break; /* computed below */
        case PC_TS_BACKUP: break;                         /* copied below */
        case PC_TS_TPL_ARAM: {
            const uint8_t* src = s->arg == 0 ? t->aram_mail_be : s->arg == 1 ? t->aram_orig_be : t->aram_diary_be;
            uint16_t ck;
            memcpy(d, src, s->len);
            if (s->arg == 0) { /* the loader's block-order detection: BE land id at +2 of the first block */
                d[2] = land[0];
                d[3] = land[1];
            }
            d[0] = 0;
            d[1] = 0;
            ck = pc_town_sanitize_checksum(d, s->len);
            d[0] = (uint8_t)(ck >> 8);
            d[1] = (uint8_t)ck;
            break;
        }
        default: return 0;
        }
    }
    {
        uint8_t* sv = out + PC_TS_MAIN_OFF;
        uint16_t ck = pc_town_sanitize_checksum(sv, PC_TS_SAVE_T_SIZE); /* checksum bytes are zero here */
        sv[0x12] = (uint8_t)(ck >> 8);
        sv[0x13] = (uint8_t)ck;
        memcpy(out + PC_TS_BACK_OFF, sv, PC_TS_SAVE_SIZE);
    }
    return 1;
}

int pc_town_gci_is_sanitized(const uint8_t* buf, size_t len) {
    return buf != NULL && len == PC_TS_GCI_SIZE && memcmp(buf + PC_TS_MARKER_OFF, PC_TS_MARKER_TEXT, PC_TS_MARKER_LEN) == 0;
}

int pc_town_gci_file_is_sanitized(const char* path) {
    uint8_t head[PC_TS_MARKER_OFF + PC_TS_MARKER_LEN];
    long sz;
    FILE* f = fopen(path, "rb");
    int r = 0;
    if (f == NULL) {
        return 0;
    }
    if (fseek(f, 0, SEEK_END) == 0 && (sz = ftell(f)) == (long)PC_TS_GCI_SIZE && fseek(f, 0, SEEK_SET) == 0 &&
        fread(head, 1, sizeof(head), f) == sizeof(head)) {
        r = memcmp(head + PC_TS_MARKER_OFF, PC_TS_MARKER_TEXT, PC_TS_MARKER_LEN) == 0;
    }
    fclose(f);
    return r;
}

int pc_town_gci_find_resident(const uint8_t* buf, size_t len, const uint8_t pid_be[20], int* slot) {
    int i;
    if (buf == NULL || pid_be == NULL || len != PC_TS_GCI_SIZE) {
        return 0;
    }
    for (i = 0; i < 4; i++) {
        const uint8_t* p = buf + PC_TS_MAIN_OFF + 0x20u + (uint32_t)i * PRIV_STRIDE;
        if (p[0x1086] == 1 && memcmp(p, pid_be, 20) == 0) {
            if (slot != NULL) {
                *slot = i;
            }
            return 1;
        }
    }
    return 0;
}
