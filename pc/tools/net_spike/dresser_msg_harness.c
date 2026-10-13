/* dresser_msg_harness.c - links the REAL pc_nook_house.c with stubs: checks the one-shot "More storage" choice patch of the owner dresser messages
 * (pc_dresser_msg_arm / pc_nook_msg_patch / pc_dresser_msg_more_choice / pc_nook_sel_build). Driven by test_dresser_msg_patch.py. */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "pc_nook_house.h"

int pc_net_game_work_offer_available(void) { return 0; }
int pc_net_game_work_view(PCWorkView* v) { memset(v, 0, sizeof(*v)); return 0; }
int pc_net_game_house_purchase_price(void) { return 18400; }
int pc_shop_upgrade_minutes_total(void) { return 5; } /* the shop upgrade rows (pc_shop_upgrade.h) */
int pc_shop_upgrade_minutes_left(void) { return 3; }
uint32_t SDL_GetTicks(void) { return 0; }

/* text "AB" + SETSELSTRk {ids} + 0x7F 0x5E + "Z" + MSGEND-like tail */
static int make(unsigned char* b, int k) {
    static const unsigned char sel[7] = { 0, 0, 0x16, 0x17, 0x18, 0x79, 0x7A };
    int n = 0, i;
    b[n++] = 'A';
    b[n++] = 'B';
    b[n++] = 0x7F;
    b[n++] = sel[k];
    for (i = 0; i < k; i++) {
        b[n++] = 0x00;
        b[n++] = (unsigned char)(0x20 + i);
    }
    b[n++] = 0x7F;
    b[n++] = 0x5E;
    b[n++] = 'Z';
    b[n++] = 0x7F;
    b[n++] = 0x01;
    return n;
}

static void dump(const char* what, const unsigned char* b, int n, int idx) {
    int i;
    printf("%s: len=%d more=%d bytes=", what, n, pc_dresser_msg_more_choice(idx));
    for (i = 0; i < n; i++) {
        printf("%02X", b[i]);
    }
    printf("\n");
}

int main(void) {
    unsigned char b[64], lbl[16];
    int n, r;
    printf("CONST MORE=%d\n", PC_NOOK_SEL_DRESSER_MORE);
    printf("label_ok=%d label=", pc_nook_sel_build(PC_NOOK_SEL_DRESSER_MORE, lbl));
    fwrite(lbl, 1, 16, stdout);
    printf("|\n");
    /* not armed: untouched */
    n = make(b, 2);
    r = pc_nook_msg_patch(2570, b, n, (int)sizeof(b));
    dump("unarmed", b, r ? r : n, 2570);
    /* armed for 2570, another message loads first: untouched, the arm stays */
    pc_dresser_msg_arm(2570);
    n = make(b, 3);
    r = pc_nook_msg_patch(2571, b, n, (int)sizeof(b));
    dump("othermsg", b, r ? r : n, 2571);
    /* the armed message: 2 choices -> 3, More before the last */
    n = make(b, 2);
    r = pc_nook_msg_patch(2570, b, n, (int)sizeof(b));
    dump("sel2", b, r ? r : n, 2570);
    /* one-shot: the next load of the same message is untouched, but the last patch is still reported until re-armed */
    n = make(b, 2);
    r = pc_nook_msg_patch(2570, b, n, (int)sizeof(b));
    dump("again", b, r ? r : n, 2570);
    /* 3 choices -> 4 */
    pc_dresser_msg_arm(2571);
    n = make(b, 3);
    r = pc_nook_msg_patch(2571, b, n, (int)sizeof(b));
    dump("sel3", b, r ? r : n, 2571);
    /* disarm (feature unavailable): nothing reported */
    pc_dresser_msg_arm(-1);
    dump("disarmed", b, 0, 2571);
    /* no room in the buffer: untouched */
    pc_dresser_msg_arm(12058);
    n = make(b, 3);
    r = pc_nook_msg_patch(12058, b, n, n + 1);
    dump("nocap", b, r ? r : n, 12058);
    printf("selftest=%d\n", pc_nook_house_selftest());
    return 0;
}
