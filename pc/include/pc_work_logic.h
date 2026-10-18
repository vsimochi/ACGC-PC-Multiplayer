#ifndef PC_WORK_LOGIC_H
#define PC_WORK_LOGIC_H
/* Nook Work Mode v2 ("Work v2": Check job / urgent bonus / quit with the item / friendship tips / auto-complete at the villager / varied delivery items).
 * PURE logic only (no game headers): the host plan in pc_net_game.c, the dialogue in pc_nook_house.c and the native selftest
 * (pc/tools/net_spike/work_v2_selftest.c) all use these functions, so the numbers the player is told are the numbers the host pays.
 *
 * Wire (protocol 8, extended in place):
 *   TXN kind 16 WORK ops 8 URGE (Check job -> "No.": the host may reveal the job as urgent and add a bonus ONCE; no item), 9 QUIT (quit while carrying the job's
 *   QUEST item: the item is removed in the same step), 10 VILLAGER_COMPLETE (the parcel handed to its villager completes the job: item out, Nook's reward + the
 *   villager's tip in, job retired, all in ONE step). tag.flags bit 0 (PC_WORK_TXN_FLAG_V2) = the request comes from a Work v2 client (a job created by such an
 *   ENTER is a v2 job: varied items, the tip decided at payout).
 *   TOWN_SVC_STATE service 11 WORK_CAPS (8 bytes): u8 version (1), u8 flags (bit 0 = the host runs Work v2), 6 reserved zero bytes. A client uses any v2 feature
 *   only after this arrived with bit 0 set; an old client ignores service 11 (unknown service), an old host never sends it (the client keeps the old dialogue).
 *   WORK_STATE (68) flags bits 1..3 (PC_WORK_SF_*) are sent ONLY to a peer that has sent a v2-flagged WORK op in this session (an old client refuses unknown bits).
 * Switch: AC_MP_WORK_V2=0 / settings.ini [Gameplay] work_v2 = 0 (host: no caps bit, ops 8..10 and the flag refused as before; client: the old dialogue). */
#include <stdint.h>

#define PC_WORK_TXN_FLAG_V2 0x01u

/* WORK_STATE (68) flags (bit 0 = Work Mode on, as before) */
#define PC_WORK_SF_MODE    0x01u
#define PC_WORK_SF_JOB_V2  0x02u /* the current / last job is a v2 job */
#define PC_WORK_SF_URGED   0x04u /* Nook revealed this job as urgent and added the bonus (once) */
#define PC_WORK_SF_AUTO    0x08u /* the last job was completed (and paid) at the villager */
#define PC_WORK_SF_ALL     0x0Fu

/* host record flags (PCNetWorkChar.wflags, persisted; 0 in every older work_jobs.dat = a v1 job) */
#define PC_WORK_WF_V2      0x01u
#define PC_WORK_WF_URGED   0x02u
#define PC_WORK_WF_AUTO    0x04u

#define PC_WORK_CAPS_LEN     8u
#define PC_WORK_CAPS_VERSION 1u

/* 32-bit mixer (murmur3 finalizer): every per-job roll is a function of (job id, salt) only, so a retry / replay / restart never re-rolls */
static inline uint32_t pc_work_mix(uint32_t job_id, uint32_t salt) {
    uint32_t h = job_id * 0x9E3779B1u ^ salt;
    h ^= h >> 16;
    h *= 0x85EBCA6Bu;
    h ^= h >> 13;
    h *= 0xC2B2AE35u;
    h ^= h >> 16;
    return h;
}

#define PC_WORK_SALT_URGENT 0x55524731u /* 'URG1' */
#define PC_WORK_SALT_BONUS  0x424F4E31u /* 'BON1' */
#define PC_WORK_SALT_TIP    0x54495031u /* 'TIP1' */
#define PC_WORK_SALT_ITEM   0x49544D31u /* 'ITM1' */

#define PC_WORK_URGENT_PERCENT 40

/* 1 = Nook will call this job urgent when the player answers "No." at Check job (decided per job, never per answer: asking again cannot re-roll it) */
static inline int pc_work_v2_urgent(uint32_t job_id) {
    return (int)(pc_work_mix(job_id, PC_WORK_SALT_URGENT) % 100u) < PC_WORK_URGENT_PERCENT;
}

