/* ac_launcher.c - ACLauncher.exe: the launcher / updater of a PACKAGED Animal Crossing PC Multiplayer installation (see docs/updater.md).
 *
 * Flow: read <dir>\version.txt -> ask GitHub Releases for the latest STABLE release of the configured repository -> if it is newer, ask the user (Yes / No) ->
 * Yes: download the release's package + its .sha256 asset, verify the SHA-256, validate and extract the zip into <dir>\.update\stage, swap the managed files in
 * (previous ones are kept in <dir>\.update\backup, restored on any failure), verify -> launch AnimalCrossing.exe. ANY problem (offline, HTTP error, bad metadata,
 * bad package, locked file ...) is reported and the installed game is launched unchanged.
 *
 * What it does NOT do: authenticate the publisher (a SHA-256 published next to the package detects corruption / a mismatching asset, it is not a signature),
 * touch rom/, save/, servers/, settings.ini, keybindings.ini, texture_pack/, logs or anything outside the explicit managed list, run Git, or update a development
 * checkout. Test-only switches (--updater-test-api / --updater-answer / --updater-no-ui) work only when the environment variable AC_UPDATER_TEST=1 is set. */
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <winhttp.h>
#include <bcrypt.h>
#include <commctrl.h>
#include <shellapi.h>
#include <tlhelp32.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <ctype.h>
#include <zlib.h>

#ifndef WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2
#define WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2 0x00000800
#endif
#ifndef WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3
#define WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3 0x00002000
#endif

#define MAX_API_BYTES (2u * 1024u * 1024u)
#define MAX_PKG_BYTES (512ull * 1024ull * 1024ull)
#define MAX_UNPACKED_BYTES (1024ull * 1024ull * 1024ull)
#define MAX_ZIP_ENTRIES 4096
#define LAUNCHER_VERSION "1"

/* ------------------------------------------------------------------ state / config */
typedef struct {
    wchar_t dir[MAX_PATH]; /* installation directory (the launcher's own) */
    char owner[80], repo[80];
    int check;         /* updater.ini check_updates */
    int prerelease;    /* updater.ini allow_prerelease */
    int show_errors;   /* updater.ini show_check_errors */
    int test;          /* AC_UPDATER_TEST=1 */
    char api_base[200]; /* https://api.github.com, or the test server */
    char dl_base[200];  /* https://github.com, or the test server */
    int answer;        /* test: -1 ask, 0 no, 1 yes */
    int noui;          /* test: no dialogs / progress window */
    int skip;          /* --updater-skip */
} Cfg;

static Cfg g;
static FILE* s_log;
static HWND s_prog_wnd, s_prog_bar, s_prog_text;

static void logf_(const char* fmt, ...) {
    char buf[1024];
    va_list ap;
    SYSTEMTIME st;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    GetLocalTime(&st);
    if (s_log != NULL) {
        fprintf(s_log, "%04d-%02d-%02d %02d:%02d:%02d %s\n", st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, buf);
        fflush(s_log);
    }
}

static void to_wide(const char* s, wchar_t* out, int cap) {
    if (MultiByteToWideChar(CP_UTF8, 0, s, -1, out, cap) == 0) {
        out[0] = 0;
    }
}

static void say_error(const char* fmt, ...) { /* reported to the log always, to the user unless the UI is off */
    char buf[900];
    wchar_t w[900];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    logf_("ERROR: %s", buf);
    if (!g.noui) {
        to_wide(buf, w, 900);
        MessageBoxW(NULL, w, L"Animal Crossing Multiplayer - updater", MB_OK | MB_ICONWARNING | MB_SETFOREGROUND);
    }
}

/* ------------------------------------------------------------------ progress window (plain Win32; no console) */
static LRESULT CALLBACK prog_proc(HWND h, UINT m, WPARAM w, LPARAM l) {
    return DefWindowProcW(h, m, w, l);
}

static void ui_open(const wchar_t* title) {
    WNDCLASSW wc;
    INITCOMMONCONTROLSEX ic;
    if (g.noui) {
        return;
    }
    ic.dwSize = sizeof(ic);
    ic.dwICC = ICC_PROGRESS_CLASS;
    InitCommonControlsEx(&ic);
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = prog_proc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.hCursor = LoadCursorW(NULL, MAKEINTRESOURCEW(32512));
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    wc.lpszClassName = L"ACLauncherProgress";
    RegisterClassW(&wc);
    s_prog_wnd = CreateWindowW(L"ACLauncherProgress", title, WS_CAPTION | WS_VISIBLE, (GetSystemMetrics(SM_CXSCREEN) - 460) / 2, (GetSystemMetrics(SM_CYSCREEN) - 130) / 2, 460, 130, NULL, NULL, wc.hInstance, NULL);
    s_prog_text = CreateWindowW(L"STATIC", L"", WS_CHILD | WS_VISIBLE, 16, 14, 420, 20, s_prog_wnd, NULL, wc.hInstance, NULL);
    s_prog_bar = CreateWindowW(PROGRESS_CLASSW, NULL, WS_CHILD | WS_VISIBLE, 16, 44, 420, 22, s_prog_wnd, NULL, wc.hInstance, NULL);
    SendMessageW(s_prog_bar, PBM_SETRANGE32, 0, 1000);
}

static void ui_set(const char* text, int permille) {
    MSG msg;
    wchar_t w[300];
    if (s_prog_wnd == NULL) {
        return;
    }
    to_wide(text, w, 300);
    SetWindowTextW(s_prog_text, w);
    SendMessageW(s_prog_bar, PBM_SETPOS, (WPARAM)(permille < 0 ? 0 : permille), 0);
    while (PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE)) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
}

static void ui_close(void) {
    if (s_prog_wnd != NULL) {
        DestroyWindow(s_prog_wnd);
        s_prog_wnd = NULL;
    }
}

/* ------------------------------------------------------------------ semantic versions */
typedef struct {
    unsigned long maj, min, pat;
    char pre[48];
} Ver;

/* "v1.2.3" / "1.2.3-rc.1": strict; returns 1 if valid */
static int ver_parse(const char* s, Ver* v, char* canon, size_t canon_cap) {
    const char* p = s;
    char* e;
    unsigned long n[3];
    int i;
    memset(v, 0, sizeof(*v));
    if (*p == 'v') {
        p++;
    }
    for (i = 0; i < 3; i++) {
        if (!isdigit((unsigned char)*p)) {
            return 0;
        }
        n[i] = strtoul(p, &e, 10);
        if (e - p > 6) {
            return 0;
        }
        p = e;
        if (i < 2) {
            if (*p != '.') {
                return 0;
            }
            p++;
        }
    }
    if (*p == '-') {
        size_t k = 0;
        p++;
        if (*p == 0) {
            return 0;
        }
        for (; *p != 0; p++, k++) {
            if (!(isalnum((unsigned char)*p) || *p == '.') || k >= sizeof(v->pre) - 1) {
                return 0;
            }
            v->pre[k] = *p;
        }
    } else if (*p != 0) {
        return 0;
    }
    v->maj = n[0];
    v->min = n[1];
    v->pat = n[2];
    if (canon != NULL) {
        snprintf(canon, canon_cap, "%lu.%lu.%lu%s%s", v->maj, v->min, v->pat, v->pre[0] ? "-" : "", v->pre);
    }
    return 1;
}

static int pre_cmp(const char* a, const char* b) { /* semver 2.0 pre-release precedence; a release (empty) is higher than any pre-release */
    if (*a == 0 && *b == 0) return 0;
    if (*a == 0) return 1;
    if (*b == 0) return -1;
    while (*a != 0 || *b != 0) {
        char ia[48] = { 0 }, ib[48] = { 0 };
        size_t la = strcspn(a, "."), lb = strcspn(b, ".");
        int na, nb, c;
        if (*a == 0) return -1;
        if (*b == 0) return 1;
        memcpy(ia, a, la);
        memcpy(ib, b, lb);
        na = strspn(ia, "0123456789") == la;
        nb = strspn(ib, "0123456789") == lb;
        if (na && nb) {
            unsigned long x = strtoul(ia, NULL, 10), y = strtoul(ib, NULL, 10);
            c = x < y ? -1 : x > y ? 1 : 0;
        } else if (na != nb) {
            c = na ? -1 : 1;
        } else {
            c = strcmp(ia, ib);
            c = c < 0 ? -1 : c > 0 ? 1 : 0;
        }
        if (c != 0) return c;
        a += la + (a[la] == '.');
        b += lb + (b[lb] == '.');
    }
    return 0;
}

