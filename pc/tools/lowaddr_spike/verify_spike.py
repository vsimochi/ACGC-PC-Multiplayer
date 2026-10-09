#!/usr/bin/env python3
"""verify_spike.py - experimental: does the "compile _GBI_STATIC_PTR data as C++" idea produce correct data?

Run from anywhere with MSYS2 UCRT64 on the machine (uses C:/msys64/ucrt64/bin):
    python pc/tools/lowaddr_spike/verify_spike.py

For every file in pc/cmake/GbiCxxSpike.cmake (DATA list) it
  1. compiles the file as C++ (the spike)                       -> build/cxx/*.o
  2. compiles it as C with '_GBI_STATIC_PTR(s)=0' (reference)   -> build/ref/*.o   (when the file allows it)
  3. links a harness (lowaddr_spike.cpp + pc_lowaddr.c + spike objects + generated stubs) with the
     low-address linker flags, runs it, and dumps every spike data symbol after startup initialization;
  4. links the same harness against the reference objects and dumps them too;
  5. checks: (a) every word that differs between spike and reference is a pointer-sized value that equals
     the address of a real symbol (+offset inside it) in the linked image, and the reference word was 0;
     (b) every non-reference file's image-range words resolve to a real symbol;
  6. runs the allocation/DLL/headroom harness several times and measures startup cost.
Output: a summary on stdout and build/report.txt.
"""
import os, re, subprocess, sys, time, glob, statistics

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')).replace('\\', '/')
BIN = 'C:/msys64/ucrt64/bin'
os.environ['PATH'] = BIN + os.pathsep + os.environ['PATH']
BUILD = ROOT + '/pc/build64/lowaddr_spike'
HERE = ROOT + '/pc/tools/lowaddr_spike'

INC = ['-I%s/include' % ROOT, '-I%s/src' % ROOT, '-I%s' % ROOT, '-I%s/pc/include' % ROOT,
       '-I%s/pc/lib/glad/include' % ROOT, '-IC:/msys64/ucrt64/include/SDL2', '-I' + BUILD]
DEF = ['-DTARGET_PC', '-DVERSION=0', '-DF3DEX_GBI_2', '-DNDEBUG', '-DBUGFIXES', '-D_LANGUAGE_C',
       '-DPC_ENHANCEMENTS', '-DKEYBOARD_TYPING', '-DPC_LOW_ADDRESS_64']
COMMON = ['-O2', '-fno-strict-aliasing', '-fwrapv', '-w', '-fpermissive']
LOWLINK = ['-Wl,--image-base=0x400000', '-Wl,--disable-dynamicbase', '-Wl,--disable-high-entropy-va']

report = []
def say(*a):
    s = ' '.join(str(x) for x in a)
    print(s); report.append(s)

