"""Builds the shadPS4 patch "Network: treat the NAT check as done" for Happy Dungeons PS4 (CUSA09120 v1.11).

Found live (memscan on the running game, network manager object vtable eboot 0x27f51a8): NetCtl state 3 (connected),
but sceNetCtlGetNatInfo always returns stunStatus 0 (= not checked) and natType 0 under shadPS4. The NAT state machine
(eboot 0x8754e0) retries forever (state 0xced), the network manager stays in 0x3d5 (waiting for NAT info), the lobby
countdown freezes at 29 and finally fails with a network error; the retry runs every frame (lobby fps drop).
Patch 1 (0x8757e5, `cmp [r15+0x2dc],0; je retry`): call a cave that sets stunStatus 2 (OK) and natType 2 (moderate)
  when the emulator left them 0, then continues into the local-net-info step.
Patch 2 (0x8bb6ca, NAT-type getter used by lobby/matching): call a cave that returns 2 when the type is 0 / the call failed.
Caves live in the unused tail of the code segment after the frame-rate cave (0x27ae100).

Second block, "Network: select() checks the game's socket": the game's only select() call (wrapper 0x8efc00, used by
the peer-to-peer send pump 0x879160 and 7 other callers) passes nfds = 0 (`xor edi,edi` at 0x8efd37). The PS4 network
stack ignores nfds; shadPS4 follows POSIX and checks no descriptor, so the socket never reads as writable and the game
never sends a single peer-to-peer packet (lobby: other players show no character / ready state, then "continuous
communication with other players cannot be maintained"). Found with the test-only counters of make_netprobe_patch.py.
Fix: 0x8efd37 `xor edi,edi; call select` -> call CAVE_SELECT (0x27ae300): edi = fd + 1 ([r14] = the socket's fd), call
select.
"""
import re
import struct
import sys

EBOOT = r"D:\PS4 ROMS\CUSA09120\eboot.bin"
CAVE_NAT = 0x27AE200
CAVE_TYPE = 0x27AE280
SITE_NAT = 0x8757E5
SITE_TYPE = 0x8BB6CA
CAVE_SELECT = 0x27AE300
SITE_SELECT = 0x8EFD37     # 31 FF E8 rel32: xor edi,edi; call select (stub 0x1da21e0)
STUB_SELECT = 0x1DA21E0


def rel32(src_next, dst):
    return struct.pack("<i", dst - src_next)


def cave_nat():
    c = bytearray()
    c += bytes.fromhex("4183BFDC02000000")          # cmp dword [r15+0x2dc],0   stunStatus
    c += b"\x75\x0B"                                # jne +11
    c += bytes.fromhex("41C787DC02000002000000")    # mov dword [r15+0x2dc],2   STUN OK
    c += bytes.fromhex("4183BFE002000000")          # cmp dword [r15+0x2e0],0   natType
    c += b"\x75\x0B"                                # jne +11
    c += bytes.fromhex("41C787E002000002000000")    # mov dword [r15+0x2e0],2   moderate
    c += b"\xC3"                                    # ret
    return bytes(c)


def cave_type():
    c = bytearray()
    c += bytes.fromhex("B802000000")                # mov eax,2
    c += bytes.fromhex("85C9")                      # test ecx,ecx   (sceNetCtlGetNatInfo result)
    c += b"\x78\x09"                                # js done
    c += bytes.fromhex("8B55E8")                    # mov edx,[rbp-0x18]  natType
    c += bytes.fromhex("85D2")                      # test edx,edx
    c += b"\x74\x02"                                # jz done
    c += bytes.fromhex("89D0")                      # mov eax,edx
    c += b"\xC3"                                    # done: ret
    return bytes(c)


def cave_select():
    c = bytearray()
    c += bytes.fromhex("418B3E")                    # mov edi,[r14]   socket fd
    c += bytes.fromhex("FFC7")                      # inc edi         nfds = fd + 1
    c += bytes.fromhex("4883EC08")                  # sub rsp,8       (reached by call: keep the stub 16-aligned)
    c += bytes.fromhex("E8") + rel32(CAVE_SELECT + len(c) + 5, STUB_SELECT)   # call select
    c += bytes.fromhex("4883C408")                  # add rsp,8
    c += bytes.fromhex("C3")                        # ret
    return bytes(c)


