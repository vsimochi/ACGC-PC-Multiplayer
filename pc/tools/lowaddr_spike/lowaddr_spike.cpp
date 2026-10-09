// lowaddr_spike.cpp - stand-alone harness for the experimental 64-bit "low address space" strategy.
//
// Built and run by verify_spike.py (not part of the normal ac_pc build). It links:
//   * pc_lowaddr.c (the real diagnostic used by the game),
//   * the GBI spike data files compiled as C++ (static _GBI_STATIC_PTR initializers become startup code),
//   * generated stubs for the symbols those files reference that live in other, not-yet-converted files,
// and does three things:
//   1. allocates the kinds of memory the game allocates (arena, ARAM buffer, heaps, textures, threads)
//      *after* SDL2/OpenGL have loaded, and validates each with the same PC_LOWADDR_CHECK the game uses;
//   2. measures how much memory can still be handed out below 4 GB (headroom);
//   3. with --dump FILE, writes the post-startup bytes of every spike data symbol so they can be diffed
//      against a C build that has the pointer words zeroed (see verify_spike.py).
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <new>
#include <vector>

#ifndef SPIKE_REF_BUILD
#define SDL_MAIN_HANDLED
#include <SDL.h>
#endif
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <psapi.h>
#endif

#include "pc_lowaddr.h"

struct SpikeSym {
    const char* name;
    const void* addr;
    size_t size;
};
#include "spike_table.inc" /* generated: static const SpikeSym g_spike_syms[]; */

#ifdef _WIN32
static LARGE_INTEGER g_t_start, g_t_main;
__attribute__((constructor(101))) static void ctor_first() { QueryPerformanceCounter(&g_t_start); }
#endif

// game-like static buffers (in the image, like emu64's texture_buffer_data/bss)
static unsigned char s_tex_data[0x200000] __attribute__((aligned(32)));
static unsigned char s_tex_bss[0x100000] __attribute__((aligned(32)));

#ifndef SPIKE_REF_BUILD
static int thread_fn(void*) {
    int local = 0;
    PC_LOWADDR_CHECK("SDL thread stack (audio producer)", &local, sizeof(local));
    return 0;
}
#endif

static int dump_syms(const char* path) {
    FILE* f = fopen(path, "w");
    if (!f) return 1;
    for (size_t i = 0; i < sizeof(g_spike_syms) / sizeof(g_spike_syms[0]); i++) {
        const SpikeSym& s = g_spike_syms[i];
        fprintf(f, "SYM %s %llx %zu ", s.name, (unsigned long long)(uintptr_t)s.addr, s.size);
        const unsigned char* b = (const unsigned char*)s.addr;
        for (size_t k = 0; k < s.size; k++) fprintf(f, "%02x", b[k]);
        fputc('\n', f);
    }
    fclose(f);
    return 0;
}

