/* pc_dresser_ui.c - see pc_dresser_ui.h. */
#include "pc_dresser_ui.h"

#include <stdio.h>
#include <string.h>

#include "pc_net_game.h"
#include "pc_dresser_layout.h"
#include "pc_dresser_items.h"
#include "pc_sell_ui.h" /* Nook bulk sell basket: BASKET mode of this screen delegates the grid contents / take / put to the local staging */

static int s_active = 0;
static int s_basket = 0; /* Nook bulk sell basket: the screen shows the sell basket (see pc_dresser_ui.h) */
static unsigned short s_vanilla[PC_DRESSER_UI_VANILLA];
static int s_pending = 0;
/* 6p: an op deferred until the in-room commit of this screen's room edits landed (op 1 put / 2 take / 3 move / 4 swap, 0 = none) */
static struct {
    int op, index, pocket, item, polls;
    uint32_t t0; /* pc_net_game_now_ms() | 1 when the op was deferred (the give-up is wall-clock) */
} s_defer;
/* 6r (B): the item carried by "Grab" (nothing changes until it is placed: the carry is a view state only) */
static struct {
    int active, from_pocket, index, item;
} s_carry;

static const char* pc_dresser_ui_op_name(int op) {
    return op == 1 ? "put" : op == 2 ? "take" : op == 3 ? "move" : op == 4 ? "swap" : "?";
}

void pc_dresser_ui_begin(const unsigned short vanilla[PC_DRESSER_UI_VANILLA]) {
    s_active = 1;
    s_basket = 0;
    memset(&s_carry, 0, sizeof(s_carry));
    pc_dresser_ui_set_vanilla(vanilla);
}

void pc_dresser_ui_begin_basket(void) {
    s_active = 1;
    s_basket = 1;
    s_pending = 0;
    s_defer.op = 0;
    memset(&s_carry, 0, sizeof(s_carry));
    memset(s_vanilla, 0, sizeof(s_vanilla));
}

int pc_dresser_ui_basket(void) {
    return s_active && s_basket;
}

void pc_dresser_ui_end(void) {
    (void)pc_dresser_layout_end(); /* the enlarged gyroid window back to its pristine vertices (no-op when not remapped) */
    s_active = 0; /* a transaction still in flight resolves in the transaction layer (the pockets follow the host post-image) */
    s_basket = 0;
    if (s_carry.active) { /* 6r: closing while carrying = cancel (nothing was changed by the grab) */
        printf("[NET][DRESSER] gyroid overlay: closed while carrying 0x%04X: grab cancelled (nothing moved)\n", (unsigned)s_carry.item);
    }
    memset(&s_carry, 0, sizeof(s_carry));
    if (s_defer.op != 0) { /* 6p: a deferred op that was never sent is dropped (nothing moved) */
        printf("[NET][DRESSER] gyroid overlay: closed with a deferred %s of 0x%04X not sent: dropped (nothing moved)\n", pc_dresser_ui_op_name(s_defer.op),
               (unsigned)s_defer.item);
        s_defer.op = 0;
        s_pending = 0;
    }
    memset(s_vanilla, 0, sizeof(s_vanilla));
}

int pc_dresser_ui_active(void) {
    return s_active;
}

void pc_dresser_ui_set_vanilla(const unsigned short vanilla[PC_DRESSER_UI_VANILLA]) {
    if (vanilla != NULL) {
        memcpy(s_vanilla, vanilla, sizeof(s_vanilla));
    } else {
        memset(s_vanilla, 0, sizeof(s_vanilla));
    }
}

int pc_dresser_ui_slot(int col, int* index) {
    /* 6r (user decision A1): ALL 27 cells are host slots 0..26 (the sidecar v3 record); the vanilla keep slots of the house image are no cells any more (their
     * residual items are taken out to the pockets when the screen opens, ac_my_room_msg_ctrl.c_inc). History: 6q 0..23 extra / 24..26 vanilla; U3 0..2 / 3..26. */
    if (col < 0 || col >= PC_DRESSER_UI_SLOTS) {
        return PC_DRESSER_UI_SLOT_NONE;
    }
    if (index != NULL) {
        *index = col;
    }
    return PC_DRESSER_UI_SLOT_EXTRA;
}

int pc_dresser_ui_col(int kind, int index) {
    if (kind == PC_DRESSER_UI_SLOT_EXTRA && index >= 0 && index < PC_DRESSER_UI_EXTRA) {
        return index;
    }
    return -1; /* 6r: the vanilla keep slots have no cell */
}