def main():
    d = open(EBOOT, "rb").read()
    text = d[0x4000:0x4000 + 0x27ADA08]
    assert text[SITE_NAT:SITE_NAT + 10] == bytes.fromhex("4183bfdc020000007440")
    assert text[SITE_TYPE:SITE_TYPE + 6] == bytes.fromhex("85c90f4945e8")
    pat_nat = text[SITE_NAT - 8:SITE_NAT + 14]
    pat_type = text[SITE_TYPE - 10:SITE_TYPE + 10]
    assert text[SITE_SELECT:SITE_SELECT + 2] == bytes.fromhex("31FF") and text[SITE_SELECT + 2] == 0xE8
    assert SITE_SELECT + 7 + struct.unpack_from("<i", text, SITE_SELECT + 3)[0] == STUB_SELECT
    pat_sel = text[SITE_SELECT - 10:SITE_SELECT + 12]
    for p in (pat_nat, pat_type, pat_sel):
        assert len(re.findall(re.escape(p), text)) == 1
    call_sel = bytes.fromhex("E8") + rel32(SITE_SELECT + 5, CAVE_SELECT) + bytes.fromhex("9090")
    call_nat = b"\xE8" + rel32(SITE_NAT + 5, CAVE_NAT) + b"\x90" * 5
    call_type = b"\xE8" + rel32(SITE_TYPE + 5, CAVE_TYPE) + b"\x90"
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    for name, code, at in (("cave_nat", cave_nat(), CAVE_NAT), ("cave_type", cave_type(), CAVE_TYPE),
                           ("cave_select", cave_select(), CAVE_SELECT)):
        print(name, file=sys.stderr)
        for i in md.disasm(code, at):
            print("  %08x: %-8s %s" % (i.address, i.mnemonic, i.op_str), file=sys.stderr)
    hexpat = lambda b: " ".join("%02X" % x for x in b)
    sys.stdout.write(f'''    <Metadata Title="Happy Dungeons" Name="Network: treat the NAT check as done (lobby countdown stuck at 29)" Author="local" Note="shadPS4 always answers sceNetCtlGetNatInfo with STUN status 0 (not checked) and NAT type 0, so the game's NAT state machine (eboot 0x8754e0) retries every frame, its network manager never becomes ready, and the lobby countdown freezes at 29 before failing with a network error. This reports STUN OK / NAT type 2 (moderate) when the emulator leaves them at 0. Caves at eboot 0x27ae200 and 0x27ae280." PatchVer="1.0" AppVer="01.11" AppElf="eboot.bin" isEnabled="true">
        <PatchList>
            <Line Type="bytes" Address="0x{CAVE_NAT + 0x400000:08x}" Value="{cave_nat().hex()}"/>
            <Line Type="bytes" Address="0x{CAVE_TYPE + 0x400000:08x}" Value="{cave_type().hex()}"/>
            <!-- eboot 0x8757e5: cmp [r15+0x2dc],0; je retry -> call cave_nat; nop x5 -->
            <Line Type="mask" Address="{hexpat(pat_nat)}" Value="{call_nat.hex()}" Offset="8"/>
            <!-- eboot 0x8bb6ca: test ecx,ecx; cmovns eax,[rbp-0x18] -> call cave_type; nop -->
            <Line Type="mask" Address="{hexpat(pat_type)}" Value="{call_type.hex()}" Offset="10"/>
        </PatchList>
    </Metadata>
    <Metadata Title="Happy Dungeons" Name="Network: select() checks the game's socket (online co-op)" Author="local" Note="The game calls select() with nfds = 0, which the PS4 ignores but shadPS4 honours, so its peer-to-peer socket never looks writable and the game never sends anything to other players (lobby shows them without a character, then 'continuous communication cannot be maintained'). This passes nfds = fd + 1. eboot 0x8efd37, cave 0x27ae300." PatchVer="1.0" AppVer="01.11" AppElf="eboot.bin" isEnabled="true">
        <PatchList>
            <Line Type="bytes" Address="0x{CAVE_SELECT + 0x400000:08x}" Value="{cave_select().hex()}"/>
            <!-- eboot 0x8efd37: xor edi,edi; call select -> call cave_select; nop x2 -->
            <Line Type="mask" Address="{hexpat(pat_sel)}" Value="{call_sel.hex()}" Offset="10"/>
        </PatchList>
    </Metadata>
''')


if __name__ == "__main__":
    main()
