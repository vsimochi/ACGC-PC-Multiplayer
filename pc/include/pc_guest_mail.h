#ifndef PC_GUEST_MAIL_H
#define PC_GUEST_MAIL_H

/* Guest mailbox ("Guest mailbox via the post office", docs/multiplayer-guest-roadmap.md): the CLIENT seams used by the game code. Implemented in pc_net_game.c; declared here
 * (not in pc_net_game.h, which almost every translation unit includes) so only the mailbox overlay, the tag overlay and the post office girl depend on it.
 *
 * A GUEST owns no house, so it has no Common_Get(now_home) (NULL by design: never faked). Its letters wait in a HOST-held 10-slot mailbox; the host copies every slot to
 * the guest's client (MAILBOX_LETTER, house 0xFF) into a client-side copy that is NOT a homes[] mailbox. The post office girl offers "Check my mail." to a READY guest and
 * opens the vanilla mailbox overlay on that copy. Taking a letter is the host transaction MAIL_TAKE (kind 13) exactly as for a resident. */

#include "types.h"
#include "m_mail.h"

#ifdef __cplusplus
extern "C" {
#endif

/* THE mailbox the mailbox / tag overlays show and edit: the guest copy for a guest network client, else the vanilla Common_Get(now_home)->mailbox -- which is NULL when
 * now_home is NULL (every caller keeps a NULL guard). HOME_MAILBOX_SIZE slots. */
Mail_c* pc_ui_mailbox(void);
/* 1 iff pc_ui_mailbox() is the guest copy (the overlay was opened at the post office, not at a house mailbox: no mail-jump landing animation at the close). */
int pc_ui_mailbox_is_guest(void);
/* The overlay may take letters: a network client only once the host fed this mailbox (resident: its house mailbox, guest: its post office mailbox); host / solo: 1. */
int pc_ui_mailbox_usable(void);

/* Post office girl: 1 = offer "Check my mail." (CLIENT role, READY link, the local player is a GUEST, the host fed its guest mailbox: a host with the guest mailbox
 * switched off or an older host never feeds it, so the row is not offered). */
int pc_net_game_guest_mail_offer_available(void);
/* Letters waiting in the guest mailbox copy (0..10). */
int pc_net_game_guest_mail_count(void);

/* Post office girl: the greeting / "anything else" rows of the post office get the "Check my mail." choice inserted before their last (cancel) choice when they are loaded
 * while armed (pc_nook_house.c pc_nook_msg_patch). arm(-1) disarms. more_choice(index) = the choice index of "Check my mail." when the LAST load of message `index`
 * was patched, else -1 (the caller keeps the vanilla mapping). */
void pc_post_mail_msg_arm(int index);
int  pc_post_mail_more_choice(int index);

/* The host reason of a guest's letter beyond its daily limit (PC_NETGAME_TXN_REASON_MAIL_LIMIT). */
#define PC_GUEST_MAIL_REJECT_LIMIT 38

/* ---- Letters TO guests ----
 * The GUEST DIRECTORY (host: its guest table; client: the host's TOWN_SVC_STATE service 7 GUEST_DIR): the guests of this town a player may write to, never the local
 * player. address_list fills up to `max` address-book entries (type PLAYER, the guest KEY as the PersonalID); address_known = the PersonalID is listed (the post office
 * girl's address check on a client; the host re-checks against its own table at MAIL_SEND). Both 0 for solo play / an older host / AC_MP_GUEST_MAIL=0 on the host. */
int pc_net_game_guest_address_list(Mail_nm_c* out, int max);
int pc_net_game_guest_address_known(const PersonalID_c* pid);
/* The HOST's own player at the post office: check = -1 not a letter to a guest (vanilla path), 0 deliverable, 2 the guest's mailbox is full; post = 1 when the letter
 * went into the guest's host-held mailbox (the caller does not hand it to the post office), else 0. */
int pc_net_game_host_guest_letter_check(Mail_c* mail);
int pc_net_game_host_guest_letter_post(Mail_c* mail);

/* ---- Villager event letters for guests (host; no-ops on a client / solo) ----
 * window: ac_event_manager.c mail_event_check hands over the residents' mail-event cursor window (ymdh raw, BEFORE the cursor moves); vtday_due: the vanilla Valentine's
 * Day letters fired; goodbye_note: m_npc.c mNpc_SetGoodbyAnimalMail -- a villager (AnmPersonalID_c*) leaves, `memories` = its n Anmmem_c memories, copied now. */
void pc_net_game_guest_event_window(u32 last_raw, u32 now_raw);
void pc_net_game_guest_vtday_due(void);
void pc_net_game_guest_goodbye_note(const void* anm_id, const void* memories, int n);

#ifdef __cplusplus
}
#endif

#endif
