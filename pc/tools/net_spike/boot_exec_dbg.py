"""(copy of the boot-crash diagnostic tool; symbol map from env BOOT_TRACE_SYMS = `nm -n` of the exe under test)
Minimal ctypes debugger: launches AnimalCrossing.exe (DEBUG_ONLY_THIS_PROCESS), records first-chance 0xC0000005,
dumps registers + StackWalk64 (pdata unwind) of faulting thread + RIP/stack walk of all threads, symbolized via nm map.
Usage: python avdbg.py <bin_dir> <log> <report> <stopfile-regex-on-log> args..."""
import ctypes, ctypes.wintypes as W, sys, os, re, bisect, time, threading, subprocess

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
BC = os.path.dirname(os.path.abspath(__file__))
SYMS = []
for ln in open(os.environ.get("BOOT_TRACE_SYMS") or os.path.join(BC, "syms.txt")):
    p = ln.split()
    if len(p) >= 3 and not p[2].startswith("."):
        SYMS.append((int(p[0], 16), p[2]))
SYMS.sort(); SADDR = [s[0] for s in SYMS]
def sym(a):
    if 0x400000 <= a < 0x2605000:
        i = bisect.bisect_right(SADDR, a) - 1
        if i >= 0: return "%s+0x%x" % (SYMS[i][1], a - SYMS[i][0])
    return "?"

class EXCEPTION_RECORD(ctypes.Structure):
    _fields_ = [("ExceptionCode", W.DWORD), ("ExceptionFlags", W.DWORD), ("ExceptionRecord", ctypes.c_void_p),
                ("ExceptionAddress", ctypes.c_void_p), ("NumberParameters", W.DWORD), ("ExceptionInformation", ctypes.c_uint64 * 15)]
class EXCEPTION_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("ExceptionRecord", EXCEPTION_RECORD), ("dwFirstChance", W.DWORD)]
class CREATE_THREAD_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("hThread", W.HANDLE), ("lpThreadLocalBase", ctypes.c_void_p), ("lpStartAddress", ctypes.c_void_p)]
class CREATE_PROCESS_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("hFile", W.HANDLE), ("hProcess", W.HANDLE), ("hThread", W.HANDLE), ("lpBaseOfImage", ctypes.c_void_p),
                ("dwDebugInfoFileOffset", W.DWORD), ("nDebugInfoSize", W.DWORD), ("lpThreadLocalBase", ctypes.c_void_p),
                ("lpStartAddress", ctypes.c_void_p), ("lpImageName", ctypes.c_void_p), ("fUnicode", W.WORD)]
class LOAD_DLL_DEBUG_INFO(ctypes.Structure):
    _fields_ = [("hFile", W.HANDLE), ("lpBaseOfDll", ctypes.c_void_p), ("dwDebugInfoFileOffset", W.DWORD),
                ("nDebugInfoSize", W.DWORD), ("lpImageName", ctypes.c_void_p), ("fUnicode", W.WORD)]
class U(ctypes.Union):
    _fields_ = [("Exception", EXCEPTION_DEBUG_INFO), ("CreateThread", CREATE_THREAD_DEBUG_INFO),
                ("CreateProcessInfo", CREATE_PROCESS_DEBUG_INFO), ("LoadDll", LOAD_DLL_DEBUG_INFO), ("pad", ctypes.c_byte * 160)]
class DEBUG_EVENT(ctypes.Structure):
    _fields_ = [("dwDebugEventCode", W.DWORD), ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD), ("u", U)]

# x64 CONTEXT (1232 bytes, 16-aligned); we use raw buffer + offsets
CTX_SIZE = 1232
OFF = dict(ContextFlags=0x30, Rax=0x78, Rcx=0x80, Rdx=0x88, Rbx=0x90, Rsp=0x98, Rbp=0xA0, Rsi=0xA8, Rdi=0xB0,
           R8=0xB8, R9=0xC0, R10=0xC8, R11=0xD0, R12=0xD8, R13=0xE0, R14=0xE8, R15=0xF0, Rip=0xF8)