static int ver_cmp(const Ver* a, const Ver* b) {
    if (a->maj != b->maj) return a->maj < b->maj ? -1 : 1;
    if (a->min != b->min) return a->min < b->min ? -1 : 1;
    if (a->pat != b->pat) return a->pat < b->pat ? -1 : 1;
    return pre_cmp(a->pre, b->pre);
}

/* ------------------------------------------------------------------ strict mini JSON */
enum { J_OBJ = 1, J_ARR, J_STR, J_NUM, J_BOOL, J_NUL };
typedef struct J {
    int t;
    char* s;
    double n;
    int b;
    struct J** kid;
    char** key;
    int nk;
} J;

typedef struct {
    const char* p;
    const char* end;
    int depth;
    int bad;
} JP;

static void jfree(J* j) {
    int i;
    if (j == NULL) return;
    for (i = 0; i < j->nk; i++) {
        jfree(j->kid[i]);
        if (j->key != NULL) free(j->key[i]);
    }
    free(j->kid);
    free(j->key);
    free(j->s);
    free(j);
}

static void jws(JP* p) {
    while (p->p < p->end && (*p->p == ' ' || *p->p == '\t' || *p->p == '\r' || *p->p == '\n')) p->p++;
}

static char* jstr(JP* p) { /* after the opening quote check */
    size_t cap = 64, n = 0;
    char* o = (char*)malloc(cap);
    p->p++;
    while (p->p < p->end && *p->p != '"') {
        unsigned char c = (unsigned char)*p->p++;
        if (c < 0x20) { p->bad = 1; break; }
        if (c == '\\') {
            if (p->p >= p->end) { p->bad = 1; break; }
            c = (unsigned char)*p->p++;
            switch (c) {
                case '"': case '\\': case '/': break;
                case 'b': c = '\b'; break;
                case 'f': c = '\f'; break;
                case 'n': c = '\n'; break;
                case 'r': c = '\r'; break;
                case 't': c = '\t'; break;
                case 'u': {
                    unsigned v = 0;
                    int k;
                    for (k = 0; k < 4; k++) {
                        if (p->p >= p->end || !isxdigit((unsigned char)*p->p)) { p->bad = 1; break; }
                        v = v * 16 + (unsigned)(isdigit((unsigned char)*p->p) ? *p->p - '0' : (tolower((unsigned char)*p->p) - 'a' + 10));
                        p->p++;
                    }
                    c = (v < 0x80 && v != 0) ? (unsigned char)v : '?';
                    break;
                }
                default: p->bad = 1; break;
            }
            if (p->bad) break;
        }
        if (n + 2 > cap) { cap *= 2; o = (char*)realloc(o, cap); }
        o[n++] = (char)c;
    }
    if (p->bad || p->p >= p->end) { p->bad = 1; free(o); return NULL; }
    p->p++;
    o[n] = 0;
    return o;
}

static J* jval(JP* p);

static J* jnew(int t) {
    J* j = (J*)calloc(1, sizeof(J));
    j->t = t;
    return j;
}

static void jadd(J* o, char* key, J* v) {
    o->kid = (J**)realloc(o->kid, sizeof(J*) * (size_t)(o->nk + 1));
    if (o->t == J_OBJ) {
        o->key = (char**)realloc(o->key, sizeof(char*) * (size_t)(o->nk + 1));
        o->key[o->nk] = key;
    }
    o->kid[o->nk++] = v;
}

static J* jval(JP* p) {
    J* j = NULL;
    jws(p);
    if (p->p >= p->end || p->depth > 12) { p->bad = 1; return NULL; }
    if (*p->p == '{' || *p->p == '[') {
        const char close = *p->p == '{' ? '}' : ']';
        j = jnew(*p->p == '{' ? J_OBJ : J_ARR);
        p->p++;
        p->depth++;
        jws(p);
        if (p->p < p->end && *p->p == close) { p->p++; p->depth--; return j; }
        for (;;) {
            char* key = NULL;
            J* v;
            jws(p);
            if (j->t == J_OBJ) {
                if (p->p >= p->end || *p->p != '"') { p->bad = 1; break; }
                key = jstr(p);
                if (key == NULL) break;
                jws(p);
                if (p->p >= p->end || *p->p != ':') { free(key); p->bad = 1; break; }
                p->p++;
            }
            v = jval(p);
            if (v == NULL) { free(key); break; }
            jadd(j, key, v);
            jws(p);
            if (p->p < p->end && *p->p == ',') { p->p++; continue; }
            if (p->p < p->end && *p->p == close) { p->p++; break; }
            p->bad = 1;
            break;
        }
        p->depth--;
        if (p->bad) { jfree(j); return NULL; }
        return j;
    }
    if (*p->p == '"') {
        j = jnew(J_STR);
        j->s = jstr(p);
        if (j->s == NULL) { jfree(j); return NULL; }
        return j;
    }
    if (p->end - p->p >= 4 && strncmp(p->p, "true", 4) == 0) { p->p += 4; j = jnew(J_BOOL); j->b = 1; return j; }
    if (p->end - p->p >= 5 && strncmp(p->p, "false", 5) == 0) { p->p += 5; return jnew(J_BOOL); }
    if (p->end - p->p >= 4 && strncmp(p->p, "null", 4) == 0) { p->p += 4; return jnew(J_NUL); }
    if (*p->p == '-' || isdigit((unsigned char)*p->p)) {
        char* e;
        j = jnew(J_NUM);
        j->n = strtod(p->p, &e);
        if (e == p->p || e > p->end) { p->bad = 1; jfree(j); return NULL; }
        p->p = e;
        return j;
    }
    p->bad = 1;
    return NULL;
}

static J* jparse(const char* buf, size_t n) {
    JP p;
    J* j;
    p.p = buf; p.end = buf + n; p.depth = 0; p.bad = 0;
    j = jval(&p);
    if (j == NULL) return NULL;
    jws(&p);
    if (p.p != p.end) { jfree(j); return NULL; }
    return j;
}

static J* jget(const J* o, const char* key) {
    int i;
    if (o == NULL || o->t != J_OBJ) return NULL;
    for (i = 0; i < o->nk; i++) {
        if (strcmp(o->key[i], key) == 0) return o->kid[i];
    }
    return NULL;
}

/* ------------------------------------------------------------------ SHA-256 (Windows CNG) */
static int sha256_file(const wchar_t* path, char hex[65]) {
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_HASH_HANDLE h = NULL;
    uint8_t dig[32], buf[65536];
    FILE* f = _wfopen(path, L"rb");
    int ok = 0, i;
    size_t n;
    if (f == NULL) return 0;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) == 0 && BCryptCreateHash(alg, &h, NULL, 0, NULL, 0, 0) == 0) {
        ok = 1;
        while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
            if (BCryptHashData(h, buf, (ULONG)n, 0) != 0) { ok = 0; break; }
        }
        if (ferror(f) || BCryptFinishHash(h, dig, 32, 0) != 0) ok = 0;
    }
    if (h != NULL) BCryptDestroyHash(h);
    if (alg != NULL) BCryptCloseAlgorithmProvider(alg, 0);
    fclose(f);
    if (ok) {
        for (i = 0; i < 32; i++) sprintf(hex + i * 2, "%02x", dig[i]);
    }
    return ok;
}

/* ------------------------------------------------------------------ HTTP (WinHTTP) */
typedef struct {
    FILE* fp;         /* file mode */
    uint8_t* mem;     /* memory mode */
    size_t memlen;
    uint64_t got;
    uint64_t expect;  /* 0 = unknown */
    uint64_t maxbytes;
    const char* what; /* progress label */
} Sink;

static int url_split(const char* url, int* secure, char* host, size_t hcap, INTERNET_PORT* port, wchar_t* path, size_t pcap) {
    wchar_t w[1024];
    URL_COMPONENTS uc;
    wchar_t hw[256];
    if (strlen(url) > 1000) return 0;
    to_wide(url, w, 1024);
    memset(&uc, 0, sizeof(uc));
    uc.dwStructSize = sizeof(uc);
    uc.lpszHostName = hw; uc.dwHostNameLength = 256;
    uc.lpszUrlPath = path; uc.dwUrlPathLength = (DWORD)pcap;
    uc.dwExtraInfoLength = (DWORD)-1;
    if (!WinHttpCrackUrl(w, 0, 0, &uc)) return 0;
    if (uc.nScheme != INTERNET_SCHEME_HTTPS && uc.nScheme != INTERNET_SCHEME_HTTP) return 0;
    *secure = uc.nScheme == INTERNET_SCHEME_HTTPS;
    *port = uc.nPort;
    if (uc.dwExtraInfoLength > 0 && uc.lpszExtraInfo != NULL) {
        size_t l = wcslen(path);
        if (l + uc.dwExtraInfoLength + 1 >= pcap) return 0;
        wcsncat(path, uc.lpszExtraInfo, uc.dwExtraInfoLength);
    }
    if (WideCharToMultiByte(CP_UTF8, 0, hw, -1, host, (int)hcap, NULL, NULL) == 0) return 0;
    return 1;
}

