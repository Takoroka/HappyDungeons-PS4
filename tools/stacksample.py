"""Sampling profiler for a running shadPS4 + Happy Dungeons (debug aid for the frame-rate work).

  py -3 -I stacksample.py <pid> <shad_log.txt> [samples=400] [interval_ms=5] [focus_hex ...]

Every interval it briefly suspends each emulator thread, reads its registers, scans its stack for return addresses
into the game's code (eboot va = host address - eboot base) and resumes it. Prints, per guest thread, the hottest
leaf locations and the hottest call sites inside each focus function (default: the frame loop 0xa4a640).
Only reads memory; threads are paused for microseconds. Use on test instances only."""
import collections
import ctypes
import ctypes.wintypes as wt
import re
import struct
import sys
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
EBOOT = r"D:\PS4 ROMS\CUSA09120\eboot.bin"
TEXT_SIZE = 0x27ADA08


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", wt.DWORD)]


def threads(pid):
    snap = k32.CreateToolhelp32Snapshot(0x4, 0)
    te = THREADENTRY32(); te.dwSize = ctypes.sizeof(te)
    out = []
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            out.append(te.th32ThreadID)
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)
    return out


def thread_name(h):
    p = ctypes.c_wchar_p()
    try:
        if k32.GetThreadDescription(h, ctypes.byref(p)) >= 0 and p.value:
            return p.value
    except AttributeError:
        pass
    return ""


def main():
    pid = int(sys.argv[1])
    log = open(sys.argv[2], encoding="utf-8", errors="replace").read()
    base = int(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)", log).group(1), 16)
    samples = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    interval = (int(sys.argv[4]) if len(sys.argv) > 4 else 5) / 1000
    focus = [int(x, 16) for x in sys.argv[5:]] or [0xA4A640]
    text = open(EBOOT, "rb").read()[0x4000:0x4000 + TEXT_SIZE]

    def fstart(va):
        return text.rfind(b"\x55\x48\x89\xe5", 0, va + 1)

    def is_ret(va):  # instruction before va is a call
        if va < 7 or va >= TEXT_SIZE:
            return False
        if text[va - 5] == 0xE8:
            return True
        if text[va - 2] == 0xFF and (text[va - 1] >> 3) & 7 == 2:
            return True
        if text[va - 3] == 0xFF and (text[va - 2] >> 3) & 7 == 2:
            return True
        if text[va - 6] == 0xFF and (text[va - 5] >> 3) & 7 == 2:
            return True
        if text[va - 7] == 0xFF and (text[va - 6] >> 3) & 7 == 2:
            return True
        return False

    hproc = k32.OpenProcess(0x0010 | 0x0400, False, pid)
    ctx_buf = ctypes.create_string_buffer(1232 + 16)
    addr = ctypes.addressof(ctx_buf); addr = (addr + 15) & ~15
    handles = {}
    leaf = collections.defaultdict(collections.Counter)
    sites = collections.defaultdict(collections.Counter)
    chains = collections.defaultdict(collections.Counter)
    names = {}
    stackbuf = ctypes.create_string_buffer(0x10000)
    got = ctypes.c_size_t()
    t0 = time.perf_counter()
    for n in range(samples):
        for tid in threads(pid):
            h = handles.get(tid)
            if h is None:
                h = k32.OpenThread(0x0002 | 0x0008 | 0x0040 | 0x0400, False, tid)
                handles[tid] = h
                names[tid] = thread_name(h) if h else ""
            if not h:
                continue
            if k32.SuspendThread(h) == 0xFFFFFFFF:
                continue
            try:
                ctypes.memset(addr, 0, 1232)
                struct.pack_into("<I", (ctypes.c_char * 1232).from_address(addr), 0x30, 0x100003)
                if not k32.GetThreadContext(h, ctypes.c_void_p(addr)):
                    continue
                raw = ctypes.string_at(addr, 1232)
                rsp, = struct.unpack_from("<Q", raw, 0x98)
                rip, = struct.unpack_from("<Q", raw, 0xF8)
                ok = k32.ReadProcessMemory(hproc, ctypes.c_void_p(rsp), stackbuf, 0x10000, ctypes.byref(got))
                stack = stackbuf.raw[:got.value] if (ok or got.value) else b""
            finally:
                k32.ResumeThread(h)
            rets = []
            for i in range(0, len(stack) - 7, 8):
                v, = struct.unpack_from("<Q", stack, i)
                va = v - base
                if 0 <= va < TEXT_SIZE and is_ret(va):
                    rets.append(va)
            if not rets and not (0 <= rip - base < TEXT_SIZE):
                continue
            leafva = rip - base if 0 <= rip - base < TEXT_SIZE else None
            key = "%#x" % leafva if leafva is not None else "host<-%#x" % rets[0]
            leaf[tid][key] += 1
            for f in focus:
                for i, r in enumerate(rets):
                    if fstart(r - 1) == f:
                        sites[(tid, f)][(r, rets[i - 1] if i else None)] += 1
                        break
            chains[tid][tuple(rets[:6])] += 1
        time.sleep(interval)
    dt = time.perf_counter() - t0
    for h in handles.values():
        if h:
            k32.CloseHandle(h)
    print("eboot base %#x, %d rounds in %.1fs" % (base, samples, dt))
    for tid in sorted(leaf, key=lambda t: -sum(leaf[t].values())):
        tot = sum(leaf[tid].values())
        print("\nthread %d %r: %d samples with game code on stack" % (tid, names.get(tid, ""), tot))
        for k, c in leaf[tid].most_common(8):
            print("   leaf %-22s %5.1f%%" % (k, 100 * c / tot))
        for f in focus:
            s = sites.get((tid, f))
            if s:
                print("   inside %#x (call site -> callee frame):" % f)
                for (r, inner), c in s.most_common(25):
                    print("     ret %#x  next %-10s %5.1f%%" % (r, "%#x" % inner if inner else "-", 100 * c / tot))
        for ch, c in chains[tid].most_common(4):
            print("   chain %s  %d" % (" < ".join("%#x" % x for x in ch), c))


if __name__ == "__main__":
    main()
