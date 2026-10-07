/* new_character_selftest.c - native unit test of the NEW-character creation path used by Play Online "New character" (pc_character_prepare_new with the in-memory
 * placeholder + pc_session_store_create_finish, exactly the calls pc_main.c pc_main_prepare_store_character / pc_m_card.c pc_guest_creation_finish make).
 * Usage: new_character_selftest <scratch_dir>   (chdir()s there: the default store save/mp lives under it; the real save is never touched). Prints PASS:/FAIL: and RESULT. */
#include "pc_character.h"
#include "pc_guest_profile.h"
#include "pc_session.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#ifdef _WIN32
#include <direct.h>
#define MKDIR(p) _mkdir(p)
#define CHDIR(p) _chdir(p)
#else
#include <unistd.h>
#define MKDIR(p) mkdir(p, 0755)
#define CHDIR(p) chdir(p)
#endif

/* pc_character.c (import_gci, unused here) references it; the real one lives in pc_town_sanitize.c */
int pc_town_gci_is_sanitized(const uint8_t* buf, size_t len) {
    (void)buf;
    (void)len;
    return 0;
}

static int s_pass, s_fail;
static void check(const char* what, int ok) {
    printf("%s: %s\n", ok ? "PASS" : "FAIL", what);
    if (ok) s_pass++; else s_fail++;
}

#define PLACEHOLDER "NewChar" /* == PC_NEW_CHARACTER_PLACEHOLDER in pc_main.c */

static int slurp(const char* path, char* out, size_t cap) {
    FILE* f = fopen(path, "rb");
    size_t n;
    if (!f) return -1;
    n = fread(out, 1, cap - 1, f);
    fclose(f);
    out[n] = '\0';
    return (int)n;
}

static void ini_path(const char* uuid, char* out, size_t cap) {
    snprintf(out, cap, "save/mp/characters/%s/character.ini", uuid);
}

int main(int argc, char** argv) {
    char err[400], before[2048], after[2048], p[300];
    PCCharacter ex, a, b, got;
    PCConnectSession* ss = pc_session();
    int i;
    if (argc < 2) {
        fprintf(stderr, "usage: new_character_selftest <scratch_dir>\n");
        return 2;
    }
    MKDIR(argv[1]);
    if (CHDIR(argv[1]) != 0) return 2;
    MKDIR("save");
    MKDIR("save/mp");

    /* an EXISTING character whose name equals the placeholder: a new request must not resolve to it */
    check("setup: existing character named like the placeholder", pc_character_prepare_new(NULL, PLACEHOLDER, &ex, err, sizeof(err)) == 1 && pc_character_write_exclusive(NULL, &ex, err, sizeof(err)) == 1);
    ini_path(ex.uuid, p, sizeof(p));
    check("setup: existing character.ini read", slurp(p, before, sizeof(before)) > 0);

    /* A: NEW request = prepare_new directly (no resolve): a fresh uuid, nothing written */
    check("A: prepare_new (placeholder) ok", pc_character_prepare_new(NULL, PLACEHOLDER, &a, err, sizeof(err)) == 1);
    check("A: fresh uuid != existing uuid", strcmp(a.uuid, ex.uuid) != 0 && strlen(a.uuid) == PC_CHARACTER_UUID_LEN);
    check("A: fresh ids differ from the existing character", !(a.player_id == ex.player_id && a.land_id == ex.land_id));
    ini_path(a.uuid, p, sizeof(p));
    check("A: nothing written before Rover finishes", slurp(p, after, sizeof(after)) < 0);

    /* the session state pc_main_prepare_store_character leaves for a NEW character */
    memset(ss, 0, sizeof(*ss));
    ss->storage = PC_CHARACTER_STORAGE_STORE;
    ss->creating = 1;
    ss->character = a;

    /* Rover finish: the typed name / gender / face are saved under the SAME uuid, the placeholder is not */
    check("C: finish with name 'Zed', gender 1, face 3 -> created", pc_session_store_create_finish("Zed", 1, 3, err, sizeof(err)) == 1);
    check("C: creating cleared", ss->creating == 0);
    check("C: session uuid unchanged by the finish", strcmp(ss->character.uuid, a.uuid) == 0);
    check("C: reload by the uuid from BEFORE Rover", pc_character_load(NULL, a.uuid, &got, err, sizeof(err)) == PC_CHARACTER_OK);
    check("C: saved name is Rover's, not the placeholder", strcmp(got.name, "Zed") == 0 && strcmp(got.name, PLACEHOLDER) != 0);
    check("C: name_bytes padded 'Zed     '", memcmp(got.name_bytes, "Zed     ", 8) == 0);
    check("C: gender / face saved", got.gender == 1 && got.face == 3);
    check("C: ids unchanged by the finish", got.player_id == a.player_id && got.land_id == a.land_id && memcmp(got.home_town_bytes, a.home_town_bytes, 8) == 0);
    check("C: a second finish is refused (not creating)", pc_session_store_create_finish("Other", 0, 0, err, sizeof(err)) == -1);

    /* B / D: an EXISTING uuid load: creating stays 0, the file is byte-identical, a finish is refused */
    memset(ss, 0, sizeof(*ss));
    check("B: resolve existing by uuid", pc_character_resolve(NULL, ex.uuid, &b, err, sizeof(err)) == PC_CHARACTER_OK && strcmp(b.uuid, ex.uuid) == 0);
    ss->storage = PC_CHARACTER_STORAGE_STORE;
    ss->creating = 0;
    ss->character = b;
    check("B: existing session is not creating", ss->creating == 0);
    check("D: finish refused for an existing character", pc_session_store_create_finish("Hack", 0, 0, err, sizeof(err)) == -1);
    ini_path(ex.uuid, p, sizeof(p));
    check("D: existing character.ini byte-identical", slurp(p, after, sizeof(after)) > 0 && strcmp(before, after) == 0);

    /* repeated new requests draw distinct uuids */
    {
        char u[5][PC_CHARACTER_UUID_LEN + 1];
        int ok = 1, j;
        for (i = 0; i < 5; i++) {
            PCCharacter t;
            ok = ok && pc_character_prepare_new(NULL, PLACEHOLDER, &t, err, sizeof(err)) == 1;
            memcpy(u[i], t.uuid, sizeof(u[i]));
            for (j = 0; j < i; j++) ok = ok && strcmp(u[i], u[j]) != 0;
            ok = ok && strcmp(u[i], ex.uuid) != 0 && strcmp(u[i], a.uuid) != 0;
        }
        check("A: repeated new requests draw distinct uuids", ok);
    }
    printf("RESULT passed=%d failed=%d\n", s_pass, s_fail);
    return s_fail == 0 ? 0 : 1;
}