static int host_is_loopback(const char* h) {
    return strcmp(h, "127.0.0.1") == 0 || strcmp(h, "localhost") == 0;
}

static int host_allowed_final(const char* h) { /* where GitHub may redirect asset downloads */
    size_t n = strlen(h);
    if (strcmp(h, "github.com") == 0 || strcmp(h, "api.github.com") == 0) return 1;
    return n > 22 && strcmp(h + n - 22, ".githubusercontent.com") == 0;
}

/* returns 0 ok; otherwise err filled (user-readable). *status = HTTP status when known. */
static int http_get(const char* url, Sink* sk, int* status, char* err, size_t ecap) {
    int secure = 0, rc = -1;
    char host[256];
    INTERNET_PORT port = 0;
    wchar_t path[1100];
    HINTERNET ses = NULL, con = NULL, req = NULL;
    wchar_t hostw[256];
    DWORD st = 0, sz = sizeof(st), proto = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2 | WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3, avail, rd;
    uint8_t buf[65536];
    char lbl[200];
    path[0] = 0;
    if (status != NULL) *status = 0;
    if (!url_split(url, &secure, host, sizeof(host), &port, path, 1100)) {
        snprintf(err, ecap, "invalid URL");
        return -1;
    }
    if (!secure && !(g.test && host_is_loopback(host))) {
        snprintf(err, ecap, "refusing a non-HTTPS URL");
        return -1;
    }
    ses = WinHttpOpen(L"ACLauncher/" L"1", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (ses == NULL) { snprintf(err, ecap, "WinHTTP is unavailable (error %lu)", GetLastError()); return -1; }
    WinHttpSetTimeouts(ses, 8000, 8000, 15000, 30000);
    WinHttpSetOption(ses, WINHTTP_OPTION_SECURE_PROTOCOLS, &proto, sizeof(proto));
    to_wide(host, hostw, 256);
    con = WinHttpConnect(ses, hostw, port, 0);
    if (con == NULL) { snprintf(err, ecap, "cannot connect to %s (error %lu)", host, GetLastError()); goto done; }
    req = WinHttpOpenRequest(con, L"GET", path, NULL, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, secure ? WINHTTP_FLAG_SECURE : 0);
    if (req == NULL) { snprintf(err, ecap, "cannot open request (error %lu)", GetLastError()); goto done; }
    WinHttpAddRequestHeaders(req, L"Accept: application/vnd.github+json, application/octet-stream, text/plain\r\nX-GitHub-Api-Version: 2022-11-28", (DWORD)-1, WINHTTP_ADDREQ_FLAG_ADD);
    if (!WinHttpSendRequest(req, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0) || !WinHttpReceiveResponse(req, NULL)) {
        DWORD e = GetLastError();
        snprintf(err, ecap, e == ERROR_WINHTTP_TIMEOUT ? "the server did not answer in time" : "network error %lu (are you online?)", e);
        goto done;
    }
    WinHttpQueryHeaders(req, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX, &st, &sz, WINHTTP_NO_HEADER_INDEX);
    if (status != NULL) *status = (int)st;
    if (!g.test || !host_is_loopback(host)) { /* the FINAL url (after redirects) must still be a GitHub host */
        wchar_t fin[1100];
        DWORD fl = sizeof(fin);
        if (WinHttpQueryOption(req, WINHTTP_OPTION_URL, fin, &fl)) {
            char fa[1100], fh[256];
            int sec2; INTERNET_PORT p2; wchar_t pw[1100];
            WideCharToMultiByte(CP_UTF8, 0, fin, -1, fa, sizeof(fa), NULL, NULL);
            pw[0] = 0;
            if (!url_split(fa, &sec2, fh, sizeof(fh), &p2, pw, 1100) || !sec2 || !host_allowed_final(fh)) {
                snprintf(err, ecap, "the download was redirected to an unexpected host");
                goto done;
            }
        }
    }
    if (st == 403 || st == 429) {
        wchar_t rem[32]; DWORD rl = sizeof(rem);
        if (WinHttpQueryHeaders(req, WINHTTP_QUERY_CUSTOM, L"x-ratelimit-remaining", rem, &rl, WINHTTP_NO_HEADER_INDEX) && wcscmp(rem, L"0") == 0) {
            snprintf(err, ecap, "GitHub API rate limit reached (HTTP %lu); try again later", st);
        } else {
            snprintf(err, ecap, "GitHub refused the request (HTTP %lu)", st);
        }
        goto done;
    }
    if (st == 404) { snprintf(err, ecap, "not found (HTTP 404)"); goto done; }
    if (st != 200) { snprintf(err, ecap, "unexpected HTTP status %lu", st); goto done; }
    {
        wchar_t cl[32]; DWORD cll = sizeof(cl);
        if (WinHttpQueryHeaders(req, WINHTTP_QUERY_CONTENT_LENGTH, WINHTTP_HEADER_NAME_BY_INDEX, cl, &cll, WINHTTP_NO_HEADER_INDEX)) {
            unsigned long long len = _wcstoui64(cl, NULL, 10);
            if (len > sk->maxbytes) { snprintf(err, ecap, "the response is larger than the allowed %llu bytes", (unsigned long long)sk->maxbytes); goto done; }
        }
    }
    for (;;) {
        avail = 0;
        if (!WinHttpQueryDataAvailable(req, &avail)) { snprintf(err, ecap, "the connection was interrupted (error %lu)", GetLastError()); goto done; }
        if (avail == 0) break;
        if (avail > sizeof(buf)) avail = sizeof(buf);
        if (!WinHttpReadData(req, buf, avail, &rd)) { snprintf(err, ecap, "the connection was interrupted (error %lu)", GetLastError()); goto done; }
        if (rd == 0) break;
        if (sk->got + rd > sk->maxbytes) { snprintf(err, ecap, "the download exceeds the allowed size"); goto done; }
        if (sk->fp != NULL) {
            if (fwrite(buf, 1, rd, sk->fp) != rd) { snprintf(err, ecap, "cannot write the download (disk full or no permission)"); goto done; }
        } else {
            sk->mem = (uint8_t*)realloc(sk->mem, sk->memlen + rd + 1);
            memcpy(sk->mem + sk->memlen, buf, rd);
            sk->memlen += rd;
            sk->mem[sk->memlen] = 0;
        }
        sk->got += rd;
        if (sk->what != NULL) {
            snprintf(lbl, sizeof(lbl), "%s: %.1f MB%s", sk->what, (double)sk->got / 1048576.0, sk->expect ? "" : "");
            ui_set(lbl, sk->expect ? (int)(sk->got * 1000 / sk->expect) : 0);
        }
    }
    rc = 0;
done:
    if (req) WinHttpCloseHandle(req);
    if (con) WinHttpCloseHandle(con);
    if (ses) WinHttpCloseHandle(ses);
    return rc;
}

/* ------------------------------------------------------------------ files */
static int is_reparse(const wchar_t* p) {
    DWORD a = GetFileAttributesW(p);
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_REPARSE_POINT) != 0;
}

/* removes a directory tree that this program created (never follows reparse points: such an entry is removed as a link only) */
static void rmtree(const wchar_t* dir) {
    WIN32_FIND_DATAW fd;
    wchar_t pat[MAX_PATH + 4], p[MAX_PATH];
    HANDLE h;
    if (is_reparse(dir)) { RemoveDirectoryW(dir); DeleteFileW(dir); return; }
    swprintf(pat, MAX_PATH + 4, L"%ls\\*", dir);
    h = FindFirstFileW(pat, &fd);
    if (h != INVALID_HANDLE_VALUE) {
        do {
            if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0) continue;
            swprintf(p, MAX_PATH, L"%ls\\%ls", dir, fd.cFileName);
            if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) && !(fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
                rmtree(p);
            } else {
                SetFileAttributesW(p, FILE_ATTRIBUTE_NORMAL);
                if (!DeleteFileW(p)) RemoveDirectoryW(p);
            }
        } while (FindNextFileW(h, &fd));
        FindClose(h);
    }
    RemoveDirectoryW(dir);
}

