"""Disassembles Happy Dungeons' eboot (va = file offset - 0x4000).
  py -3 -I edis.py <va> [count=60]        linear from va
  py -3 -I edis.py -f <va>                whole function (until ret/int3 run after the last jump target)
  py -3 -I edis.py -x <va> [lo] [hi]      call/jmp/lea/mov references to va inside [lo, hi) (default whole text)"""
import re
import struct
import sys

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

EBOOT = r"D:\PS4 ROMS\CUSA09120\eboot.bin"
TEXT = 0x27ADA08
d = open(EBOOT, "rb").read()
text = d[0x4000:0x4000 + TEXT]
md = Cs(CS_ARCH_X86, CS_MODE_64)


def show(i):
    print("  %08x: %-24s %-7s %s" % (i.address, i.bytes.hex(), i.mnemonic, i.op_str))


def linear(va, n):
    for k, i in enumerate(md.disasm(text[va:va + n * 15], va)):
        if k >= n:
            break
        show(i)


def function(va):
    far = va
    out = []
    for i in md.disasm(text[va:va + 0x8000], va):
        out.append(i)
        if i.mnemonic.startswith("j") or i.mnemonic == "call":
            m = re.match(r"0x([0-9a-f]+)$", i.op_str)
            if m and i.mnemonic != "call":
                t = int(m.group(1), 16)
                if va <= t < va + 0x8000:
                    far = max(far, t)
        if i.mnemonic in ("ret", "int3", "ud2") or (i.mnemonic == "jmp" and not re.match(r"0x", i.op_str)):
            if i.address >= far:
                break
        if i.mnemonic == "jmp" and re.match(r"0x([0-9a-f]+)$", i.op_str) and i.address >= far:
            t = int(i.op_str, 16)
            if not (va <= t < i.address + 0x8000) or t < va:
                break
    for i in out:
        show(i)


def xref(target, lo=0, hi=TEXT):
    # rel32 call/jmp/rip-relative scan (fast, byte-level)
    for k in range(lo, hi - 5):
        b = text[k]
        if b in (0xE8, 0xE9):
            if k + 5 + struct.unpack_from("<i", text, k + 1)[0] == target:
                print("  %08x  %s" % (k, "call" if b == 0xE8 else "jmp"))
    # rip-relative: disp32 such that next_ip + disp == target, check by trying instruction lengths 6..10
    pat = None
    for k in range(lo + 3, hi - 4):
        disp = struct.unpack_from("<i", text, k)[0]
        for tail in (0, 1, 4):
            nxt = k + 4 + tail
            if nxt + disp == target:
                for st in range(max(lo, k - 4), k):
                    try:
                        ins = next(md.disasm(text[st:st + 15], st))
                    except StopIteration:
                        continue
                    if ins.address + ins.size == nxt and "rip" in ins.op_str:
                        print("  %08x  %s %s" % (st, ins.mnemonic, ins.op_str))
                        break


a = sys.argv[1:]
if a[0] == "-f":
    function(int(a[1], 16))
elif a[0] == "-x":
    xref(int(a[1], 16), *(int(x, 16) for x in a[2:4]))
else:
    linear(int(a[0], 16), int(a[1]) if len(a) > 1 else 60)
