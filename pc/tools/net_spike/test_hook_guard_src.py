#!/usr/bin/env python3
"""test_hook_guard_src.py - source audit: every test-only hook (state-injecting flag / env switch) lives inside the PC_NET_TEST_HOOKS guard AND needs AC_TEST_HOOKS=1 at runtime.
No game process is started. Usage: python test_hook_guard_src.py"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.abspath(os.path.join(HERE, "..", ".."))
results = []


def ck(d, c):
    results.append((d, bool(c)))
    print(("PASS - " if c else "FAIL - ") + d)


def rd(*p):
    return open(os.path.join(PC, *p), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


def guarded_spans(src):
    """[(start, end)] of every #ifdef PC_NET_TEST_HOOKS ... (#else|#endif) region (nesting-aware, depth of the guard only)."""
    spans, stack, start = [], [], None
    for m in re.finditer(r"^#\s*(if|ifdef|ifndef|else|elif|endif)\b(.*)$", src, re.M):
        kind, rest = m.group(1), m.group(2)
        if kind in ("if", "ifdef", "ifndef"):
            is_g = kind == "ifdef" and "PC_NET_TEST_HOOKS" in rest
            stack.append(is_g)
            if is_g:
                start = m.end()
        elif kind in ("else", "elif") and stack and stack[-1] and start is not None:
            spans.append((start, m.start()))
            start = None
        elif kind == "endif" and stack:
            if stack.pop() and start is not None:
                spans.append((start, m.start()))
                start = None
    return spans


def inside(src, needle, spans):
    return all(any(a <= m.start() < b for a, b in spans) for m in re.finditer(re.escape(needle), src)) and needle in src


mn, ng, mc, rl = rd("src", "pc_main.c"), rd("src", "pc_net_game.c"), rd("src", "pc_m_card.c"), rd("src", "pc_relaunch.c")
cm = rd("CMakeLists.txt")
sm, sg, sc = guarded_spans(mn), guarded_spans(ng), guarded_spans(mc)

ck("CMake: option PC_TEST_HOOKS (default ON for the dev build, release builds configure it OFF) defines PC_NET_TEST_HOOKS for exactly the hook translation units",
   'option(PC_TEST_HOOKS' in cm and "ON)" in cm.split('option(PC_TEST_HOOKS')[1].split("\n")[0] and "PC_NET_TEST_HOOKS" in cm and "RELEASE BUILDS MUST CONFIGURE -DPC_TEST_HOOKS=OFF" in cm)
for flag in ('"--house-buy-test"', '"--nook-test"', '"--house-test-host-edit"', '"--d3-test-wallet-add"', '"--promote-fault"', '"--mail-test-force-delivery"', '"--txn-fault="'):
    needle = 'argv[i], ' + flag if flag != '"--txn-fault="' else 'argv[i], "--txn-fault="'
    ck("pc_main.c: the parse branch of %s is inside #ifdef PC_NET_TEST_HOOKS" % flag, inside(mn, needle, sm))
ck("pc_main.c: every other --mail-test-* parse branch is inside the guard", all(inside(mn, 'argv[i], "--mail-test-%s' % n, sm) for n in ("send=", "poke-museum=", "take", "seed-mailbox=", "seed-reply=")))
ck("pc_main.c: a hook flag without AC_TEST_HOOKS=1 is REFUSED with exit 2 (pc_is_test_hook_flag + pc_test_hooks_enabled) BEFORE the --help / parse chain; the env switches go through pc_test_hook_getenv",
   "pc_is_test_hook_flag(argv[i]) && !pc_test_hooks_enabled()" in mn and mn.index("pc_is_test_hook_flag(argv[i]) && !pc_test_hooks_enabled()") < mn.index('strcmp(argv[i], "--help") == 0')
   and 'getenv("AC_RELAUNCH_DRYRUN")' not in mn.replace('pc_test_hook_getenv("AC_RELAUNCH_DRYRUN")', "") and 'getenv("AC_TOWN_NO_MSGBOX")' not in mn.replace('pc_test_hook_getenv("AC_TOWN_NO_MSGBOX")', "")
   and 'getenv("AC_RELAUNCH_DRYRUN")' not in rl.replace('pc_test_hook_getenv("AC_RELAUNCH_DRYRUN")', ""))
ck("pc_main.c: pc_test_hooks_enabled() is false without PC_NET_TEST_HOOKS and requires the env value exactly `1`", 'strcmp(e, "1") == 0' in mn and inside(mn, 'getenv("AC_TEST_HOOKS")', sm))
ck("pc_main.c --help: the hook flags are listed only on the `Test builds only` line (no per-flag help entries remain)", "Test builds only" in mn and "  --house-buy-test H|auto  Client-only" not in mn and "  --nook-test SPEC    Client-only" not in mn)
for fn in ("pcnetgame_house_test_host_edit", "pcnetgame_txn_fault_fire", "pcnetgame_mail_test_force_delivery", "pcnetgame_run_house_buy_test_hook", "pcnetgame_run_nook_test_hook"):
    sig = [m for m in re.finditer(r"^static [a-z]+ %s\(" % fn, ng, re.M)]
    ck("pc_net_game.c: %s is defined inside the guard (the #else branch is an empty stub)" % fn, len(sig) == 2 and any(a <= sig[0].start() < b for a, b in sg) and not any(a <= sig[1].start() < b for a, b in sg))
ck("pc_net_game.c: both --d3-test-wallet-add blocks and the client handoff crash hook are inside the guard", inside(ng, "--d3-test-wallet-add %d: local wallet", sg) and inside(ng, "--d3-test-wallet-add-late %d: local wallet", sg)
   and inside(ng, 'pc_test_hook_getenv("AC_PROMOTE_CLIENT_CRASH")', sg))
ck("pc_net_game.c / pc_m_card.c: every g_pc_promote_fault use is inside the guard", all(inside(ng, n, sg) for n in ("g_pc_promote_fault == 2", "g_pc_promote_fault == 3")) and all(inside(mc, n, sc) for n in ("g_pc_promote_fault == 1", "g_pc_promote_fault == 4")))
ck("pc_platform.h declares g_pc_promote_fault only under the guard", re.search(r"#ifdef PC_NET_TEST_HOOKS\nextern int\s+g_pc_promote_fault;", rd("include", "pc_platform.h")) is not None)
ok = all(ok_ for _d, ok_ in results)
print("-" * 60)
print("%d/%d checks passed" % (sum(1 for _d, c in results if c), len(results)))
sys.exit(0 if ok else 1)
