"""Samples the game's synced frame counters on running clients (read-only).
  py -3 -I syncmon.py <seconds> <pid> <log> [<pid> <log> ...]"""
import re, struct, sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import memscan
secs = float(sys.argv[1]); args = sys.argv[2:]
cl = []
for i in range(0, len(args), 2):
    pid, log = int(args[i]), args[i + 1]
    base = int(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)", open(log, encoding="utf-8", errors="replace").read()).group(1), 16)
    cl.append((pid, memscan.open_proc(pid), base))
def snap(h, base):
    a = memscan.read(h, base + 0x3986270, 0x40)
    b = memscan.read(h, base + 0x5755000, 0x40)
    t = memscan.read(h, base + 0x5782EA4, 0x30)
    o = memscan.read(h, base + 0x28E72A3, 1) + memscan.read(h, base + 0x5754100 + 0xF28, 4) + memscan.read(h, base + 0x5D36030, 4) + memscan.read(h, base + 0x28EAD30, 4)
    return a, b, t, o
s0 = [(time.time(), snap(h, b)) for _, h, b in cl]
time.sleep(secs)
s1 = [(time.time(), snap(h, b)) for _, h, b in cl]
for (pid, _, _), (ta, (a0, b0, t0, o0)), (tb, (a1, b1, t1, o1)) in zip(cl, s0, s1):
    el = tb - ta
    print("pid %d  (%.1fs)  online flag %d  lockstep active %d  patch online mode %d  fps global %d" % ((pid, el, o1[0]) + struct.unpack_from("<III", o1, 1)))
    for k in range(4):
        d0, f0 = struct.unpack_from("<dI", a0, k * 16); d1, f1 = struct.unpack_from("<dI", a1, k * 16)
        print("   blk3986 #%d  double %.4f -> %.4f   frame %d -> %d  (%.1f/s)" % (k, d0, d1, f0, f1, (f1 - f0) / el))
    w0 = struct.unpack("<16I", b0); w1 = struct.unpack("<16I", b1)
    print("   blk5755  " + " ".join("%d" % x for x in w0[:12]))
    print("         -> " + " ".join("%d" % x for x in w1[:12]))
    print("   engine frames %.1f/s  fps int %d" % ((struct.unpack_from("<I", t1, 0)[0] - struct.unpack_from("<I", t0, 0)[0]) / el, struct.unpack_from("<I", t1, 0x14)[0]))
