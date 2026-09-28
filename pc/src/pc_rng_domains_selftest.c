/* pc_rng_domains_selftest.c - see pc_rng_domains_selftest.h. Runs standalone (no ROM/save). */
#include <stdio.h>

#include "libc64/qrand.h"
#include "libc64/qrand_domains.h"
#include "pc_rng_domains_selftest.h"

static int s_fail = 0;
static int s_check_count = 0;

#define ST_CHECK(cond, ...)                                                   \
  do {                                                                       \
    s_check_count++;                                                        \
    if (!(cond)) {                                                          \
      s_fail++;                                                             \
      printf("[RNG-SELFTEST] FAIL %s:%d: ", __FILE__, __LINE__);            \
      printf(__VA_ARGS__);                                                  \
      printf("\n");                                                        \
    }                                                                       \
  } while (0)

/* ---- 1. same seed + same domain -> identical sequence -------------------------------------- */
static void test_same_seed_same_domain(void) {
  u32 a[8];
  u32 b[8];
  int i;

  sqrand_d(QRAND_DOMAIN_WEATHER, 0xC0FFEEu);
  for (i = 0; i < 8; i++) a[i] = qrand_d(QRAND_DOMAIN_WEATHER);

  sqrand_d(QRAND_DOMAIN_WEATHER, 0xC0FFEEu);
  for (i = 0; i < 8; i++) b[i] = qrand_d(QRAND_DOMAIN_WEATHER);

  for (i = 0; i < 8; i++) {
    ST_CHECK(a[i] == b[i], "same seed/domain diverged at i=%d (%u vs %u)", i, (unsigned)a[i],
             (unsigned)b[i]);
  }
}

/* ---- 2. different domain -> independent sequence (no cross-contamination) ------------------ */
static void test_different_domain_independent(void) {
  u32 weather_seq[8];
  int i;
  int any_diff = 0;

  sqrand_d(QRAND_DOMAIN_WEATHER, 0x1234u);
  sqrand_d(QRAND_DOMAIN_STALK_MARKET, 0x1234u); /* same seed value, different domain */

  for (i = 0; i < 8; i++) weather_seq[i] = qrand_d(QRAND_DOMAIN_WEATHER);
  for (i = 0; i < 8; i++) (void)qrand_d(QRAND_DOMAIN_STALK_MARKET); /* advance in lockstep */

  /* Same seed, same algorithm, same state layout per domain: two domains seeded with the same raw
   * seed value DO produce the same raw sequence (each is just its own independent LCG instance --
   * domain separation isolates STATE, it is not intended to make same-seeded domains diverge from
   * each other). What must hold is that each domain's own state is untouched by the other -- i.e.
   * re-seeding one to the same value reproduces its own earlier sequence regardless of what the
   * other domain did in between (covered by test 3/4 below). Here we only sanity-check that the
   * two arrays were actually captured from genuinely separate state words by seeding them
   * DIFFERENTLY and confirming the sequences differ. */
  sqrand_d(QRAND_DOMAIN_STALK_MARKET, 0x5678u);
  for (i = 0; i < 8; i++) {
    u32 v = qrand_d(QRAND_DOMAIN_STALK_MARKET);
    if (v != weather_seq[i]) any_diff = 1;
  }
  ST_CHECK(any_diff, "stalk-market domain seeded differently from weather still matched it");
}

/* ---- 3/4. consuming one domain's stream must not alter another's next value ----------------- */
static void test_cross_domain_isolation(QRandDomain untouched, QRandDomain consumed,
                                         const char* name) {
  u32 expected;
  u32 actual;
  int i;

  sqrand_d(untouched, 0xABCDu);
  expected = qrand_d(untouched); /* the value `untouched` would give right after being seeded */

  /* Rewind `untouched` back to the same seed, then hammer the OTHER domain in between. */
  sqrand_d(untouched, 0xABCDu);
  sqrand_d(consumed, 0x99999999u);
  for (i = 0; i < 5000; i++) {
    (void)qrand_d(consumed);
    (void)fqrand_d(consumed);
    (void)fqrand2_d(consumed);
  }

  actual = qrand_d(untouched);
  ST_CHECK(actual == expected,
           "%s: consuming another domain changed this domain's next value (%u vs %u)", name,
           (unsigned)actual, (unsigned)expected);
}

