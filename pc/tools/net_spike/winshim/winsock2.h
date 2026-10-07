/* winshim/winsock2.h - TEST-ONLY shim: lets pc/src/pc_net.c (Winsock) compile and run natively on Linux for tools/net_spike/peer_table_loopback_selftest.c.
 * Maps exactly the handful of Winsock names pc_net.c uses onto BSD sockets. Never part of the game build. */
#ifndef WINSHIM_WINSOCK2_H
#define WINSHIM_WINSOCK2_H
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <stdint.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

typedef int SOCKET;
typedef unsigned long u_long;
typedef struct { int unused; } WSADATA;
#define INVALID_SOCKET (-1)
#define SOCKET_ERROR (-1)
#define FIONBIO 1
#define MAKEWORD(a, b) ((uint16_t)(((uint8_t)(a)) | (((uint16_t)((uint8_t)(b))) << 8)))
#define WSAEWOULDBLOCK 10035
#define WSAECONNRESET 10054
#define WSAEMSGSIZE 10040
#define closesocket(s) close(s)
static inline int WSAStartup(uint16_t v, WSADATA* d) { (void)v; (void)d; return 0; }
static inline int WSACleanup(void) { return 0; }
static inline int ioctlsocket(SOCKET s, long cmd, u_long* arg) {
    int fl = fcntl(s, F_GETFL, 0);
    (void)cmd;
    if (fl < 0) return -1;
    return fcntl(s, F_SETFL, *arg ? (fl | O_NONBLOCK) : (fl & ~O_NONBLOCK));
}
static inline int WSAGetLastError(void) {
    if (errno == EWOULDBLOCK || errno == EAGAIN) return WSAEWOULDBLOCK;
    if (errno == ECONNREFUSED || errno == ECONNRESET) return WSAECONNRESET;
    if (errno == EMSGSIZE) return WSAEMSGSIZE;
    return errno;
}
static inline int winshim_recvfrom(SOCKET s, char* b, int l, int f, struct sockaddr* a, int* n) {
    socklen_t sl = (socklen_t)*n;
    int r = (int)recvfrom(s, b, (size_t)l, f, a, &sl);
    *n = (int)sl;
    return r;
}
#define recvfrom(s, b, l, f, a, n) winshim_recvfrom((s), (b), (l), (f), (a), (n))
static inline uint32_t GetTickCount(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}
typedef struct { uint32_t LowPart; int32_t HighPart; } LARGE_INTEGER;
static inline int QueryPerformanceCounter(LARGE_INTEGER* q) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    q->LowPart = (uint32_t)ts.tv_nsec ^ (uint32_t)ts.tv_sec;
    q->HighPart = (int32_t)(ts.tv_sec >> 8);
    return 1;
}
static inline uint32_t GetCurrentProcessId(void) { return (uint32_t)getpid(); }
#endif