int pc_dresser_ui_slot_item(int col) {
    int index = 0;
    if (s_basket) {
        return s_active ? pc_sell_ui_index_item(col) : 0; /* `col` = the 6 x 3 cursor table index: the staged pocket of that basket cell (0 = empty / the Sell button / beyond) */
    }
    return pc_dresser_ui_slot(col, &index) == PC_DRESSER_UI_SLOT_EXTRA ? pc_net_game_dresser_item(index) : 0;
}

/* ---- S5b ---- */
static PCDresserVanillaTakeFn s_take_fn = NULL;
static PCDresserVanillaPutFn s_put_fn = NULL;

/* Frame-rate independence: the deferred op gives up after PC_DRESSER_UI_DEFER_MS of WALL-CLOCK (it used to be 600 polls = 10 s only at 60 fps, 0.6 s on an
 * uncapped client, which refused the op while the room commit was still on its way). The poll count is kept for the log only. */
#define PC_DRESSER_UI_DEFER_MS 10000

/* 1 = sent, 2 = deferred (waits for the in-room commit; pending), 0 = refused */
static int pc_dresser_ui_begin_or_defer(int op, int index, int pocket, int item) {
    const int r = pc_net_game_dresser_begin(op, index, pocket, item);
    if (r == 1) {
        return 1;
    }
    if (r < 0 && pc_net_game_dresser_last_block() == 5) {
        s_defer.op = op;
        s_defer.index = index;
        s_defer.pocket = pocket;
        s_defer.item = item;
        s_defer.polls = 0;
        s_defer.t0 = pc_net_game_now_ms() | 1u;
        pc_net_game_saving_notice();
        printf("[NET][DRESSER] gyroid overlay: %s of 0x%04X WAITS for the in-room commit of this screen's vanilla-slot edits (notice 4)\n", pc_dresser_ui_op_name(op),
               (unsigned)item);
        return 2;
    }
    return 0;
}

