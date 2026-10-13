#ifndef PC_GUEST_EVENT_DUE_H
#define PC_GUEST_EVENT_DUE_H

/* Guest mailbox, villager EVENT letters: the date rule of the vanilla mail_event_check() (src/actor/ac_event_manager.c), pure so a native test can run it.
 * Dates are the event manager's `ymdh` raw form: (year % 100) << 24 | month << 16 | day << 8 | hour. The window is the host's mail-event cursor:
 * last_raw = Save_t event_save_common.last_date (the previous check), now_raw = today. A letter dated (month, day) is due when that date (hour 0) lies in
 * (last_raw, now_raw], the target year being the cursor's year when the window wraps the new year before the target month, else this year
 * (check_past_day + the vanilla year choice). xmas_rule adds the Christmas card's extra vanilla condition: same year as the cursor and today's day of the
 * month <= 25 + 7. No state: the dedupe IS the cursor (the residents' rule), which the host persists in its own save. */

#include <stdint.h>

static inline int pc_guest_event_due(uint32_t last_raw, uint32_t now_raw, unsigned month, unsigned day, int xmas_rule) {
    const unsigned ly = (last_raw >> 24) & 0xFFu, lm = (last_raw >> 16) & 0xFFu;
    const unsigned ny = (now_raw >> 24) & 0xFFu, nm = (now_raw >> 16) & 0xFFu, nd = (now_raw >> 8) & 0xFFu;
    unsigned ty;
    uint32_t t;

    if (month < 1u || month > 12u || day < 1u || day > 31u) {
        return 0;
    }
    /* the vanilla guard of the whole pass: a cursor between Jan 1st and Dec 31st 23h */
    if ((last_raw & 0x00FFFFFFu) < 0x00010100u || (last_raw & 0x00FFFFFFu) > 0x000C1F17u) {
        return 0;
    }
    ty = (lm > nm && lm <= month) ? ly : ny;
    t = ((uint32_t)ty << 24) | ((uint32_t)month << 16) | ((uint32_t)day << 8);
    if (last_raw > now_raw || !(last_raw < t && t <= now_raw)) {
        return 0;
    }
    if (xmas_rule && !(ny == ly && nd <= day + 7u)) {
        return 0;
    }
    return 1;
}

#endif