CONTEXT_FULL = 0x10000B
def get_ctx(hThread):
    raw = ctypes.create_string_buffer(CTX_SIZE + 16)
    addr = (ctypes.addressof(raw) + 15) & ~15
    ctypes.c_uint32.from_address(addr + OFF["ContextFlags"]).value = CONTEXT_FULL
    if not k32.GetThreadContext(W.HANDLE(hThread), ctypes.c_void_p(addr)):
        return None, None, raw
    regs = {k: ctypes.c_uint64.from_address(addr + o).value for k, o in OFF.items() if k != "ContextFlags"}
    return regs, addr, raw

class ADDRESS64(ctypes.Structure):
    _fields_ = [("Offset", ctypes.c_uint64), ("Segment", W.WORD), ("Mode", ctypes.c_int)]
class KDHELP64(ctypes.Structure):
    _fields_ = [("pad", ctypes.c_uint64 * 20)]
class STACKFRAME64(ctypes.Structure):
    _fields_ = [("AddrPC", ADDRESS64), ("AddrReturn", ADDRESS64), ("AddrFrame", ADDRESS64), ("AddrStack", ADDRESS64),
                ("AddrBStore", ADDRESS64), ("FuncTableEntry", ctypes.c_void_p), ("Params", ctypes.c_uint64 * 4),
                ("Far", W.BOOL), ("Virtual", W.BOOL), ("Reserved", ctypes.c_uint64 * 3), ("KdHelp", KDHELP64)]
dbghelp.StackWalk64.argtypes = [W.DWORD, W.HANDLE, W.HANDLE, ctypes.POINTER(STACKFRAME64), ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
dbghelp.SymFunctionTableAccess64.restype = ctypes.c_void_p
dbghelp.SymFunctionTableAccess64.argtypes = [W.HANDLE, ctypes.c_uint64]
dbghelp.SymGetModuleBase64.restype = ctypes.c_uint64
dbghelp.SymGetModuleBase64.argtypes = [W.HANDLE, ctypes.c_uint64]

def walk(hProc, hThread, regs, ctxaddr, maxf=40):
    out = []
    sf = STACKFRAME64()
    sf.AddrPC.Offset = regs["Rip"]; sf.AddrPC.Mode = 3
    sf.AddrFrame.Offset = regs["Rbp"]; sf.AddrFrame.Mode = 3
    sf.AddrStack.Offset = regs["Rsp"]; sf.AddrStack.Mode = 3
    FTA = ctypes.cast(dbghelp.SymFunctionTableAccess64, ctypes.c_void_p)
    GMB = ctypes.cast(dbghelp.SymGetModuleBase64, ctypes.c_void_p)
    for _ in range(maxf):
        if not dbghelp.StackWalk64(0x8664, hProc, W.HANDLE(hThread), ctypes.byref(sf), ctypes.c_void_p(ctxaddr), None, FTA, GMB, None):
            break
        if sf.AddrPC.Offset == 0: break
        out.append("  0x%x %s" % (sf.AddrPC.Offset, sym(sf.AddrPC.Offset)))
    return out

def read(hProc, addr, n):
    buf = ctypes.create_string_buffer(n); got = ctypes.c_size_t()
    k32.ReadProcessMemory(hProc, ctypes.c_void_p(addr), buf, n, ctypes.byref(got))
    return buf.raw[:got.value]

def stackscan(hProc, rsp, n=0x3000):
    data = read(hProc, rsp, n); out = []
    for i in range(0, len(data) - 7, 8):
        v = int.from_bytes(data[i:i+8], "little")
        if 0x401000 <= v < 0x401000 + 0x4243a0:
            out.append("  [rsp+0x%x] 0x%x %s" % (i, v, sym(v)))
    return out[:60]

class STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR), ("dwX", W.DWORD),
                ("dwY", W.DWORD), ("dwXSize", W.DWORD), ("dwYSize", W.DWORD), ("dwXCountChars", W.DWORD), ("dwYCountChars", W.DWORD),
                ("dwFillAttribute", W.DWORD), ("dwFlags", W.DWORD), ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]
class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE), ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]
class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", W.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", W.BOOL)]

