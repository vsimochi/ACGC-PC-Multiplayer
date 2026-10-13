/* pc_museum_net.h - worker B: the museum for network players (implemented in pc_net_game.c; the sidecar store is pc_museum_donors.c).
 *
 * MUSEUM LETTERS WITH A GIFT: a client (resident or guest) posts a letter to the museum through the host transaction MUSEUM_MAIL (TXN kind 19, the MAIL_SEND path of the post girl).
 * GUEST DONOR NAMES: guest donations carry donor nibble 5 (the save format has no other value); the host keeps WHICH guest gave an exhibit in museum_donors.dat and mirrors the
 * names to every client (town service 8 MUSEUM_DONORS), so plaques and Blathers name the guest instead of "a former resident". */
#ifndef PC_MUSEUM_NET_H
#define PC_MUSEUM_NET_H

#ifdef __cplusplus
extern "C" {
#endif

#define PC_MUSEUM_MAIL_REJECT_FULL 42 /* == PC_NETGAME_TXN_REASON_MUSEUM_FULL: the sender's museum record has no room (the letter and its gift stay) */
#define PC_MUSEUM_MAIL_REJECT_OFF  43 /* == PC_NETGAME_TXN_REASON_MUSEUM_MAIL_OFF: the host has the feature off */

/* m_museum.c mMsm_DepositFossil (the vanilla day pass, any role): the host runs the same pass for its guests. */
void pc_net_game_museum_day_pass(int order_info);

/* Exhibit index space of the attribution: fossil 0..24, art 25..39, insect 40..79, fish 80..119 (mMmd_FOSSIL/ART/INSECT/FISH_NUM). -1 = not an exhibit item. */
#define PC_MUSEUM_EXHIBITS 120
int pc_museum_exhibit_of_item(unsigned short item);
/* The guest who donated exhibit `exhibit` when its donor nibble is 5 (DELETED_PLAYER) and the host attributed it: 1 + the guest's name (PLAYER_NAME_LEN bytes) in name_out,
 * *is_self = 1 when that guest is THIS process's player; 0 = no attribution (vanilla "former resident"). Host: the sidecar; client: the last MUSEUM_DONORS service. */
int pc_museum_guest_donor(int exhibit, unsigned char* name_out, int* is_self);
/* HOST, pc_m_card.c pc_mp_promote_create (inside its rollback snapshot): the promoted guest's attributed exhibits (nibble 5) become resident slot `slot`'s (nibble slot + 1). */
int pc_net_game_museum_promote(const void* gkey, int slot);

#ifdef __cplusplus
}
#endif
#endif