/* ---- 5. re-seeding a domain produces reproducible results ----------------------------------- */
static void test_reseed_reproducible(void) {
  u32 first[4];
  u32 second[4];
  int i;

  sqrand_d(QRAND_DOMAIN_DAILY_SIM, 0x42424242u);
  for (i = 0; i < 4; i++) first[i] = qrand_d(QRAND_DOMAIN_DAILY_SIM);

  /* consume a bunch more so the state has moved far away from the seed */
  for (i = 0; i < 1000; i++) (void)qrand_d(QRAND_DOMAIN_DAILY_SIM);

  sqrand_d(QRAND_DOMAIN_DAILY_SIM, 0x42424242u); /* re-seed */
  for (i = 0; i < 4; i++) second[i] = qrand_d(QRAND_DOMAIN_DAILY_SIM);

  for (i = 0; i < 4; i++) {
    ST_CHECK(first[i] == second[i], "re-seed not reproducible at i=%d (%u vs %u)", i,
             (unsigned)first[i], (unsigned)second[i]);
  }
}

/* ---- 6. range-generating functions stay within requested bounds ----------------------------- */
static void test_range_bounds(void) {
  int i;

  sqrand_d(QRAND_DOMAIN_VILLAGER, 0x778899u);

  /* n = 1: must always be exactly 0 */
  for (i = 0; i < 200; i++) {
    int v = RANDOM_D(QRAND_DOMAIN_VILLAGER, 1);
    ST_CHECK(v == 0, "RANDOM_D(domain, 1) produced %d, expected 0", v);
  }

  /* a "large" range well within int range */
  for (i = 0; i < 2000; i++) {
    int v = RANDOM_D(QRAND_DOMAIN_VILLAGER, 100000);
    ST_CHECK(v >= 0 && v < 100000, "RANDOM_D(domain, 100000) out of range: %d", v);
  }

  /* fqrand_d must stay in [0, 1) */
  for (i = 0; i < 2000; i++) {
    f32 v = fqrand_d(QRAND_DOMAIN_VILLAGER);
    ST_CHECK(v >= 0.0f && v < 1.0f, "fqrand_d out of [0,1): %f", (double)v);
  }

  /* fqrand2_d must stay in [-0.5, 0.5) */
  for (i = 0; i < 2000; i++) {
    f32 v = fqrand2_d(QRAND_DOMAIN_VILLAGER);
    ST_CHECK(v >= -0.5f && v < 0.5f, "fqrand2_d out of [-0.5,0.5): %f", (double)v);
  }

  /* RANDOM_D_CENTER_F(domain, n) must stay in [-n/2, n/2) */
  for (i = 0; i < 2000; i++) {
    f32 v = RANDOM_D_CENTER_F(QRAND_DOMAIN_VILLAGER, 10.0f);
    ST_CHECK(v >= -5.0f && v < 5.0f, "RANDOM_D_CENTER_F(domain, 10) out of range: %f", (double)v);
  }
}

/* ---- 7. existing global RANDOM()/fqrand() behavior is bit-for-bit unchanged ----------------- */
static void test_global_rng_unchanged(void) {
  /* Hand-derived from the exact qrand.c formula (x = x*0x19660D + 0x3C6EF35F) for seed 1234, so
   * this only passes if qrand.c itself is still bit-for-bit what it was before this change -- it
   * does not depend on having a "before" build to compare against. */
  u32 x = 1234u;
  u32 expected[5];
  u32 actual;
  int i;

  for (i = 0; i < 5; i++) {
    x = x * 0x19660Du + 0x3C6EF35Fu;
    expected[i] = x;
  }

  sqrand(1234u);
  for (i = 0; i < 5; i++) {
    actual = qrand();
    ST_CHECK(actual == expected[i], "qrand() regression at i=%d: got %u, expected %u", i,
             (unsigned)actual, (unsigned)expected[i]);
  }

  /* fqrand()/fqrand2() must still stay in their documented ranges too. */
  sqrand(0xDEADBEEFu);
  for (i = 0; i < 500; i++) {
    f32 v = fqrand();
    ST_CHECK(v >= 0.0f && v < 1.0f, "fqrand() out of [0,1) after change: %f", (double)v);
  }
  sqrand(0xDEADBEEFu);
  for (i = 0; i < 500; i++) {
    f32 v = fqrand2();
    ST_CHECK(v >= -0.5f && v < 0.5f, "fqrand2() out of [-0.5,0.5) after change: %f", (double)v);
  }

  /* Leave the global stream reseeded to something fixed so this self-test itself is reproducible
   * end-to-end and never leaks a "random" global state into whatever runs after it. */
  sqrand(1u);
}

