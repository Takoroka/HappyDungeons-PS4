"""Test-only shadPS4 patch: counts Happy Dungeons' own peer-to-peer socket traffic (for the two-client tests).

The game talks to peers through POSIX sockets (libScePosix sendto/recvfrom on its DGRAM_P2P socket, vport 29000):
  0x8efeab  sendto(fd, buf, len, 0, sockaddr*, 16)      main path (per-peer sockaddr stored by the transport)
  0x8eff0e  sendto(fd, buf, len, 0, {ip:3658, vport})   port 3658 hard-coded
  0x8efb52 / 0x8efbb7  recvfrom(..., MSG_DONTWAIT, from*, fromlen*)   polled every frame
Each call site becomes a call to a cave that counts calls and successful results (eax > 0) and keeps the last
address (sockaddr bytes 2..9 = port, ip, vport) - read them with tools/netprobe.py. Never enable on the main profile.
Counters at eboot 0x5d36200 (unused data tail, after the frame-rate patch state)."""
import re
import struct
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from make_fps_patch import Asm, EBOOT  # noqa: E402

STUB_SENDTO = 0x1DA2200
STUB_RECVFROM = 0x1DA21D0
STUB_CONNINFO = 0x1DA32D0   # sceNpMatching2SignalingGetConnectionInfoA(ctx, room, member, code, info*)
C = 0x5D36200
SITES = [  # (name, call site, stub, counter offset, kind)
    ("send", 0x8EFEAB, STUB_SENDTO, 0x00, "send"),
    ("send3658", 0x8EFF0E, STUB_SENDTO, 0x10, "send"),
    ("recv1", 0x8EFB52, STUB_RECVFROM, 0x20, "recv"),
    ("recv2", 0x8EFBB7, STUB_RECVFROM, 0x30, "recv"),
    ("conninfo1", 0x164D13A, STUB_CONNINFO, 0x40, "info"),   # signaling handler: GetConnectionInfoA(code 5)
    ("conninfo2", 0x164D619, STUB_CONNINFO, 0x50, "info"),   # peer check 0x164d5d0: same call
    # signaling handler 0x164ceb0 loads the per-connection listener: mov rdi,[r12+X] (8 bytes); stub = X
    ("lsn_est", 0x164D269, 0xC0, 0x60, "load"),      # event 0x5102 established
    ("lsn_dead", 0x164D015, 0x90, 0x70, "load"),     # event 0x5101 dead
    ("lsn_5103", 0x164D02B, 0xF0, 0x80, "load"),     # event 0x5103
    # event-bus hops after the listener: handler found -> mov rdi,[reg+0x40]; test rdi,rdi (7 bytes)
    ("bus1", 0x8D8876, "488B7B40", 0x90, "hop"),      # 0x8d87d0 (listener vfunc +0x10)
    ("bus2", 0x8DA3D6, "488B7940", 0xA0, "hop"),      # 0x8da320
    # function entry counters (prologue 55 48 89 e5 41 57 -> jmp cave + nop); last = rsi (2nd argument)
    ("f988220", 0x988220, None, 0xB0, "entry"),       # lobby/game state update
    ("f989730", 0x989730, None, 0xC0, "entry"),
    ("f8afc00", 0x8AFC00, None, 0xD0, "entry"),
    ("f8b95a0", 0x8B95A0, None, 0xE0, "entry"),       # member sync -> 0x8fbc10
    ("f8fbc10", 0x8FBC10, None, 0xF0, "entry"),       # copy member net info into the GNet member table
    ("f8c9240", 0x8C9240, None, 0x100, "entry"),      # room member-joined parser
    # stream-style traffic: connect(fd, sockaddr*, 16) call sites (last = sockaddr bytes 2..9), send/recv tail jumps
    ("connect", 0x8EF9A0, 0x1DA2170, 0x110, "conn"),       # 0x8ef990: caller-supplied sockaddr
    ("conn3658", 0x8EFA47, 0x1DA2170, 0x120, "conn"),      # 0x8efa00: ip:3658 hard-coded
    ("send_s", 0x8EFE87, 0x1DA21F0, 0x130, "tail"),        # 0x8efc00 -> send()
    ("recv_s", 0x8EFB17, 0x1DA21C0, 0x140, "tail"),        # 0x8efab0 -> recv()
]
PROLOGUE = bytes.fromhex("554889E54157")
CAVE0 = 0x27AE700
RING_IDX = C + 0x1FC                         # packet ring: index, then 16 x (u64 len + 64 bytes) at C + 0x200
RING = C + 0x200


