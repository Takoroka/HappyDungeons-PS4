"""Hardware write-watch on a running shadPS4 test client: reports which game code writes an address (test only).
  py -3 -I hwwatch.py <pid> <shad_log.txt> <eboot_va_hex>[,<va2>...] [hits=6] [seconds=8]
Attaches as a debugger, sets data breakpoints (write, 4 bytes) in every thread, prints the writing instruction
(eboot va) and the rbp-chain return addresses, then removes the breakpoints and detaches (the game keeps running).
Never use on the user's own game."""
import ctypes
import ctypes.wintypes as wt
import re
import struct
import sys
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenThread.restype = wt.HANDLE
k32.OpenProcess.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE

pid = int(sys.argv[1])
base = int(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)",
                     open(sys.argv[2], encoding="utf-8", errors="replace").read()).group(1), 16)
vas = [int(x, 16) for x in sys.argv[3].split(",")][:4]
max_hits = int(sys.argv[4]) if len(sys.argv) > 4 else 6
secs = float(sys.argv[5]) if len(sys.argv) > 5 else 8.0
TEXT = 0x27ADA08

hproc = k32.OpenProcess(0x1F0FFF, False, pid)


def threads():
    class TE(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                    ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", wt.LONG), ("tpDeltaPri", wt.LONG),
                    ("dwFlags", wt.DWORD)]
    snap = k32.CreateToolhelp32Snapshot(4, 0)
    te = TE()
    te.dwSize = ctypes.sizeof(TE)
    out = []
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            out.append(te.th32ThreadID)
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)
    return out


CTX_SIZE = 1232
raw_ctx = ctypes.create_string_buffer(CTX_SIZE + 16)
ctx_addr = (ctypes.addressof(raw_ctx) + 15) & ~15


def ctx_get(ht, flags):
    ctypes.memset(ctx_addr, 0, CTX_SIZE)
    struct.pack_into("<I", (ctypes.c_char * CTX_SIZE).from_address(ctx_addr), 0x30, flags)
    if not k32.GetThreadContext(ht, ctypes.c_void_p(ctx_addr)):
        return None
    return (ctypes.c_char * CTX_SIZE).from_address(ctx_addr)


def set_drs(enable):
    n = 0
    for tid in threads():
        ht = k32.OpenThread(0x0002 | 0x0008 | 0x0010 | 0x0040, False, tid)
        if not ht:
            continue
        c = ctx_get(ht, 0x00100010)
        if c is not None:
            dr7 = 0
            for i, va in enumerate(vas):
                struct.pack_into("<Q", c, 0x48 + 8 * i, (base + va) if enable else 0)
                if enable:
                    dr7 |= (1 << (2 * i)) | (0b01 << (16 + 4 * i)) | (0b11 << (18 + 4 * i))
            struct.pack_into("<Q", c, 0x68, 0)
            struct.pack_into("<Q", c, 0x70, dr7)
            struct.pack_into("<I", c, 0x30, 0x00100010)
            if k32.SetThreadContext(ht, ctypes.c_void_p(ctx_addr)):
                n += 1
        k32.CloseHandle(ht)
    return n


def rd(a, n):
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t()
    k32.ReadProcessMemory(hproc, ctypes.c_void_p(a), buf, n, ctypes.byref(got))
    return buf.raw[:got.value]


def chain(tid):
    ht = k32.OpenThread(0x0008 | 0x0040, False, tid)
    c = ctx_get(ht, 0x0010000B)
    k32.CloseHandle(ht)
    if c is None:
        return []
    rbp = struct.unpack_from("<Q", c, 0xA0)[0]
    rsp = struct.unpack_from("<Q", c, 0x98)[0]
    regs = {n: struct.unpack_from("<Q", c, o)[0] for n, o in (("rax", 0x78), ("rcx", 0x80), ("rdx", 0x88),
            ("rbx", 0x90), ("rsi", 0xA8), ("rdi", 0xB0), ("r12", 0xD8), ("r13", 0xE0), ("r14", 0xE8), ("r15", 0xF0))}
    out = []
    for _ in range(8):
        q = rd(rbp, 16)
        if len(q) != 16:
            break
        nrbp, ret = struct.unpack("<QQ", q)
        if base <= ret < base + TEXT:
            out.append(ret - base)
        if nrbp <= rbp:
            break
        rbp = nrbp
    return out, regs, rsp


if not k32.DebugActiveProcess(pid):
    sys.exit("attach failed %d" % ctypes.get_last_error())
k32.DebugSetProcessKillOnExit(False)
ev = ctypes.create_string_buffer(0x200)
armed = False
hits = 0
seen = {}
t_end = None
done_arming_off = False
try:
    while True:
        if not k32.WaitForDebugEvent(ev, 200):
            if t_end and time.time() > t_end and not done_arming_off:
                k32.DebugBreakProcess(hproc)
            continue
        code, epid, tid = struct.unpack_from("<III", ev, 0)
        cont = 0x00010002
        if code == 1:
            exc = struct.unpack_from("<I", ev, 16)[0]
            addr = struct.unpack_from("<Q", ev, 32)[0]
            if exc == 0x80000003 and not armed:
                print("threads armed:", set_drs(True), flush=True)
                armed = True
                t_end = time.time() + secs
            elif exc == 0x80000003 and armed and (hits >= max_hits or time.time() > t_end):
                print("threads disarmed:", set_drs(False), flush=True)
                done_arming_off = True
                k32.ContinueDebugEvent(epid, tid, cont)
                break
            elif exc == 0x80000004:
                va = addr - base
                ch = chain(tid)
                key = va
                seen[key] = seen.get(key, 0) + 1
                if seen[key] <= 2:
                    rets, regs, rsp = ch if ch else ([], {}, 0)
                    vals = [struct.unpack("<I", rd(base + v, 4))[0] for v in vas]
                    print("write at eboot %#x (tid %d) now %s  chain %s" % (va, tid, vals,
                          " <- ".join("%#x" % r for r in rets)), flush=True)
                    print("    " + " ".join("%s=%#x" % kv for kv in regs.items()), flush=True)
                hits += 1
                if hits >= max_hits and not done_arming_off:
                    k32.DebugBreakProcess(hproc)
            elif exc not in (0x80000003,):
                cont = 0x80010001
        elif code == 3:
            for off in (16,):
                h = struct.unpack_from("<Q", ev, off)[0]
                if h:
                    k32.CloseHandle(wt.HANDLE(h))
        elif code == 6:
            h = struct.unpack_from("<Q", ev, 16)[0]
            if h:
                k32.CloseHandle(wt.HANDLE(h))
        elif code == 5:
            print("process exited")
            break
        k32.ContinueDebugEvent(epid, tid, cont)
finally:
    k32.DebugActiveProcessStop(pid)
    print("detached; hits %d; sites %s" % (hits, {"%#x" % k: v for k, v in seen.items()}))