int main(int argc, char** argv) {
#ifdef _WIN32
    QueryPerformanceCounter(&g_t_main);
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    printf("startup_init_us=%.1f (first constructor -> main: CRT + libstdc++ + spike data constructors)\n",
           (double)(g_t_main.QuadPart - g_t_start.QuadPart) * 1e6 / (double)freq.QuadPart);
#endif
    const char* dump_path = NULL;
    for (int i = 1; i < argc; i++)
        if (!strcmp(argv[i], "--dump") && i + 1 < argc) dump_path = argv[++i];
    if (dump_path) {
        int rc = dump_syms(dump_path);
        printf("dumped %zu symbols to %s\n", sizeof(g_spike_syms) / sizeof(g_spike_syms[0]), dump_path);
#ifdef SPIKE_REF_BUILD
        return rc;
#endif
    }
#ifndef SPIKE_REF_BUILD
    pc_lowaddr_init();

    // 1. load the same DLLs the game loads (SDL2 + OpenGL driver) BEFORE allocating, like pc_platform_init()
    int have_gl = 0;
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO | SDL_INIT_GAMECONTROLLER) == 0) {
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 3);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_CORE);
        SDL_Window* w = SDL_CreateWindow("lowaddr", 0, 0, 640, 480, SDL_WINDOW_OPENGL | SDL_WINDOW_HIDDEN);
        SDL_GLContext ctx = w ? SDL_GL_CreateContext(w) : NULL;
        have_gl = ctx != NULL;
        printf("SDL video init ok, GL 3.3 core context: %s\n", have_gl ? "yes" : SDL_GetError());
        SDL_Thread* t = SDL_CreateThread(thread_fn, "probe", NULL);
        if (t) SDL_WaitThread(t, NULL);
    } else {
        printf("SDL_Init failed: %s\n", SDL_GetError());
    }

    // 2. game-like allocations
    void* os_arena = malloc(24u << 20); /* pc_os.c: OS arena */
    PC_LOWADDR_CHECK("OS arena (24 MB)", os_arena, 24u << 20);
    void* aram = malloc(16u << 20); /* pc_aram.c: ARAM host buffer */
    PC_LOWADDR_CHECK("ARAM host buffer (16 MB)", aram, 16u << 20);
    PC_LOWADDR_CHECK("emu64 texture cache (.data)", s_tex_data, sizeof(s_tex_data));
    PC_LOWADDR_CHECK("emu64 texture cache (.bss)", s_tex_bss, sizeof(s_tex_bss));
    for (int i = 0; i < 64; i++) { /* JKR-style child heaps and actor-sized allocations */
        size_t sz = (i < 4) ? (1u << 20) : 0xBA8;
        void* h = malloc(sz);
        PC_LOWADDR_CHECK(i < 4 ? "JKR heap" : "actor allocation", h, sz);
    }
    for (int i = 0; i < 16; i++) { /* operator new (what C++ game/JSystem code uses) */
        char* n = new char[1u << 18];
        PC_LOWADDR_CHECK("operator new", n, 1u << 18);
    }
    std::vector<unsigned char> v;
    for (int i = 0; i < 16; i++) { /* growth reallocations (up to 128 MB) */
        v.resize((size_t)1 << (12 + i));
        PC_LOWADDR_CHECK("std::vector growth", v.data(), v.size());
    }
    {   /* VirtualAlloc, as an arena replacement would use */
        void* va = VirtualAlloc(NULL, 64u << 20, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
        PC_LOWADDR_CHECK("VirtualAlloc(NULL) 64 MB", va, 64u << 20);
    }

    // 3. loaded modules (informational: DLLs are expected to be high; the game must not keep their addresses in u32)
#ifdef _WIN32
    {
        HMODULE mods[256];
        DWORD need = 0;
        int high = 0, low = 0;
        if (K32EnumProcessModules(GetCurrentProcess(), mods, sizeof(mods), &need)) {
            for (unsigned i = 0; i < need / sizeof(HMODULE) && i < 256; i++) {
                if ((uint64_t)(uintptr_t)mods[i] >= PC_LOWADDR_LIMIT) high++; else low++;
            }
        }
        printf("loaded modules: %d below 4 GB, %d above 4 GB (DLLs; not game memory)\n", low, high);
        char nm[MAX_PATH];
        for (unsigned i = 0; i < need / sizeof(HMODULE) && i < 256; i++) {
            if (GetModuleFileNameA(mods[i], nm, sizeof(nm)) &&
                (strstr(nm, "SDL2") || strstr(nm, "opengl32") || strstr(nm, "nvoglv") || strstr(nm, "ig9icd") ||
                 strstr(nm, "amdvlk") || strstr(nm, "atio") || strstr(nm, "libstdc") || strstr(nm, "libwinpthread")))
                printf("  module %p %s\n", (void*)mods[i], nm);
        }
    }
#endif

    // 4. headroom: hand out 64 MB blocks until malloc crosses 4 GB or fails (survey, no abort)
    {
        size_t total = 0, blocks = 0;
        uint64_t highest = 0;
        for (;;) {
            void* p = malloc(64u << 20);
            if (!p) break;
            uint64_t end = (uint64_t)(uintptr_t)p + (64u << 20);
            if (end > PC_LOWADDR_LIMIT) { highest = end; free(p); break; }
            highest = end;
            total += 64u << 20;
            blocks++;
            if (blocks >= 60) break; /* ~3.8 GB, enough to know */
        }
        printf("headroom: %zu MB handed out by malloc below 4 GB before %s (highest end 0x%llx)\n", total >> 20,
               blocks >= 60 ? "stopping at cap" : "crossing/failing", (unsigned long long)highest);
    }

    int viol = pc_lowaddr_report();
    printf("violations=%d gl=%d\n", viol, have_gl);
    if (have_gl) { /* leave */ }
    SDL_Quit();
    return viol ? 2 : 0;
#else
    return 0;
#endif
}