/* the shared tail of every host operation of this screen */
static int pc_dresser_ui_send(int op, int index, int pocket, int item, const char* what) {
    const int r = pc_dresser_ui_begin_or_defer(op, index, pocket, item);
    if (r == 0) {
        printf("[NET][DRESSER] gyroid overlay: %s refused locally\n", what);
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    s_pending = 1;
    printf("[NET][DRESSER] gyroid overlay: %s item 0x%04X (%s)\n", what, (unsigned)item, r == 2 ? "waits for the in-room commit" : "host transaction");
    return PC_DRESSER_UI_ACT_PENDING;
}

void pc_dresser_ui_set_vanilla_ops(PCDresserVanillaTakeFn take, PCDresserVanillaPutFn put) {
    s_take_fn = take;
    s_put_fn = put;
}

int pc_dresser_ui_pending(void) {
    return s_pending;
}

int pc_dresser_ui_take(int col) {
    int index = 0, item, pocket;
    char what[96];
    if (s_basket) {
        return s_active ? pc_sell_ui_take(pc_sell_ui_table_cell(col)) : PC_DRESSER_UI_ACT_REFUSED; /* "Take back": local staging only (cursor table index -> cell) */
    }
    if (!s_active || s_pending || pc_dresser_ui_slot(col, &index) != PC_DRESSER_UI_SLOT_EXTRA || (item = pc_net_game_dresser_item(index)) == 0) {
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    pocket = pc_net_game_dresser_free_pocket();
    if (pocket < 0) {
        return PC_DRESSER_UI_ACT_NO_ROOM;
    }
    snprintf(what, sizeof(what), "take extra slot %d item 0x%04X -> pocket %d", index, (unsigned)item, pocket);
    return pc_dresser_ui_send(2, index, pocket, item, what);
}

int pc_dresser_ui_put(int pocket_slot) {
    const int item = pc_net_game_dresser_pocket_item(pocket_slot);
    char what[96];
    if (s_basket) {
        return s_active ? pc_sell_ui_put(pocket_slot) : PC_DRESSER_UI_ACT_REFUSED; /* "Add to Basket": local staging only */
    }
    if (!s_active || s_pending || item == 0 || !pc_net_game_dresser_item_storable(item)) {
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    if (pc_net_game_dresser_count() >= PC_DRESSER_UI_EXTRA) {
        return PC_DRESSER_UI_ACT_FULL; /* 6r: 27 host slots, every item type alike: nothing moves */
    }
    snprintf(what, sizeof(what), "put pocket %d item 0x%04X into the extra storage", pocket_slot, (unsigned)item);
    return pc_dresser_ui_send(1, 0xFF, pocket_slot, item, what);
}

/* ---- 6r (B) "Grab": A on a cell or a pocket item -> carried (drawn with the hand); A on a target places it (empty) or swaps (occupied); A on the origin,
 * B or closing the screen cancels. Nothing changes before the placement, and every placement is ONE operation: cell<->cell MOVE (op 3), cell->empty pocket
 * TAKE, pocket->empty cell PUT (both with the chosen slot / pocket), cell<->occupied pocket SWAP (op 4, host-authoritative, the item predicate on both
 * sides); pocket<->pocket is the plain local pocket swap (vanilla inventory behaviour). ---- */
int pc_dresser_ui_grab(int from_pocket, int index) {
    const int item = from_pocket ? pc_net_game_dresser_pocket_item(index) : pc_dresser_ui_slot_item(index);
    if (!s_active || s_pending || item == 0 || s_basket) { /* the basket has no Grab */
        return 0;
    }
    s_carry.active = 1;
    s_carry.from_pocket = from_pocket;
    s_carry.index = index;
    s_carry.item = item;
    printf("[NET][DRESSER] gyroid overlay: GRAB 0x%04X from %s %d (carried; nothing changed yet)\n", (unsigned)item, from_pocket ? "pocket" : "grid cell", index);
    return 1;
}

int pc_dresser_ui_carry_item(void) {
    return (s_active && s_carry.active) ? s_carry.item : 0;
}

int pc_dresser_ui_carry_hides(int from_pocket, int index) {
    if (s_active && s_basket) {
        return from_pocket && pc_sell_ui_pocket_hidden(index); /* a pocket in the basket is drawn in its basket cell only (view only: the pocket is unchanged) */
    }
    return s_active && s_carry.active && (s_carry.from_pocket != 0) == (from_pocket != 0) && s_carry.index == index;
}

void pc_dresser_ui_carry_cancel(void) {
    if (s_carry.active) {
        printf("[NET][DRESSER] gyroid overlay: grab of 0x%04X cancelled (nothing moved)\n", (unsigned)s_carry.item);
    }
    memset(&s_carry, 0, sizeof(s_carry));
}

int pc_dresser_ui_place(int to_pocket, int index) {
    const int from_pocket = s_carry.from_pocket, src = s_carry.index, item = s_carry.item;
    char what[128];
    int target;
    if (!s_active || !s_carry.active) {
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    memset(&s_carry, 0, sizeof(s_carry)); /* placed or refused: the carry ends either way (a refusal changes nothing) */
    if (s_pending) {
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    if (from_pocket == to_pocket && src == index) {
        printf("[NET][DRESSER] gyroid overlay: grab of 0x%04X put back on its origin (nothing moved)\n", (unsigned)item);
        return PC_DRESSER_UI_ACT_NONE;
    }
    /* the carried item must still be where it was grabbed (a host answer may have changed the view meanwhile) */
    if ((from_pocket ? pc_net_game_dresser_pocket_item(src) : pc_dresser_ui_slot_item(src)) != item) {
        printf("[NET][DRESSER] gyroid overlay: the grabbed 0x%04X is no longer at its origin: placement refused (nothing moved)\n", (unsigned)item);
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    target = to_pocket ? pc_net_game_dresser_pocket_item(index) : pc_dresser_ui_slot_item(index);
    if (!from_pocket && !to_pocket) { /* cell -> cell: MOVE (an occupied target = swap inside the pool) */
        snprintf(what, sizeof(what), "GRAB place: grid %d -> grid %d (%s)", src, index, target != 0 ? "SWAP" : "MOVE");
        return pc_dresser_ui_send(3, src | (index << 8), 0, item, what);
    }
    if (!from_pocket && to_pocket) { /* cell -> pocket: TAKE into that empty pocket, or SWAP with the pocket item */
        if (target == 0) {
            snprintf(what, sizeof(what), "GRAB place: grid %d -> empty pocket %d (TAKE)", src, index);
            return pc_dresser_ui_send(2, src, index, item, what);
        }
        if (!pc_net_game_dresser_pocket_storable(index)) {
            pc_net_game_dresser_ui_notice(11); /* "That item can't be stored in a dresser" */
            printf("[NET][DRESSER] gyroid overlay: GRAB swap grid %d <-> pocket %d refused: the pocket item 0x%04X cannot be stored\n", src, index, (unsigned)target);
            return PC_DRESSER_UI_ACT_REFUSED;
        }
        snprintf(what, sizeof(what), "GRAB place: grid %d <-> pocket %d (SWAP)", src, index);
        return pc_dresser_ui_send(4, src, index, item, what);
    }
    if (from_pocket && !to_pocket) { /* pocket -> cell: PUT into that empty slot, or SWAP with the slot item */
        if (!pc_net_game_dresser_pocket_storable(src)) {
            pc_net_game_dresser_ui_notice(11);
            printf("[NET][DRESSER] gyroid overlay: GRAB pocket %d -> grid %d refused: 0x%04X cannot be stored\n", src, index, (unsigned)item);
            return PC_DRESSER_UI_ACT_REFUSED;
        }
        if (target == 0) {
            snprintf(what, sizeof(what), "GRAB place: pocket %d -> empty grid %d (PUT)", src, index);
            return pc_dresser_ui_send(1, index, src, item, what);
        }
        snprintf(what, sizeof(what), "GRAB place: pocket %d <-> grid %d (SWAP)", src, index);
        return pc_dresser_ui_send(4, index, src, target, what);
    }
    /* pocket -> pocket: the plain local pocket move / swap (vanilla inventory behaviour; no class changes) */
    if (!pc_net_game_dresser_pocket_swap(src, index)) {
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    printf("[NET][DRESSER] gyroid overlay: GRAB place: pocket %d -> pocket %d (local %s)\n", src, index, target != 0 ? "swap" : "move");
    return PC_DRESSER_UI_ACT_DONE;
}

int pc_dresser_ui_poll(void) {
    int r;
    if (!s_pending) {
        return PC_DRESSER_UI_ACT_NONE;
    }
    if (s_defer.op != 0) { /* 6p: retry the deferred op; it is sent as soon as the room is committed (the gate passes), else it gives up */
        const int b = pc_net_game_dresser_begin(s_defer.op, s_defer.index, s_defer.pocket, s_defer.item);
        if (b == 1) {
            printf("[NET][DRESSER] gyroid overlay: the deferred %s of 0x%04X was SENT after %d frame(s) (the room was committed)\n", pc_dresser_ui_op_name(s_defer.op),
                   (unsigned)s_defer.item, s_defer.polls);
            s_defer.op = 0;
            return PC_DRESSER_UI_ACT_PENDING;
        }
        ++s_defer.polls;
        if (b < 0 && pc_net_game_dresser_last_block() == 5 && (int32_t)(pc_net_game_now_ms() - s_defer.t0) < PC_DRESSER_UI_DEFER_MS) {
            pc_net_game_saving_notice();
            return PC_DRESSER_UI_ACT_PENDING;
        }
        printf("[NET][DRESSER] gyroid overlay: the deferred %s of 0x%04X gave up after %d frame(s) (refusal kind %d): nothing moved\n", pc_dresser_ui_op_name(s_defer.op),
               (unsigned)s_defer.item, s_defer.polls, pc_net_game_dresser_last_block());
        s_defer.op = 0;
        s_pending = 0;
        return PC_DRESSER_UI_ACT_REFUSED;
    }
    r = pc_net_game_dresser_poll();
    if (r == PC_NETGAME_TS_OP_PENDING) {
        return PC_DRESSER_UI_ACT_PENDING;
    }
    s_pending = 0;
    printf("[NET][DRESSER] gyroid overlay: host answer %s (reason %d)\n", r == PC_NETGAME_TS_OP_APPLIED ? "APPLIED" : "REJECTED", pc_net_game_ts_last_reject_reason());
    if (r == PC_NETGAME_TS_OP_APPLIED) {
        return PC_DRESSER_UI_ACT_DONE;
    }
    return pc_net_game_ts_last_reject_reason() == 22 /* NO_ROOM: the dresser is full */ ? PC_DRESSER_UI_ACT_FULL : PC_DRESSER_UI_ACT_REFUSED;
}

static unsigned s_input_seq = 0;
void pc_dresser_ui_note_input(void) {
    s_input_seq++;
}
unsigned pc_dresser_ui_input_seq(void) {
    return s_input_seq;
}