/* creates every directory of rel (below base), refusing reparse points; rel uses '\\' */
static int mkdirs_below(const wchar_t* base, const wchar_t* rel_dir) {
    wchar_t cur[MAX_PATH];
    const wchar_t* p = rel_dir;
    wcscpy(cur, base);
    while (*p != 0) {
        size_t n = wcscspn(p, L"\\");
        size_t l = wcslen(cur);
        if (l + n + 2 >= MAX_PATH) return 0;
        cur[l] = L'\\';
        wcsncpy(cur + l + 1, p, n);
        cur[l + 1 + n] = 0;
        if (GetFileAttributesW(cur) == INVALID_FILE_ATTRIBUTES) {
            if (!CreateDirectoryW(cur, NULL)) return 0;
        } else if (is_reparse(cur)) {
            return 0;
        }
        p += n;
        if (*p == L'\\') p++;
    }
    return 1;
}

static int read_small_file(const wchar_t* path, char* out, size_t cap) {
    FILE* f = _wfopen(path, L"rb");
    size_t n;
    if (f == NULL) return 0;
    n = fread(out, 1, cap - 1, f);
    fclose(f);
    out[n] = 0;
    return 1;
}

/* ------------------------------------------------------------------ release discovery */
typedef struct {
    char tag[64];
    char ver[64];    /* canonical x.y.z[-pre] */
    Ver v;
    int prerelease;
    char zip_name[160], zip_url[512], sha_name[180], sha_url[512];
    uint64_t zip_size;
    char api_digest[80]; /* "sha256:<hex>" if GitHub published it, else "" */
} Release;

static int release_from_json(const J* r, Release* out, char* err, size_t ecap) {
    const J* t = jget(r, "tag_name");
    const J* dr = jget(r, "draft");
    const J* pr = jget(r, "prerelease");
    const J* as = jget(r, "assets");
    int i, have_zip = 0, have_sha = 0;
    char want_zip[200], want_sha[220], want_url_zip[420], want_url_sha[440];
    memset(out, 0, sizeof(*out));
    if (r == NULL || r->t != J_OBJ || t == NULL || t->t != J_STR || pr == NULL || pr->t != J_BOOL || as == NULL || as->t != J_ARR) {
        snprintf(err, ecap, "the release metadata is malformed");
        return 0;
    }
    if (dr != NULL && dr->t == J_BOOL && dr->b) { snprintf(err, ecap, "the release is a draft"); return 0; }
    if (strlen(t->s) >= sizeof(out->tag) || !ver_parse(t->s, &out->v, out->ver, sizeof(out->ver))) {
        snprintf(err, ecap, "the release tag '%.40s' is not a valid version (expected vMAJOR.MINOR.PATCH[-pre])", t->s);
        return 0;
    }
    strcpy(out->tag, t->s);
    out->prerelease = pr->b;
    if ((out->v.pre[0] != 0) != (out->prerelease != 0)) {
        snprintf(err, ecap, "the release tag and its prerelease flag disagree");
        return 0;
    }
    snprintf(want_zip, sizeof(want_zip), "ACGC-PC-Multiplayer-%s-win64.zip", out->ver);
    snprintf(want_sha, sizeof(want_sha), "%s.sha256", want_zip);
    snprintf(want_url_zip, sizeof(want_url_zip), "%s/%s/%s/releases/download/%s/%s", g.dl_base, g.owner, g.repo, out->tag, want_zip);
    snprintf(want_url_sha, sizeof(want_url_sha), "%s/%s/%s/releases/download/%s/%s", g.dl_base, g.owner, g.repo, out->tag, want_sha);
    for (i = 0; i < as->nk; i++) {
        const J* a = as->kid[i];
        const J* nm = jget(a, "name");
        const J* u = jget(a, "browser_download_url");
        const J* sz = jget(a, "size");
        const J* stt = jget(a, "state");
        if (nm == NULL || nm->t != J_STR || u == NULL || u->t != J_STR) continue;
        if (stt != NULL && stt->t == J_STR && strcmp(stt->s, "uploaded") != 0) continue;
        if (strcmp(nm->s, want_zip) == 0) {
            const J* dg = jget(a, "digest");
            if (strcmp(u->s, want_url_zip) != 0) { snprintf(err, ecap, "the package asset URL does not match the configured repository / release"); return 0; }
            if (sz == NULL || sz->t != J_NUM || sz->n < 22 || sz->n > (double)MAX_PKG_BYTES) { snprintf(err, ecap, "the package asset has an invalid size"); return 0; }
            out->zip_size = (uint64_t)sz->n;
            if (dg != NULL && dg->t == J_STR && strncmp(dg->s, "sha256:", 7) == 0 && strlen(dg->s) == 71) strcpy(out->api_digest, dg->s);
            strcpy(out->zip_name, want_zip);
            strcpy(out->zip_url, u->s);
            have_zip = 1;
        } else if (strcmp(nm->s, want_sha) == 0) {
            if (strcmp(u->s, want_url_sha) != 0) { snprintf(err, ecap, "the checksum asset URL does not match the configured repository / release"); return 0; }
            strcpy(out->sha_name, want_sha);
            strcpy(out->sha_url, u->s);
            have_sha = 1;
        }
    }
    if (!have_zip || !have_sha) {
        snprintf(err, ecap, "release %.40s lacks its package and/or .sha256 asset (expected %s and %s); a maintainer must publish both", out->tag, "ACGC-PC-Multiplayer-<version>-win64.zip", ".zip.sha256");
        return 0;
    }
    return 1;
}

/* 1 = found a release in *out; 0 = failure (err filled); -1 = no (matching) release exists */
static int find_latest(Release* out, char* err, size_t ecap) {
    char url[400];
    Sink sk;
    int st = 0;
    J* j;
    memset(&sk, 0, sizeof(sk));
    sk.maxbytes = MAX_API_BYTES;
    if (!g.prerelease) {
        snprintf(url, sizeof(url), "%s/repos/%s/%s/releases/latest", g.api_base, g.owner, g.repo);
        if (http_get(url, &sk, &st, err, ecap) != 0) {
            free(sk.mem);
            if (st == 404) { snprintf(err, ecap, "no official release has been published yet"); return -1; }
            return 0;
        }
        j = jparse((const char*)sk.mem, sk.memlen);
        free(sk.mem);
        if (j == NULL) { snprintf(err, ecap, "the release metadata is not valid JSON"); return 0; }
        {
            int ok = release_from_json(j, out, err, ecap);
            jfree(j);
            if (ok && out->prerelease) { snprintf(err, ecap, "the 'latest' release is a prerelease; refusing it"); return 0; }
            return ok;
        }
    } else {
        int i, best = 0;
        Release cand;
        char e2[200];
        snprintf(url, sizeof(url), "%s/repos/%s/%s/releases?per_page=15", g.api_base, g.owner, g.repo);
        if (http_get(url, &sk, &st, err, ecap) != 0) { free(sk.mem); return st == 404 ? (snprintf(err, ecap, "no official release has been published yet"), -1) : 0; }
        j = jparse((const char*)sk.mem, sk.memlen);
        free(sk.mem);
        if (j == NULL || j->t != J_ARR) { jfree(j); snprintf(err, ecap, "the release list is not valid JSON"); return 0; }
        for (i = 0; i < j->nk; i++) {
            if (release_from_json(j->kid[i], &cand, e2, sizeof(e2)) && (!best || ver_cmp(&cand.v, &out->v) > 0)) {
                *out = cand;
                best = 1;
            }
        }
        jfree(j);
        if (!best) { snprintf(err, ecap, "no usable release found"); return -1; }
        return 1;
    }
}

/* ------------------------------------------------------------------ package validation + extraction */
typedef struct {
    char name[208];
    uint32_t crc, csize, usize, lho;
    uint16_t method;
    int isdir;
} ZEnt;

static uint32_t rd32(const uint8_t* p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }
static uint16_t rd16(const uint8_t* p) { return (uint16_t)(p[0] | p[1] << 8); }

static int reserved_name(const char* seg, size_t n) {
    static const char* const R[] = { "con", "prn", "aux", "nul" };
    char b[16];
    size_t k = 0, i;
    for (; k < n && seg[k] != '.' && k < 15; k++) b[k] = (char)tolower((unsigned char)seg[k]);
    b[k] = 0;
    for (i = 0; i < 4; i++) if (strcmp(b, R[i]) == 0) return 1;
    if ((strncmp(b, "com", 3) == 0 || strncmp(b, "lpt", 3) == 0) && b[3] >= '1' && b[3] <= '9' && b[4] == 0) return 1;
    return 0;
}

