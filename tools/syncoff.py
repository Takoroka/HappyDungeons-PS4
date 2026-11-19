"""Prints each client's synced step number, synced game time and their offset (time - steps/60), sampled n times.
  py -3 -I syncoff.py <n> <interval_s> <pid> <log> [<pid> <log> ...]"""
import re, struct, sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import memscan
n, iv = int(sys.argv[1]), float(sys.argv[2]); a = sys.argv[3:]
cl = []
for i in range(0, len(a), 2):
    base = int(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)", open(a[i + 1], encoding="utf-8", errors="replace").read()).group(1), 16)
    cl.append((a[i + 1].split("/")[0], memscan.open_proc(int(a[i])), base))
for k in range(n):
    row = []
    for name, h, base in cl:
        x, f = struct.unpack("<dI", memscan.read(h, base + 0x3986270, 12))
        row.append("%s step %d time %.4f off %+.4f" % (name, f, x, x - f / 60))
    print(time.strftime("%H:%M:%S"), " | ".join(row), flush=True)
    if k + 1 < n:
        time.sleep(iv)
