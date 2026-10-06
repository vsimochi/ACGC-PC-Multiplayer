/* work_menu_harness.c - links the REAL pc_nook_house.c with stubs for the job state: checks the first page patch of Nook's talk (test_work_menu_patch.py) */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "pc_nook_house.h"

static int g_offer = 1;
static PCWorkView g_view;
static int g_view_known = 1;

int pc_net_game_work_offer_available(void) { return g_offer; }
int pc_net_game_work_view(PCWorkView* v) { *v = g_view; return g_view_known; }
int pc_net_game_house_purchase_price(void) { return 18400; }
uint32_t SDL_GetTicks(void) { return 0; }

/* the vanilla message 0x1092 as dumped from a real game (AC_TEST_DUMP_MSG): "Yes, yes! What can I do for you, hm?" + SETSELSTR4 {0x000A, 0x01C4, 0x0009, 0x000B} */
static const unsigned char VANILLA[] = { 0x59, 0x65, 0x73, 0x2C, 0x20, 0x79, 0x65, 0x73, 0x21, 0x7F, 0x03, 0x08, 0x20, 0x57, 0x68, 0x61, 0x74, 0x20, 0x63, 0x61, 0x6E, 0x20, 0x49, 0xCD, 0x64, 0x6F,
                                         0x20, 0x66, 0x6F, 0x72, 0x20, 0x79, 0x6F, 0x75, 0x2C, 0x20, 0x68, 0x6D, 0x3F, 0x7F, 0x18, 0x00, 0x0A, 0x01, 0xC4, 0x00, 0x09, 0x00, 0x0B, 0x7F, 0x5E,
                                         0x7F, 0x04, 0x7F, 0x0D, 0x7F, 0x19, 0x7F, 0x09, 0x09, 0x00, 0x01, 0xCD, 0x7F, 0x01 };

static int run(const char* what, int idx) {
    unsigned char buf[1536];
    int len = (int)sizeof(VANILLA), n, i, pos = -1;
    memcpy(buf, VANILLA, sizeof(VANILLA));
    n = pc_nook_msg_patch(idx, buf, len, (int)sizeof(buf));
    for (i = 0; i + 1 < (n ? n : len); i++) {
        if (buf[i] == 0x7F && (buf[i + 1] == 0x18 || buf[i + 1] == 0x79)) {
            pos = i;
            break;
        }
    }
    if (pos < 0) {
        printf("%s: no SETSELSTR found\n", what);
        return 1;
    }
    printf("%s: patched=%d len=%d first_page_work=%d code=0x%02X ids=", what, n != 0, n ? n : len, pc_nook_first_page_work(), buf[pos + 1]);
    for (i = 0; i < (buf[pos + 1] == 0x79 ? 5 : 4); i++) {
        printf("%d%s", (buf[pos + 2 + 2 * i] << 8) | buf[pos + 3 + 2 * i], i < 4 ? "," : "");
    }
    printf(" tail_ok=%d\n", n ? (memcmp(&buf[n - 14], &VANILLA[sizeof(VANILLA) - 14], 14) == 0) : 1);
    return 0;
}

int main(void) {
    int bad = 0;
    printf("CONST SEL_WORK=%d SEL_JOB=%d\n", PC_NOOK_SEL_WORK, PC_NOOK_SEL_JOB);
    memset(&g_view, 0, sizeof(g_view));
    g_offer = 0;
    bad += run("offline", 0x1092);            /* solo / not connected: the vanilla page, untouched */
    g_offer = 1;
    g_view.state = 0;
    bad += run("nojob", 0x1092);              /* no active job: 'I'd like to work' */
    g_view.state = 1;
    g_view.mode_on = 1;
    bad += run("activejob", 0x1092);          /* active job: 'Check my job' */
    g_view.state = 0;
    bad += run("completed", 0x1092);          /* job completed / quit (state NONE again): back to 'I'd like to work' */
    g_view_known = 0;
    bad += run("unknownyet", 0x1092);         /* nothing received yet: treated as no job (ENTER resumes an existing one) */
    bad += run("othermsg", 0x1093);           /* any other message is never touched */
    printf("selftest=%d\n", pc_nook_house_selftest());
    return bad;
}
