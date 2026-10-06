/* pc_servers.h - saved SERVER PROFILES (M3): save/mp/servers.ini. A server profile is only a DESTINATION (address + port); characters are NOT tied to
 * servers (last_character / last_town are UI hints only). Pure C (libc + OS file APIs), no game dependency.
 *
 * File format: repeated sections
 *     [server]
 *     name = Friends
 *     address = 192.168.1.20
 *     port = 7777
 *     last_character = Roger
 *     last_town = Foo
 * '#' / ';' start a comment line. pc_settings.c ignores sections, this module has its own parser. ANYTHING it cannot parse (unknown section / key,
 * a key outside a section, bad name / address / port, duplicate name, missing name or address, too many entries, file > 64 KiB) makes the whole file CORRUPT:
 * it is reported, never auto-repaired, never overwritten, moved or deleted (every write is refused with PC_SERVERS_CORRUPT).
 *
 * ADDRESS = an IPv4 dotted-quad literal (a.b.c.d, each 0..255, no leading zeros) OR a DNS hostname (labels of [A-Za-z0-9-], max 63 chars in total), stored
 * verbatim. The hostname is only RESOLVED at connection time (pc_net_client_connect, getaddrinfo AF_INET); this module never touches the network. A name made only of
 * digits and dots that is not a valid IPv4 literal is refused. IPv6 literals are not supported (':' separates the port). Port 1..65535, default 7777. Writes are atomic (tmp + replace). */
#ifndef PC_SERVERS_H
#define PC_SERVERS_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_SERVER_MAX 32
#define PC_SERVER_NAME_MAX 32
#define PC_SERVER_DEFAULT_PORT 7777
#define PC_SERVER_ADDR_MAX 64 /* incl. NUL: hostnames up to 63 chars (same size as the town-cache origin and --connect buffers) */

typedef struct PCServer {
    char name[PC_SERVER_NAME_MAX + 1];
    char address[PC_SERVER_ADDR_MAX];            /* IPv4 dotted quad or DNS hostname, stored verbatim */
    int  port;                                   /* 1..65535 */
    char last_character[PC_SERVER_NAME_MAX + 1]; /* UI hint, "" = none */
    char last_town[PC_SERVER_NAME_MAX + 1];      /* UI hint, "" = none */
} PCServer;

enum { PC_SERVERS_OK = 0, PC_SERVERS_ERR = -1, PC_SERVERS_CORRUPT = -2, PC_SERVERS_EXISTS = -3, PC_SERVERS_NOTFOUND = -4, PC_SERVERS_FULL = -5, PC_SERVERS_INVALID = -6 };

/* Validation: 1 = ok, 0 = bad with a reason in err (may be NULL). */
int pc_servers_name_check(const char* name, char* err, size_t errcap);    /* 1..32 printable ASCII, none of [ ] = " \ , no leading / trailing space */
int pc_servers_address_check(const char* addr, char* err, size_t errcap); /* IPv4 dotted quad literal or DNS hostname */
int pc_servers_port_check(long port);                                     /* 1..65535 */

/* "HOST[:PORT]" -> address + port (default 7777). 1 = ok, 0 = bad (err). */
int pc_servers_parse_hostport(const char* text, char address[PC_SERVER_ADDR_MAX], int* port, char* err, size_t errcap);

/* dir == NULL -> "save/mp". A missing file is an empty list (OK). CORRUPT: err says why, *count = 0. */
int pc_servers_load(const char* dir, PCServer* out, int cap, int* count, char* err, size_t errcap);
int pc_servers_find(const PCServer* list, int count, const char* name); /* case-insensitive; index or -1 */
int pc_servers_add(const char* dir, const PCServer* s, char* err, size_t errcap);                       /* EXISTS when the name (any case) is taken */
int pc_servers_update(const char* dir, const char* name, const PCServer* s, char* err, size_t errcap);  /* replaces the entry `name` (rename allowed; keeps the hints) */
int pc_servers_delete(const char* dir, const char* name, char* err, size_t errcap);
int pc_servers_set_last(const char* dir, const char* name, const char* character, const char* town);   /* hints only; NULL keeps the old value */

#ifdef __cplusplus
}
#endif
#endif