/* a safe, normalised relative path ('/' separators, no leading/trailing slash) or 0 */
static int safe_rel(char* name, int* isdir) {
    size_t n = strlen(name), i, segstart = 0;
    *isdir = 0;
    if (n > 0 && name[n - 1] == '/') { *isdir = 1; name[--n] = 0; }
    if (n == 0 || n > 200) return 0;
    for (i = 0; i <= n; i++) {
        unsigned char c = (unsigned char)name[i];
        if (i == n || c == '/') {
            size_t sl = i - segstart;
            if (sl == 0) return 0;
            if ((sl == 1 && name[segstart] == '.') || (sl == 2 && name[segstart] == '.' && name[segstart + 1] == '.')) return 0;
            if (name[i - 1] == '.' || name[i - 1] == ' ') return 0;
            if (reserved_name(name + segstart, sl)) return 0;
            segstart = i + 1;
        } else if (!(isalnum(c) || c == '_' || c == '.' || c == '-' || c == '+' || c == '(' || c == ')' || c == ' ')) {
            return 0; /* rejects '\\', ':', control characters, '*' '?' '"' '<' '>' '|', non-ASCII, ... */
        }
    }
    return 1;
}

static int managed_allowed(const char* n) { /* the explicit list of replaceable program files */
    static const char* const exact[] = { "AnimalCrossing.exe", "SDL2.dll", "version.txt", "LICENSE", "RELEASE_NOTES.txt", "UPDATING.txt" };
    size_t i;
    for (i = 0; i < sizeof(exact) / sizeof(exact[0]); i++) if (_stricmp(n, exact[i]) == 0) return 1;
    return _strnicmp(n, "shaders/", 8) == 0 || _strnicmp(n, "licenses/", 9) == 0;
}

static int zip_list(FILE* f, ZEnt* ents, int* count, char* err, size_t ecap) {
    uint8_t tail[66000], cdh[46];
    size_t tn, i;
    long long fsize, cd_off, cd_size;
    int n, k, j;
    uint64_t total = 0;
    _fseeki64(f, 0, SEEK_END);
    fsize = _ftelli64(f);
    if (fsize < 22) { snprintf(err, ecap, "the package is not a zip file"); return 0; }
    tn = (size_t)(fsize < (long long)sizeof(tail) ? fsize : (long long)sizeof(tail));
    _fseeki64(f, fsize - (long long)tn, SEEK_SET);
    if (fread(tail, 1, tn, f) != tn) { snprintf(err, ecap, "cannot read the package"); return 0; }
    for (i = tn - 22 + 1; i-- > 0;) {
        if (rd32(tail + i) == 0x06054b50u) break;
        if (i == 0) { snprintf(err, ecap, "the package is not a valid zip (no end record)"); return 0; }
    }
    n = rd16(tail + i + 10);
    cd_size = rd32(tail + i + 12);
    cd_off = rd32(tail + i + 16);
    if (rd16(tail + i + 4) != 0 || rd16(tail + i + 6) != 0 || rd16(tail + i + 8) != n || n == 0xFFFF || cd_size == 0xFFFFFFFFll || cd_off == 0xFFFFFFFFll) {
        snprintf(err, ecap, "multi-disk / zip64 packages are not supported");
        return 0;
    }
    if (n <= 0 || n > MAX_ZIP_ENTRIES || cd_off + cd_size > fsize) { snprintf(err, ecap, "the package directory is invalid"); return 0; }
    _fseeki64(f, cd_off, SEEK_SET);
    *count = 0;
    for (k = 0; k < n; k++) {
        uint16_t nl, el, cl, flags;
        uint32_t ext;
        ZEnt* z = &ents[*count];
        char nm[300];
        if (fread(cdh, 1, 46, f) != 46 || rd32(cdh) != 0x02014b50u) { snprintf(err, ecap, "the package directory is corrupt"); return 0; }
        flags = rd16(cdh + 8);
        z->method = rd16(cdh + 10);
        z->crc = rd32(cdh + 16);
        z->csize = rd32(cdh + 20);
        z->usize = rd32(cdh + 24);
        nl = rd16(cdh + 28); el = rd16(cdh + 30); cl = rd16(cdh + 32);
        ext = rd32(cdh + 38);
        z->lho = rd32(cdh + 42);
        if (nl == 0 || nl >= sizeof(nm)) { snprintf(err, ecap, "the package contains an invalid file name"); return 0; }
        if (fread(nm, 1, nl, f) != nl) { snprintf(err, ecap, "the package directory is corrupt"); return 0; }
        nm[nl] = 0;
        _fseeki64(f, (long long)el + cl, SEEK_CUR);
        if (flags & 1) { snprintf(err, ecap, "encrypted packages are not supported"); return 0; }
        if ((cdh[5] == 3) && (((ext >> 16) & 0xF000u) == 0xA000u)) { snprintf(err, ecap, "the package contains a symbolic link ('%.60s'); refused", nm); return 0; }
        if (ext & 0x400u) { snprintf(err, ecap, "the package contains a reparse point ('%.60s'); refused", nm); return 0; }
        if (strchr(nm, '\\') != NULL || nm[0] == '/' ) { snprintf(err, ecap, "the package contains an unsafe path '%.60s'; refused", nm); return 0; }
        strcpy(z->name, nm);
        if (!safe_rel(z->name, &z->isdir)) { snprintf(err, ecap, "the package contains an unsafe path '%.60s'; refused", nm); return 0; }
        if (!z->isdir && !managed_allowed(z->name)) { snprintf(err, ecap, "the package contains '%.60s', which is not a managed program file; refused", nm); return 0; }
        if (z->isdir) {
            if (!managed_allowed(z->name) && _stricmp(z->name, "shaders") != 0 && _stricmp(z->name, "licenses") != 0) { snprintf(err, ecap, "the package contains the directory '%.60s'; refused", nm); return 0; }
            continue;
        }
        if (z->method != 0 && z->method != 8) { snprintf(err, ecap, "unsupported compression method"); return 0; }
        if (z->usize == 0xFFFFFFFFu || z->csize == 0xFFFFFFFFu) { snprintf(err, ecap, "zip64 entries are not supported"); return 0; }
        total += z->usize;
        if (total > MAX_UNPACKED_BYTES) { snprintf(err, ecap, "the package would unpack to more than the allowed size"); return 0; }
        for (j = 0; j < *count; j++) {
            if (_stricmp(ents[j].name, z->name) == 0) { snprintf(err, ecap, "the package lists '%.60s' twice; refused", nm); return 0; }
        }
        (*count)++;
    }
    return 1;
}