def cave(at, stub, off, kind, site=None):
    a = Asm(at)
    if kind == "entry":                          # count, keep rsi, run the moved prologue, jump back
        a.rel("F0FF05", C + off)                 # lock inc [calls]
        a.rel("488935", C + off + 8)             # mov [last],rsi
        a.b += PROLOGUE
        a.jmp(site + len(PROLOGUE))
        return a.link()
    if kind in ("conn", "tail"):
        a.rel("F0FF05", C + off)                 # lock inc [calls]
        if kind == "conn":
            a.raw("488B4602")                    # mov rax,[rsi+2]  port/ip/vport
            a.rel("488905", C + off + 8)         # mov [last],rax
            a.raw("56")                          # push rsi (16-align for the stub)
        else:
            a.raw("4883EC08")                    # sub rsp,8 (reached by jmp: realign)
        a.call(stub)
        if kind == "conn":
            a.raw("5E")                          # pop rsi
        else:
            a.raw("4883C408")                    # add rsp,8
        a.raw("85C0")                            # test eax,eax
        a.jcc("88" if kind == "conn" else "8E", "done")   # conn: js (0 = ok) / tail: jle
        a.rel("F0FF05", C + off + 4)             # lock inc [ok]
        if kind == "tail":
            a.raw("8905") ; a.fix.append((len(a.b) - 2 + 2, len(a.b) + 4, C + off + 12)) ; a.b += b"\0\0\0\0"  # mov [last+4],eax (bytes)
        a.label("done")
        a.raw("C3")
        return a.link()
    if kind == "hop":                            # stub = hex of the original 4-byte mov rdi,[reg+0x40]
        a.rel("F0FF05", C + off)                 # lock inc [calls]
        a.raw(stub)                              # mov rdi,[reg+0x40]
        a.rel("48893D", C + off + 8)             # mov [last],rdi
        a.raw("4885FF")                          # test rdi,rdi   (flags for the caller's je)
        a.raw("C3")
        return a.link()
    if kind == "load":                           # stub = r12 displacement of the listener pointer
        a.rel("F0FF05", C + off)                 # lock inc [calls]
        a.raw("498BBC24") ; a.b += struct.pack("<I", stub)   # mov rdi,[r12+disp]
        a.rel("48893D", C + off + 8)             # mov [last],rdi
        a.raw("C3")
        return a.link()
    a.rel("F0FF05", C + off)                     # lock inc [calls]
    if kind == "send":
        a.raw("498B4002")                        # mov rax,[r8+2]   port/ip/vport
        a.rel("488905", C + off + 8)             # mov [last],rax
        if off == 0:                             # main send path: keep the first 64 bytes of the last 16 packets
            a.raw("57 56 51 50".replace(" ", ""))            # push rdi, rsi, rcx, rax
            a.rel("8B05", RING_IDX)                          # mov eax,[ring idx]
            a.rel("FF05", RING_IDX)                          # inc dword [ring idx]
            a.raw("83E00F")                                  # and eax,15
            a.raw("6BC048")                                  # imul eax,eax,0x48
            a.rel("488D3D", RING)                            # lea rdi,[ring]
            a.raw("4801C7")                                  # add rdi,rax
            a.raw("488917")                                  # mov [rdi],rdx   length
            a.raw("4883C708")                                # add rdi,8
            a.raw("B940000000")                              # mov ecx,64
            a.raw("F3A4")                                    # rep movsb       packet bytes
            a.raw("58 59 5E 5F".replace(" ", ""))            # pop rax, rcx, rsi, rdi
    a.raw("4150")                                # push r8 (keeps the stack 16-aligned for the stub)
    a.call(stub)
    a.raw("4158")                                # pop r8
    a.raw("85C0")                                # test eax,eax
    if kind == "info":
        a.jcc("85", "done")                      # jnz: failed (0 = success)
    else:
        a.jcc("8E", "done")                      # jle
    a.rel("F0FF05", C + off + 4)                 # lock inc [ok]
    if kind == "info":                           # keep the first 8 bytes the emulator returned (addr, port)
        a.raw("498B08")                          # mov rcx,[r8]
        a.rel("48890D", C + off + 8)             # mov [last],rcx
    if kind == "recv":
        a.raw("4D85C0")                          # test r8,r8
        a.jcc("84", "done")
        a.raw("498B4802")                        # mov rcx,[r8+2]  sender port/ip/vport
        a.rel("48890D", C + off + 8)             # mov [last],rcx
    a.label("done")
    a.raw("C3")
    return a.link()


