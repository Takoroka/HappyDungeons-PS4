"""Finds counters near a value that advance at a given rate in a running Happy Dungeons (read-only).
  py -3 -I findctr.py <pid> <shad_log.txt> <value> [tol=8] [rate_lo=20] [rate_hi=40] [width=4|2]
Scans eboot data + heap for aligned ints within tol of value, waits ~1 s, keeps those whose value moved by
rate_lo..rate_hi per second (same direction), prints them with their owner vtable (nearest preceding vtable ptr)."""
import re
import struct
import sys
import time

import numpy as np

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import memscan  # noqa: E402

pid = int(sys.argv[1])
log = open(sys.argv[2], encoding="utf-8", errors="replace").read()
val = int(sys.argv[3], 0)
tol = int(sys.argv[4]) if len(sys.argv) > 4 else 8
rlo = float(sys.argv[5]) if len(sys.argv) > 5 else 20
rhi = float(sys.argv[6]) if len(sys.argv) > 6 else 40
width = int(sys.argv[7]) if len(sys.argv) > 7 else 4
base = int(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)", log).group(1), 16)
h = memscan.open_proc(pid)
dt = np.int32 if width == 4 else np.uint16
ranges = [(base + 0x27B0000, base + 0x5D38000)]
for b, s in memscan.regions(h):
    lo, hi = max(b, 0x200000000), min(b + s, 0x320000000)
    if lo < hi:
        ranges.append((lo, hi))
cands = []
t0 = time.time()
for lo, hi in ranges:
    a = lo
    while a < hi:
        n = min(64 << 20, hi - a)
        d = memscan.read(h, a, n)
        if d:
            m = len(d) // width * width
            arr = np.frombuffer(d[:m], dtype=dt).astype(np.int64)
            for i in np.nonzero(np.abs(arr - val) <= tol)[0].tolist():
                cands.append(a + i * width)
        a += n
t1 = time.time()
print("%d candidates (scan %.1fs)" % (len(cands), t1 - t0), flush=True)
fmt = "<i" if width == 4 else "<H"


def rd(a):
    b = memscan.read(h, a, width)
    return struct.unpack(fmt, b)[0] if len(b) == width else None


v0 = {c: rd(c) for c in cands}
ta = time.time()
time.sleep(1.0)
v1 = {c: rd(c) for c in cands}
tb = time.time()
el = tb - ta + (ta - t1) * 0  # reads are quick
keep = []
for c in cands:
    if v0[c] is None or v1[c] is None:
        continue
    d = v1[c] - v0[c]
    if width == 2:
        d = (d + 0x8000) % 0x10000 - 0x8000
    r = d / el
    if rlo <= abs(r) <= rhi:
        keep.append((c, v0[c], v1[c], r))
print("moving at %g..%g/s: %d" % (rlo, rhi, len(keep)))


def owner(c):
    if base + 0x27B0000 <= c < base + 0x5D38000:
        return "global %#x" % (c - base)
    back = 0x4000
    win = memscan.read(h, c - back, back)
    for off in range(len(win) - 8, -1, -8):
        v = struct.unpack_from("<Q", win, off)[0]
        if base + 0x27B0000 <= v < base + 0x2900000:
            q = memscan.read(h, v, 8)
            if len(q) == 8 and base <= struct.unpack("<Q", q)[0] < base + 0x27ADA08:
                return "vt %#x +%#x" % (v - base, back - off)
    return "?"


for c, a, b, r in keep[:60]:
    print("  %#x  %d -> %d  (%.1f/s)  %s" % (c, a, b, r, owner(c)))
