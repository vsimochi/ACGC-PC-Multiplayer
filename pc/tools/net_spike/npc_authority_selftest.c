/* npc_authority_selftest.c - native model test of pc_npc_authority.h (villager stale fallback / hysteresis, relay lease, talk hold cap).
 * Build + run (git-bash, PATH appended with /c/msys64/ucrt64/bin), from pc/:
 *   gcc -std=gnu11 -Wall -Iinclude tools/net_spike/npc_authority_selftest.c -o build64_w3/npc_authority_selftest.exe && build64_w3/npc_authority_selftest.exe */
#include "pc_npc_authority.h"

#include <stdio.h>

static int s_fail = 0, s_n = 0;
#define CHECK(c) do { s_n++; if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); s_fail++; } } while (0)

/* feed a 20 Hz stream from t0 for dur ms, updating every 16 ms like a game loop; returns the number of transitions seen */
static int run_stream(PCNpcAuthSlot* s, const PCNpcAuthCfg* c, uint32_t t0, uint32_t dur, uint32_t period, int* last_tr) {
    uint32_t t, next = t0;
    int n = 0;
    for (t = t0; t < t0 + dur; t += 16) {
        int r;
        if (period != 0 && t >= next) {
            pc_npc_auth_on_sample(s, t, c);
            next += period;
        }
        r = pc_npc_auth_update(s, t, 0, c);
        if (r != PC_NPC_AUTH_NOCHANGE) {
            n++;
            *last_tr = r;
        }
    }
    return n;
}