static int zip_extract_all(FILE* f, const ZEnt* ents, int count, const wchar_t* stage, char* err, size_t ecap) {
    int k;
    static uint8_t in[65536], out[65536];
    for (k = 0; k < count; k++) {
        const ZEnt* z = &ents[k];
        uint8_t lh[30];
        long long data;
        uint16_t nl, el;
        wchar_t rel[260], full[MAX_PATH], dirpart[260];
        wchar_t* slash;
        FILE* o;
        z_stream zs;
        uint32_t crc = crc32(0, NULL, 0);
        uint64_t produced = 0, left = z->csize;
        int zr = Z_OK;
        _fseeki64(f, z->lho, SEEK_SET);
        if (fread(lh, 1, 30, f) != 30 || rd32(lh) != 0x04034b50u) { snprintf(err, ecap, "the package is corrupt (bad local header)"); return 0; }
        nl = rd16(lh + 26); el = rd16(lh + 28);
        data = (long long)z->lho + 30 + nl + el;
        _fseeki64(f, data, SEEK_SET);
        to_wide(z->name, rel, 260);
        { wchar_t* q; for (q = rel; *q; q++) if (*q == L'/') *q = L'\\'; }
        wcscpy(dirpart, rel);
        slash = wcsrchr(dirpart, L'\\');
        if (slash != NULL) { *slash = 0; if (!mkdirs_below(stage, dirpart)) { snprintf(err, ecap, "cannot create the staging directory"); return 0; } }
        if (swprintf(full, MAX_PATH, L"%ls\\%ls", stage, rel) < 0) { snprintf(err, ecap, "path too long"); return 0; }
        o = _wfopen(full, L"wb");
        if (o == NULL) { snprintf(err, ecap, "cannot create a staging file (permission or disk space)"); return 0; }
        if (z->method == 0) {
            if (z->csize != z->usize) { fclose(o); snprintf(err, ecap, "the package is corrupt"); return 0; }
            while (left > 0) {
                size_t want = left > sizeof(in) ? sizeof(in) : (size_t)left, n = fread(in, 1, want, f);
                if (n != want || fwrite(in, 1, n, o) != n) { fclose(o); snprintf(err, ecap, "cannot extract (truncated package or disk full)"); return 0; }
                crc = crc32(crc, in, (uInt)n);
                produced += n;
                left -= n;
            }
        } else {
            memset(&zs, 0, sizeof(zs));
            if (inflateInit2(&zs, -15) != Z_OK) { fclose(o); snprintf(err, ecap, "zlib failure"); return 0; }
            while (zr != Z_STREAM_END) {
                if (zs.avail_in == 0) {
                    size_t want = left > sizeof(in) ? sizeof(in) : (size_t)left, n = want ? fread(in, 1, want, f) : 0;
                    if (n != want || n == 0) { inflateEnd(&zs); fclose(o); snprintf(err, ecap, "the package is truncated or corrupt"); return 0; }
                    left -= n;
                    zs.next_in = in; zs.avail_in = (uInt)n;
                }
                zs.next_out = out; zs.avail_out = sizeof(out);
                zr = inflate(&zs, Z_NO_FLUSH);
                if (zr != Z_OK && zr != Z_STREAM_END) { inflateEnd(&zs); fclose(o); snprintf(err, ecap, "the package is corrupt (inflate error %d)", zr); return 0; }
                {
                    size_t have = sizeof(out) - zs.avail_out;
                    produced += have;
                    if (produced > z->usize) { inflateEnd(&zs); fclose(o); snprintf(err, ecap, "the package entry is larger than declared; refused"); return 0; }
                    crc = crc32(crc, out, (uInt)have);
                    if (fwrite(out, 1, have, o) != have) { inflateEnd(&zs); fclose(o); snprintf(err, ecap, "cannot extract (disk full)"); return 0; }
                }
            }
            inflateEnd(&zs);
        }
        if (fclose(o) != 0 || produced != z->usize || crc != z->crc) { snprintf(err, ecap, "the package entry '%.60s' failed its size / CRC check", z->name); return 0; }
    }
    return 1;
}

/* ------------------------------------------------------------------ install (swap with backup + rollback) */
typedef struct { wchar_t rel[260]; int had_old; int placed; } Moved;

static int apply_install(const wchar_t* dir, const wchar_t* stage, const wchar_t* backup, const ZEnt* ents, int count, char* err, size_t ecap) {
    Moved* mv = (Moved*)calloc((size_t)count, sizeof(Moved));
    int k, nmoved = 0, ok = 1;
    for (k = 0; k < count && ok; k++) {
        wchar_t rel[260], dst[MAX_PATH], src[MAX_PATH], bak[MAX_PATH], parent[260], *sl;
        Moved* m = &mv[nmoved];
        to_wide(ents[k].name, rel, 260);
        { wchar_t* q; for (q = rel; *q; q++) if (*q == L'/') *q = L'\\'; }
        wcscpy(m->rel, rel);
        wcscpy(parent, rel);
        sl = wcsrchr(parent, L'\\');
        if (sl != NULL) { *sl = 0; if (!mkdirs_below(dir, parent)) { snprintf(err, ecap, "cannot create a program directory (or it is a link)"); ok = 0; break; } }
        swprintf(dst, MAX_PATH, L"%ls\\%ls", dir, rel);
        swprintf(src, MAX_PATH, L"%ls\\%ls", stage, rel);
        swprintf(bak, MAX_PATH, L"%ls\\%ls", backup, rel);
        if (is_reparse(dst)) { snprintf(err, ecap, "'%.60s' is a link; refused", ents[k].name); ok = 0; break; }
        if (GetFileAttributesW(dst) != INVALID_FILE_ATTRIBUTES) {
            if (sl != NULL && !mkdirs_below(backup, parent)) { snprintf(err, ecap, "cannot create the backup directory"); ok = 0; break; }
            if (!MoveFileExW(dst, bak, MOVEFILE_REPLACE_EXISTING)) {
                snprintf(err, ecap, "cannot replace '%.60s' (is the game still running? error %lu)", ents[k].name, GetLastError());
                ok = 0; break;
            }
            m->had_old = 1;
        }
        nmoved++;
        if (!MoveFileExW(src, dst, MOVEFILE_REPLACE_EXISTING | MOVEFILE_COPY_ALLOWED)) {
            snprintf(err, ecap, "cannot install '%.60s' (error %lu)", ents[k].name, GetLastError());
            ok = 0; break;
        }
        m->placed = 1;
    }
    if (ok) { /* verify what is on disk now */
        for (k = 0; k < count; k++) {
            wchar_t rel[260], dst[MAX_PATH];
            WIN32_FILE_ATTRIBUTE_DATA ad;
            to_wide(ents[k].name, rel, 260);
            { wchar_t* q; for (q = rel; *q; q++) if (*q == L'/') *q = L'\\'; }
            swprintf(dst, MAX_PATH, L"%ls\\%ls", dir, rel);
            if (!GetFileAttributesExW(dst, GetFileExInfoStandard, &ad) || ad.nFileSizeHigh != 0 || ad.nFileSizeLow != ents[k].usize) {
                snprintf(err, ecap, "verification of '%.60s' failed after installation", ents[k].name);
                ok = 0; break;
            }
        }
    }
    if (!ok) { /* roll back in reverse order */
        int r;
        for (r = nmoved - 1; r >= 0; r--) {
            wchar_t dst[MAX_PATH], bak[MAX_PATH];
            swprintf(dst, MAX_PATH, L"%ls\\%ls", dir, mv[r].rel);
            swprintf(bak, MAX_PATH, L"%ls\\%ls", backup, mv[r].rel);
            if (mv[r].placed) DeleteFileW(dst);
            if (mv[r].had_old) {
                if (!MoveFileExW(bak, dst, MOVEFILE_REPLACE_EXISTING)) logf_("ROLLBACK FAILED for %ls (error %lu); the previous file is kept in %ls", mv[r].rel, GetLastError(), backup);
            }
        }
        logf_("rolled back to the previous installation");
    }
    free(mv);
    return ok;
}

/* ------------------------------------------------------------------ the update itself */
static int expected_digest_from_file(const wchar_t* path, const char* zip_name, char hex[65]) {
    char buf[1024];
    size_t i;
    if (!read_small_file(path, buf, sizeof(buf))) return 0;
    for (i = 0; i < 64; i++) if (!isxdigit((unsigned char)buf[i])) return 0;
    if (buf[64] != ' ' && buf[64] != '\t' && buf[64] != '\r' && buf[64] != '\n' && buf[64] != 0) return 0;
    for (i = 0; i < 64; i++) hex[i] = (char)tolower((unsigned char)buf[i]);
    hex[64] = 0;
    { /* if a file name is given it must be ours */
        const char* p = buf + 64;
        while (*p == ' ' || *p == '\t' || *p == '*') p++;
        if (*p != 0 && *p != '\r' && *p != '\n') {
            size_t n = strcspn(p, "\r\n");
            if (n != strlen(zip_name) || strncmp(p, zip_name, n) != 0) return 0;
        }
    }
    return 1;
}

