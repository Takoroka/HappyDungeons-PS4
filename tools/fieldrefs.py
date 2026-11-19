"""Static helper for framesweep.py results: which game code touches a counter field.

  py -3 -I fieldrefs.py vt <vtable_va> <offset> [depth=1]   -> methods of that vtable (and their direct callees up to
                                                              `depth`) with instructions using [reg+offset]
  py -3 -I fieldrefs.py global <va>                          -> instructions that reference that global (rip-relative)

Vtable entries are resolved through the eboot's RELATIVE relocations (the file holds zeros there)."""
import re
import struct
import sys

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from capstone.x86 import X86_OP_MEM, X86_REG_RIP

EBOOT = r"D:\PS4 ROMS\CUSA09120\eboot.bin"
TEXT_SIZE = 0x27ADA08


def load():
    d = open(EBOOT, "rb").read()
    phoff = struct.unpack_from("<Q", d, 0x20)[0]
    phentsize, phnum = struct.unpack_from("<HH", d, 0x36)
    segs = [struct.unpack_from("<IIQQQQQQ", d, phoff + i * phentsize) for i in range(phnum)]
    dyn = [s for s in segs if s[0] == 2][0]
    dl = [s for s in segs if s[0] == 0x61000000][0]
    tags = {}
    for i in range(dyn[5] // 16):
        t, v = struct.unpack_from("<qQ", d, dyn[2] + i * 16)
        tags.setdefault(t, v)
    rela, sz = dl[2] + tags[0x6100002F], tags[0x61000031]
    rel = {}
    for i in range(sz // 24):
        off, info, add = struct.unpack_from("<QQq", d, rela + i * 24)
        if (info & 0xFFFFFFFF) == 8:
            rel[off] = add
    return d[0x4000:0x4000 + TEXT_SIZE], rel


def func_end(text, start, limit=0x6000):
    nxt = text.find(b"\x55\x48\x89\xe5", start + 4, start + limit)
    return nxt if nxt > 0 else start + limit


def scan_func(md, text, start, pred):
    out, calls = [], []
    for i in md.disasm(text[start:func_end(text, start)], start):
        if i.mnemonic == "call" and i.op_str.startswith("0x"):
            calls.append(int(i.op_str, 16))
        if pred(i):
            out.append(i)
    return out, calls


def main():
    text, rel = load()
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    mode = sys.argv[1]
    if mode == "vt":
        vt, off = int(sys.argv[2], 16), int(sys.argv[3], 16)
        depth = int(sys.argv[4]) if len(sys.argv) > 4 else 1
        methods = []
        miss = 0
        for k in range(400):               # entries without a RELATIVE reloc are imports/pure virtuals: skip
            t = rel.get(vt + 8 * k)
            if t is not None and 0 < t < TEXT_SIZE:
                methods.append(t); miss = 0
            else:
                miss += 1
                if miss >= 3 or (t is not None and not (0 < t < TEXT_SIZE)):
                    break
        print("vtable %#x: %d methods" % (vt, len(methods)))

        def pred(i):
            for op in i.operands:
                if op.type == X86_OP_MEM and op.mem.base != X86_REG_RIP and op.mem.disp == off:
                    return True
            return False
        seen = set()
        frontier = [(m, "m%d" % j) for j, m in enumerate(methods)]
        for level in range(depth + 1):
            nxt = []
            for f, tag in frontier:
                if f in seen:
                    continue
                seen.add(f)
                hits, calls = scan_func(md, text, f, pred)
                for i in hits:
                    print("  %-12s func %#x  %08x: %-8s %s" % (tag, f, i.address, i.mnemonic, i.op_str))
                nxt += [(c, tag + ">%#x" % c) for c in calls if 0 < c < TEXT_SIZE]
            frontier = nxt
    else:
        target = int(sys.argv[2], 16)
        for m in re.finditer(rb"[\x48\x4c\x44\x41]?[\x8b\x89\x8d\xff\x83\xc7\x3b\x39\x0f]", text):
            pass  # (kept simple below)
        b = text
        for pos in range(0, len(b) - 4):
            disp = struct.unpack_from("<i", b, pos)[0]
            for k in (0, 1, 4):
                if pos + 4 + k + disp == target:
                    for st in range(pos - 4, pos):
                        ins = next(md.disasm(b[st:st + 15], st), None)
                        if ins and ins.address == st and st < pos < st + ins.size and "rip" in ins.op_str:
                            print("  %08x: %-8s %s" % (st, ins.mnemonic, ins.op_str))


if __name__ == "__main__":
    main()
