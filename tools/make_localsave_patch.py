"""Builds the optional shadPS4 patch "Saves: keep them on this PC (not on the server)" for Happy Dungeons PS4
(CUSA09120 v1.11).

  py -3 -I make_localsave_patch.py [--enabled] > runs/saves_local.xml

The game keeps its saves in PSN cloud storage (TUS slots 1, 3, 4, 5 and 9999), through its save-manager thread
0xa6cb20 and four wrappers; the patch redirects their five TUS calls to caves that use files instead:
  0x9db63d  sceNpTusGetDataA (load: returns bytes read; error 0x8055084A = no data -> "new player")
  0x9dbb14  sceNpTusGetDataA (status only: slot exists?)
  0xa6d964  sceNpTusGetDataA (status only, slot 1; reads status+0x24 = hasData)
  0x9db84e  sceNpTusSetDataA (save)
  0x9db9cf  sceNpTusDeleteMultiSlotDataA (delete)
Files: /download0/hds_<account id hex>_<slot hex>.bin and .bak (shadPS4 maps /download0 to the game's folder
user/download/CUSA09120, on every OS). Each file = "HDS1", u32 length, data, u32 length again; a save writes the .bak
first, then the .bin, so a save cut short (crash, power loss) is detected and the other copy loads. Delete writes
empty (length 0) files, which read as "no data". The online parts of the game still use shadNet as before.
Caves at eboot 0x27af600.. (unused code tail, after the frame-rate patch's)."""
import re
import struct
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from make_fps_patch import Asm, EBOOT  # noqa: E402

CAVE = 0x27AF600
OPEN, READ, WRITE, CLOSE = 0x1DA30A0, 0x1DA30B0, 0x1DA30C0, 0x1DA3080   # sceKernelOpen/Read/Write/Close
GETDATA, SETDATA, DELMULTI = 0x1DA2520, 0x1DA2530, 0x1DA2500
SITES = [(0x9DB63D, GETDATA, "get"), (0x9DBB14, GETDATA, "get"), (0xA6D964, GETDATA, "get"),
         (0x9DB84E, SETDATA, "set"), (0x9DB9CF, DELMULTI, "del")]
MAGIC = 0x31534448                 # "HDS1"
NOT_FOUND = 0x8055084A             # SCE_NP_COMMUNITY_SERVER_ERROR_USER_STORAGE_DATA_NOT_FOUND
WRITE_FAILED = 0x80550801
O_WRONLY_CREAT_TRUNC = 0x601


def prologue(a, frame):
    a.raw("55 4889E5 53 4154 4155 4156 4157".replace(" ", ""))   # push rbp; mov rbp,rsp; push rbx,r12-r15
    a.raw("4883EC") ; a.b += bytes([frame])                      # sub rsp,frame (keeps rsp 16-aligned)


def epilogue(a, frame):
    a.raw("4883C4") ; a.b += bytes([frame])                      # add rsp,frame
    a.raw("415F 415E 415D 415C 5B 5D C3".replace(" ", ""))       # pop r15-r12,rbx,rbp; ret


