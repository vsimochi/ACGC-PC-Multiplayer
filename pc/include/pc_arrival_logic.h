#ifndef PC_ARRIVAL_LOGIC_H
#define PC_ARRIVAL_LOGIC_H

/* Guest-arrival fixes T2 / T5: the PURE decision logic (header-only, libc-free, natively unit-testable:
 * pc/tools/net_spike/test_guest_train.py + guest_train_selftest.c). The game code (src/game/m_train_control.c,
 * src/game/m_player_common.c_inc) only feeds it values read from the game and acts on the answer.
 *
 * T2 (title-demo train leak): the title scene's START1 train is parked by mTRC_mati_init(). The PC bootstraps (--bootstrap-resident,
 * the host observer, a guest arrival) bind a player from the still-running title scene, mTRC_init() resets the train, but the title scene
 * keeps ticking during the outgoing wipe and mTRC_move re-parks the train. In the town it stays parked forever. The edge
 * "title demo 1 seen -> anything else" fires exactly once per title exit.
 *
 * T5 (borderless edge): with borderless acres the 10 player BG-check states have no block clamp and the acre-change wade has no validity
 * gate. The clamp is brought back ONLY where the new block is invalid. */

/* The state is one latch: nonzero once mTRC_move observed title demo START1. */

/* Returns 1 when the train must be re-initialised now (the title-demo-1 -> other edge), else 0. Consumes the latch on the edge.
 *   was_start1        in/out latch
 *   demo_no           mEv_CheckTitleDemo() now
 *   start1_value      mEv_TITLEDEMO_START1
 *   draw_type_is_pre_game  nonzero when field_draw_type is the player-select / train interior (vanilla re-inits the train there anyway
 *                     through mSDI_StartDataInit, so the vanilla flow is left completely untouched) */
static inline int pcarr_trc_title_edge(int* was_start1, int demo_no, int start1_value, int draw_type_is_pre_game) {
    if (demo_no == start1_value) {
        *was_start1 = 1;
        return 0;
    }

    if (!*was_start1) {
        return 0;
    }

    *was_start1 = 0;
    return draw_type_is_pre_game ? 0 : 1;
}

/* The ride-off demo (guest arrival) sets train_coming_flag = 3 on its first town frame; the re-init must keep it, otherwise the
 * guest freezes in the demo's train_birth_wait forever. Returns the coming flag to restore after the re-init. */
static inline unsigned pcarr_trc_coming_after_reinit(unsigned coming_flag_before, unsigned arrival_value) {
    return coming_flag_before == arrival_value ? arrival_value : 0u;
}

/* T5: world position -> block index with FLOOR semantics (the game's mFI_Wpos2BlockNum truncates toward zero, which maps the strip
 * -size < x < 0 to block 0). A negative coordinate is therefore a block index < 0 (invalid). */
static inline int pcarr_floor_div(float v, float size) {
    int q = (int)(v / size);
    if (v < 0.0f && (float)q * size != v) {
        q--;
    }
    return q;
}

/* T5: nonzero = the borderless BG check must additionally run the vanilla block clamp (mCoBG_UniqueWallCheck, which clamps against the
 * previous, valid, block): only on the FG field and only when the new position is in an invalid block. */
static inline int pcarr_borderless_clamp_needed(int is_fg_field, int new_block_valid) {
    return is_fg_field && !new_block_valid;
}

/* T5: nonzero = the borderless acre-change wade may start (never into an invalid block on the FG field). */
static inline int pcarr_borderless_wade_allowed(int is_fg_field, int new_block_valid) {
    return !is_fg_field || new_block_valid;
}

#endif /* PC_ARRIVAL_LOGIC_H */
