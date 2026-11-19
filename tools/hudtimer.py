"""Reads the quest timer shown on each client's HUD, at (nearly) the same instant on all clients (read-only).
  py -3 -I hudtimer.py <samples> <interval_s> <pid> <log> [<pid> <log> ...]
The HUD timer object (vtable eboot 0x2874910, method 13 = 0x1a49880) keeps the value it displays at +0x88
(float seconds left, e.g. 1764.4 = 29:24). Also prints the lockstep step number of each client at that moment."""
import re
import struct
import sys
import time

import numpy as np

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import memscan  # noqa: E402

n, iv = int(sys.argv[1]), float(sys.argv[2])
a = sys.argv[3:]
cl = []
for i in range(0, len(a), 2):
    pid, log = int(a[i]), a[i + 1]
    base = int(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)",
                         open(log, encoding="utf-8", errors="replace").read()).group(1), 16)
    h = memscan.open_proc(pid)
    vt = base + 0x2874910
    objs = []
    for b, s in memscan.regions(h):
        lo, hi = max(b, 0x200000000), min(b + s, 0x320000000)
        x = lo
        while x < hi:
            m = min(64 << 20, hi - x)
            d = memscan.read(h, x, m)
            if d:
                arr = np.frombuffer(d[:len(d) // 8 * 8], dtype=np.uint64)
                for k in np.nonzero(arr == vt)[0].tolist():
                    objs.append(x + k * 8)
            x += m
    # keep the HUD timer objects whose +0x88 looks like a running quest timer
    live = [o for o in objs if 0.0 < struct.unpack("<f", memscan.read(h, o + 0x88, 4))[0] < 7200.0]
    cl.append((log.split("/")[0], h, base, live))
    print("%s: %d HUD timer object(s) %s" % (cl[-1][0], len(live), [hex(o) for o in live]), flush=True)


def fmt(t):
    return "%02d:%05.2f" % (int(t // 60), t % 60)


for k in range(n):
    row = []
    snap = []
    for name, h, base, live in cl:   # read all clients back to back (microseconds apart)
        vals = [struct.unpack("<f", memscan.read(h, o + 0x88, 4))[0] for o in live]
        step = struct.unpack("<I", memscan.read(h, base + 0x3986278, 4))[0]
        snap.append((name, vals, step))
    for name, vals, step in snap:
        row.append("%s step %d timer %s" % (name, step, " ".join("%.3f s (%s)" % (v, fmt(v)) for v in vals)))
    print(time.strftime("%H:%M:%S"), " | ".join(row), flush=True)
    if k + 1 < n:
        time.sleep(iv)
