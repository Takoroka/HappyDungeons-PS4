"""Reads Happy Dungeons' game timer from a running shadPS4 (guest memory is mapped at the same addresses).
Usage: py -3 -I fpsmon.py <pid> <shad_log.txt> [seconds] [interval]
Prints frames/s (timer frame counter), step (+0x18), scaled step (+0x24) and dt (+0x2c); with the v2 decouple patch
also the fps globals (0x28ead30/34) and the patch state (0x5d36000: U60/T30 of the last frame, smoothed frame time)."""
import ctypes, re, struct, sys, time
pid = int(sys.argv[1]); log = sys.argv[2]
secs = float(sys.argv[3]) if len(sys.argv) > 3 else 10; iv = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
base = int(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)", open(log, encoding="utf-8", errors="replace").read()).group(1), 16)
TIMER = base + 0x5782EA4
FPSG = base + 0x28EAD30
STATE = base + 0x5D36000
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
def rd(addr, n):
    buf = ctypes.create_string_buffer(n); got = ctypes.c_size_t()
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
        raise OSError(ctypes.get_last_error())
    return buf.raw
prev = None; t0 = time.time(); rows = []
while time.time() - t0 < secs:
    b = rd(TIMER, 0x30); now = time.perf_counter()
    frames = struct.unpack_from("<I", b, 0)[0]
    fps_i = struct.unpack_from("<I", b, 0x14)[0]
    step, sstep = struct.unpack_from("<f", b, 0x18)[0], struct.unpack_from("<f", b, 0x24)[0]
    dt = struct.unpack_from("<f", b, 0x2C)[0]
    g = rd(FPSG, 8); st = rd(STATE, 0x24)
    gi, gf = struct.unpack_from("<I", g, 0)[0], struct.unpack_from("<f", g, 4)[0]
    acc60, acc30 = struct.unpack_from("<QQ", st, 8)
    u, t = struct.unpack_from("<II", st, 0x18); ema = struct.unpack_from("<f", st, 0x20)[0]
    if prev:
        rate = (frames - prev[0]) / (now - prev[1])
        rows.append(rate)
        print("frames/s %6.1f  fpsSetting %d  globals %d/%.1f  step %.5f  scaled %.5f  dt %.5f  U60 %d T30 %d  ema %.2f ms" % (
            rate, fps_i, gi, gf, step, sstep, dt, u, t, ema * 1000), flush=True)
    prev = (frames, now); time.sleep(iv)
if rows: print("avg frames/s %.1f" % (sum(rows) / len(rows)))