def build():
    a = Asm(CAVE)
    # ---- entry points first (fixed offsets for the call sites) ----
    a.jmp("get") ; a.jmp("set") ; a.jmp("del")
    a.align(16, b"\xCC")

    # hexout: rdi = dest, rax = value, ecx = digits -> writes the low `digits` nibbles as hex at rdi (not advanced)
    a.label("hexout")
    a.rel("4C8D15", "digits")              # lea r10,[digits]
    a.raw("4C8D5C0FFF")                    # lea r11,[rdi+rcx-1]
    a.label("hex_l")
    a.raw("89C2 83E20F 418A1412 418813 49FFCB 48C1E804 FFC9".replace(" ", ""))
    a.raw("75") ; a.b += bytes([(a.labels["hex_l"] - (len(a.b) + 1)) & 0xFF])   # jnz hex_l
    a.raw("C3")

    # build_path: rdi = buf, rsi = account id, edx = slot, rcx = suffix (".bin"/".bak" + NUL)
    a.label("build_path")
    a.raw("4154 4155 4156".replace(" ", ""))              # push r12,r13,r14
    a.raw("4989F4 4189D5 4989CE".replace(" ", ""))        # mov r12,rsi; mov r13d,edx; mov r14,rcx
    a.rel("488D35", "prefix")                             # lea rsi,[prefix]
    a.raw("B90F000000 F3A4".replace(" ", ""))             # mov ecx,15; rep movsb
    a.raw("4C89E0 B910000000".replace(" ", ""))           # mov rax,r12; mov ecx,16
    a.call("hexout")
    a.raw("4883C710 C6075F 48FFC7".replace(" ", ""))      # add rdi,16; mov byte [rdi],'_'; inc rdi
    a.raw("4489E8 B908000000".replace(" ", ""))           # mov eax,r13d; mov ecx,8
    a.call("hexout")
    a.raw("4883C708 4C89F6 B905000000 F3A4".replace(" ", ""))   # add rdi,8; mov rsi,r14; mov ecx,5; rep movsb
    a.raw("415E 415D 415C C3".replace(" ", ""))

    # read_file: rdi = path, rsi = buf (0: just check), rdx = buf size -> rax = data length, or -1 (missing/invalid)
    a.label("read_file")
    prologue(a, 0x18)
    a.raw("4989F4 4989D5".replace(" ", ""))               # mov r12,rsi; mov r13,rdx
    a.raw("31F6 31D2".replace(" ", ""))                   # xor esi,esi (O_RDONLY); xor edx,edx
    a.call(OPEN)
    a.raw("85C0") ; a.jcc("88", "rf_missing")             # test eax,eax; js
    a.raw("89C3")                                         # mov ebx,eax  fd
    a.raw("89DF 4889E6 BA08000000".replace(" ", ""))      # mov edi,ebx; mov rsi,rsp; mov edx,8
    a.call(READ)
    a.raw("4883F808") ; a.jcc("85", "rf_bad")             # cmp rax,8; jne
    a.raw("813C24") ; a.b += struct.pack("<I", MAGIC) ; a.jcc("85", "rf_bad")   # cmp dword [rsp],MAGIC
    a.raw("448B742404")                                   # mov r14d,[rsp+4]  length
    a.raw("4585F6") ; a.jcc("84", "rf_bad")               # test r14d,r14d; jz
    a.raw("4D85E4") ; a.jcc("84", "rf_ok")                # test r12,r12; jz (status only)
    a.raw("4D39EE") ; a.jcc("87", "rf_bad")               # cmp r14,r13; ja (too big)
    a.raw("89DF 4C89E6 4C89F2".replace(" ", ""))          # mov edi,ebx; mov rsi,r12; mov rdx,r14
    a.call(READ)
    a.raw("4C39F0") ; a.jcc("85", "rf_bad")               # cmp rax,r14; jne
    a.raw("89DF 488D742408 BA04000000".replace(" ", ""))  # mov edi,ebx; lea rsi,[rsp+8]; mov edx,4
    a.call(READ)
    a.raw("4883F804") ; a.jcc("85", "rf_bad")             # cmp rax,4; jne
    a.raw("4439742408") ; a.jcc("85", "rf_bad")           # cmp [rsp+8],r14d; jne
    a.label("rf_ok")
    a.raw("89DF") ; a.call(CLOSE)                         # mov edi,ebx; close
    a.raw("4C89F0")                                       # mov rax,r14
    a.jmp("rf_out")
    a.label("rf_bad")
    a.raw("89DF") ; a.call(CLOSE)
    a.label("rf_missing")
    a.raw("48C7C0FFFFFFFF")                               # mov rax,-1
    a.label("rf_out")
    epilogue(a, 0x18)

    # write_file: rdi = path, rsi = data, rdx = length -> eax 0 ok, -1 failed
    a.label("write_file")
    prologue(a, 0x18)
    a.raw("4989F4 4989D5".replace(" ", ""))               # mov r12,rsi; mov r13,rdx
    a.raw("BE") ; a.b += struct.pack("<I", O_WRONLY_CREAT_TRUNC)   # mov esi,O_WRONLY|O_CREAT|O_TRUNC
    a.raw("BAB6010000")                                   # mov edx,0666
    a.call(OPEN)
    a.raw("85C0") ; a.jcc("88", "wf_fail")
    a.raw("89C3")                                         # mov ebx,eax
    a.raw("C70424") ; a.b += struct.pack("<I", MAGIC)     # mov dword [rsp],MAGIC
    a.raw("44896C2404")                                   # mov [rsp+4],r13d
    a.raw("89DF 4889E6 BA08000000".replace(" ", ""))      # mov edi,ebx; mov rsi,rsp; mov edx,8
    a.call(WRITE)
    a.raw("4883F808") ; a.jcc("85", "wf_bad")
    a.raw("4D85ED") ; a.jcc("84", "wf_trailer")           # test r13,r13; jz
    a.raw("89DF 4C89E6 4C89EA".replace(" ", ""))          # mov edi,ebx; mov rsi,r12; mov rdx,r13
    a.call(WRITE)
    a.raw("4C39E8") ; a.jcc("85", "wf_bad")               # cmp rax,r13; jne
    a.label("wf_trailer")
    a.raw("44896C2408")                                   # mov [rsp+8],r13d
    a.raw("89DF 488D742408 BA04000000".replace(" ", ""))  # mov edi,ebx; lea rsi,[rsp+8]; mov edx,4
    a.call(WRITE)
    a.raw("4883F804") ; a.jcc("85", "wf_bad")
    a.raw("89DF") ; a.call(CLOSE)
    a.raw("31C0") ; a.jmp("wf_out")                       # xor eax,eax
    a.label("wf_bad")
    a.raw("89DF") ; a.call(CLOSE)
    a.label("wf_fail")
    a.raw("B8FFFFFFFF")                                   # mov eax,-1
    a.label("wf_out")
    epilogue(a, 0x18)

    def path(suffix):                                     # rdi = rsp (path buffer), rsi = r15, edx = ebx
        a.raw("4889E7 4C89FE 89DA".replace(" ", ""))      # mov rdi,rsp; mov rsi,r15; mov edx,ebx
        a.rel("488D0D", suffix)                           # lea rcx,[suffix]
        a.call("build_path")

    # get: GetDataA(req, account, slot, status, statusSize, data, recvdSize, option)
    a.label("get")
    prologue(a, 0x48)
    a.raw("4989CC 4D89CD 4C8B7510 4989F7 89D3".replace(" ", ""))   # r12=status r13=data r14=[rbp+0x10] r15=acct ebx=slot
    for suffix, nxt in (("suf_bin", "get_found"), ("suf_bak", None)):
        path(suffix)
        a.raw("4889E7 4C89EE 4C89F2".replace(" ", ""))    # mov rdi,rsp; mov rsi,r13; mov rdx,r14
        a.call("read_file")
        a.raw("4885C0")                                   # test rax,rax
        if nxt:
            a.jcc("89", nxt)                              # jns found
        else:
            a.jcc("88", "get_none")                       # js none
    a.label("get_found")
    a.raw("4D85E4") ; a.jcc("84", "get_ret")              # test r12,r12; jz
    a.raw("41C744242401000000")                           # mov dword [r12+0x24],1   hasData
    a.label("get_ret")
    a.raw("4D85ED") ; a.jcc("85", "get_out")              # test r13,r13; jnz -> eax = bytes read
    a.raw("31C0") ; a.jmp("get_out")                      # status only: 0 = ok
    a.label("get_none")
    a.raw("B8") ; a.b += struct.pack("<I", NOT_FOUND)
    a.label("get_out")
    epilogue(a, 0x48)

    # set: SetDataA(req, account, slot, totalSize, sendSize, data, info, infoSize, ...)
    a.label("set")
    prologue(a, 0x48)
    a.raw("4989F7 89D3 4D89CC 4D89C5".replace(" ", ""))   # r15=acct ebx=slot r12=data r13=sendSize
    for suffix in ("suf_bak", "suf_bin"):                 # backup first, then the main file
        path(suffix)
        a.raw("4889E7 4C89E6 4C89EA".replace(" ", ""))    # mov rdi,rsp; mov rsi,r12; mov rdx,r13
        a.call("write_file")
        a.raw("85C0") ; a.jcc("88", "set_fail")
    a.raw("31C0") ; a.jmp("set_out")
    a.label("set_fail")
    a.raw("B8") ; a.b += struct.pack("<I", WRITE_FAILED)
    a.label("set_out")
    epilogue(a, 0x48)

    # del: DeleteMultiSlotDataA(req, account, slot array, count, option)
    a.label("del")
    prologue(a, 0x48)
    a.raw("4989F7 4989D4 4189CD".replace(" ", ""))        # r15=acct r12=array r13d=count
    a.label("del_l")
    a.raw("4585ED") ; a.jcc("84", "del_done")             # test r13d,r13d; jz
    a.raw("418B1C24")                                     # mov ebx,[r12]
    for suffix in ("suf_bin", "suf_bak"):
        path(suffix)
        a.raw("4889E7 31F6 31D2".replace(" ", ""))        # mov rdi,rsp; xor esi,esi; xor edx,edx (empty file)
        a.call("write_file")
    a.raw("4983C404 41FFCD".replace(" ", ""))             # add r12,4; dec r13d
    a.jmp("del_l")
    a.label("del_done")
    a.raw("31C0")
    epilogue(a, 0x48)

    a.label("prefix") ; a.b += b"/download0/hds_"
    a.label("suf_bin") ; a.b += b".bin\0"
    a.label("suf_bak") ; a.b += b".bak\0"
    a.label("digits") ; a.b += b"0123456789abcdef"
    code = a.link()
    return code, {"get": CAVE, "set": CAVE + 5, "del": CAVE + 10}