def run(cmd, check=True, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        print('CMD FAILED:', ' '.join(cmd)); print(r.stdout[-2000:]); print(r.stderr[-3000:]); sys.exit(1)
    return r

def spike_files():
    txt = open(ROOT + '/pc/cmake/GbiCxxSpike.cmake', encoding='utf-8').read()
    m = re.search(r'PC_GBI_CXX_SPIKE_DATA_FILES(.*?)^\)', txt, re.S | re.M)
    out = []
    for l in m.group(1).splitlines():
        l = l.split('#')[0].strip()
        if l: out.append(l)
    return out

def oname(f): return f.replace('/', '_')[:-2] + '.o'

def nm(objs, extra):
    out = run(['nm', '-S'] + extra + objs).stdout
    syms = []
    for l in out.splitlines():
        p = l.split()
        if len(p) == 4 and re.fullmatch(r'[0-9a-fA-F]+', p[0]):
            syms.append((int(p[0], 16), int(p[1], 16), p[2], p[3]))
        elif len(p) == 3 and re.fullmatch(r'[0-9a-fA-F]+', p[0]):
            syms.append((int(p[0], 16), 0, p[1], p[2]))
        elif len(p) == 2 and p[0] == 'U':
            syms.append((0, 0, 'U', p[1]))
    return syms

os.makedirs(BUILD + '/cxx', exist_ok=True); os.makedirs(BUILD + '/ref', exist_ok=True)
files = spike_files()
say('spike data files:', len(files))

# ---- 1/2 compile
t0 = time.time()
cxx_objs, ref_objs, ref_fail = [], [], []
cxx_times = []
for f in files:
    o = '%s/cxx/%s' % (BUILD, oname(f))
    t = time.time()
    run(['g++', '-x', 'c++', '-Dthis=this_arg', '-std=gnu++17'] + COMMON + DEF + INC + ['-c', '%s/%s' % (ROOT, f), '-o', o])
    cxx_times.append(time.time() - t)
    cxx_objs.append(o)
    r = '%s/ref/%s' % (BUILD, oname(f))
    rr = run(['gcc', '-std=gnu11', '-Dnullptr=NULL', '-D_GBI_STATIC_PTR(s)=0'] + COMMON + DEF + INC + ['-c', '%s/%s' % (ROOT, f), '-o', r], check=False)
    if rr.returncode == 0: ref_objs.append(r)
    else:
        rr = run(['g++', '-x', 'c++', '-Dthis=this_arg', '-std=gnu++17', '-D_GBI_STATIC_PTR(s)=0'] + COMMON + DEF + INC + ['-c', '%s/%s' % (ROOT, f), '-o', r], check=False)
        if rr.returncode == 0: ref_objs.append(r)
        ref_fail.append(f)
say('C++ spike: %d/%d files compile (%.1fs total, %.2fs avg)' % (len(cxx_objs), len(files), time.time() - t0, statistics.mean(cxx_times)))
say('reference (pointer words = 0): %d/%d compile; of those, files where plain C failed (other non-constant initializers, e.g. m_scene.h u32 pointers) and the reference was built as C++ instead: %s' % (len(ref_objs), len(files), ', '.join(os.path.basename(x) for x in ref_fail) or 'none'))

# ---- symbols (COFF objects carry no symbol sizes, so derive them from section layout via objdump)
def obj_data_syms(obj):
    hdr = run(['objdump', '-h', obj]).stdout
    secs = {}
    for m in re.finditer(r'^\s*(\d+)\s+(\S+)\s+([0-9a-f]+)\s+', hdr, re.M):
        secs[int(m.group(1)) + 1] = (m.group(2), int(m.group(3), 16))
    tab = run(['objdump', '-t', obj]).stdout
    per = {}
    for m in re.finditer(r'\(sec\s+(\d+)\)\(fl 0x\w+\)\(ty\s+\w+\)\(scl\s+(\d+)\)\s+\(nx \d+\)\s+0x([0-9a-f]+)\s+(\S+)', tab):
        sec, scl, off, name = int(m.group(1)), int(m.group(2)), int(m.group(3), 16), m.group(4)
        if sec not in secs: continue
        sn = secs[sec][0]
        if not re.match(r'\.(data|bss|rdata)', sn): continue
        per.setdefault(sec, []).append((off, name, scl == 2))
    out = {}
    for sec, lst in per.items():
        lst.sort()
        for i, (off, name, ext) in enumerate(lst):
            nxt = next((o for o, _, _ in lst[i + 1:] if o > off), secs[sec][1])
            out[name] = (secs[sec][0], off, nxt - off, ext)
    return out
def data_syms(objs):
    d = {}
    for o in objs: d.update(obj_data_syms(o))
    return d
cxx_all = data_syms(cxx_objs)
ref_all = data_syms(ref_objs)
cxx_glob = {n: v for n, v in cxx_all.items() if v[3] and v[2] > 0 and not n.startswith('.')}
ref_glob = {n: v for n, v in ref_all.items() if v[3] and v[2] > 0 and not n.startswith('.')}
cxx_local = {n for n, v in cxx_all.items() if not v[3]}
say('global data symbols (C++): %d, (C ref): %d' % (len(cxx_glob), len(ref_glob)))
lost = sorted(n for n in ref_glob if n not in cxx_glob)
say('symbols that lose external linkage when compiled as C++ (const-linkage problem): %d %s' % (len(lost), lost[:8]))
sizes_ne = sorted(n for n in ref_glob if n in cxx_glob and ref_glob[n][2] != cxx_glob[n][2])
say('symbols whose size differs C vs C++: %d %s' % (len(sizes_ne), sizes_ne[:8]))
secs_used = {}
for n, v in cxx_glob.items(): secs_used[v[0].split('$')[0]] = secs_used.get(v[0].split('$')[0], 0) + v[2]
say('bytes of spike data by section (C++):', ', '.join('%s=%d' % kv for kv in sorted(secs_used.items())))
secs_ref = {}
for n, v in ref_glob.items(): secs_ref[v[0].split('$')[0]] = secs_ref.get(v[0].split('$')[0], 0) + v[2]
say('bytes of spike data by section (C ref):  ', ', '.join('%s=%d' % kv for kv in sorted(secs_ref.items())))

def undefined(objs):
    defined = set()
    for o in objs:
        for l in run(['nm', '--defined-only', o]).stdout.splitlines():
            p = l.split()
            if len(p) >= 3: defined.add(p[-1])
    und = set()
    for o in objs:
        for l in run(['nm', '-u', o]).stdout.splitlines():
            p = l.split()
            if p: und.add(p[-1])
    return sorted(und - defined)
BAD = re.compile(r'^(_?_?imp_|_?_?gxx|_?_?cxa|_Unwind|_?_?dso|__mingw|__ZdlPv|__Znwy|__Znay|memcpy|memset|memmove|strlen|strcmp|strcpy|printf|sprintf|fprintf|abort|exit|malloc|free|_?_?chkstk|__stack_chk)')
def split_und(objs):
    und = [u for u in undefined(objs) if not BAD.match(u)]
    funcs = [u for u in und if u.startswith('_Z') or u.startswith('pc_')]
    return [u for u in und if u not in funcs], funcs
data_und, func_und = split_und(cxx_objs)
say('symbols referenced by the spike files but defined elsewhere in the game (stubbed for the harness): %d data, %d functions %s' % (len(data_und), len(func_und), func_und[:4]))
mangled = [u for u in func_und if u.startswith('_Z')]
if mangled:
    say('FINDING: %d call(s) compile to C++-MANGLED names (%s): the header declaring them lacks extern "C"; a C++-compiled data file cannot link against the C definition' % (len(mangled), mangled[0]))

# ---- generate table + stubs
tbl = ['// generated by verify_spike.py', 'extern "C" {']
ent = []
for n, s in sorted(cxx_glob.items()):
    tbl.append('extern char %s[];' % n)
    ent.append('    { "%s", (const void*)%s, %d },' % (n, n, s[2]))
tbl.append('}'); tbl.append('static const SpikeSym g_spike_syms[] = {'); tbl += ent; tbl.append('};')
open(BUILD + '/spike_table.inc', 'w').write('\n'.join(tbl) + '\n')
# reference table: same names restricted to symbols present in ref build
tbl2 = ['// generated by verify_spike.py (reference)', 'extern "C" {']
ent2 = []
for n, s in sorted(ref_glob.items()):
    tbl2.append('extern char %s[];' % n); ent2.append('    { "%s", (const void*)%s, %d },' % (n, n, s[2]))
tbl2.append('}'); tbl2.append('static const SpikeSym g_spike_syms[] = {'); tbl2 += ent2; tbl2.append('};')
os.makedirs(BUILD + '/refh', exist_ok=True)
open(BUILD + '/refh/spike_table.inc', 'w').write('\n'.join(tbl2) + '\n')
def make_stubs(objs, path):
    d_und, f_und = split_und(objs)
    with open(path, 'w') as fh:
        fh.write('/* generated: stand-ins for game symbols referenced by the spike files (never executed) */\n')
        for u in d_und:
            fh.write('char %s[65536] __attribute__((aligned(32)));\n' % u)
        for i, u in enumerate(f_und):
            if u.startswith('_Z'):
                fh.write('void stubfn_%d(void) {}\n__asm__(".globl %s; .set %s, stubfn_%d");\n' % (i, u, u, i))
            else:
                fh.write('void %s(void) {}\n' % u)
    return len(d_und), len(f_und)
make_stubs(cxx_objs, BUILD + '/spike_stubs.c')
if ref_objs: make_stubs(ref_objs, BUILD + '/spike_stubs_ref.c')
run(['gcc', '-O0', '-c', BUILD + '/spike_stubs.c', '-o', BUILD + '/spike_stubs.o'])
if ref_objs: run(['gcc', '-O0', '-c', BUILD + '/spike_stubs_ref.c', '-o', BUILD + '/spike_stubs_ref.o'])

# ---- build harness (spike) and reference dumper
lowaddr_o = BUILD + '/pc_lowaddr.o'
run(['gcc', '-std=gnu11', '-O2', '-Wall'] + DEF + INC + ['-c', ROOT + '/pc/src/pc_lowaddr.c', '-o', lowaddr_o])
sdl = run(['pkg-config', '--cflags', '--libs', 'sdl2']).stdout.split()
sdl_libs = [x for x in sdl if x.startswith('-l') or x.startswith('-L')]

exe = BUILD + '/lowaddr_spike.exe'
run(['g++', '-std=gnu++17', '-O2', '-fpermissive', '-w'] + DEF + INC + [HERE + '/lowaddr_spike.cpp', BUILD + '/spike_stubs.o'] + cxx_objs + [lowaddr_o, '-o', exe] + LOWLINK + sdl_libs + ['-lpsapi', '-static-libgcc', '-static-libstdc++'])
say('harness linked with low-address flags:', ' '.join(LOWLINK))
img = run(['objdump', '-p', exe]).stdout
ib = re.search(r'ImageBase\s+([0-9a-f]+)', img).group(1); dc = re.search(r'DllCharacteristics\s+([0-9a-f]+)', img).group(1)
say('  PE ImageBase=0x%s DllCharacteristics=0x%s (0x0020 HIGH_ENTROPY_VA %s, 0x0040 DYNAMIC_BASE %s)' % (ib, dc, 'set' if int(dc, 16) & 0x20 else 'clear', 'set' if int(dc, 16) & 0x40 else 'clear'))

ref_exe = None
if ref_objs:
    ref_exe = BUILD + '/lowaddr_ref.exe'
    run(['g++', '-std=gnu++17', '-O2', '-fpermissive', '-w', '-DSPIKE_REF_BUILD'] + DEF + [x for x in INC if 'build64' not in x] + ['-I' + BUILD + '/refh', HERE + '/lowaddr_spike.cpp', BUILD + '/spike_stubs_ref.o'] + ref_objs + [lowaddr_o, '-o', ref_exe] + LOWLINK + ['-static-libgcc', '-static-libstdc++'])

# ---- run
def parse_dump(path):
    d = {}
    for l in open(path):
        p = l.split()
        if p and p[0] == 'SYM':
            d[p[1]] = (int(p[2], 16), int(p[3]), bytes.fromhex(p[4]) if len(p) > 4 else b'')
    return d
r = run([exe, '--dump', BUILD + '/dump_cxx.txt'], check=False, cwd=BUILD)
harness_out = r.stdout + r.stderr
if ref_exe:
    run([ref_exe, '--dump', BUILD + '/dump_ref.txt'], cwd=BUILD)

# symbol maps of the linked images (spike exe and reference exe): address -> (symbol, offset)
import bisect
def mk_resolver(exe_path):
    # C++ mangles file-local (static) data as _ZL<len><name>; strip that so C and C++ names compare equal
    demangle = lambda n: re.sub(r'^_ZL\d+', '', n)
    syms = sorted((a, sz, demangle(n)) for a, sz, t, n in nm([exe_path], ['--defined-only']) if t.upper() in 'DBRCGS')
    starts = [x[0] for x in syms]
    # COFF has no sizes: a pointer may land inside a symbol; use the containing (preceding) symbol
    def resolve(addr):
        i = bisect.bisect_right(starts, addr) - 1
        if i < 0: return None
        a, sz, n = syms[i]
        if addr - a > 0x100000: return None
        return (n, addr - a)
    return resolve
resolve = mk_resolver(exe)
resolve_ref = mk_resolver(ref_exe) if ref_exe else None
img_lo, img_hi = int(ib, 16), int(ib, 16) + 0x10000000

cx = parse_dump(BUILD + '/dump_cxx.txt')
rf = parse_dump(BUILD + '/dump_ref.txt') if ref_exe else {}
tot_words = ptr_words = bad_ptr = seg_words = reloc_same = 0
same = differ_expected = differ_unexpected = 0
unexpected = []
files_checked = 0
for n, (addr, size, data) in cx.items():
    words = [int.from_bytes(data[i:i+4], 'little') for i in range(0, len(data) - 3, 4)]
    refd = rf.get(n)
    for wi, w in enumerate(words):
        tot_words += 1
        is_ptr_like = img_lo <= w < img_hi
        rw_ = None
        if refd and len(refd[2]) == len(data):
            rw_ = int.from_bytes(refd[2][wi*4:wi*4+4], 'little')
        if rw_ is not None:
            if w == rw_: same += 1; continue
            if rw_ == 0 and resolve(w) is not None: differ_expected += 1; ptr_words += 1; continue
            if rw_ == 0 and 0 < (w >> 24) < 0x10 and w < 0x10000000: seg_words += 1; continue  # N64 segment address passed through _GBI_STATIC_PTR
            if rw_ != 0 and resolve(w) is not None and resolve_ref(rw_) is not None and resolve(w) == resolve_ref(rw_): reloc_same += 1; continue
            differ_unexpected += 1; unexpected.append((n, wi * 4, rw_, w))
        else:
            if is_ptr_like:
                ptr_words += 1
                if resolve(w) is None: bad_ptr += 1; unexpected.append((n, wi * 4, None, w))
say('---- data check ----')
say('symbols dumped (C++): %d, with C reference: %d' % (len(cx), len([n for n in cx if n in rf])))
say('32-bit words compared: %d' % tot_words)
say('  identical to reference: %d' % same)
say('  differ from reference where the reference word was 0 and the C++ word == address of a real symbol (+offset): %d  <- initialized pointers' % differ_expected)
say('  differ from reference where the reference word was 0 and the C++ word is an N64 segment address (0x0Sxxxxxx; correct, kept as an integer by design): %d' % seg_words)
say('  non-zero in both builds, different absolute address but the SAME target symbol+offset (images are laid out differently; pointers set by other means, e.g. m_scene.h u32 tables): %d' % reloc_same)
say('  differ unexpectedly: %d' % differ_unexpected)
say('  words in files without reference that look like image pointers: resolving to a real symbol: %d, NOT resolving: %d' % (ptr_words - differ_expected - bad_ptr, bad_ptr))
for u in unexpected[:12]: say('   UNEXPECTED', u)
# how many words in the reference are zero at a pointer position but ALSO zero in C++ (missed init)?
missed = 0
for n, (addr, size, data) in cx.items():
    if n in rf and len(rf[n][2]) == len(data): pass
say('  -> spike data is %s' % ('CONSISTENT with the C reference' if differ_unexpected == 0 and bad_ptr == 0 else 'NOT consistent (see UNEXPECTED)'))

# ---- harness + timing
say('---- harness output (low-address allocation validation) ----')
for l in harness_out.splitlines(): say('  ' + l)
say('exit code', r.returncode)
say('log: %s/lowaddr.log' % BUILD)
times = []
for _ in range(15):
    o = run([exe], check=False, cwd=BUILD).stdout
    m = re.search(r'startup_init_us=([0-9.]+)', o)
    if m: times.append(float(m.group(1)))
if times:
    say('startup init (first constructor -> main), 15 runs: min %.0f us, median %.0f us, max %.0f us' % (min(times), statistics.median(times), max(times)))

# ---- static-init cost of the spike constructors
ctor = run(['size', '-A'] + cxx_objs).stdout
st = sum(int(m.group(1)) for m in re.finditer(r'\.text\.startup\S*\s+(\d+)', ctor))
n_ptr = differ_expected if ref_objs else 0
say('constructor code size of spike files: %d bytes for %d initialized pointer words' % (st, n_ptr))

open(BUILD + '/report.txt', 'w').write('\n'.join(report) + '\n')
