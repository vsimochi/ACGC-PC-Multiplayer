# Dresser storage: manual verification checklist (23:12 dev build, one session, ~40 min)

Scope: only the six things the automated tests cannot judge by eye. Nothing here changes the default (`AC_MP_DRESSER_STORAGE` stays OFF unless you set it).
Evidence that already exists (runtime/protocol-tested, do not redo): all 27 cells reachable by pad, take/put through the host, leave-commit, restart, replay,
migration logic, refusal on the real pad path. See `docs/dresser-storage-redesign-plan.md` section 6d.

## 0. Setup (do once)

Use DISPOSABLE copies, never your live save.

1. Copy the folder `pc\build64\bin_fixture4_w3` (it holds the 23:12 exe and a test town) to two new folders:
   `pc\build64\bin_manual_host` and `pc\build64\bin_manual_client`. Check the exe date in both is `Oct 11 23:12` (else copy `pc\build64\bin\AnimalCrossing.exe` over it).
2. Environment for BOTH processes (cmd.exe; your display/volume rule):
   `set AC_DISPLAY_NAME=samsung` and `set AC_MASTER_VOLUME=1`
3. Host window (folder `bin_manual_host`), furniture sync is on by default for hosts:
   ```
   set AC_MP_DRESSER_STORAGE=1
   set AC_TEST_HOOKS=1
   set AC_TEST_DRESSER_SEED=1:50=1068,54=1068
   AnimalCrossing.exe --host 7777 --bootstrap-resident 0 --verbose
   ```
   `AC_TEST_DRESSER_SEED` (format `HOUSE:CELL=ITEMHEX,CELL=ITEMHEX`) writes two identical chests (item 0x1068, a real storage piece) into EMPTY floor-0 cells 50 and 54 of resident 1's house at the first probe.
   If the host log says a cell was not empty, pick other cells (`1:CELL=1068,CELL=1068`, cells above 40 are usually free) and restart.
4. Client window (folder `bin_manual_client`): `AnimalCrossing.exe --connect 127.0.0.1:7777 --verbose --bootstrap-resident 1`
   You play as resident 1 (owner of the seeded house). Dresser storage is for the CLIENT/resident player: the host player's own dresser stays vanilla (3 slots), by design.
5. Log checks (the console of each window, or the log file): host `[NET][DRESSER] host: extra dresser storage ON`, host `seeded furniture 0x1068`, client
   `[NET][DRESSER] client: the host offers dresser storage (enabled=1)`. If any is missing, stop: the feature is not active.
6. In the client, walk into your own house (resident 1) and stand next to a chest. Put 8 or more ordinary items in your pockets first (any normal items).

Record result for each check in the table at the end: PASS / FAIL / NOTE (what you saw).

## 1. The 3 x 9 grid: alignment and legibility (native resolution)

- Setup: client in own house, standing in front of a chest. Press **A** on the chest (empty chest: vanilla message with a "More storage" choice; a chest with 2-3 items:
  the take-out list with "More storage" before Cancel). Choose **More storage**.
- Expected: the gyroid-style inventory opens. NO gyroid portrait and NO gyroid dialogue box. One page with 27 cells in 3 rows of 9, the pocket grid below it.
- Look at: (a) rows/columns evenly spaced, none overlapping the window border (pitch is 22 px, icons 21 px); (b) empty cells look empty and distinct from filled ones;
  (c) every item icon recognisable; (d) the window frame is not clipped at the screen edge.
- Record: PASS if all four hold; FAIL + which one (take a screenshot with your usual tool).

## 2. Cursor movement across all slots and the pocket grid

- Setup: same overlay. Inputs: **D-pad / left stick** only.
- Steps: (1) from the first cell, press **right** 8 times: cursor should reach the 9th cell of row 1; (2) press **down** twice: row 3; (3) press **down** again: the cursor
  goes to the nearest column of the TOP pocket row; (4) **up**: back to the nearest dresser column; (5) walk the cursor through all 27 cells (row by row) and the 15 pockets.
- Expected: every one of the 27 cells and 15 pockets reachable, no skipped cell, no dead end, no cursor stuck. Note: the menu repeats a held direction after a short delay,
  and a quick tap works too. L/R do nothing (no paging).
- Record PASS / FAIL (+ the position where it misbehaved).

## 3. Taking and storing items, vanilla slots and extra slots

