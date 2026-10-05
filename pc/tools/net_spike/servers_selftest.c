/* servers_selftest.c - standalone native unit test of pc_servers.c (saved server profiles) and pc_relaunch_build_args (Play Online relaunch command line).
 * Usage: servers_selftest <scratch_dir>   (everything lives under <scratch_dir>). Prints PASS:/FAIL: lines and "RESULT passed=N failed=M". */
#include "pc_relaunch.h"
#include "pc_servers.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#ifdef _WIN32
#include <direct.h>
#define MKDIR(p) _mkdir(p)
#else
#define MKDIR(p) mkdir(p, 0755)
#endif

static int g_pass = 0, g_fail = 0;

static void check(const char* what, int cond) {
    if (cond) {
        g_pass++;
        printf("PASS: %s\n", what);
    } else {
        g_fail++;
        printf("FAIL: %s\n", what);
    }
}

static int slurp(const char* path, char* buf, size_t cap) {
    FILE* fp = fopen(path, "rb");
    size_t n;
    if (!fp) return -1;
    n = fread(buf, 1, cap - 1, fp);
    fclose(fp);
    buf[n] = '\0';
    return (int)n;
}

static void put(const char* path, const char* text) {
    FILE* fp = fopen(path, "wb");
    fwrite(text, 1, strlen(text), fp);
    fclose(fp);
}

static PCServer mk(const char* name, const char* addr, int port) {
    PCServer s;
    memset(&s, 0, sizeof(s));
    snprintf(s.name, sizeof(s.name), "%s", name);
    snprintf(s.address, sizeof(s.address), "%s", addr);
    s.port = port;
    return s;
}

