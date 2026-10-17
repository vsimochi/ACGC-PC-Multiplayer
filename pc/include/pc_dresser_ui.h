/* pc_dresser_ui.h - the DRESSER MODE of the gyroid (haniwa) overlay (TARGET_PC, multiplayer dresser storage v2; docs/dresser-storage-redesign-plan.md U3).
 *
 * The gyroid overlay normally shows the 4 items of Save.homes[h].haniwa. In dresser mode (U3) it shows ALL 27 slots of the opened dresser on ONE page, a
 * 9 x 3 grid in place of the gyroid portrait / dialogue / 4-slot strip: 6q: grid 0..23 = extra slots 1..24 (host-side storage, pc_net_game_dresser_item(); every
 * item type fills them first), grid 24..26 = the 3 vanilla keep slots of the dresser (FTR_ACTOR.items, house image; read-only copy taken when the overlay opens). The save's real gyroid struct is never read or written in dresser mode.
 * Pure state (no game headers): the overlays and ac_my_room call these only inside TARGET_PC blocks. */
#ifndef PC_DRESSER_UI_H
#define PC_DRESSER_UI_H

#ifdef __cplusplus
extern "C" {
#endif

#define PC_DRESSER_UI_COLS     9  /* U3: ONE page, a 9 x 3 grid */
#define PC_DRESSER_UI_ROWS     3
#define PC_DRESSER_UI_SLOTS    27
#define PC_DRESSER_UI_VANILLA  3
#define PC_DRESSER_UI_EXTRA    27 /* 6r: every cell is a host slot */

enum { PC_DRESSER_UI_SLOT_NONE = 0, PC_DRESSER_UI_SLOT_VANILLA = 1, PC_DRESSER_UI_SLOT_EXTRA = 2 };

void pc_dresser_ui_begin(const unsigned short vanilla[PC_DRESSER_UI_VANILLA]); /* enter dresser mode (page 1) with a copy of the vanilla keep slots */
void pc_dresser_ui_end(void);
int  pc_dresser_ui_active(void);
/* Nook bulk sell basket (pc_sell_ui.h): the SAME screen in BASKET mode. pc_dresser_ui_active() is 1 as well (window, cells, icons, hand, tags, the scripted test
 * pad all stay the dresser screen's); the grid is 5 x 3 (one cell per pocket), cells show the staged pockets, take / put STAGE locally (pc_sell_ui_take / _put:
 * nothing is sent, nothing changes in the pockets), staged pockets are hidden in the pocket grid, there is no Grab. begin_basket() leaves any dresser state;
 * pc_dresser_ui_begin() / _end() leave basket mode. */
void pc_dresser_ui_begin_basket(void);
int  pc_dresser_ui_basket(void);
void pc_dresser_ui_set_vanilla(const unsigned short vanilla[PC_DRESSER_UI_VANILLA]);
/* The kind of grid slot `col` (0..26, row-major 9 x 3) and its index (6r): every cell is host slot `col` (EXTRA); the vanilla keep slots have no cell. */
int  pc_dresser_ui_slot(int col, int* index);
int  pc_dresser_ui_col(int kind, int index); /* the inverse: the grid cell of (kind, index), -1 = none */
int  pc_dresser_ui_slot_item(int col);   /* the item shown in that grid slot (0 = empty) */

/* ---- S5b: take / put (vanilla slots through the room's vanilla keep-item path, extra slots as host transactions) ---- */
enum { PC_DRESSER_UI_ACT_NONE = 0, PC_DRESSER_UI_ACT_DONE, PC_DRESSER_UI_ACT_PENDING, PC_DRESSER_UI_ACT_NO_ROOM, PC_DRESSER_UI_ACT_FULL, PC_DRESSER_UI_ACT_REFUSED };
/* Vanilla keep-slot operations provided by the room while the overlay is open (ac_my_room): take: 1 = the item went into a pocket, 0 = no free pocket,
 * -1 = refused (edit lock); put: 1 = put into a vanilla slot (pocket emptied), 0 = the 3 vanilla slots are full, -1 = refused. */
typedef int (*PCDresserVanillaTakeFn)(int index);
typedef int (*PCDresserVanillaPutFn)(int item, int pocket_slot);
void pc_dresser_ui_set_vanilla_ops(PCDresserVanillaTakeFn take, PCDresserVanillaPutFn put);
int  pc_dresser_ui_take(int col);         /* the item of that grid slot -> a pocket */
int  pc_dresser_ui_put(int pocket_slot);  /* that pocket item -> the first free host slot (6r: 27, every item type alike) */
int  pc_dresser_ui_pending(void);         /* an extra-slot transaction is in flight (the overlay ignores input) */
/* 6r (B) "Grab": grab(from_pocket, cell or pocket index) = carry it (1 = carried; nothing changes yet); place(to_pocket, index) = ONE operation: empty
 * target = move / take / put, occupied = swap (cell<->cell MOVE, cell<->pocket SWAP op 4; pocket<->pocket local); the origin = cancel. carry_item() = the
 * carried item (0 = none; the hand draws it); carry_cancel() = B. Closing the screen cancels. */
int  pc_dresser_ui_grab(int from_pocket, int index);
int  pc_dresser_ui_place(int to_pocket, int index);
int  pc_dresser_ui_carry_item(void);
void pc_dresser_ui_carry_cancel(void);
int  pc_dresser_ui_carry_hides(int from_pocket, int index); /* 6s: 1 = that cell / pocket is the origin of the carried item (its icon is not drawn) */
int  pc_dresser_ui_poll(void);            /* PENDING while in flight; DONE / FULL / REFUSED exactly once when it resolves; NONE otherwise */
/* TEST feedback: the overlay noted a consumed input (cursor move / page turn / tag decision / take or put); a sequence number */
void     pc_dresser_ui_note_input(void);
unsigned pc_dresser_ui_input_seq(void);

#ifdef __cplusplus
}
#endif
#endif