def main():
    d = open(EBOOT, "rb").read()
    text = d[0x4000:0x4000 + 0x27ADA08]
    lines, at = [], CAVE0
    hexpat = lambda b: " ".join("%02X" % x for x in b)
    for name, site, stub, off, kind in SITES:
        if kind == "load":
            assert text[site:site + 4] == bytes.fromhex("498BBC24") and struct.unpack_from("<I", text, site + 4)[0] == stub, name
        elif kind == "hop":
            assert text[site:site + 7] == bytes.fromhex(stub + "4885FF"), name
        elif kind == "entry":
            assert text[site:site + len(PROLOGUE)] == PROLOGUE, name
        elif kind == "tail":
            assert text[site] == 0xE9 and site + 5 + struct.unpack_from("<i", text, site + 1)[0] == stub, name
        else:
            assert text[site] == 0xE8 and site + 5 + struct.unpack_from("<i", text, site + 1)[0] == stub, name
        code = cave(at, stub, off, kind, site)
        back = 10                                # widen the pattern until it is unique
        while len(re.findall(re.escape(text[site - back:site + 16]), text)) != 1:
            back += 8
            assert back < 200, name
        pat = text[site - back:site + 16]
        pad = {"load": 3, "hop": 2, "entry": 1}.get(kind, 0)
        op = b"\xE9" if kind in ("entry", "tail") else b"\xE8"   # entry/tail hooks jump (no extra return address)
        call = op + struct.pack("<i", at - (site + 5)) + b"\x90" * pad
        lines.append(f'            <Line Type="bytes" Address="0x{at + 0x400000:08x}" Value="{code.hex()}"/>')
        lines.append(f'            <!-- {name}: eboot {site:#x} call stub -> call cave {at:#x} -->')
        lines.append(f'            <Line Type="mask" Address="{hexpat(pat)}" Value="{call.hex()}" Offset="{back}"/>')
        at = (at + len(code) + 0x1F) & ~0x1F
    assert at <= 0x27B0000
    sys.stdout.write('''<?xml version="1.0"?>
<Patch>
    <TitleID>
        <ID>CUSA09120</ID>
    </TitleID>
    <Metadata Title="Happy Dungeons" Name="Test only: count peer-to-peer socket traffic" Author="local" Note="Counts the game's sendto/recvfrom calls and successes and keeps the last address, at eboot 0x5d36200 (tools\\netprobe.py). For the two-client test profiles only." PatchVer="1.0" AppVer="01.11" AppElf="eboot.bin" isEnabled="true">
        <PatchList>
''' + "\n".join(lines) + '''
        </PatchList>
    </Metadata>
</Patch>
''')


if __name__ == "__main__":
    main()
