/* ded_console_selftest.c - a scripted driver around the pure console module (pc/src/pc_ded_console.c) for test_dedicated_console.py.
 * stdin lines:  width N | prompt TEXT | key CODE (decimal; 32..126 = a character) | type TEXT | log TEXT | format HH MM SS COLOR WIDTH RAW | classify RAW | echo
 *               body WIDTH RAW  (pc_dedc_wrap_body)
 * stdout lines: OUT <text with \r \n \e escaped>   (bytes the console would receive)
 *               LINE <submitted command>            (an ENTER handed this line over)
 *               FMT <text, \n escaped>  /  CLS <0|1|2>
 * Build: gcc -I pc/include -o ded_console_selftest ded_console_selftest.c ../../src/pc_ded_console.c */
#include "pc_ded_console.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void put_escaped(const char* tag, const char* s) {
    printf("%s ", tag);
    for (; *s; s++) {
        if (*s == '\r') {
            fputs("\\r", stdout);
        } else if (*s == '\n') {
            fputs("\\n", stdout);
        } else if (*s == 0x1b) {
            fputs("\\e", stdout);
        } else if (*s == '\\') {
            fputs("\\\\", stdout);
        } else {
            fputc(*s, stdout);
        }
    }
    fputc('\n', stdout);
}

int main(void) {
    PCDedcEdit e;
    char line[1024];
    char out[8192];
    char sub[PC_DEDC_LINE_MAX + 1];
    int width = 80;
    char prompt[32] = "> ";
    pc_dedc_init(&e);
    while (fgets(line, sizeof(line), stdin) != NULL) {
        size_t n = strlen(line);
        while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r')) {
            line[--n] = '\0';
        }
        if (strncmp(line, "width ", 6) == 0) {
            width = atoi(line + 6);
        } else if (strncmp(line, "prompt ", 7) == 0) {
            snprintf(prompt, sizeof(prompt), "%s", line + 7);
        } else if (strncmp(line, "key ", 4) == 0) {
            int k = atoi(line + 4);
            if (pc_dedc_key(&e, k, sub, sizeof(sub))) {
                pc_dedc_commit_echo(prompt, sub, out, sizeof(out));
                put_escaped("OUT", out);
                printf("LINE %s\n", sub);
            }
            pc_dedc_render(&e, prompt, width, out, sizeof(out));
            put_escaped("OUT", out);
        } else if (strncmp(line, "type ", 5) == 0) {
            const char* p = line + 5;
            for (; *p; p++) {
                pc_dedc_key(&e, (unsigned char)*p, sub, sizeof(sub));
            }
            pc_dedc_render(&e, prompt, width, out, sizeof(out));
            put_escaped("OUT", out);
        } else if (strncmp(line, "log ", 4) == 0) {
            pc_dedc_emit(&e, prompt, width, 1, line + 4, out, sizeof(out));
            put_escaped("OUT", out);
        } else if (strncmp(line, "format ", 7) == 0) {
            int hh, mm, ss, col, w, off = 0;
            if (sscanf(line + 7, "%d %d %d %d %d%n", &hh, &mm, &ss, &col, &w, &off) >= 5) {
                pc_dedc_format(line + 7 + off + 1, hh, mm, ss, col, w, out, sizeof(out));
                put_escaped("FMT", out);
            }
        } else if (strncmp(line, "body ", 5) == 0) {
            int w, off = 0;
            if (sscanf(line + 5, "%d%n", &w, &off) >= 1) {
                pc_dedc_wrap_body(line + 5 + off + 1, w, out, sizeof(out));
                put_escaped("BODY", out);
            }
        } else if (strncmp(line, "classify ", 9) == 0) {
            printf("CLS %d\n", pc_dedc_classify(line + 9));
        } else if (strcmp(line, "echo") == 0) {
            printf("BUF %s|%d|%d\n", e.buf, e.len, e.cur);
        }
    }
    return 0;
}