def main():
    bin_dir, log, report, stop_rx = sys.argv[1:5]; args = sys.argv[5:]
    exe = os.path.join(bin_dir, "AnimalCrossing.exe")
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    k32.CreateFileW.restype = W.HANDLE
    hlog = k32.CreateFileW(log, 0x40000000, 3, ctypes.byref(sa), 2, 0x80, None)
    si = STARTUPINFO(); si.cb = ctypes.sizeof(si); si.dwFlags = 0x100; si.hStdOutput = hlog; si.hStdError = hlog
    pi = PROCESS_INFORMATION()
    cmd = subprocess.list2cmdline([exe] + args)
    if not k32.CreateProcessW(exe, ctypes.create_unicode_buffer(cmd), None, None, True, 0x2, None, bin_dir,
                              ctypes.byref(si), ctypes.byref(pi)):
        print("CreateProcess failed", ctypes.get_last_error()); return 3
    hProc = pi.hProcess
    threads = {pi.dwThreadId: pi.hThread}
    rep = open(report, "w")
    stop = {"flag": False}
    rx = re.compile(stop_rx)
    def watcher():
        t0 = time.time()
        while not stop["flag"] and time.time() - t0 < 90:
            try:
                if rx.search(open(log, "rb").read().decode("utf-8", "replace")):
                    break
            except OSError: pass
            time.sleep(0.2)
        stop["flag"] = True
        k32.TerminateProcess(hProc, 1)
    threading.Thread(target=watcher, daemon=True).start()
    ev = DEBUG_EVENT(); outcome = "exit"
    while True:
        if not k32.WaitForDebugEvent(ctypes.byref(ev), 1000):
            continue
        code = ev.dwDebugEventCode; cont = 0x00010002  # DBG_CONTINUE
        if code == 3:
            cpi = ev.u.CreateProcessInfo
            if cpi.hFile: k32.CloseHandle(W.HANDLE(cpi.hFile))
            dbghelp.SymInitialize(hProc, None, True)
        elif code == 2:
            threads[ev.dwThreadId] = ev.u.CreateThread.hThread
            rep.write("thread %d created start=0x%x %s\n" % (ev.dwThreadId, ev.u.CreateThread.lpStartAddress or 0, sym(ev.u.CreateThread.lpStartAddress or 0)))
        elif code == 4:
            threads.pop(ev.dwThreadId, None)
        elif code == 6:
            if ev.u.LoadDll.hFile: k32.CloseHandle(W.HANDLE(ev.u.LoadDll.hFile))
        elif code == 1:
            er = ev.u.Exception.ExceptionRecord
            if er.ExceptionCode == 0x80000003:
                pass
            elif er.ExceptionCode == 0xC0000005:
                cont = 0x80010001
                if ev.u.Exception.dwFirstChance:
                    outcome = "AV"
                    rep.write("=== FIRST-CHANCE ACCESS VIOLATION tid=%d at 0x%x %s; %s address 0x%x\n" % (
                        ev.dwThreadId, er.ExceptionAddress or 0, sym(er.ExceptionAddress or 0),
                        {0: "READ", 1: "WRITE", 8: "EXEC"}.get(er.ExceptionInformation[0], "?"), er.ExceptionInformation[1]))
                    for tid, h in list(threads.items()):
                        regs, ca, raw = get_ctx(h)
                        if regs is None: rep.write("tid %d: no ctx\n" % tid); continue
                        rep.write("--- thread %d%s RIP=0x%x %s\n" % (tid, " (FAULTING)" if tid == ev.dwThreadId else "", regs["Rip"], sym(regs["Rip"])))
                        if tid == ev.dwThreadId:
                            rep.write("  " + " ".join("%s=%x" % (k, v) for k, v in regs.items()) + "\n")
                            rep.write("  code@rip-32: " + read(hProc, regs["Rip"] - 32, 48).hex() + "\n")
                        rep.write("\n".join(walk(hProc, h, regs, ca)) + "\n")
                        if tid == ev.dwThreadId:
                            rep.write("  stack scan:\n" + "\n".join(stackscan(hProc, regs["Rsp"])) + "\n")
                    rep.flush()
                    stop["flag"] = True
                    k32.TerminateProcess(hProc, 0xC0000005)
            else:
                cont = 0x80010001
        elif code == 5:
            break
        k32.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, cont)
    stop["flag"] = True
    k32.CloseHandle(W.HANDLE(hlog))
    rep.write("OUTCOME %s\n" % outcome); rep.close()
    print(outcome)
    return 0

if __name__ == "__main__":
    sys.exit(main())
