/* pc_session.c - see pc_session.h. */
#include "pc_session.h"
#include "pc_guest_profile.h"

#include <stdio.h>
#include <string.h>

static PCConnectSession s_session;
static char s_active_path[300];

PCConnectSession* pc_session(void) {
    return &s_session;
}

void pc_session_apply_server(const PCServer* s) {
    snprintf(s_session.host, sizeof(s_session.host), "%s", s->address);
    s_session.port = s->port;
    snprintf(s_session.server_name, sizeof(s_session.server_name), "%s", s->name);
}

int pc_session_select_town(const uint8_t land_name[8], uint16_t land_id, uint32_t terrain_hash) {
    char key[PC_CHARACTER_TOWNKEY_LEN + 1], path[300];
    if (s_session.storage != PC_CHARACTER_STORAGE_STORE) {
        return 0;
    }
    pc_character_town_key_format(land_name, land_id, terrain_hash, key);
    if (!pc_character_token_path(NULL, s_session.character.uuid, key, path, sizeof(path))) {
        return 0;
    }
    if (strcmp(path, s_active_path) == 0) {
        return 0;
    }
    memcpy(s_active_path, path, strlen(path) + 1);
    pc_guest_token_path_set_override(s_active_path);
    return 1;
}

int pc_session_legacy_token_lookup(const uint8_t land_name[8], uint16_t land_id, uint32_t terrain_hash, const uint8_t home_pid[20], PCMpGtkEntry* out) {
    static PCMpGtkFile gf;
    static uint8_t raw[PC_MP_GTK_FILE_SIZE + 8];
    char path[96];
    FILE* fp;
    size_t n;
    int i;
    if (s_session.storage != PC_CHARACTER_STORAGE_STORE || !s_session.character.has_legacy ||
        !pc_guest_profile_file_path(NULL, s_session.character.legacy_profile[0] != '\0' ? s_session.character.legacy_profile : NULL, 1, path, sizeof(path))) {
        return 0;
    }
    fp = fopen(path, "rb");
    if (fp == NULL) {
        return 0;
    }
    n = fread(raw, 1, sizeof(raw), fp);
    fclose(fp);
    if (pc_mp_gtoken_parse(raw, n, &gf) != PC_MP_GST_OK) {
        return 0;
    }
    i = pc_mp_gtoken_find(&gf, land_name, land_id, terrain_hash, home_pid);
    if (i < 0) {
        return 0;
    }
    *out = gf.e[i];
    return 1;
}

int pc_session_store_create_finish(const char* name, int gender, int face, char* err, size_t errcap) {
    PCCharacter c = s_session.character;
    size_t i, n = strlen(name);
    int r;
    if (s_session.storage != PC_CHARACTER_STORAGE_STORE || !s_session.creating || n < 1 || n > 8) {
        snprintf(err, errcap, "no store character is being created");
        return -1;
    }
    memcpy(c.name, name, n + 1);
    for (i = 0; i < 8; i++) {
        c.name_bytes[i] = i < n ? (uint8_t)name[i] : (uint8_t)' ';
    }
    c.gender = gender;
    c.face = face;
    err[0] = '\0';
    r = pc_character_write_exclusive(NULL, &c, err, errcap);
    if (r == 1) {
        s_session.character = c;
        s_session.creating = 0;
        (void)pc_character_default_set(NULL, c.uuid);
    }
    return r;
}