def main():
    enabled = "--enabled" in sys.argv
    d = open(EBOOT, "rb").read()
    text = d[0x4000:0x4000 + 0x27ADA08]
    code, entry = build()
    assert CAVE + len(code) <= 0x27B0000
    from capstone import CS_ARCH_X86, CS_MODE_64, Cs
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    for i in md.disasm(code, CAVE):
        print("  %08x: %-24s %-8s %s" % (i.address, i.bytes.hex(), i.mnemonic, i.op_str), file=sys.stderr)
    hexpat = lambda b: " ".join("%02X" % x for x in b)
    lines = ['            <Line Type="bytes" Address="0x%08x" Value="%s"/>' % (CAVE + 0x400000, code.hex())]
    for site, stub, kind in SITES:
        assert text[site] == 0xE8 and site + 5 + struct.unpack_from("<i", text, site + 1)[0] == stub, hex(site)
        back = 10
        while len(re.findall(re.escape(text[site - back:site + 10]), text)) != 1:
            back += 4
            assert back < 80, hex(site)
        call = b"\xE8" + struct.pack("<i", entry[kind] - (site + 5))
        lines.append("            <!-- eboot %#x: call %s -> local file cave -->" % (site, {"get": "sceNpTusGetDataA", "set": "sceNpTusSetDataA", "del": "sceNpTusDeleteMultiSlotDataA"}[kind]))
        lines.append('            <Line Type="mask" Address="%s" Value="%s" Offset="%d"/>' % (hexpat(text[site - back:site + 10]), call.hex(), back))
    sys.stdout.write('''<?xml version="1.0"?>
<Patch>
    <TitleID>
        <ID>CUSA09120</ID>
    </TitleID>
    <Metadata Title="Happy Dungeons" Name="Saves: keep them on this PC (not on the server)" Author="local" Note="Optional. Your saves are stored as files in shadPS4's folder for this game (user/download/CUSA09120, hds_*.bin/.bak) instead of on the server's cloud storage. Your character is then no longer shared with the PC version or with other computers. Turning this off again goes back to the server's save. Built by tools\\\\make_localsave_patch.py." PatchVer="1.0" AppVer="01.11" AppElf="eboot.bin" isEnabled="%s">
        <PatchList>
%s
        </PatchList>
    </Metadata>
</Patch>
''' % ("true" if enabled else "false", "\n".join(lines)))


if __name__ == "__main__":
    main()