/* the urgent bonus: 100, 150, 200, 250 or 300 Bells, added to the reward once */
static inline uint32_t pc_work_v2_urgent_bonus(uint32_t job_id) {
    return 100u + (pc_work_mix(job_id, PC_WORK_SALT_BONUS) % 5u) * 50u;
}

/* The villager's tip for a delivery: a random base of 50..300 Bells (steps of 10) plus 2 Bells per friendship point (friendship clamped to 0..127, the
 * vanilla range of Anmmem_c.friendship), rounded down to 10 Bells: 50..550 in all. friendship < 0 (no memory of the player, a falling-out) = 0. */
#define PC_WORK_TIP_BASE_MIN 50u
#define PC_WORK_TIP_BASE_MAX 300u
#define PC_WORK_TIP_PER_FRIEND 2u
static inline uint32_t pc_work_v2_tip(uint32_t job_id, int friendship) {
    const uint32_t base = PC_WORK_TIP_BASE_MIN + (pc_work_mix(job_id, PC_WORK_SALT_TIP) % ((PC_WORK_TIP_BASE_MAX - PC_WORK_TIP_BASE_MIN) / 10u + 1u)) * 10u;
    const uint32_t f = friendship <= 0 ? 0u : (friendship >= 127 ? 127u : (uint32_t)friendship);
    return ((base + f * PC_WORK_TIP_PER_FRIEND) / 10u) * 10u;
}

/* one entry of a pool, deterministic in the job id */
static inline uint16_t pc_work_v2_pick(const uint16_t* pool, int n, uint32_t job_id) {
    return (pool == 0 || n <= 0) ? (uint16_t)0 : pool[pc_work_mix(job_id, PC_WORK_SALT_ITEM) % (uint32_t)n];
}

/* service 11 blob */
static inline void pc_work_caps_build(uint8_t* blob, int on) {
    int i;
    for (i = 0; i < (int)PC_WORK_CAPS_LEN; i++) {
        blob[i] = 0;
    }
    blob[0] = (uint8_t)PC_WORK_CAPS_VERSION;
    blob[1] = on ? 1u : 0u;
}
static inline int pc_work_caps_valid(const uint8_t* blob) {
    int i;
    if (blob[0] != (uint8_t)PC_WORK_CAPS_VERSION || (blob[1] & ~1u) != 0u) {
        return 0;
    }
    for (i = 2; i < (int)PC_WORK_CAPS_LEN; i++) {
        if (blob[i] != 0) {
            return 0;
        }
    }
    return 1;
}

/* The choices of the v2 Check job row (the "did you bring ..." question), shared by the message builder and the actor so both agree on what choice k means.
 * job_type / obj_state = PC_WORK_JOB_* / PC_WORK_OBJ_* (pc_nook_house.h: 1 fruit, 2 fetch, 3 deliver; 0 start, 1 carrying, 2 delivered); has_item = the pockets
 * hold what the next step needs (FRUIT / FETCH carrying: the item to hand in; DELIVER start: a free pocket for the parcel). acts = PC_WORK_ACT_*; returns 2..3. A delivered job offers REPORT / LATER (never quit: it is paid). */
enum { PC_WORK_ACT_HAND = 0, PC_WORK_ACT_LATER = 1, PC_WORK_ACT_QUIT = 2, PC_WORK_ACT_TAKE = 3, PC_WORK_ACT_REPORT = 4, PC_WORK_ACT_NO = 5 };
static inline int pc_work_v2_row(int job_type, int obj_state, int has_item, int* acts, int cap) {
    int n = 0;
    if (cap < 3) {
        return 0;
    }
    if (job_type == 3 && obj_state == 2) {
        acts[n++] = PC_WORK_ACT_REPORT; /* a delivery handed over without auto-complete (an older client / job): Nook still pays it */
        acts[n++] = PC_WORK_ACT_LATER;
        return n;
    }
    if (job_type == 3 && obj_state == 0) {
        if (has_item) {
            acts[n++] = PC_WORK_ACT_TAKE;
        }
    } else if ((job_type == 1 || (job_type == 2 && obj_state == 1)) && has_item) {
        acts[n++] = PC_WORK_ACT_HAND;
    }
    acts[n++] = PC_WORK_ACT_NO;
    acts[n++] = PC_WORK_ACT_QUIT;
    return n;
}

#endif
