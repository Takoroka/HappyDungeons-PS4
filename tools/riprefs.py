"""Lists instructions whose rip-relative operand points into [lo, hi) of Happy Dungeons' eboot (static).
  py -3 -I riprefs.py <lo_hex> <hi_hex>"""
import sys

import numpy as np
from capstone import CS_ARCH_X86, CS_MODE_64, Cs

EBOOT = r"D:\PS4 ROMS\CUSA09120\eboot.bin"
TEXT = 0x27ADA08
d = open(EBOOT, "rb").read()
text = d[0x4000:0x4000 + TEXT]
lo, hi = int(sys.argv[1], 16), int(sys.argv[2], 16)
md = Cs(CS_ARCH_X86, CS_MODE_64)
buf = np.frombuffer(text, dtype=np.uint8)
n = len(buf) - 4
disp = (buf[0:n].astype(np.int64) | (buf[1:n + 1].astype(np.int64) << 8) | (buf[2:n + 2].astype(np.int64) << 16)
        | (buf[3:n + 3].astype(np.int64) << 24))
disp = np.where(disp >= 1 << 31, disp - (1 << 32), disp)
pos = np.arange(n, dtype=np.int64)
seen = set()
for tail in (0, 1, 2, 4):
    tgt = pos + 4 + tail + disp
    for k in np.nonzero((tgt >= lo) & (tgt < hi))[0].tolist():
        nxt = k + 4 + tail
        for st in range(max(0, k - 6), k):
            try:
                ins = next(md.disasm(text[st:st + 15], st))
            except StopIteration:
                continue
            if ins.address + ins.size == nxt and "rip" in ins.op_str and st not in seen:
                seen.add(st)
                print("  %08x: %-7s %s   -> %#x" % (st, ins.mnemonic, ins.op_str, k + 4 + tail + int(disp[k])))
                break
