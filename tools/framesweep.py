"""Finds per-frame counters in a running Happy Dungeons (shadPS4) - read-only.

  py -3 -I framesweep.py <pid> <shad_log.txt> [out.json] [gap_s=0.5]

At high frame rates a counter that the game steps once per frame moves twice as fast as one that follows real time
in 1/30 s units. After the decouple patch, a per-frame counter is fine when its limit was converted with the fps
setting (seconds*fps) and wrong when the limit is a fixed number of 30 fps frames. This tool lists the per-frame
counters so their code can be checked.

Pass 1: for each 64 MB chunk of guest memory (eboot data + 0x200000000-0x320000000), read it twice `gap_s` apart and
        note the game's frame counter around each read; keep aligned int32 fields that moved by +-(frames elapsed).
Pass 2: re-read the candidate pages on ~45 consecutive frames; keep fields that change by exactly +-1 on most frames.
Pass 3: owner = nearest preceding qword that points at a vtable in the eboot (a table whose first entry is game code);
        report vtable va, field offset, direction, value range, and how many objects share (vtable, offset).
Never writes to the process."""
import collections
import ctypes
import json
import re
import struct
import sys
import time

import numpy as np

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import memscan  # noqa: E402

TEXT_SIZE = 0x27ADA08
VT_LO, VT_HI = 0x27B0000, 0x2900000          # vtables live in .data.rel.ro here
DATA_LO, DATA_HI = 0x27B0000, 0x5D38000      # eboot data segments
HEAP_LO, HEAP_HI = 0x200000000, 0x320000000
CHUNK = 64 << 20


def main():
    pid = int(sys.argv[1])
    log = open(sys.argv[2], encoding="utf-8", errors="replace").read()
    out = sys.argv[3] if len(sys.argv) > 3 else None
    gap = float(sys.argv[4]) if len(sys.argv) > 4 else 0.5
    base = int(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)", log).group(1), 16)
    h = memscan.open_proc(pid)
    rd = lambda a, n: memscan.read(h, a, n)
    timer = base + 0x5782EA4

    def frame():
        b = rd(timer, 4)
        if len(b) != 4:
            raise SystemExit("game closed")
        return struct.unpack("<I", b)[0]
    text = rd(base, TEXT_SIZE)

    ranges = [(base + DATA_LO, base + DATA_HI)]
    for b, s in memscan.regions(h):
        lo, hi = max(b, HEAP_LO), min(b + s, HEAP_HI)
        if lo < hi:
            ranges.append((lo, hi))
    chunks = []
    for lo, hi in ranges:
        a = lo
        while a < hi:
            chunks.append((a, min(CHUNK, hi - a)))
            a += CHUNK
    total = sum(n for _, n in chunks)
    print("scanning %d MB in %d chunks, gap %.2fs" % (total >> 20, len(chunks), gap), flush=True)

    # pass 1
    cands = []
    t0 = time.time()
    for a, n in chunks:
        f0 = frame(); A = rd(a, n); f1 = frame()
        time.sleep(gap)
        f2 = frame(); B = rd(a, n); f3 = frame()
        m = min(len(A), len(B)) // 4 * 4
        if m == 0:
            continue
        va = np.frombuffer(A[:m], dtype=np.int32).astype(np.int64)
        vb = np.frombuffer(B[:m], dtype=np.int32).astype(np.int64)
        d = np.abs(vb - va)
        lo_n, hi_n = max(f2 - f1, 1), f3 - f0
        if lo_n < 12:
            continue
        idx = np.nonzero((d >= lo_n) & (d <= hi_n))[0]
        for i in idx.tolist():
            cands.append(a + i * 4)
    print("pass 1: %d candidates in %.0fs" % (len(cands), time.time() - t0), flush=True)

    # pass 2: per-frame sampling of candidate pages
    pages = sorted({c & ~0xFFF for c in cands})
    cset = set(cands)
    hist = collections.defaultdict(list)     # candidate -> [(frame, value)]
    fr_hist = []
    last_f = frame(); samples = 0; t1 = time.time()
    while samples < 60 and time.time() - t1 < 30:
        f = frame()
        if f == last_f:
            continue
        snap = {p: rd(p, 0x1000) for p in pages}
        if frame() != f:             # the game moved on while reading: values may mix two frames
            last_f = f
            continue
        last_f = f
        fr_hist.append(f)
        for c in cands:
            pg = snap.get(c & ~0xFFF)
            if pg and len(pg) == 0x1000:
                hist[c].append((f, struct.unpack_from("<i", pg, c & 0xFFF)[0]))
        samples += 1
    print("pass 2: %d frames sampled for %d pages in %.1fs" % (samples, len(pages), time.time() - t1), flush=True)
    confirmed = []
    for c, pts in hist.items():
        up = down = n = 0
        for (fa, xa), (fb, xb) in zip(pts, pts[1:]):
            df, dv = fb - fa, xb - xa
            if df <= 0:
                continue
            n += 1
            up += dv == df
            down += dv == -df
        if n < 10:
            continue
        vals = [x for _, x in pts]
        steps = [0] * n
        if max(up, down) >= 0.8 * len(steps):
            v = vals
            sign = "+" if up > down else "-"
            if fr_hist and abs(v[-1] - fr_hist[-1]) <= 3:
                sign = "stamp"               # a copy of the engine frame number, not a timer
            confirmed.append((c, sign, min(v), max(v)))
    print("confirmed per-frame counters: %d" % len(confirmed), flush=True)

    # pass 3: owners
    def is_vtable(v):
        if not (base + VT_LO <= v < base + VT_HI):
            return False
        q = rd(v, 8)
        return len(q) == 8 and base <= struct.unpack("<Q", q)[0] < base + TEXT_SIZE

    rows = []
    for c, sign, vmin, vmax in confirmed:
        owner = None
        if base + DATA_LO <= c < base + DATA_HI:
            owner = ("global", c - base)
        else:
            back = 0x40000
            win = rd(c - back, back)
            for off in range(len(win) - 8, -1, -8):
                v = struct.unpack_from("<Q", win, off)[0]
                if base + VT_LO <= v < base + VT_HI and is_vtable(v):
                    owner = ("vt%#x" % (v - base), back - off)
                    break
        rows.append({"addr": "%#x" % c, "owner": owner[0] if owner else "?",
                     "offset": "%#x" % owner[1] if owner else "?", "dir": sign, "min": vmin, "max": vmax})
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["owner"], r["offset"], r["dir"])].append(r)
    print("\n%-14s %-9s %-3s %5s  %s" % ("owner", "offset", "dir", "count", "value range / example"))
    for (o, off, sgn), rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print("%-14s %-9s %-3s %5d  %d..%d  e.g. %s" % (o, off, sgn, len(rs), min(r["min"] for r in rs),
                                                       max(r["max"] for r in rs), rs[0]["addr"]))
    if out:
        json.dump({"base": "%#x" % base, "rows": rows}, open(out, "w"), indent=1)


if __name__ == "__main__":
    main()
