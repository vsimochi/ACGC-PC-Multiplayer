#ifndef PC_SETTINGS_H
#define PC_SETTINGS_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int window_width;
    int window_height;
    int fullscreen;       /* 0=windowed, 1=fullscreen, 2=borderless */
    int vsync;            /* 0=off, 1=on */
    int max_fps;          /* 0=uncapped, otherwise frame limiter target */
    int msaa;             /* 0=off, 2/4/8=samples */
    int texture_filtering; /* 0=force nearest-neighbor, 1=use the game's texture filtering */
    int preload_textures; /* 0=off (load on demand), 1=on (load all at startup), 2=on + cache file */
    int disable_resetti;  /* 0=normal (Resetti appears on reset), 1=disable reset penalty */
    int disable_shop_visitor_req; /* 0=normal (Nookington's needs a foreign-town shopper), 1=skip requirement */
    int borderless_acres; /* 0=original acre transitions (faster, draws less), 1=continuous camera/movement */
    int nes_aspect;       /* NES emulator aspect: 0=fullscreen stretch, 1=4:3 pillarbox (default) */
    int master_volume;    /* Applied at the PC audio output, 0-100 (default 100) */
    int stick_deadzone;   /* Gamepad main stick deadzone, percent 0-40 (default 12) */
    int cstick_deadzone;  /* Gamepad C-stick deadzone, percent 0-40 (default 12) */
    int max_guests;       /* HOST only (G4): most guests (foreigners, never residents) bound at once, 1..254 (pc_guest_admit_limit(); default 4); malformed / out of range = ignored */
    int interest_management; /* HOST only (capacity phase 5): 1 (default) = the MOVE relay is thinned by the receiver's interest (pc_interest.h), 0 = every sample to every peer (the old behaviour) */
    int guest_memory_mb;  /* HOST only (capacity phase 3): memory budget of the guest store in MB (default 256, 1..1048576): how many guests the host keeps = budget / ~28 KB per guest; there is no fixed guest table any more */
    int max_peers;        /* HOST only (capacity phase 2): most simultaneous transport peers (residents + guests + a connecting town-fetch client), 1..254 (default 8); out of range = ignored */
    int allow_new_guests; /* HOST only (M-D): 1 = a NEW guest key may be admitted (default), 0 = only guests already known to guests.dat; a new key is refused (SERVER_FULL) */
    int resident_tokens;  /* HOST only (M-E): 0 = off (default, nothing minted / checked), 1 = tofu, 2 = required (see docs/multiplayer-guest-roadmap.md) */
    int personal_sync;    /* HOST only (personal data sync, diary): -1 = AUTO (default: on only while town_serve != off), 0 = off, 1 = on */
    int show_ping;        /* CLIENT only: 1 = show the round-trip time (ms) to the host while connected (default 0; F4 toggles it for the session) */
    int town_serve;       /* HOST only (M-B): serve the town to pre-boot --town-fetch clients: -1 = AUTO (default: on for a --dedicated host, off otherwise), 0 = OFF, 1 = a SANITIZED copy (M-G), 2 = the full GCI (every resident's private data) */
} PCSettings;

extern PCSettings g_pc_settings;
/* G4: `--max-guests N` (host test / operator override of settings.ini max_guests); 0 = no override. Set by pc_main.c, valid 1..254. */
extern int g_pc_max_guests_override;
/* Capacity phase 2: `--max-peers N` (host operator override of settings.ini max_peers); 0 = no override. Set by pc_main.c, valid 1..254. */
extern int g_pc_max_peers_override;
/* M-D: `--allow-new-guests 0|1` (host override of settings.ini allow_new_guests); -1 = no override. */
extern int g_pc_allow_new_guests_override;
/* M-E: `--resident-tokens off|tofu|required` (host override of settings.ini resident_tokens); -1 = no override. */
extern int g_pc_resident_tokens_override;
/* M-B: `--town-serve on|off` (host operator override of settings.ini town_serve); -1 = no override. */
extern int g_pc_town_serve_override;
/* Effective town_serve mode: --town-serve override, else settings.ini town_serve, else AUTO = 1 (sanitized) for a --host --dedicated process and 0 otherwise. */
int pc_settings_town_serve_effective(void);
/* Personal data sync: `--personal-sync on|off` (host override of settings.ini personal_sync); -1 = no override. */
extern int g_pc_personal_sync_override;

void pc_settings_load(void);
void pc_settings_save(void);
void pc_settings_apply(void);
void pc_settings_cycle_resolution(int* width, int* height, int dir);

#ifdef __cplusplus
}
#endif

#endif /* PC_SETTINGS_H */
