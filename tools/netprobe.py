"""Reads the test-only net probe counters (tools/make_netprobe_patch.py) from running hidden clients.
  py -3 -I netprobe.py <pid> <shad_log.txt> [<pid> <shad_log.txt> ...]"""
import ctypes
import re
import struct
import sys

k32 = ctypes.WinDLL("kernel32")
NAMES = ["send", "send3658", "recv1", "recv2", "conninfo1", "conninfo2", "lsn_est", "lsn_dead", "lsn_5103", "bus1", "bus2", "f988220", "f989730", "f8afc00", "f8b95a0", "f8fbc10", "f8c9240", "connect", "conn3658", "send_s", "recv_s"]


def addr(q):
    b = struct.pack("<Q", q)
    port = struct.unpack(">H", b[0:2])[0]
    ip = ".".join(str(x) for x in b[2:6])
    vport = struct.unpack(">H", b[6:8])[0]
    return "%s:%d vport %d" % (ip, port, vport) if q else "-"


args = sys.argv[1:]
for i in range(0, len(args), 2):
    pid, log = int(args[i]), args[i + 1]
    base = int(re.search(r"Loading module eboot\.bin to (0x[0-9a-f]+)",
                         open(log, encoding="utf-8", errors="replace").read()).group(1), 16)
    h = k32.OpenProcess(0x10 | 0x400, False, pid)
    buf = ctypes.create_string_buffer(0x160)
    got = ctypes.c_size_t()
    k32.ReadProcessMemory(h, ctypes.c_void_p(base + 0x5D36200), buf, 0x160, ctypes.byref(got))
    raw = buf.raw
    print("pid %d (%s)" % (pid, log.split("/")[-4] if "/" in log else log))
    for j, n in enumerate(NAMES):
        calls, ok, last = struct.unpack_from("<IIQ", raw, j * 0x10)
        if n.startswith("conninfo"):
            b = struct.pack("<Q", last)
            print("   %-9s calls %8d  ok %8d  raw %s  -> addr %s port %d" % (n, calls, ok, b.hex(), ".".join(str(x) for x in b[0:4]), struct.unpack(">H", b[4:6])[0]))
        elif n.startswith(("lsn", "bus", "f8", "f9")):
            print("   %-9s events %7d  listener %#x" % (n, calls, last))
        else:
            print("   %-9s calls %8d  ok %8d  last %s" % (n, calls, ok, addr(last)))
