"""Dumps the test-only packet ring (last 16 packets sent on the game's main P2P path) - see make_netprobe_patch.py.
  py -3 -I netring.py <pid> <shad_log.txt>"""
import ctypes, re, struct, sys
k32 = ctypes.WinDLL("kernel32")
pid, log = int(sys.argv[1]), sys.argv[2]
base = int(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)", open(log, encoding="utf-8", errors="replace").read()).group(1), 16)
h = k32.OpenProcess(0x10 | 0x400, False, pid)
buf = ctypes.create_string_buffer(0x4 + 16 * 0x48); got = ctypes.c_size_t()
k32.ReadProcessMemory(h, ctypes.c_void_p(base + 0x5D36200 + 0x1FC), buf, len(buf), ctypes.byref(got))
raw = buf.raw; idx = struct.unpack_from("<I", raw, 0)[0]
print("packets sent:", idx)
for k in range(16):
    j = (idx + k) % 16
    ln = struct.unpack_from("<Q", raw, 4 + j * 0x48)[0]
    data = raw[4 + j * 0x48 + 8:4 + j * 0x48 + 0x48]
    print("%3d len %4d  %s" % (j, ln, data[:min(ln, 64)].hex(" ")))
