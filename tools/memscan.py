"""Read-only memory helpers for a running shadPS4 (guest memory sits at the same addresses in the host process).
  py -3 -I memscan.py <pid> range <lo_hex> <hi_hex>     -> aligned qwords whose value is in [lo, hi)
  py -3 -I memscan.py <pid> dump <addr_hex> <len_hex>   -> hex dump
  py -3 -I memscan.py <pid> base <shad_log.txt>         -> eboot base from the log
Never writes to the process."""
import ctypes
import ctypes.wintypes as wt
import re
import sys

import numpy as np

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wt.DWORD),
                ("PartitionId", wt.WORD), ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD), ("Protect", wt.DWORD),
                ("Type", wt.DWORD)]


def open_proc(pid):
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        raise OSError(ctypes.get_last_error())
    return h


def read(h, addr, n):
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t()
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got))
    return buf.raw[:got.value] if ok or got.value else b""


def regions(h, lo=0x10000, hi=0x7FFFFFFF0000):
    addr = lo
    mbi = MBI()
    readable = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
    while addr < hi:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize
        if mbi.State == 0x1000 and (mbi.Protect & 0xFF) in readable and not (mbi.Protect & 0x100):
            yield base, size
        addr = base + size


def scan_range(h, lo, hi, limit=200):
    out = []
    for base, size in regions(h):
        off = 0
        while off < size:
            n = min(64 << 20, size - off)
            data = read(h, base + off, n)
            if len(data) >= 8:
                arr = np.frombuffer(data[:len(data) // 8 * 8], dtype=np.uint64)
                idx = np.nonzero((arr >= lo) & (arr < hi))[0]
                for i in idx:
                    out.append((base + off + int(i) * 8, int(arr[i])))
                    if len(out) >= limit:
                        return out
            off += n
    return out


def main():
    pid = int(sys.argv[1]); cmd = sys.argv[2]
    if cmd == "base":
        t = open(sys.argv[3], encoding="utf-8", errors="replace").read()
        print(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)", t).group(1)); return
    h = open_proc(pid)
    if cmd == "range":
        lo, hi = int(sys.argv[3], 16), int(sys.argv[4], 16)
        for a, v in scan_range(h, lo, hi):
            print("%#x -> %#x" % (a, v))
    elif cmd == "dump":
        a, n = int(sys.argv[3], 16), int(sys.argv[4], 16)
        data = read(h, a, n)
        for i in range(0, len(data), 16):
            print("%#014x +%04x  %s" % (a + i, i, data[i:i + 16].hex(" ")))


if __name__ == "__main__":
    main()