int main(void) {
    PCNpcAuthCfg c;
    PCNpcAuthSlot s;
    int tr = 0, i;
    uint32_t t;

    pc_npc_auth_default_cfg(&c);
    CHECK(c.stale_ms == 2500u && c.grace_ms == 4000u && c.fresh_needed == 5u);

    /* 1. never any sample: HOST during the grace window, LOCAL right after it */
    pc_npc_auth_reset(&s, 1000);
    CHECK(pc_npc_auth_update(&s, 1000, 0, &c) == PC_NPC_AUTH_NOCHANGE && !s.local);
    CHECK(pc_npc_auth_update(&s, 1000 + 3999, 0, &c) == PC_NPC_AUTH_NOCHANGE && !s.local);
    CHECK(pc_npc_auth_update(&s, 1000 + 4000, 0, &c) == PC_NPC_AUTH_TO_LOCAL && s.local);
    CHECK(pc_npc_auth_update(&s, 1000 + 4001, 0, &c) == PC_NPC_AUTH_NOCHANGE && s.local);

    /* 2. a steady 20 Hz stream never goes local, even across a long run */
    pc_npc_auth_reset(&s, 0);
    CHECK(run_stream(&s, &c, 0, 60000, 50, &tr) == 0 && !s.local);

    /* 3. the stream stops: LOCAL about stale_ms after the last sample, not before */
    pc_npc_auth_reset(&s, 0);
    run_stream(&s, &c, 0, 5000, 50, &tr); /* last sample at t=4950 */
    CHECK(!s.local);
    CHECK(pc_npc_auth_update(&s, s.last_recv_ms + 2499, 0, &c) == PC_NPC_AUTH_NOCHANGE);
    CHECK(pc_npc_auth_update(&s, s.last_recv_ms + 2500, 0, &c) == PC_NPC_AUTH_TO_LOCAL && s.local);

    /* 4. recovery needs N consecutive fresh samples (hysteresis) */
    t = s.last_recv_ms + 10000;
    for (i = 0; i < 4; i++) {
        pc_npc_auth_on_sample(&s, t + (uint32_t)i * 50u, &c);
        CHECK(pc_npc_auth_update(&s, t + (uint32_t)i * 50u, 0, &c) == PC_NPC_AUTH_NOCHANGE && s.local);
    }
    pc_npc_auth_on_sample(&s, t + 200, &c);
    CHECK(pc_npc_auth_update(&s, t + 200, 0, &c) == PC_NPC_AUTH_TO_HOST && !s.local);

    /* 5. straggler packets (one every second) never bring it back: no flapping */
    pc_npc_auth_reset(&s, 0);
    s.local = 1;
    {
        int n = run_stream(&s, &c, 0, 30000, 1000, &tr);
        CHECK(n == 0 && s.local);
    }

    /* 6. a stream with gaps just under the stale limit stays HOST; a single TO_LOCAL / TO_HOST pair per real outage */
    pc_npc_auth_reset(&s, 0);
    {
        int n = run_stream(&s, &c, 0, 20000, 2000, &tr); /* 0.5 Hz: each gap 2 s < 2.5 s */
        CHECK(n == 0 && !s.local);
        n = run_stream(&s, &c, 20000, 6000, 0, &tr); /* outage */
        CHECK(n == 1 && tr == PC_NPC_AUTH_TO_LOCAL);
        n = run_stream(&s, &c, 26000, 2000, 50, &tr); /* back */
        CHECK(n == 1 && tr == PC_NPC_AUTH_TO_HOST);
    }

    /* 7. must_hold vetoes HOST -> LOCAL but never forces LOCAL -> HOST */
    pc_npc_auth_reset(&s, 0);
    CHECK(pc_npc_auth_update(&s, 9000, 1, &c) == PC_NPC_AUTH_NOCHANGE && !s.local);
    CHECK(pc_npc_auth_update(&s, 9001, 0, &c) == PC_NPC_AUTH_TO_LOCAL && s.local);
    CHECK(pc_npc_auth_update(&s, 9002, 1, &c) == PC_NPC_AUTH_NOCHANGE && s.local);

    /* 8. actor (re)created: a stale ring must be cleared + a new grace window; a fresh ring is kept */
    pc_npc_auth_reset(&s, 0);
    pc_npc_auth_on_sample(&s, 100, &c);
    CHECK(pc_npc_auth_on_actor_created(&s, 100 + 2500, &c) == 1 && !s.have && !s.local);
    CHECK(pc_npc_auth_update(&s, 100 + 2500 + 3999, 0, &c) == PC_NPC_AUTH_NOCHANGE);
    CHECK(pc_npc_auth_update(&s, 100 + 2500 + 4000, 0, &c) == PC_NPC_AUTH_TO_LOCAL);
    pc_npc_auth_reset(&s, 0);
    pc_npc_auth_on_sample(&s, 100, &c);
    s.local = 1;
    CHECK(pc_npc_auth_on_actor_created(&s, 600, &c) == 0 && s.have && !s.local);
    /* grace covers a recently-dead stream too: no LOCAL inside the window even though the sample is old */
    CHECK(pc_npc_auth_update(&s, 600 + 3000, 0, &c) == PC_NPC_AUTH_NOCHANGE);
    CHECK(pc_npc_auth_update(&s, 600 + 4000, 0, &c) == PC_NPC_AUTH_TO_LOCAL);

    /* 9. clock wrap */
    pc_npc_auth_reset(&s, 0xFFFFF000u);
    pc_npc_auth_on_sample(&s, 0xFFFFFF00u, &c);
    CHECK(pc_npc_auth_update(&s, 0x00000100u, 0, &c) == PC_NPC_AUTH_NOCHANGE); /* 0x200 ms after the sample */
    CHECK(pc_npc_auth_update(&s, 0xFFFFFF00u + 4100u, 0, &c) == PC_NPC_AUTH_TO_LOCAL);

    /* 10. relay lease */
    {
        PCNpcProxyLease l;
        pc_npc_proxy_reset(&l);
        CHECK(pc_npc_proxy_offer(&l, 3, 1000, 0, PC_NPC_PROXY_LEASE_MS) == 1 && l.owner == 3);
        CHECK(pc_npc_proxy_offer(&l, 5, 1100, 0, PC_NPC_PROXY_LEASE_MS) == 0 && l.owner == 3); /* exclusive */
        CHECK(pc_npc_proxy_offer(&l, 3, 1200, 0, PC_NPC_PROXY_LEASE_MS) == 1);
        CHECK(pc_npc_proxy_offer(&l, 5, 1200 + 1499, 0, PC_NPC_PROXY_LEASE_MS) == 0);
        CHECK(pc_npc_proxy_offer(&l, 5, 1200 + 1500, 0, PC_NPC_PROXY_LEASE_MS) == 1 && l.owner == 5); /* lapsed */
        CHECK(pc_npc_proxy_offer(&l, 5, 3000, 1, PC_NPC_PROXY_LEASE_MS) == 0 && l.owner == -1);       /* host actor live */
        CHECK(pc_npc_proxy_offer(&l, 3, 3001, 0, PC_NPC_PROXY_LEASE_MS) == 1 && l.owner == 3);
    }

    /* 11. hold cap */
    CHECK(!pc_npc_talk_hold_capped(1000, 1000 + 119999, PC_NPC_TALK_HOLD_CAP_MS));
    CHECK(pc_npc_talk_hold_capped(1000, 1000 + 120000, PC_NPC_TALK_HOLD_CAP_MS));
    CHECK(!pc_npc_talk_hold_capped(0, 500000, PC_NPC_TALK_HOLD_CAP_MS));   /* no begin recorded */
    CHECK(!pc_npc_talk_hold_capped(1000, 900000, 0));                     /* cap disabled */
    CHECK(pc_npc_talk_hold_capped(0xFFFFFF00u, 0xFFFFFF00u + 120000u, PC_NPC_TALK_HOLD_CAP_MS)); /* wrap */

    printf("npc_authority_selftest: %d checks, %d failed\n", s_n, s_fail);
    return s_fail ? 1 : 0;
}
