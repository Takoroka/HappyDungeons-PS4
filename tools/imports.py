"""Lists Happy Dungeons' import stubs (PLT va -> NID symbol name). py -3 -I imports.py [filter...]"""
import struct, sys
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
d = open(r"D:\PS4 ROMS\CUSA09120\eboot.bin", "rb").read()
phoff = struct.unpack_from("<Q", d, 0x20)[0]; phentsize, phnum = struct.unpack_from("<HH", d, 0x36)
segs = [struct.unpack_from("<IIQQQQQQ", d, phoff + i * phentsize) for i in range(phnum)]
dyn = [s for s in segs if s[0] == 2][0]; dl = [s for s in segs if s[0] == 0x61000000][0]
tags = {}
for i in range(dyn[5] // 16):
    t, v = struct.unpack_from("<qQ", d, dyn[2] + i * 16); tags.setdefault(t, v)
base = dl[2]
strtab = base + tags[0x61000035]; symtab = base + tags[0x61000039]
jmprel, pltsz = base + tags[0x61000029], tags[0x6100002D]
got2name = {}
for i in range(pltsz // 24):
    off, info, add = struct.unpack_from("<QQq", d, jmprel + i * 24)
    sym = info >> 32
    nm = struct.unpack_from("<I", d, symtab + sym * 24)[0]
    e = d.index(b"\0", strtab + nm); got2name[off] = d[strtab + nm:e].decode()
text = d[0x4000:0x4000 + 0x27ADA08]
md = Cs(CS_ARCH_X86, CS_MODE_64)
flt = sys.argv[1:]
for a in range(0x1DA1000, 0x1DA5000, 0x10):
    try: i = next(md.disasm(text[a:a + 6], a))
    except StopIteration: continue
    if i.mnemonic == "jmp" and "rip" in i.op_str:
        got = a + 6 + struct.unpack_from("<i", text, a + 2)[0]
        n = got2name.get(got, "?")
        if not flt or any(f in n for f in flt): print("%#x %s" % (a, n))