int main(int argc, char** argv) {
    char dir[300], path[320], tmp[330], err[400], a[16], b1[4096], b2[4096];
    PCServer l[PC_SERVER_MAX + 4], s;
    int n = 0, port = 0, i, r;
    if (argc < 2) {
        fprintf(stderr, "usage: servers_selftest <scratch_dir>\n");
        return 2;
    }
    snprintf(dir, sizeof(dir), "%s", argv[1]);
    MKDIR(dir);
    snprintf(path, sizeof(path), "%s/servers.ini", dir);
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);

    /* validation */
    check("name: ok", pc_servers_name_check("Friends 1", NULL, 0));
    check("name: empty refused", !pc_servers_name_check("", err, sizeof(err)));
    check("name: 33 chars refused", !pc_servers_name_check("123456789012345678901234567890123", err, sizeof(err)));
    check("name: 32 chars ok", pc_servers_name_check("12345678901234567890123456789012", err, sizeof(err)));
    check("name: '[' refused", !pc_servers_name_check("a[b", err, sizeof(err)));
    check("name: ']' refused", !pc_servers_name_check("a]b", err, sizeof(err)));
    check("name: '=' refused", !pc_servers_name_check("a=b", err, sizeof(err)));
    check("name: newline refused", !pc_servers_name_check("a\nb", err, sizeof(err)));
    check("name: tab / control refused", !pc_servers_name_check("a\tb", err, sizeof(err)));
    check("name: leading space refused", !pc_servers_name_check(" ab", err, sizeof(err)));
    check("address: ok", pc_servers_address_check("192.168.1.20", err, sizeof(err)));
    check("address: 255.255.255.255 ok", pc_servers_address_check("255.255.255.255", err, sizeof(err)));
    check("address: hostname refused", !pc_servers_address_check("example.com", err, sizeof(err)));
    check("address: localhost refused (no resolution)", !pc_servers_address_check("localhost", err, sizeof(err)));
    check("address: 256 refused", !pc_servers_address_check("1.2.3.256", err, sizeof(err)));
    check("address: 3 parts refused", !pc_servers_address_check("1.2.3", err, sizeof(err)));
    check("address: 5 parts refused", !pc_servers_address_check("1.2.3.4.5", err, sizeof(err)));
    check("address: leading zero refused", !pc_servers_address_check("01.2.3.4", err, sizeof(err)));
    check("address: trailing dot refused", !pc_servers_address_check("1.2.3.4.", err, sizeof(err)));
    check("address: with port refused", !pc_servers_address_check("1.2.3.4:7777", err, sizeof(err)));
    check("port: 0 refused, 65536 refused, 1 / 65535 ok", !pc_servers_port_check(0) && !pc_servers_port_check(65536) && pc_servers_port_check(1) && pc_servers_port_check(65535));
    check("hostport: default port 7777", pc_servers_parse_hostport("10.0.0.5", a, &port, err, sizeof(err)) && port == 7777 && strcmp(a, "10.0.0.5") == 0);
    check("hostport: explicit port", pc_servers_parse_hostport("10.0.0.5:9000", a, &port, err, sizeof(err)) && port == 9000);
    check("hostport: bad port refused", !pc_servers_parse_hostport("10.0.0.5:0", a, &port, err, sizeof(err)) && !pc_servers_parse_hostport("10.0.0.5:x", a, &port, err, sizeof(err)) &&
                                          !pc_servers_parse_hostport("10.0.0.5:", a, &port, err, sizeof(err)));

    /* round trip */
    r = pc_servers_load(dir, l, PC_SERVER_MAX, &n, err, sizeof(err));
    check("load: missing file = OK, empty", r == PC_SERVERS_OK && n == 0);
    s = mk("Friends", "192.168.1.20", 7777);
    check("add Friends", pc_servers_add(dir, &s, err, sizeof(err)) == PC_SERVERS_OK);
    s = mk("Work", "10.0.0.5", 9000);
    check("add Work", pc_servers_add(dir, &s, err, sizeof(err)) == PC_SERVERS_OK);
    s = mk("FRIENDS", "1.1.1.1", 1);
    check("add duplicate (case-insensitive) refused EXISTS", pc_servers_add(dir, &s, err, sizeof(err)) == PC_SERVERS_EXISTS);
    s = mk("Bad", "example.com", 7777);
    check("add hostname refused INVALID", pc_servers_add(dir, &s, err, sizeof(err)) == PC_SERVERS_INVALID);
    s = mk("Bad", "1.1.1.1", 0);
    check("add port 0 refused INVALID", pc_servers_add(dir, &s, err, sizeof(err)) == PC_SERVERS_INVALID);
    r = pc_servers_load(dir, l, PC_SERVER_MAX, &n, err, sizeof(err));
    check("list: 2 entries in order", r == PC_SERVERS_OK && n == 2 && strcmp(l[0].name, "Friends") == 0 && l[0].port == 7777 && strcmp(l[1].name, "Work") == 0 && l[1].port == 9000 &&
                                      strcmp(l[1].address, "10.0.0.5") == 0);
    check("find: case-insensitive", pc_servers_find(l, n, "wORK") == 1 && pc_servers_find(l, n, "nope") == -1);
    check("no .tmp left behind after writes", slurp(tmp, b1, sizeof(b1)) < 0);
    check("set_last stores the hints", pc_servers_set_last(dir, "work", "Roger", "Foo") == PC_SERVERS_OK);
    s = mk("Work2", "10.0.0.6", 9001);
    check("update (rename, new address / port)", pc_servers_update(dir, "WORK", &s, err, sizeof(err)) == PC_SERVERS_OK);
    r = pc_servers_load(dir, l, PC_SERVER_MAX, &n, err, sizeof(err));
    check("update applied and hints kept", r == PC_SERVERS_OK && n == 2 && strcmp(l[1].name, "Work2") == 0 && l[1].port == 9001 && strcmp(l[1].address, "10.0.0.6") == 0 &&
                                           strcmp(l[1].last_character, "Roger") == 0 && strcmp(l[1].last_town, "Foo") == 0);
    s = mk("friends", "10.0.0.6", 9001);
    check("update to a taken name refused EXISTS", pc_servers_update(dir, "Work2", &s, err, sizeof(err)) == PC_SERVERS_EXISTS);
    check("update unknown refused NOTFOUND", pc_servers_update(dir, "zzz", &s, err, sizeof(err)) == PC_SERVERS_NOTFOUND);
    check("delete Friends", pc_servers_delete(dir, "friends", err, sizeof(err)) == PC_SERVERS_OK);
    check("delete again refused NOTFOUND", pc_servers_delete(dir, "Friends", err, sizeof(err)) == PC_SERVERS_NOTFOUND);
    r = pc_servers_load(dir, l, PC_SERVER_MAX, &n, err, sizeof(err));
    check("list after delete: only Work2", r == PC_SERVERS_OK && n == 1 && strcmp(l[0].name, "Work2") == 0);
    check("a hand-written file with comments and defaults parses",
          (put(path, "# c\n; c\n[server]\nname = Hand\naddress = 127.0.0.1\n\n[server]\r\nname=Two\r\naddress=127.0.0.2\r\nport=1\r\n"), 1) &&
              pc_servers_load(dir, l, PC_SERVER_MAX, &n, err, sizeof(err)) == PC_SERVERS_OK && n == 2 && l[0].port == 7777 && l[1].port == 1);

    /* corrupt files are never overwritten */
    {
        static const char* const bad[] = {
            "[server]\nname = A\naddress = example.com\n",          /* hostname */
            "[server]\nname = A\n",                                  /* no address */
            "name = A\naddress = 1.2.3.4\n",                        /* key outside a section */
            "[other]\nx = 1\n",                                      /* unknown section */
            "[server]\nname = A\naddress = 1.2.3.4\nbogus = 1\n",   /* unknown key */
            "[server]\nname = A\naddress = 1.2.3.4\nport = 99999\n", /* port */
            "[server]\nname = A\naddress = 1.2.3.4\n[server]\nname = a\naddress = 1.2.3.5\n", /* duplicate name */
            "garbage without structure\n",
        };
        int all = 1, keep = 1, noadd = 1, nodel = 1, noupd = 1, noset = 1;
        for (i = 0; i < (int)(sizeof(bad) / sizeof(bad[0])); i++) {
            put(path, bad[i]);
            if (pc_servers_load(dir, l, PC_SERVER_MAX, &n, err, sizeof(err)) != PC_SERVERS_CORRUPT || n != 0 || err[0] == '\0') all = 0;
            s = mk("New", "9.9.9.9", 7777);
            if (pc_servers_add(dir, &s, err, sizeof(err)) != PC_SERVERS_CORRUPT) noadd = 0;
            if (pc_servers_delete(dir, "A", err, sizeof(err)) != PC_SERVERS_CORRUPT) nodel = 0;
            if (pc_servers_update(dir, "A", &s, err, sizeof(err)) != PC_SERVERS_CORRUPT) noupd = 0;
            if (pc_servers_set_last(dir, "A", "x", "y") != PC_SERVERS_CORRUPT) noset = 0;
            if (slurp(path, b1, sizeof(b1)) != (int)strlen(bad[i]) || strcmp(b1, bad[i]) != 0) keep = 0;
            if (slurp(tmp, b2, sizeof(b2)) >= 0) keep = 0;
        }
        check("8 corrupt shapes all load as CORRUPT with a reason", all);
        check("add / delete / update / set_last all refuse CORRUPT", noadd && nodel && noupd && noset);
        check("the corrupt file is byte-identical afterwards and no .tmp exists", keep);
    }

    /* max entries */
    remove(path);
    for (i = 0; i < PC_SERVER_MAX; i++) {
        char nm[16];
        snprintf(nm, sizeof(nm), "S%d", i);
        s = mk(nm, "10.1.1.1", 7000 + i);
        if (pc_servers_add(dir, &s, err, sizeof(err)) != PC_SERVERS_OK) break;
    }
    check("32 servers can be added", i == PC_SERVER_MAX);
    s = mk("Extra", "10.1.1.1", 1234);
    check("the 33rd is refused FULL", pc_servers_add(dir, &s, err, sizeof(err)) == PC_SERVERS_FULL);
    r = pc_servers_load(dir, l, PC_SERVER_MAX, &n, err, sizeof(err));
    check("list still has exactly 32", r == PC_SERVERS_OK && n == PC_SERVER_MAX);
    check("no .tmp after the fill", slurp(tmp, b1, sizeof(b1)) < 0);

    /* relaunch command line */
    check("relaunch args: character uuid", pc_relaunch_build_args("192.168.1.20", 7777, PC_RELAUNCH_CHARACTER, "ab12", b1, sizeof(b1)) &&
                                               strcmp(b1, "--connect 192.168.1.20:7777 --character \"ab12\" --town-fetch --online-ui") == 0);
    check("relaunch args: legacy profile", pc_relaunch_build_args("10.0.0.5", 9000, PC_RELAUNCH_PROFILE, "roger", b1, sizeof(b1)) &&
                                               strcmp(b1, "--connect 10.0.0.5:9000 --guest-profile \"roger\" --town-fetch --online-ui") == 0);
    check("relaunch args: default guest", pc_relaunch_build_args("10.0.0.5", 9000, PC_RELAUNCH_DEFAULT_GUEST, NULL, b1, sizeof(b1)) && strcmp(b1, "--connect 10.0.0.5:9000 --guest --town-fetch --online-ui") == 0);
    check("relaunch args: injection / bad values refused",
          !pc_relaunch_build_args("10.0.0.5", 9000, PC_RELAUNCH_CHARACTER, "a\" --host", b1, sizeof(b1)) && !pc_relaunch_build_args("example.com", 9000, PC_RELAUNCH_DEFAULT_GUEST, NULL, b1, sizeof(b1)) &&
              !pc_relaunch_build_args("10.0.0.5", 0, PC_RELAUNCH_DEFAULT_GUEST, NULL, b1, sizeof(b1)) && !pc_relaunch_build_args("10.0.0.5", 9000, PC_RELAUNCH_CHARACTER, "", b1, sizeof(b1)));

    /* M-C: forwarded display options (whitelist only, a number for --framelimit) */
    {
        char* fa[] = { "ac", "--verbose", "--host", "--fullscreen", "--framelimit", "30", "--bootstrap-resident", "1", "--uber-shader", "--framelimit", "x;rm", "--no-framelimit" };
        pc_relaunch_forward_capture((int)(sizeof(fa) / sizeof(fa[0])), fa);
        check("forward: whitelisted options only", strcmp(pc_relaunch_forwarded(), " --verbose --framelimit 30 --uber-shader --no-framelimit") == 0);
        check("relaunch args: forwarded options appended + --town-fetch", pc_relaunch_build_args("10.0.0.5", 9000, PC_RELAUNCH_CHARACTER, "ab12", b1, sizeof(b1)) &&
                  strcmp(b1, "--connect 10.0.0.5:9000 --character \"ab12\" --town-fetch --online-ui --verbose --framelimit 30 --uber-shader --no-framelimit") == 0);
        check("relaunch args: injection still refused with forwarded options", !pc_relaunch_build_args("10.0.0.5", 9000, PC_RELAUNCH_CHARACTER, "a b", b1, sizeof(b1)));
        pc_relaunch_forward_capture(0, NULL);
        check("forward: cleared", pc_relaunch_forwarded()[0] == 0);
    }

    printf("RESULT passed=%d failed=%d\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