/* 1 if a process running THIS installation's AnimalCrossing.exe exists (a second instance next to an updated one could corrupt saves) */
static int game_running(const wchar_t* dir) {
    PROCESSENTRY32W pe;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    wchar_t want[MAX_PATH];
    int found = 0;
    if (snap == INVALID_HANDLE_VALUE) return 0;
    swprintf(want, MAX_PATH, L"%ls\\AnimalCrossing.exe", dir);
    pe.dwSize = sizeof(pe);
    if (Process32FirstW(snap, &pe)) {
        do {
            if (_wcsicmp(pe.szExeFile, L"AnimalCrossing.exe") == 0) {
                HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pe.th32ProcessID);
                if (h != NULL) {
                    wchar_t img[MAX_PATH];
                    DWORD n = MAX_PATH;
                    if (QueryFullProcessImageNameW(h, 0, img, &n) && _wcsicmp(img, want) == 0) found = 1;
                    CloseHandle(h);
                }
            }
        } while (!found && Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    return found;
}

static int do_update(const Release* rel, const char* old_ver, char* err, size_t ecap) {
    wchar_t upd[MAX_PATH], pkg[MAX_PATH], shaf[MAX_PATH], stage[MAX_PATH], backup[MAX_PATH], pat[MAX_PATH + 4];
    char want[65], got[65], lbl[200];
    Sink sk;
    FILE* f = NULL;
    ZEnt* ents = NULL;
    int count = 0, st, ok = 0;
    ULARGE_INTEGER freeb;
    if (game_running(g.dir)) { snprintf(err, ecap, "the game is still running; close it and start the launcher again (an update never replaces a running game)"); return 0; }
    swprintf(upd, MAX_PATH, L"%ls\\.update", g.dir);
    if (GetFileAttributesW(upd) != INVALID_FILE_ATTRIBUTES && is_reparse(upd)) { snprintf(err, ecap, "'.update' is a link; refused"); return 0; }
    CreateDirectoryW(upd, NULL);
    swprintf(pkg, MAX_PATH, L"%ls\\package.zip.part", upd);
    swprintf(shaf, MAX_PATH, L"%ls\\package.sha256", upd);
    swprintf(stage, MAX_PATH, L"%ls\\stage", upd);
    { wchar_t ov[40]; swprintf(ov, 40, L"backup-%S", old_ver); swprintf(backup, MAX_PATH, L"%ls\\%ls", upd, ov); }
    rmtree(stage);
    rmtree(backup);
    DeleteFileW(pkg);
    if (GetDiskFreeSpaceExW(g.dir, &freeb, NULL, NULL) && freeb.QuadPart < rel->zip_size * 3ull + 64ull * 1048576ull) {
        snprintf(err, ecap, "not enough free disk space (need about %llu MB)", (unsigned long long)((rel->zip_size * 3ull) >> 20) + 64);
        return 0;
    }
    ui_open(L"Animal Crossing Multiplayer - updating");
    /* 1. the published checksum */
    memset(&sk, 0, sizeof(sk));
    sk.maxbytes = 4096; sk.what = "Downloading checksum";
    if (http_get(rel->sha_url, &sk, &st, err, ecap) != 0) { free(sk.mem); goto fail; }
    { FILE* sf = _wfopen(shaf, L"wb"); if (sf == NULL) { free(sk.mem); snprintf(err, ecap, "cannot write to the update directory"); goto fail; } fwrite(sk.mem, 1, sk.memlen, sf); fclose(sf); free(sk.mem); }
    if (!expected_digest_from_file(shaf, rel->zip_name, want)) { snprintf(err, ecap, "the published checksum file is malformed"); goto fail; }
    if (rel->api_digest[0] != 0 && strcmp(rel->api_digest + 7, want) != 0) { snprintf(err, ecap, "the checksum file disagrees with the digest GitHub reports for the package"); goto fail; }
    /* 2. the package */
    memset(&sk, 0, sizeof(sk));
    sk.fp = _wfopen(pkg, L"wb");
    if (sk.fp == NULL) { snprintf(err, ecap, "cannot create the download file (permission)"); goto fail; }
    sk.maxbytes = rel->zip_size; sk.expect = rel->zip_size; sk.what = "Downloading update";
    st = http_get(rel->zip_url, &sk, &st, err, ecap);
    fclose(sk.fp);
    if (st != 0) goto fail;
    if (sk.got != rel->zip_size) { snprintf(err, ecap, "the download is incomplete (%llu of %llu bytes)", (unsigned long long)sk.got, (unsigned long long)rel->zip_size); goto fail; }
    /* 3. verify BEFORE anything is opened as an archive */
    ui_set("Verifying download", 0);
    if (!sha256_file(pkg, got)) { snprintf(err, ecap, "cannot hash the download"); goto fail; }
    if (strcmp(got, want) != 0) { snprintf(err, ecap, "the downloaded package does not match its published SHA-256 (corrupt or wrong file); nothing was installed"); goto fail; }
    logf_("sha256 verified: %s", got);
    /* 4. validate + extract into staging */
    ents = (ZEnt*)calloc(MAX_ZIP_ENTRIES, sizeof(ZEnt));
    f = _wfopen(pkg, L"rb");
    if (f == NULL || !zip_list(f, ents, &count, err, ecap)) goto fail;
    {
        int k, has_exe = 0, has_ver = 0;
        for (k = 0; k < count; k++) { if (_stricmp(ents[k].name, "AnimalCrossing.exe") == 0) has_exe = 1; if (_stricmp(ents[k].name, "version.txt") == 0) has_ver = 1; }
        if (!has_exe || !has_ver) { snprintf(err, ecap, "the package layout is wrong (AnimalCrossing.exe and version.txt are required)"); goto fail; }
    }
    CreateDirectoryW(stage, NULL);
    ui_set("Unpacking update", 0);
    if (!zip_extract_all(f, ents, count, stage, err, ecap)) goto fail;
    fclose(f); f = NULL;
    {   /* the staged version.txt must say exactly the release's version */
        wchar_t vf[MAX_PATH]; char vt[64]; Ver pv; char canon[64];
        swprintf(vf, MAX_PATH, L"%ls\\version.txt", stage);
        if (!read_small_file(vf, vt, sizeof(vt))) { snprintf(err, ecap, "the package's version.txt is unreadable"); goto fail; }
        vt[strcspn(vt, "\r\n")] = 0;
        if (!ver_parse(vt, &pv, canon, sizeof(canon)) || strcmp(canon, rel->ver) != 0) { snprintf(err, ecap, "the package's version.txt ('%.30s') does not match the release (%s)", vt, rel->ver); goto fail; }
    }
    if (game_running(g.dir)) { snprintf(err, ecap, "the game was started while the update was downloading; close it and try again"); goto fail; }
    /* 5. swap in (old files move to the backup), verify, roll back on failure */
    ui_set("Installing update", 1000);
    if (!CreateDirectoryW(backup, NULL) && GetLastError() != ERROR_ALREADY_EXISTS) { snprintf(err, ecap, "cannot create the backup directory"); goto fail; }
    if (!apply_install(g.dir, stage, backup, ents, count, err, ecap)) goto fail;
    ok = 1;
    swprintf(pat, MAX_PATH + 4, L"%ls\\package.zip.part", upd);
    DeleteFileW(pkg);
    DeleteFileW(shaf);
    rmtree(stage);
    {   /* keep only the newest backup */
        WIN32_FIND_DATAW fd; HANDLE h; wchar_t sp[MAX_PATH + 4];
        swprintf(sp, MAX_PATH + 4, L"%ls\\backup-*", upd);
        h = FindFirstFileW(sp, &fd);
        if (h != INVALID_HANDLE_VALUE) {
            do {
                wchar_t p[MAX_PATH];
                swprintf(p, MAX_PATH, L"%ls\\%ls", upd, fd.cFileName);
                if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) && wcscmp(p, backup) != 0) rmtree(p);
            } while (FindNextFileW(h, &fd));
            FindClose(h);
        }
    }
    snprintf(lbl, sizeof(lbl), "Updated to %s", rel->ver);
    logf_("%s (previous program files kept in .update\\backup-%s)", lbl, old_ver);
    goto out;
fail:
    if (f != NULL) fclose(f);
    DeleteFileW(pkg);
    rmtree(stage);
out:
    free(ents);
    ui_close();
    return ok;
}

/* ------------------------------------------------------------------ setup / launch */
static int dev_checkout(const wchar_t* dir) { /* a Git working tree or a CMake build tree around the launcher: never auto-update that */
    wchar_t p[MAX_PATH];
    int depth;
    wcscpy(p, dir);
    for (depth = 0; depth < 8; depth++) {
        wchar_t t[MAX_PATH + 16];
        wchar_t* sl;
        swprintf(t, MAX_PATH + 16, L"%ls\\.git", p);
        if (GetFileAttributesW(t) != INVALID_FILE_ATTRIBUTES) return 1;
        swprintf(t, MAX_PATH + 16, L"%ls\\CMakeCache.txt", p);
        if (GetFileAttributesW(t) != INVALID_FILE_ATTRIBUTES) return 1;
        sl = wcsrchr(p, L'\\');
        if (sl == NULL || sl <= p + 2) break;
        *sl = 0;
    }
    return 0;
}