- Cells 1-3 (first three) are the chest's vanilla slots; cells 4-27 are the extra slots (host-stored).
- Steps (each: **A** on the cell or pocket -> small option window -> **A** on the first option "Put this in" / "Take it"):
  1. Put a pocket item into cell 1 (vanilla slot): item leaves the pocket, appears in cell 1.
  2. Put 3 more pocket items: they fill cells 2, 3, then the first extra slot (cell 4). Extra-slot changes appear after a short host round trip, not instantly.
  3. Take the item from cell 4 (extra) back into a pocket: it returns to the first free pocket.
  4. Take the item from cell 1 (vanilla): returns to a pocket.
  5. Close with **B**, leave the room, come back, reopen: the contents are exactly as you left them.
  6. Quit the client, stop the host, restart both (same commands): reopen: same contents.
  7. Count: pockets + the 27 cells must always add up to the same number of items you started with.
- Expected: no item ever disappears or duplicates; vanilla slots behave like the old vanilla chest.
- Record PASS / FAIL (+ the step number and what happened).

## 4. Legacy-pool migration and overflow claim

Uses the old (unreleased) shared 60-slot pool format. Do this on the same disposable folders, with the HOST STOPPED.

1. Stop host and client. In `bin_manual_host`, check `save\mp\dresser_store.dat` does not exist (rename the current one aside if it does; keep it).
2. Create a legacy pool of 60 items for resident 1:
   `python pc\tools\net_spike\make_legacy_dresser_pool.py --bin-dir pc\build64\bin_manual_host --slot 1 --count 60`
3. Start host and client as in section 0 (two chests seeded, both identical, both empty of extras).
4. Expected host log: the old file is renamed to `dresser_store.dat.v1.bak` (kept, never deleted), then at your first probe: `v1 MIGRATED resident 1 ... 48 item(s) -> 2 dresser(s) ... 12 to the legacy overflow`.
5. In the client open the FIRST chest (More storage): 24 extra cells filled in the legacy slot order (items 0x2480.. in ascending order), the second chest has the next 24. Nothing appears twice.
6. Overflow claim: in the first chest take out 2 extra items to pockets, close (B), then reopen the chest: 2 of the 12 overflow items now fill the free extras (host log: claim lines).
   The overflow is NOT shown anywhere until a dresser has free room; this is the known limitation (record it as NOTE, not FAIL, unless the claim does not happen).
7. Restart host and client: no second migration (log shows no `v1 MIGRATED`), contents identical.
- Record PASS / FAIL (+ counts).

## 5. Refusal feedback: is it understandable?

In the 3 x 9 layout the gyroid text box is gone, so "You don't have room." / "Got it!" no longer appear as text: feedback is the item option window plus sounds. This check is about whether a player understands it.

- Steps: (a) fill all 27 cells, then try to put one more pocket item: what do you see/hear?; (b) with all 15 pockets full, try to take an item from an extra cell:
  what do you see/hear?; (c) stand in front of a chest that holds extra items and try to PUSH/PULL/ROTATE it (hold **A** facing it, then move) and to PICK IT UP (**B**):
  expected the notice "Take the stored items out first" for about 3 seconds and the chest does not move; (d) take the extras out, then push, rotate and pick up: now allowed.
- Expected: nothing moves on a refusal; the refusal is noticeable (sound and/or notice text).
- Record for (a), (b): PASS if you understood WHY nothing happened without help, otherwise NOTE (suggest a wording); for (c)/(d): PASS/FAIL.

## 6. WARNING: identical dressers swapped by an OLD client stay bound to the CELL

- What is true: if a client WITHOUT this feature (an old build) swaps two identical dressers in place, the host cannot tell that from moving a keep item between them, so the extra
  storage stays with the CELL, not with the piece. The 3 vanilla slots do follow the piece. Nothing is lost or duplicated, but the extras may appear in the "other" chest.
- Mitigations in place: feature-enabled clients refuse to move/rotate/pick up a chest that holds extras (check 5c); when an old client moves pieces to NEW cells (not swapping in place)
  the storage follows its piece when the pieces are distinguishable.
- Do NOT test this on a save you care about. Optional observation only: join the host with the released v0.1.1 client, swap two seeded chests, and note which chest shows the extras.
  Expected result: they stay at the same cells. Record as NOTE; this is an accepted limitation, not a defect.

## Result table (copy and fill)

| # | Check | PASS / FAIL / NOTE | What I saw |
|---|---|---|---|
| 1 | 3 x 9 grid aligned, 27 slots legible | | |
| 2 | Cursor reaches all 27 cells + 15 pockets | | |
| 3 | Take/store vanilla + extra slots, persistence, conservation | | |
| 4 | Legacy migration (48 + 12 overflow), claim, no re-migration | | |
| 5a | Full-chest put refusal understandable | | |
| 5b | Pockets-full take refusal understandable | | |
| 5c | Move/rotate/pickup refused with the notice | | |
| 5d | Move/pickup allowed again after emptying | | |
| 6 | Old-client swap stays cell-bound (accepted limitation) | | |

Enable-by-default decision: only after checks 1-5 are PASS (6 is a documented limitation). The default stays OFF until you decide.
