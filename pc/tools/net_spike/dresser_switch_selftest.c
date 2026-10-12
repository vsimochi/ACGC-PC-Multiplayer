/* dresser_switch_selftest.c - native selftest of pc_dresser_feature_enabled_from_env (pc/include/pc_dresser_store.h, docs 6n).
 * Build standalone: gcc -std=gnu11 -I pc/include pc/tools/net_spike/dresser_switch_selftest.c -o <scratch>/dresser_switch_selftest.exe */
#include <stdio.h>

#include "pc_dresser_store.h"

int main(void) {
    static const struct {
        const char* env;
        int def, want;
    } t[] = {
        { NULL, 0, 0 }, { NULL, 1, 1 },     /* unset -> the build default */
        { "", 0, 0 },   { "", 1, 1 },       /* empty -> the build default */
        { "0", 0, 0 },  { "0", 1, 0 },      /* "0" -> OFF, also in the dev build */
        { "1", 0, 1 },  { "1", 1, 1 },      /* "1" -> ON, also in release */
        { "x", 0, 0 },  { "x", 1, 1 },      /* anything else -> the build default */
        { "10", 0, 0 }, { "01", 1, 1 }, { "true", 0, 0 }, { " 1", 1, 1 },
    };
    int i, fail = 0;
    for (i = 0; i < (int)(sizeof(t) / sizeof(t[0])); i++) {
        const int got = pc_dresser_feature_enabled_from_env(t[i].env, t[i].def);
        if (got != t[i].want) {
            printf("FAIL env=%s%s%s default=%d -> %d (want %d)\n", t[i].env ? "\"" : "", t[i].env ? t[i].env : "(unset)", t[i].env ? "\"" : "", t[i].def, got, t[i].want);
            fail++;
        }
    }
    printf("dresser_switch_selftest: %s (%d cases, %d failures)\n", fail ? "FAIL" : "PASS", i, fail);
    return fail ? 1 : 0;
}