static void read_ini(void) {
    wchar_t p[MAX_PATH];
    char buf[4096], *line, *save;
    swprintf(p, MAX_PATH, L"%ls\\updater.ini", g.dir);
    if (!read_small_file(p, buf, sizeof(buf))) return;
    for (line = strtok_s(buf, "\r\n", &save); line != NULL; line = strtok_s(NULL, "\r\n", &save)) {
        char* eq = strchr(line, '=');
        if (line[0] == '#' || line[0] == ';' || eq == NULL) continue;
        *eq++ = 0;
        while (*eq == ' ') eq++;
        if (strcmp(line, "repository") == 0) {
            char* sl = strchr(eq, '/');
            if (sl != NULL && sl - eq < 79 && strlen(sl + 1) < 79) {
                char o[80], r[80];
                size_t i;
                int bad = 0;
                memcpy(o, eq, (size_t)(sl - eq)); o[sl - eq] = 0;
                strcpy(r, sl + 1);
                r[strcspn(r, " \t")] = 0;
                for (i = 0; o[i]; i++) if (!(isalnum((unsigned char)o[i]) || o[i] == '-' || o[i] == '_' || o[i] == '.')) bad = 1;
                for (i = 0; r[i]; i++) if (!(isalnum((unsigned char)r[i]) || r[i] == '-' || r[i] == '_' || r[i] == '.')) bad = 1;
                if (!bad && o[0] && r[0]) { strcpy(g.owner, o); strcpy(g.repo, r); }
                else logf_("updater.ini: invalid repository ignored");
            }
        } else if (strcmp(line, "check_updates") == 0) g.check = atoi(eq) != 0;
        else if (strcmp(line, "allow_prerelease") == 0) g.prerelease = atoi(eq) != 0;
        else if (strcmp(line, "show_check_errors") == 0) g.show_errors = atoi(eq) != 0;
    }
}

static int launch_game(int argc, wchar_t** argv, const int* is_ours) {
    wchar_t cmd[8192], exe[MAX_PATH];
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    int i;
    swprintf(exe, MAX_PATH, L"%ls\\AnimalCrossing.exe", g.dir);
    swprintf(cmd, 8192, L"\"%ls\"", exe);
    for (i = 1; i < argc; i++) {
        if (is_ours[i]) continue;
        wcscat(cmd, L" \"");
        wcsncat(cmd, argv[i], 1000);
        wcscat(cmd, L"\"");
    }
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    if (!CreateProcessW(exe, cmd, NULL, NULL, FALSE, 0, NULL, g.dir, &si, &pi)) {
        wchar_t m[600];
        logf_("cannot start the game (error %lu)", GetLastError());
        if (!g.noui) {
            swprintf(m, 600, L"AnimalCrossing.exe could not be started (error %lu).\n\nExpected at:\n%ls\n\nIf an update failed, restore the files from .update\\backup-* (see docs/updater.md).", GetLastError(), exe);
            MessageBoxW(NULL, m, L"Animal Crossing Multiplayer", MB_OK | MB_ICONERROR);
        }
        return 2;
    }
    logf_("launched AnimalCrossing.exe (pid %lu)", pi.dwProcessId);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}

int WINAPI wWinMain(HINSTANCE hi, HINSTANCE hp, PWSTR cl, int sw) {
    int argc = 0, i, rc;
    wchar_t** argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    int* ours = (int*)calloc((size_t)argc + 1, sizeof(int));
    wchar_t lp[MAX_PATH], self[MAX_PATH], *sl;
    char vtxt[64], inst_ver[64], err[500];
    Ver iv;
    Release rel;
    (void)hi; (void)hp; (void)cl; (void)sw;
    memset(&g, 0, sizeof(g));
    g.answer = -1; g.check = 1; g.show_errors = 1;
    strcpy(g.owner, "vsimochi"); strcpy(g.repo, "ACGC-PC-Multiplayer");
    strcpy(g.api_base, "https://api.github.com"); strcpy(g.dl_base, "https://github.com");
    GetModuleFileNameW(NULL, self, MAX_PATH);
    wcscpy(g.dir, self);
    sl = wcsrchr(g.dir, L'\\');
    if (sl != NULL) *sl = 0;
    { wchar_t e[8]; g.test = GetEnvironmentVariableW(L"AC_UPDATER_TEST", e, 8) > 0 && e[0] == L'1'; }
    for (i = 1; i < argc; i++) { /* the launcher's own switches; everything else goes to the game */
        if (wcsncmp(argv[i], L"--updater-", 10) != 0) continue;
        ours[i] = 1;
        if (wcscmp(argv[i], L"--updater-skip") == 0) g.skip = 1;
        else if (wcscmp(argv[i], L"--updater-no-ui") == 0) g.noui = g.test; /* test-only switches: consumed (never passed to the game) but honoured only with AC_UPDATER_TEST=1 */
        else if (i + 1 < argc && (wcscmp(argv[i], L"--updater-answer") == 0 || wcscmp(argv[i], L"--updater-test-api") == 0 || wcscmp(argv[i], L"--updater-dir") == 0)) {
            const wchar_t* sw = argv[i];
            ours[++i] = 1;
            if (!g.test) continue;
            if (wcscmp(sw, L"--updater-answer") == 0) g.answer = wcscmp(argv[i], L"yes") == 0;
            else if (wcscmp(sw, L"--updater-dir") == 0) wcscpy(g.dir, argv[i]);
            else {
                char u[200];
                WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, u, sizeof(u), NULL, NULL);
                strcpy(g.api_base, u); strcpy(g.dl_base, u);
            }
        }
    }
    swprintf(lp, MAX_PATH, L"%ls\\updater.log", g.dir);
    { /* keep the log small */
        WIN32_FILE_ATTRIBUTE_DATA ad;
        if (GetFileAttributesExW(lp, GetFileExInfoStandard, &ad) && ad.nFileSizeLow > 262144) DeleteFileW(lp);
    }
    s_log = _wfopen(lp, L"ab");
    logf_("launcher %s start, dir=%ls", LAUNCHER_VERSION, g.dir);
    read_ini();
    { wchar_t e[8]; if (GetEnvironmentVariableW(L"AC_UPDATER_DISABLE", e, 8) > 0 && e[0] == L'1') g.skip = 1; }
    if (g.skip || !g.check) { logf_("update check disabled"); goto launch; }
    if (!g.test && dev_checkout(g.dir)) { logf_("this directory is inside a Git / CMake build tree: automatic updates are disabled for development checkouts"); goto launch; }
    if (g.test && dev_checkout(g.dir)) { logf_("this directory is inside a Git / CMake build tree: automatic updates are disabled for development checkouts"); goto launch; }
    {
        wchar_t vp[MAX_PATH];
        swprintf(vp, MAX_PATH, L"%ls\\version.txt", g.dir);
        if (!read_small_file(vp, vtxt, sizeof(vtxt))) { logf_("no version.txt: not a packaged installation, update check skipped"); goto launch; }
        vtxt[strcspn(vtxt, "\r\n")] = 0;
        if (!ver_parse(vtxt, &iv, inst_ver, sizeof(inst_ver))) { logf_("version.txt ('%.30s') is not a valid version; update check skipped", vtxt); goto launch; }
    }
    logf_("installed version %s; checking %s/%s", inst_ver, g.owner, g.repo);
    rc = find_latest(&rel, err, sizeof(err));
    if (rc == -1) { logf_("check: %s", err); goto launch; }
    if (rc == 0) {
        logf_("CHECK FAILED: %s", err);
        if (g.show_errors && !g.noui) say_error("The update check failed: %s\n\nThe installed version (%s) will be launched.", err, inst_ver);
        goto launch;
    }
    if (ver_cmp(&rel.v, &iv) <= 0) { logf_("up to date (latest %s)", rel.ver); goto launch; }
    logf_("newer release available: %s", rel.ver);
    {
        int yes;
        if (g.answer >= 0) yes = g.answer;
        else {
            wchar_t m[600];
            swprintf(m, 600, L"A new version of Animal Crossing Multiplayer is available.\n\nInstalled version: %S\nLatest version: %S\n\nWould you like to update now?\n\nYes = update now      No = launch the current version", inst_ver, rel.ver);
            yes = MessageBoxW(NULL, m, L"Animal Crossing Multiplayer - update available", MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND | MB_DEFBUTTON2) == IDYES;
        }
        if (!yes) { logf_("user declined update to %s; launching the installed version unchanged", rel.ver); goto launch; }
    }
    if (do_update(&rel, inst_ver, err, sizeof(err))) {
        logf_("RESULT: updated %s -> %s", inst_ver, rel.ver);
    } else {
        logf_("RESULT: update failed, installed version %s kept: %s", inst_ver, err);
        say_error("The update could not be installed: %s\n\nYour installed version (%s) was kept and will be launched.", err, inst_ver);
    }
launch:
    rc = launch_game(argc, argv, ours);
    if (s_log != NULL) fclose(s_log);
    return rc;
}
