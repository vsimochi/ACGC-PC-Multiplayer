"""Breakpoint tracer: INT3 at the pc_net_game_poll call to pcnetgame_is_real_player_actor (address from env BOOT_TRACE_BP);
logs actor ptr (rcx) and gamePT->exec symbol per movement-send tick, until bootstrap bound/exit/AV."""
import sys, os, time, ctypes, ctypes.wintypes as W, subprocess, re, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot_exec_dbg as D
k32 = D.k32
BP = int(os.environ["BOOT_TRACE_BP"], 16); GAMEPT = int(os.environ["BOOT_TRACE_GAMEPT"], 16)  # call site of pcnetgame_is_real_player_actor in pc_net_game_poll / address of gamePT (from nm/objdump of the exe under test)
def setctx(h, addr): return k32.SetThreadContext(W.HANDLE(h), ctypes.c_void_p(addr))
def wr(hp, a, b):
    n = ctypes.c_size_t(); k32.WriteProcessMemory(hp, ctypes.c_void_p(a), b, len(b), ctypes.byref(n)); k32.FlushInstructionCache(hp, None, 0)
def main():
    bin_dir, log, out = sys.argv[1:4]; args = sys.argv[4:]
    exe = os.path.join(bin_dir, "AnimalCrossing.exe")
    sa = D.SECURITY_ATTRIBUTES(ctypes.sizeof(D.SECURITY_ATTRIBUTES), None, True)
    k32.CreateFileW.restype = W.HANDLE
    hlog = k32.CreateFileW(log, 0x40000000, 3, ctypes.byref(sa), 2, 0x80, None)
    si = D.STARTUPINFO(); si.cb = ctypes.sizeof(si); si.dwFlags = 0x100; si.hStdOutput = hlog; si.hStdError = hlog
    pi = D.PROCESS_INFORMATION()
    k32.CreateProcessW(exe, ctypes.create_unicode_buffer(subprocess.list2cmdline([exe] + args)), None, None, True, 0x2, None, bin_dir, ctypes.byref(si), ctypes.byref(pi))
    hp = pi.hProcess; threads = {pi.dwThreadId: pi.hThread}; f = open(out, "w"); t0 = time.time()
    rx = re.compile(r"resident bound, transitioning to town"); stop = {"f": False}
    def watcher():
        while not stop["f"] and time.time() - t0 < 90:
            try:
                if rx.search(open(log, "rb").read().decode("utf-8", "replace")): time.sleep(0.5); break
            except OSError: pass
            time.sleep(0.2)
        k32.TerminateProcess(hp, 1)
    threading.Thread(target=watcher, daemon=True).start()
    orig = None; rearm = False; ev = D.DEBUG_EVENT(); last = None; hits = 0
    while True:
        if not k32.WaitForDebugEvent(ctypes.byref(ev), 1000): continue
        c = ev.dwDebugEventCode; cont = 0x00010002
        if c == 3:
            orig = D.read(hp, BP, 1); wr(hp, BP, b"\xcc")
        elif c == 2: threads[ev.dwThreadId] = ev.u.CreateThread.hThread
        elif c == 1:
            er = ev.u.Exception.ExceptionRecord; code = er.ExceptionCode
            if code == 0x80000003 and (er.ExceptionAddress or 0) == BP:
                h = threads[ev.dwThreadId]; regs, ca, raw = D.get_ctx(h)
                gp = int.from_bytes(D.read(hp, GAMEPT, 8), "little")
                ex = int.from_bytes(D.read(hp, gp + 8, 8), "little") if gp else 0
                hits += 1
                key = (D.sym(ex), regs["Rcx"])
                if key != last:
                    log_tail = ""
                    try:
                        ls = [l for l in open(log, "rb").read().decode("utf-8", "replace").splitlines() if l.startswith("[PC]") or "SFS" in l]
                        log_tail = ls[-1][:70] if ls else ""
                    except OSError: pass
                    f.write("t=%.3f hit#%d gamePT=0x%x exec=%s local(rcx)=0x%x | lastlog: %s\n" % (time.time() - t0, hits, gp, key[0], regs["Rcx"], log_tail)); f.flush()
                    last = key
                wr(hp, BP, orig)
                ctypes.c_uint64.from_address(ca + D.OFF["Rip"]).value = BP
                eflags_off = 0x44
                ctypes.c_uint32.from_address(ca + eflags_off).value |= 0x100
                setctx(h, ca); rearm = True
            elif code == 0x80000004 and rearm:
                wr(hp, BP, b"\xcc"); rearm = False
            elif code == 0x80000003: pass
            elif code == 0xC0000005 and ev.u.Exception.dwFirstChance:
                f.write("AV at 0x%x %s addr 0x%x\n" % (er.ExceptionAddress or 0, D.sym(er.ExceptionAddress or 0), er.ExceptionInformation[1])); f.flush()
                k32.TerminateProcess(hp, 0xC0000005); cont = 0x80010001
            else: cont = 0x80010001
        elif c == 5: break
        k32.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, cont)
    stop["f"] = True; f.write("hits=%d\n" % hits); f.close(); k32.CloseHandle(W.HANDLE(hlog))
main()
