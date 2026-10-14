# Animal Crossing Multiplayer — Beta 0.2.0

Update to Beta 0.1.1. This is an unofficial fan project, not affiliated with or endorsed by Nintendo, and it is still a beta: keep backups of your `save\` and `servers\` folders before playing and before every update. Most of what is new in this release was verified by automated tests (real game processes driven by scripts, and scripted test clients against a real host), **not yet by a person playing it**; see "Known limitations and what was not verified" below.

## What you need
Windows 10/11 (64-bit), a GPU/driver with OpenGL 3.3 core profile, and **your own legally obtained disc image** of *Animal Crossing* (USA, GAFE01_00) in the `rom\` folder. The packages contain no game data and nothing downloads any.

## Installing and updating
- **New install:** unzip `ACGC-PC-Multiplayer-0.2.0-win64-first-install.zip` into a writable folder, put your disc image in `rom\`, start `ACLauncher.exe`.
- **Existing installation:** `ACLauncher.exe` (or the question `Start Server.bat` asks at server start) offers this release and installs it after you say yes (SHA-256 checked, previous program files kept in `.update\backup-<version>`, rolled back on any problem). `ACGC-PC-Multiplayer-0.2.0-win64.zip` is the package the launcher downloads; you do not need it yourself.
- **What an update touches:** only `AnimalCrossing.exe`, `SDL2.dll`, `shaders\`, `licenses\` and the notes. Your `server_config.ini`, `settings.ini`, `keybindings.ini`, `rom\`, `save\`, `servers\` and logs are never changed by the updater.
- **Nothing updates the launcher or the server starter.** `ACLauncher.exe`, `Start Server.bat`, `server_starter.ps1` and `SERVER_SETUP.txt` come only with the first-install zip. They did not change in 0.2.0: an installation that already has the 0.1.1 launcher and starter needs nothing else.
- **Everybody should update.** Host and players should all run 0.2.0. A 0.1.1 game can still connect to a 0.2.0 host (and the other way round), but the new features need 0.2.0 on both sides; see "Playing with older versions" below.

### One-time step for existing 0.1.0 installations (only for the server startup update check)
The 0.1.0 launcher cannot be replaced by the updater and does not know the startup update check that came with 0.1.1. To get the newer launcher and starter once: open `ACGC-PC-Multiplayer-0.2.0-win64-first-install.zip` (do not extract it over your folder) and copy **only** `ACLauncher.exe`, `Start Server.bat`, `server_starter.ps1` and `SERVER_SETUP.txt` over the ones in your folder. **Never copy `server_config.ini`, `settings.ini`, `rom\`, `save\` or `servers\`.** Until you do this, nothing breaks: the 0.1.0 launcher and starter keep working with the 0.2.0 program files.

### Already in 0.1.1 (for anyone updating straight from 0.1.0)
- **Dedicated server console:** command answers use the same time / level / subsystem format as the server notices, wrap to the window width, and your half-typed command survives log output and window resizes.
- **Dedicated server startup update check:** with `check_updates = 1` in `server_config.ini`, `Start Server.bat` asks GitHub for a newer stable release and installs it only after you answer **Y**.
- **Host and play on the same PC:** `Start Server.bat` no longer refuses to start because your own game of the same folder is running.

### New files a 0.2.0 host writes (back up before updating)
A 0.2.0 host keeps some new data next to its town (`servers\<id>\` for a dedicated server, `save\mp\` for a game that hosts):
- `dresser_store.dat`: the extra dresser storage (format v3). A v2 file from a pre-release test build is converted automatically on the first start; the original is kept as `dresser_store.dat.v2.bak` and is never overwritten.
- `shop_upgrade.ini`: the shop-upgrade closure in progress, so a restarted host resumes it at the original deadline (removed again after the renewal was saved).
- `museum_donors.dat`: which guest donated which museum exhibit.
- Guest files (`*.gst`) of guests who have **unread letters waiting** are written in a newer format (v4); all other guest files keep the 0.1.1 format.

**Going back to 0.1.1 after running a 0.2.0 host is not recommended.** An older build cannot read a v4 guest file: it moves that file aside (`<id>.gst.corrupt-<time>`), stops admitting guests until the problem is fixed (residents are unaffected) and logs what to do. Nothing is deleted, and when 0.2.0 starts again it restores such a file by itself; but if the older build had loaded an older copy of that guest instead, the letters waiting in the newer file are lost. An older build also ignores the new files above (extra dresser items stay in `dresser_store.dat` but cannot be reached; guest donors read "former resident"). Restore your backup instead of downgrading.

## What changed since 0.1.1

### Dresser / wardrobe storage (on by default)
- Every dresser in a resident's house now has **27 storage slots** on the host (instead of the vanilla 3), shown in a new dresser screen with a 3 x 9 grid next to your pockets.
- Almost any item can be stored: furniture, clothing, umbrellas, tools, paper, fish and insects, carpets and wallpaper, fruit, music and more. Not storable: turnips (they would dodge spoiling) and the Wisp's spirits.
- **Grab** an item to move it between slots, between a slot and a pocket, or to swap two items; every move is a host transaction (journalled and replay-safe, so a lost connection or a host restart neither loses nor duplicates an item).
- A dresser holding stored items can be **moved and rotated** (its storage follows it); it cannot be picked up until it is empty. Items that were in the old 3 vanilla slots are moved to your pockets the first time you open the new screen (or stay put, with a notice, while your pockets are full).
- Host switch: `AC_MP_DRESSER_STORAGE=0` in the host's environment turns the feature off (the vanilla 3 slots remain). The host player's own dresser on a game that hosts keeps the vanilla 3 slots.

### Nook's shop upgrade: an announced 5-minute closure
- When the town qualifies for the next shop, the next player who talks to Tom Nook (a resident, a guest or the host's own player) hears that the shop will close for an upgrade. The shop then **closes for 5 minutes for everybody**: players inside are shown out, purchases are refused, the doors say how long it will take, and every screen shows a notice. After 5 real minutes the host renews the shop with the game's own upgrade (new building, new stock, the reopening leaflet, guests included) and tells everyone it is open again.
- On a multiplayer host this replaces the vanilla two-day schedule. `AC_MP_SHOP_UPGRADE=0` on the host restores the old behaviour.
- Restart-safe: the closure deadline is kept in `shop_upgrade.ini`; a host restarted during the closure resumes it.
- Dedicated console: `shop-upgrade` shows the state; `shop-upgrade start confirm` starts a pending upgrade by hand (for example when every player still runs an older version that cannot hear Nook's announcement).
- Clients receive the closed / pending / reopened state, the renewed building and its cleaned-up footprint; a player standing in front of the shop gets the new building and door after the renewal.
- Fixed: on a game that hosts, a host player who was busy (in a menu or a conversation) during the renewal could cost the town its upgrade.

### House upgrades through Tom Nook for residents on a client
- Residents who play on a client now get Nook's vanilla offers: a bigger house (with the roof colour choice), the basement and the statue, plus the "your house is done" announcement with the new loan.
- Every change is made by the host. The host builds a due order by itself once nobody is in that house, without a restart; the new exterior appears at the next town load.
- Painting the roof at Nook is still not available on a client.

### Guests (players without a house)
- **Museum donations:** guests can donate fossils, paintings, fish and insects to Blathers. The host remembers which guest donated what (`museum_donors.dat`), so plaques and Blathers name the guest (up to 8 different guests; more read "former resident"). When a guest buys a house, its exhibits are credited to the new resident.
- **Letters with a gift to the museum** now work for residents and guests on a client (the gift is donated, appraised or returned as in the original game); guests receive the museum's reply letters in their guest mailbox.
- **Guest mailbox:** a guest's letters wait on the host (10 slots). Ask the post office clerk: **"Check my mail."** opens the normal mailbox screen. Guests can also write letters to residents (5 per day by default, host setting `AC_MP_GUEST_MAIL_PER_DAY`). When a guest buys a house, its letters move into the new house mailbox.
- **Letters to guests:** the address book lists the guests of the town, so residents and other guests can write to them; villagers send guests their birthday, Christmas, Valentine's Day and goodbye letters, villager replies, and the museum completion letter.
- **Able Sisters:** guests see the shop's designs and can trade design pages with Mable. A trade that the host refuses is rolled back on your side, so a design can no longer be lost in an exchange; Mable declines a trade while the previous one is still being confirmed.
- **Villager outfits** that change because of a design trade are now the same for everybody.
- Host switches (all on by default; `=0` turns one off): `AC_MP_GUEST_MAIL` (guest mailbox and letters to guests), `AC_MP_GUEST_PAGES` (designs for guests), `AC_MP_MUSEUM_MAIL` (letters with a gift to the museum), `AC_MP_MUSEUM_DONORS` (guest donor names).

### Wildlife
- **Ants** are visible to everybody and can be caught.
- **Spirits** (the Wisp's hunt): several spirits stack in one pocket slot, a spirit someone caught no longer reappears elsewhere, and accepting the Wisp's request starts the hunt for everybody.
- **Hidden bugs** revealed with a tool (mole cricket, bagworm, spider, pill bug) come out for every player, and the cockroach's pose matches on every screen.
- **Bee swarms**, the **soccer ball / basketball** (now kept by the host, so everybody sees one ball), the item drop of a shaken or chopped tree, and Nook's wallpaper / carpet preview are shown to the other players.

### Player-vs-player tool hits
- Hitting another player with the **axe** makes them trip and slide a short way; the **shovel** gives a short shocked reaction; the **net** a quick flinch and the net stops on them. The other players see the reaction, the face and hear the sound.
- Reactions only: no items, Bells, pockets or field change, no damage. Always on, for residents and guests alike, in the town only; a player who is talking, in a menu, fishing, at a door or in a cutscene is never interrupted.
- Fixed on the way: an axe swing at another player no longer bounces off and wears the axe, and the shovel no longer digs a hole under a player.

### Other fixes
- **Blathers** said "a former resident already donated it" and acted as if he handed the item back after a donation that had actually succeeded (residents on a client were affected too). The exhibit and your pockets were correct; only the dialogue was wrong. Fixed.
- **House mailbox letters vanished every time a host started** (residents' letters too): a startup clean-up of the original game cleared every house mailbox of the loaded town. Fixed.
- Smaller fixes for a game that hosts: villagers no longer try to start a conversation with the host player while the host sits in the Esc menu, villagers keep walking for the clients while the host player has a menu open, Nook's shop renewal no longer blocks purchases on a host that stands outdoors, a fishing-rod safety timer that never released the player, and a client's Nook Work Mode reward that did not fit in the wallet.

## Playing with older versions
The network protocol number stays 8, so 0.1.1 and 0.2.0 games can connect to each other. New messages and transaction types were added in place (message ids 77-81, 83-87 and 90-92; transaction kinds 18, 19 and 20; town services 6, 7 and 8; refusal reasons 37, 38, 42, 43 and 45-47). An older game ignores the messages it does not know. **The new features need 0.2.0 on the host AND on the client.**
- **A 0.1.1 player on a 0.2.0 host:** keeps playing as before, without the new features: the vanilla 3 dresser slots only (stored extras stay safe on the host; if a 0.1.1 player swaps two identical dressers, the extra storage may stay with the spot rather than the piece), no guest mailbox, no house upgrades through Nook, no hits from or to other players. During a shop-upgrade closure the shop looks open to a 0.1.1 player but its purchases are refused, and it never hears Nook's announcement (the host operator can start the upgrade with `shop-upgrade start confirm`).
- **A 0.2.0 player on a 0.1.1 host:** the game degrades gracefully: the new features are simply not offered (vanilla dresser, no guest mailbox, plain Nook greeting, the local ball), and the post office hands a letter with a gift to the museum back to you (as in 0.1.1) instead of sending something the old host does not know. For about 15 seconds after joining, moving a dresser or another storage piece is refused while the game asks the host whether it supports extra storage. Guest museum donations and guest letters are refused by a 0.1.1 host and the item or letter stays with you. None of this disconnects you.
- How this was checked: a 0.2.0 game was connected to the real released 0.1.1 program for the ball fallback, for the old game ignoring the new messages, and for a museum letter with a gift (the letter is refused locally, the old host records no protocol error and the player stays connected; a build without this safeguard did trigger the old host's protocol-error counter). The other combinations were checked by reading the code of both versions (every new request is either never sent to a 0.1.1 host or answered by it with an ordinary refusal) and by tests with scripted clients.

## Known limitations and what was not verified
- **Not verified by a person:** almost everything in "What changed": the dresser screen (an early version was looked at; the final look was not), Nook's shop-upgrade and house-upgrade texts, the post office "Check my mail." row and the address book with guests, plaques naming a guest, Mable's trades, the player-vs-player animations, slide and sounds, and the new wildlife on screen. The automated tests drove real host and client processes (including real Nook, Blathers and post-office conversations through scripted button presses) and checked logs, saves and network traffic; they do not judge how anything looks or sounds. Also not verified: a real-ROM playthrough by a person, a clean-machine install and real public (internet) multiplayer.
- **Event NPCs are still not shared** (Redd, Saharah, Joan, the Artist, the Designer, the Gypsy, Kapp'n, the Wisp's rewards on a client): two players can buy the same unique stock. Island / Kapp'n sync, the fishing tournament and bug-off, text chat and teleporting are still missing.
- Gyroids stacked outside houses and dancing out of sync were reported once and could still not be reproduced or fixed.
- A dedicated host that runs for days does not roll the day over fully (weather, villagers moving in or out, daily mail and growth only happen at start or when a scene loads); Nook's house construction is the exception and runs on its own.
- Villagers remember only 7 players: with many guests a villager can forget one (and then sends it no birthday card or goodbye letter).
- A host crash in the short moment between saving the town and saving a guest's file can give that guest back a donated item or duplicate a villager's reply letter (never an exhibit).
- Player hits are judged from the attacker's view of the other player (the host allows a small distance difference), and the attacker's swing looks like a miss even when the host turns the hit down.
- If the game of the folder is a process the updater cannot inspect (for example one started as administrator), the starter refuses to update; the launcher itself can then only rely on its rollback.
- The checksum detects corruption, **not tampering**; there is no signature and the executables are unsigned, so Windows SmartScreen / antivirus may warn.
- Some diagnostic environment variables (`PC_*_DIAG`, `PC_FORCE_TALK_*`) are still read; they do nothing unless set. The test-hook mechanism is compiled out of this build.
- **Legal status (not cleared, unchanged):** the executable is built from a public decompilation and contains compiled code and data tables derived from the original game and from SDK headers carrying SGI/Nintendo copyright notices; supplying your own disc image does not settle the licensing of that compiled code. Read `licenses\THIRD_PARTY_NOTICES.txt` and `licenses\THIRD_PARTY_NOTICES.md`; nothing here is legal advice.

Full status: https://github.com/vsimochi/ACGC-PC-Multiplayer/blob/v0.2.0/TODO.md . Project: https://github.com/vsimochi/ACGC-PC-Multiplayer
