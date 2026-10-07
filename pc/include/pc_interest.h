/* pc_interest.h - capacity phase 5: INTEREST MANAGEMENT of the host's MOVE relay (pure logic, native tests in tools/net_spike/interest_roster_selftest.c).
 *
 * The host relays every client's 20 Hz MOVE stream to every other READY client: O(N^2) datagrams. Nothing is ever dropped for good: a receiver always gets SOME samples of every
 * player it can see, only at a lower rate when the sender is far away or elsewhere. The rate is a function of the SENDER's and the RECEIVER's last known place:
 *   NEAR   unknown scene or position of either side (fail open), the same interior, or the same town field within PC_INTEREST_NEAR_ACRES acres (Chebyshev)  -> every sample
 *   MID    same town field, up to PC_INTEREST_MID_ACRES acres                                                                                           -> every 2nd sample (10 Hz)
 *   FAR    same town field, farther                                                                                                                     -> every 4th sample (5 Hz)
 *   APART  different scenes (one is indoors / in another house)                                                                                         -> every 20th sample (1 Hz), enough for puppet liveness
 * A sender whose place just changed is BOOSTed (every sample, PC_INTEREST_BOOST_SAMPLES samples) so a player leaving a house is seen at once. The thresholds are in ACRES
 * (the game's own spatial unit: 640 world units), never an ad-hoc distance, and nobody is culled to zero: a distant player keeps moving on the receiver's screen. */
#ifndef PC_INTEREST_H
#define PC_INTEREST_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PC_INTEREST_ACRE_UNITS    640.0f /* mFI_BK_WORLDSIZE_BASE (asserted in pc_net_game.c) */
#define PC_INTEREST_NEAR_ACRES    1
#define PC_INTEREST_MID_ACRES     3
#define PC_INTEREST_BOOST_SAMPLES 40 /* 2 s at 20 Hz */

enum { PC_INTEREST_NEAR = 0, PC_INTEREST_MID = 1, PC_INTEREST_FAR = 2, PC_INTEREST_APART = 3, PC_INTEREST_TIERS = 4 };

typedef struct PCInterestView {
    uint8_t  scene_known; /* the player's PLAYER_SCENE has been announced */
    uint8_t  scene_id;
    uint16_t owner;       /* house owner id of an interior scene */
    uint8_t  in_town;     /* the scene is the town field (SCENE_FLAG_IN_TOWN) */
    uint8_t  pos_known;
    float    x, z;        /* world position (XZ) */
} PCInterestView;

/* The tier of `sender` as seen by `dest`. */
int pc_interest_tier(const PCInterestView* sender, const PCInterestView* dest);
/* Samples between two relayed ones of that tier (1 = every sample). */
int pc_interest_period(int tier);
/* 1 = relay sample number `count` (the sender's running count) at this tier; boost > 0 forces 1. */
int pc_interest_relay(int tier, uint32_t count, int boost);

#ifdef __cplusplus
}
#endif
#endif