/* ---- Phase 4 seed-formula helpers: pure functions, deterministic, in-bounds ------------------ */
static void test_seed_formula_helpers(void) {
  u32 town_a = qrand_domain_hash_town(1234, (const u8*)"MyTown", 6);
  u32 town_a_again = qrand_domain_hash_town(1234, (const u8*)"MyTown", 6);
  u32 town_b = qrand_domain_hash_town(1234, (const u8*)"OtherTn", 7);
  u32 town_c = qrand_domain_hash_town(5678, (const u8*)"MyTown", 6);

  ST_CHECK(town_a == town_a_again, "qrand_domain_hash_town not deterministic");
  ST_CHECK(town_a != town_b, "qrand_domain_hash_town ignored the name");
  ST_CHECK(town_a != town_c, "qrand_domain_hash_town ignored the id");

  {
    u32 day1 = qrand_domain_day_epoch(2002, 4, 15);
    u32 day1_again = qrand_domain_day_epoch(2002, 4, 15);
    u32 day2 = qrand_domain_day_epoch(2002, 4, 16);
    ST_CHECK(day1 == day1_again, "qrand_domain_day_epoch not deterministic");
    ST_CHECK(day1 != day2, "qrand_domain_day_epoch did not change across days");
  }

  {
    /* same town + same day + different domain -> different seed (that's the whole point) */
    u32 seed_weather = qrand_domain_make_seed(town_a, 100u, QRAND_DOMAIN_WEATHER);
    u32 seed_stalk = qrand_domain_make_seed(town_a, 100u, QRAND_DOMAIN_STALK_MARKET);
    u32 seed_weather_again = qrand_domain_make_seed(town_a, 100u, QRAND_DOMAIN_WEATHER);
    ST_CHECK(seed_weather == seed_weather_again, "qrand_domain_make_seed not deterministic");
    ST_CHECK(seed_weather != seed_stalk, "qrand_domain_make_seed ignored the domain");

    /* and it actually produces a usable, reproducible stream end to end */
    {
      u32 a, b;
      sqrand_d(QRAND_DOMAIN_WEATHER, seed_weather);
      a = qrand_d(QRAND_DOMAIN_WEATHER);
      sqrand_d(QRAND_DOMAIN_WEATHER, seed_weather);
      b = qrand_d(QRAND_DOMAIN_WEATHER);
      ST_CHECK(a == b, "seed formula -> sqrand_d -> qrand_d round trip not reproducible");
    }
  }
}

int pc_rng_domains_selftest(void) {
  s_fail = 0;
  s_check_count = 0;

  printf("[RNG-SELFTEST] start\n");

  test_same_seed_same_domain();
  test_different_domain_independent();
  test_cross_domain_isolation(QRAND_DOMAIN_FISH_SPAWN, QRAND_DOMAIN_WEATHER,
                               "fish unaffected by weather");
  test_cross_domain_isolation(QRAND_DOMAIN_VILLAGER, QRAND_DOMAIN_FISH_SPAWN,
                               "villager unaffected by fish");
  test_reseed_reproducible();
  test_range_bounds();
  test_global_rng_unchanged();
  test_seed_formula_helpers();

  printf("[RNG-SELFTEST] done: %d check(s), %d failure(s)\n", s_check_count, s_fail);
  return s_fail;
}
