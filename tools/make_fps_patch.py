r"""Builds the shadPS4 patch "Decouple gameplay from frame rate" for Happy Dungeons PS4 (CUSA09120 v1.11).

The game runs one logic update per rendered frame. Stock, everything assumes 30 fps, so at 60 fps it plays at 2x.
This patch makes every kind of game timing follow real time, at any frame rate (30, 60, 120, uncapped, unsteady):

 1. Delta time. Timer object 0x5782ea4 (+0x14 fps int, +0x18 step, +0x20 timescale, +0x24 step*timescale,
    +0x2c dt). Frame loop 0xa4a640 calls SetFps(timer, [0x28ead30]) then `vmovss xmm0,[r15+0x24]` (0xa4a71e) ->
    SetDelta. That load becomes a call to FRAME_CAVE, which measures real time (clock_gettime MONOTONIC, clamped
    1-100 ms) and writes it into +0x18 / +0x24.
 2. fps value. ~100 places convert with the engine's fps globals 0x28ead30 (int) / 0x28ead34 (float): seconds*fps ->
    frame waits (call 0xb1bca0), fps*5 -> 5 s timers, 1/fps -> per-frame steps, frame counts / fps -> seconds. These
    were designed for the setting to change (the Xbox build runs them at 60). FRAME_CAVE sets both globals (and
    timer+0x14) to round(1 / smoothed frame time), so they all match the real rate. Hitch frames (> 2x the average,
    e.g. loading) barely move the average (setting clamped 10-1000), so short stalls never change it; a steady new rate is picked up within
    a second. The setting only changes when the measured rate is 0.75+ fps away from it (no 59/60 flapping, never
    stuck one below the real rate). Scene init 0x247c90 resets the
    globals to 30; the next frame puts the real value back.
 3. 1/60 s clock. 0x10785c (player status manager 0x107380) counts down by max(60/fps, 1) per frame in integer
    1/60 s units, so it only works when fps divides 60. It now subtracts U60 = whole 1/60 s of real time elapsed
    this frame (FRAME_CAVE keeps the remainder), e.g. 0,1,0,1 at 120 fps.
 4. The one `fps == 60` special case, 0xa60a1f (UI/menu countdown set from a 30 fps frame count: x*2 at 60,
    x otherwise), becomes x*fps/30 for any rate (16.16 factor kept by FRAME_CAVE).
 5. Rapid stab. Player basic-attack state 0x4528c0: the mash history [state+0x74] shifts one bit per update and
    StRushAttack needs 4 presses inside the last 14 bits ([state+0x7c] = 14, set at 0x452e4e) - 467 ms at 30 fps,
    233 ms at 60 (the only step-counted gate in the attack path; same finding as the Xbox build). The shift at
    0x452e56 now goes through STAB_CAVE: shift by T30 = whole 1/30 s of real time elapsed this frame, then record
    this frame's press. 14 bits = 467 ms at every frame rate.
 6. Online co-op (v3). The game's net play is INPUT LOCKSTEP: every client simulates every player from the
    exchanged pad inputs, so all clients must run the same steps with the same step length. Found live
    (tools\hwwatch.py / syncmon.py on a 30 + 120 fps pair):
      - frame loop 0xa4a640 start: if [0x28e72a3] (network play) -> GameMain[0x3b024] = lockstep(0x5754100)
        ->vfunc+0x10 (0x97e3b0): 1 = wait (a player's next input is missing, or our step count [+0xf00] reached
        the peers' limit). While it is 1 the game update 0x251250 skips the step (only per-frame parts run).
      - a step records the local input (0xdbc7f0 -> 0xc5a730 -> 0x179260, [+0xf00]++) and the logic thread runs
        0x251250 with dt = timer+0x24; its clock 0x107380 adds dt to GameMain+0x3f820.. and counts steps (+0x3f828).
    With v2, each client stepped with its own frame time: a 120 fps host paired with a 30 fps guest got 30 steps/s
    (it waits for the guest's inputs) of 1/120 s each, so its whole game ran at 1/4 speed (quest timer, enemies)
    and drifted from the guest (shared clock 132 s vs 546 s), until the session broke up.
    Now, while the lockstep is active (network play flag and lockstep+0xf28 set), FRAME_CAVE paces frames to a
    shared ONLINE_FPS tick (sleep via sceKernelUsleep, then spin, to a deadline schedule) and makes every frame one
    stock ONLINE_FPS frame: step = 1/ONLINE_FPS, fps globals = ONLINE_FPS, U60 = 60/ONLINE_FPS, SCALE fixed, and
    the rapid-stab shift comes from the shared step number ([lockstep+0xf00] parity) instead of the local clock.
    Every client then runs identical steps at the same real-time rate. Solo play (lockstep inactive, also in the
    online Game Home when alone) keeps the variable real-time behaviour.
    v3.1: slower clients catch up with several steps per rendered frame. The frame loop already wraps the whole
    update (logic step, input record + send, lockstep sync) in a step loop - BeginSteps(timer, n) 0xa4b563 ...
    NextStep 0xa4be09, render after the loop - which retail clamps to n = 1. Online, n = 1 + whole ticks the
    client is behind (2 at 30 fps, up to MAX_STEPS = 4, i.e. full speed down to 15 fps), and before every extra
    step NEXT_CAVE redoes what the frame start does before a step: SetFps(timer, 60) (a step that had to wait for
    input zeroes the step length via SetFps(timer, 0) at 0xa4b754; v3.1 missed this, so a 30 fps client ran some
    steps with length 0 and drifted) and the lockstep check (all players' inputs for that step present?). A 30 fps
    player thus keeps the shared 60 Hz tick with 60 / 120 / 240 fps players.
    Verified (v3): 60 + 240 fps pair (12 min) and 60/240/120/60 fps four players (6 min): synced clock identical
    on every client, 60 steps/s = real time, no drop. (v3.2): 30 + 240 fps pair, 2 steps per frame on the 30 fps
    client, synced clock identical on both.

Things checked and left alone: timer frame counter +0 (only used as "already done this frame" stamps); the
timer +0x30 counter (debug display); fps*N / fps division users (correct once the globals are real).

Caves live in the unused tail of the code segment (filesz 0x27ada08, mapped to 0x27b0000): FRAME_CAVE 0x27af100,
STAB_CAVE 0x27af000 (0x27ae200/0x27ae280 NAT patch, 0x27ae300 select patch, 0x27ae700-0x27aeb00 test-only
netprobe). State in the unused data tail 0x5d36000:
  +0 last ns, +8 1/60 remainder ns, +0x10 1/30 remainder ns, +0x18 U60, +0x1c T30, +0x20 smoothed frame time (s),
  +0x24 fps/30 in 16.16, +0x28 online frame deadline (ns), +0x30 online mode.
shadPS4 `bytes` addresses are eboot va + 0x400000; in-function sites use `mask` lines (unique byte patterns).
Optional second entry: flip rate 30 Hz -> every vblank (eboot 0x4529a); frame rate = vblank_frequency / 2.
"""
import re
import struct
import sys

EBOOT = r"D:\PS4 ROMS\CUSA09120\eboot.bin"
FRAME_CAVE = 0x27AF100        # (v2 used 0x27ae400)
STAB_CAVE = 0x27AF000         # (0x27ae700-0x27aeb00: test-only netprobe caves)
STATE = 0x5D36000
LAST, ACC60, ACC30, U60, T30, EMA, SCALE = 0x00, 0x08, 0x10, 0x18, 0x1C, 0x20, 0x24
DEADLINE, ONLINE = 0x28, 0x30            # online tick: next frame deadline (ns), 1 while the lockstep runs
# Online co-op is input lockstep (see ONLINE_DOC): every player must run the same steps with the same length.
ONLINE_FPS = 60                          # shared online tick (the Xbox One build's native rate)
ONLINE_FLAG = 0x28E72A3                  # byte: network play (the frame loop only runs the lockstep check when set)
LOCKSTEP = 0x5754100                     # lockstep object: +0xf28 active, +0xf00 local step count
TICK_NS = 1_000_000_000 // ONLINE_FPS
TOL_NS = 1_000_000                       # a frame may start up to 1 ms early without waiting
USLEEP_STUB = 0x1DA1750                  # sceKernelUsleep
STEPS = 0x34                             # online: game steps to run this frame
MAX_STEPS = 4                            # full speed down to 15 fps
MAX_LAG_NS = 8 * TICK_NS                 # further behind than this (a hitch): drop the backlog
# frame-loop step loop (built for several updates per frame, retail clamps it to 1): BeginSteps(timer, n) at
# 0xa4b563, body = update (logic step, input record, lockstep sync), NextStep at 0xa4be09, render after the loop
BEGIN_CAVE = 0x27AF040
NEXT_CAVE = 0x27AF080
BEGIN_SITE, BEGINSTEPS = 0xA4B563, 0xA468D0
NEXT_SITE, NEXTSTEP = 0xA4BE09, 0xA46900
SETFPS = 0xA46940                        # SetFps(timer, fps): +0x14 fps, +0x18 1/fps, +0x24 1/fps*timescale (0 -> all 0)
GAMEMAIN = 0x3946A50                     # +0x3b024 lockstep wait flag, +0x3b028 its count
WAIT_COPY = 0x5BE4788                    # frame start copies the wait flag here too
LS_COUNT = 0xC5A890                      # lockstep count after the wait check (frame start: -> GameMain+0x3b028)
FPS_MAX = 1000
FPS_INT = 0x28EAD30
FPS_FLT = 0x28EAD34
CALL_SITE = 0xA4A71E       # vmovss xmm0,[r15+0x24]
CLOCK60_SITE = 0x10785C    # lea rdi,[fps]; mov eax,60; xor edx,edx; idiv [rdi]; test eax,eax; cmovle eax,r12d
FPS60_SITE = 0xA60A1F      # lea rdx,[fps]; ... cmp [rdx],60; jne ...
FPS60_CONT = 0xA60A6C
FPS60_KEEP = 0xA60A47      # branch target: the patched code must end before it
STAB_SITE = 0x452E56     # 0x452e56: mov cl,[r14+0x3fc] ... mov [r15+0x74],eax (22 bytes)
CLOCK_GETTIME_STUB = 0x1DA1FB0
FLIP_SITE = 0x4529A        # BE 01 00 00 00  mov esi,1  -> sceVideoOutSetFlipRate(handle, 1)
MIN_NS = 1_000_000         # 1 ms
MAX_NS = 100_000_000       # 100 ms (slower than 10 fps -> game slows down instead of jumping)
NS60 = 16_666_667
NS30 = 33_333_333


class Asm:
    """Tiny helper: raw bytes + labels, rel32 jumps/calls and rip-relative fixups."""

    def __init__(self, origin):
        self.org = origin
        self.b = bytearray()
        self.labels = {}
        self.fix = []          # (pos of rel32, end of instruction, target label or absolute address)

    def at(self):
        return self.org + len(self.b)

    def raw(self, hexstr):
        self.b += bytes.fromhex(hexstr.replace(" ", ""))

    def label(self, name):
        self.labels[name] = len(self.b)

    def rel(self, prefix_hex, target, suffix=b""):
        """Instruction = prefix + rel32 (+ suffix bytes, e.g. an imm after a rip operand)."""
        self.raw(prefix_hex)
        self.fix.append((len(self.b), len(self.b) + 4 + len(suffix), target))
        self.b += b"\0\0\0\0" + suffix

    def jcc(self, cc_hex, target):      # 0F 8x rel32
        self.rel("0F" + cc_hex, target)

    def jmp(self, target):
        self.rel("E9", target)

    def call(self, target):
        self.rel("E8", target)

    def align(self, n, fill=b"\xCC"):
        while len(self.b) % n:
            self.b += fill

    def data_f32(self, name, value):
        self.align(4)
        self.label(name)
        self.b += struct.pack("<f", value)

    def link(self):
        for pos, end, target in self.fix:
            dst = self.org + self.labels[target] if isinstance(target, str) else target
            self.b[pos:pos + 4] = struct.pack("<i", dst - (self.org + end))
        return bytes(self.b)


def frame_cave():
    a = Asm(FRAME_CAVE)
    st = lambda off: STATE + off
    # now = clock_gettime(CLOCK_MONOTONIC) in ns
    a.raw("4883EC08")                      # sub rsp,8
    a.call("get_now")
    a.raw("4883C408")                      # add rsp,8
    a.rel("488D0D", st(0))                 # lea rcx,[STATE]
    a.raw("488B11")                        # mov rdx,[rcx]       last
    a.raw("488901")                        # mov [rcx],rax       now
    a.raw("C4C17A104724")                  # vmovss xmm0,[r15+0x24]   (engine value; returned when untouched)
    a.raw("4885D2")                        # test rdx,rdx
    a.jcc("84", "first")                   # jz first
    # online co-op (lockstep running)? -> fixed shared tick
    a.rel("803D", ONLINE_FLAG, b"\x00")    # cmp byte [online],0
    a.jcc("84", "offline")
    a.rel("833D", LOCKSTEP + 0xF28, b"\x00")   # cmp dword [lockstep+0xf28],0
    a.jcc("85", "online")
    a.label("offline")
    a.raw("48C74128") ; a.b += b"\0\0\0\0"     # mov qword [rcx+DEADLINE],0
    a.raw("C7413000000000")                # mov dword [rcx+ONLINE],0
    a.raw("4829D0")                        # sub rax,rdx         dt ns
    a.raw("483D") ; a.b += struct.pack("<i", MIN_NS)   # cmp rax,MIN
    a.jcc("8D", "min_ok")                  # jge
    a.raw("B8") ; a.b += struct.pack("<I", MIN_NS)     # mov eax,MIN
    a.label("min_ok")
    a.raw("483D") ; a.b += struct.pack("<i", MAX_NS)   # cmp rax,MAX
    a.jcc("8E", "max_ok")                  # jle
    a.raw("B8") ; a.b += struct.pack("<I", MAX_NS)     # mov eax,MAX
    a.label("max_ok")
    a.raw("4889C6")                        # mov rsi,rax         keep dt
    # U60 = whole 1/60 s elapsed (remainder carried)
    a.raw("48034108")                      # add rax,[rcx+8]
    a.raw("31D2")                          # xor edx,edx
    a.raw("41B8") ; a.b += struct.pack("<I", NS60)     # mov r8d,NS60
    a.raw("49F7F0")                        # div r8
    a.raw("48895108")                      # mov [rcx+8],rdx
    a.raw("894118")                        # mov [rcx+0x18],eax  U60
    # T30 = whole 1/30 s elapsed
    a.raw("4889F0")                        # mov rax,rsi
    a.raw("48034110")                      # add rax,[rcx+0x10]
    a.raw("31D2")                          # xor edx,edx
    a.raw("41B8") ; a.b += struct.pack("<I", NS30)     # mov r8d,NS30
    a.raw("49F7F0")                        # div r8
    a.raw("48895110")                      # mov [rcx+0x10],rdx
    a.raw("89411C")                        # mov [rcx+0x1c],eax  T30
    # dt in seconds
    a.raw("C4E1F22ACE")                    # vcvtsi2ss xmm1,xmm1,rsi
    a.rel("C5F2590D", "ns2s")              # vmulss xmm1,xmm1,[ns2s]
    # smoothed frame time -> fps globals
    a.raw("C5FA105120")                    # vmovss xmm2,[rcx+0x20]   ema
    a.raw("C5E057DB")                      # vxorps xmm3,xmm3,xmm3
    a.raw("C5F82ED3")                      # vucomiss xmm2,xmm3
    a.jcc("85", "have_ema")                # jne
    a.rel("C5FA1015", "inv30")             # vmovss xmm2,[1/30]
    a.label("have_ema")
    a.raw("C5EA58DA")                      # vaddss xmm3,xmm2,xmm2    2*ema
    a.raw("C5F25DE3")                      # vminss xmm4,xmm1,xmm3    x = min(dt, 2*ema)
    a.rel("C5FA102D", "w_normal")          # vmovss xmm5,[w_normal]
    a.raw("C5F82ECB")                      # vucomiss xmm1,xmm3
    a.jcc("86", "weighted")                # jbe (dt <= 2*ema: normal frame)
    a.rel("C5FA102D", "w_hitch")           # vmovss xmm5,[w_hitch]
    a.label("weighted")
    a.raw("C5DA5CE2")                      # vsubss xmm4,xmm4,xmm2
    a.raw("C5DA59E5")                      # vmulss xmm4,xmm4,xmm5
    a.raw("C5EA58D4")                      # vaddss xmm2,xmm2,xmm4    ema += w*(x-ema)
    a.raw("C5FA115120")                    # vmovss [rcx+0x20],xmm2
    a.rel("C5FA101D", "one")               # vmovss xmm3,[1.0]
    a.raw("C5E25EDA")                      # vdivss xmm3,xmm3,xmm2    measured rate = 1/ema
    # hysteresis on the unrounded rate: keep the current setting while it is within 0.75 fps of the
    # measured rate (no 59/60 flapping, but never stuck one below the real rate)
    a.rel("C5CA2A35", FPS_INT)             # vcvtsi2ss xmm6,xmm6,[fps int]
    a.raw("C5E25CFE")                      # vsubss xmm7,xmm3,xmm6
    a.raw("C5CA5CF3")                      # vsubss xmm6,xmm6,xmm3
    a.raw("C5C25FFE")                      # vmaxss xmm7,xmm7,xmm6    |rate - setting|
    a.rel("C5F82E3D", "hyst")              # vucomiss xmm7,[0.75]
    a.jcc("82", "fps_done")                # jb
    a.raw("C5FA2DC3")                      # vcvtss2si eax,xmm3       fps = round(rate)
    a.raw("83F80A")                        # cmp eax,10
    a.jcc("8D", "lo_ok")
    a.raw("B80A000000")                    # mov eax,10
    a.label("lo_ok")
    a.raw("3D") ; a.b += struct.pack("<I", FPS_MAX)   # cmp eax,FPS_MAX
    a.jcc("8E", "hi_ok")
    a.raw("B8") ; a.b += struct.pack("<I", FPS_MAX)   # mov eax,FPS_MAX
    a.label("hi_ok")
    a.rel("8905", FPS_INT)                 # mov [fps int],eax
    a.raw("C5E22AD8")                      # vcvtsi2ss xmm3,xmm3,eax
    a.rel("C5FA111D", FPS_FLT)             # vmovss [fps float],xmm3
    a.raw("41894714")                      # mov [r15+0x14],eax
    a.label("fps_done")
    # SCALE = fps setting / 30 in 16.16 fixed point, every frame (used by the fps==60 site)
    a.rel("8B05", FPS_INT)                 # mov eax,[fps int]
    a.raw("C1E010")                        # shl eax,16
    a.raw("31D2")                          # xor edx,edx
    a.raw("41B81E000000")                  # mov r8d,30
    a.raw("41F7F0")                        # div r8d
    a.raw("894124")                        # mov [rcx+0x24],eax
    # real dt into the timer unless the game paused it (step 0)
    a.raw("418B5718")                      # mov edx,[r15+0x18]
    a.raw("85D2")                          # test edx,edx
    a.jcc("84", "done")
    a.raw("C4C17A114F18")                  # vmovss [r15+0x18],xmm1
    a.raw("C4C1725947 20".replace(" ", ""))  # vmulss xmm0,xmm1,[r15+0x20]
    a.raw("C4C17A114724")                  # vmovss [r15+0x24],xmm0
    a.label("done")
    a.raw("C3")                            # ret
    a.label("first")                       # first frame: behave like one 30 fps frame
    a.raw("C7411802000000")                # mov dword [rcx+0x18],2
    a.raw("C7411C01000000")                # mov dword [rcx+0x1c],1
    a.raw("C7412400000100")                # mov dword [rcx+0x24],0x10000   SCALE 1.0
    a.raw("C3")
    # ---- online: pace frames to the shared tick, then make this frame one stock ONLINE_FPS frame ----
    a.label("online")                      # rax = now, rcx = STATE
    a.raw("488B5128")                      # mov rdx,[rcx+DEADLINE]
    a.raw("4885D2")                        # test rdx,rdx
    a.jcc("84", "resync")
    a.raw("4889C6")                        # mov rsi,rax
    a.raw("4829D6")                        # sub rsi,rdx          now - deadline
    a.raw("4881FE") ; a.b += struct.pack("<i", MAX_LAG_NS)     # cmp rsi,MAX_LAG (far behind, e.g. a hitch: restart)
    a.jcc("8F", "resync")
    a.raw("4881FE") ; a.b += struct.pack("<i", -2 * TICK_NS)   # cmp rsi,-2*TICK (clock jump)
    a.jcc("8C", "resync")
    a.raw("4881FE") ; a.b += struct.pack("<i", -TOL_NS)        # cmp rsi,-TOL
    a.jcc("8D", "on_time")
    a.label("wait")                        # rsi = now - deadline < 0
    a.raw("48F7DE")                        # neg rsi              time left
    a.raw("4881FE") ; a.b += struct.pack("<i", 2_000_000)      # cmp rsi,2 ms
    a.jcc("8C", "spin")
    a.raw("488D86") ; a.b += struct.pack("<i", -1_500_000)     # lea rax,[rsi-1.5ms]
    a.raw("31D2")                          # xor edx,edx
    a.raw("41B8E8030000")                  # mov r8d,1000
    a.raw("49F7F0")                        # div r8               microseconds
    a.raw("89C7")                          # mov edi,eax
    a.raw("4883EC08")                      # sub rsp,8
    a.call(USLEEP_STUB)                    # sceKernelUsleep
    a.raw("4883C408")                      # add rsp,8
    a.raw("EB02")                          # jmp reclock
    a.label("spin")
    a.raw("F390")                          # pause
    a.raw("4883EC08")                      # reclock: sub rsp,8
    a.call("get_now")
    a.raw("4883C408")                      # add rsp,8
    a.rel("488D0D", st(0))                 # lea rcx,[STATE]
    a.raw("488B5128")                      # mov rdx,[rcx+DEADLINE]
    a.raw("4889C6")                        # mov rsi,rax
    a.raw("4829D6")                        # sub rsi,rdx
    a.jcc("88", "wait")                    # js wait
    a.raw("EB05")                          # jmp on_time
    a.label("resync")
    a.raw("4889C2")                        # mov rdx,rax          next tick = now
    a.raw("31F6")                          # xor esi,esi          behind = 0
    a.label("on_time")                     # rax = now, rdx = next tick, rsi = now - next (>= -TOL)
    # steps this frame = 1 + whole ticks we are behind (a 30 fps client runs 2 per frame), at most MAX_STEPS
    a.raw("4989C1")                        # mov r9,rax           now
    a.raw("4989D2")                        # mov r10,rdx          next tick
    a.raw("488D86") ; a.b += struct.pack("<i", TOL_NS)         # lea rax,[rsi+TOL]
    a.raw("31D2")                          # xor edx,edx
    a.raw("41B8") ; a.b += struct.pack("<I", TICK_NS)          # mov r8d,TICK
    a.raw("49F7F0")                        # div r8
    a.raw("FFC0")                          # inc eax              steps
    a.raw("83F8") ; a.b += bytes([MAX_STEPS])                   # cmp eax,MAX_STEPS
    a.jcc("86", "steps_ok")                # jbe
    a.raw("B8") ; a.b += struct.pack("<I", MAX_STEPS)          # mov eax,MAX_STEPS
    a.label("steps_ok")
    a.raw("894134")                        # mov [rcx+STEPS],eax
    a.raw("4869C0") ; a.b += struct.pack("<i", TICK_NS)        # imul rax,rax,TICK
    a.raw("4901C2")                        # add r10,rax          next tick += steps*TICK
    a.raw("4C895128")                      # mov [rcx+DEADLINE],r10
    a.raw("4C8909")                        # mov [rcx],r9         last = now
    a.raw("C7413001000000")                # mov dword [rcx+ONLINE],1
    a.raw("C74118") ; a.b += struct.pack("<I", 60 // ONLINE_FPS)        # U60
    a.raw("C7411C") ; a.b += struct.pack("<I", 30 // ONLINE_FPS)        # T30 (stab cave uses the step count)
    a.raw("C74124") ; a.b += struct.pack("<I", (ONLINE_FPS << 16) // 30)  # SCALE
    a.raw("C74120") ; a.b += struct.pack("<f", 1 / ONLINE_FPS)          # ema
    a.rel("C705", FPS_INT, struct.pack("<I", ONLINE_FPS))               # mov dword [fps int],R
    a.rel("C705", FPS_FLT, struct.pack("<f", float(ONLINE_FPS)))        # mov dword [fps float],R
    a.raw("41C74714") ; a.b += struct.pack("<I", ONLINE_FPS)            # mov dword [r15+0x14],R
    a.raw("418B5718")                      # mov edx,[r15+0x18]
    a.raw("85D2")                          # test edx,edx         paused (step 0): keep
    a.jcc("84", "online_ret")
    a.rel("C5FA100D", "inv_tick")          # vmovss xmm1,[1/R]
    a.raw("C4C17A114F18")                  # vmovss [r15+0x18],xmm1
    a.raw("C4C1725947 20".replace(" ", ""))  # vmulss xmm0,xmm1,[r15+0x20]
    a.raw("C4C17A114724")                  # vmovss [r15+0x24],xmm0
    a.label("online_ret")
    a.raw("C3")
    # rax = clock_gettime(CLOCK_MONOTONIC) in ns (called with rsp 16-aligned)
    a.label("get_now")
    a.raw("4883EC18")                      # sub rsp,0x18
    a.raw("BF04000000")                    # mov edi,4
    a.raw("4889E6")                        # mov rsi,rsp
    a.call(CLOCK_GETTIME_STUB)
    a.raw("48690424") ; a.b += struct.pack("<I", 1_000_000_000)   # imul rax,[rsp],1e9
    a.raw("4803442408")                    # add rax,[rsp+8]
    a.raw("4883C418")                      # add rsp,0x18
    a.raw("C3")
    a.data_f32("inv_tick", 1 / ONLINE_FPS)
    a.data_f32("ns2s", 1e-9)
    a.data_f32("inv30", 1 / 30)
    a.data_f32("one", 1.0)
    a.data_f32("w_normal", 1 / 8)
    a.data_f32("w_hitch", 1 / 256)
    a.data_f32("hyst", 0.75)
    return a.link()


def stab_cave():
    a = Asm(STAB_CAVE)                     # in: eax = press history, r14 = player, r15 = attack state
    a.rel("833D", STATE + ONLINE, b"\x00")  # cmp dword [ONLINE],0
    a.jcc("85", "online")
    a.rel("8B0D", STATE + T30)             # mov ecx,[T30]
    a.label("shift")
    a.raw("D3E0")                          # shl eax,cl        one bit per 1/30 s of real time
    a.raw("410FB68EFC030000")              # movzx ecx,byte [r14+0x3fc]
    a.raw("D1E9")                          # shr ecx,1
    a.raw("83E101")                        # and ecx,1          attack pressed this frame
    a.raw("09C8")                          # or eax,ecx
    a.raw("41894774")                      # mov [r15+0x74],eax
    a.raw("C3")
    # online: every player simulates this, so the shift must come from the shared lockstep step number
    a.label("online")
    a.rel("8B0D", LOCKSTEP + 0xF00)        # mov ecx,[lockstep step count]
    a.raw("F7C1") ; a.b += struct.pack("<I", ONLINE_FPS // 30 - 1)   # test ecx,R/30-1
    a.raw("0F94C1")                        # sete cl           1 on every (R/30)th step = once per 1/30 s
    a.raw("0FB6C9")                        # movzx ecx,cl
    a.jmp("shift")
    return a.link()


def begin_cave():
    a = Asm(BEGIN_CAVE)                    # in: rdi = timer, esi = steps (retail: min(BoostFrame, 1))
    a.rel("833D", STATE + ONLINE, b"\x00")  # cmp dword [ONLINE],0
    a.raw("7406")                          # je +6
    a.rel("8B35", STATE + STEPS)           # mov esi,[STEPS]
    a.jmp(BEGINSTEPS)
    return a.link()


def next_cave():
    a = Asm(NEXT_CAVE)                     # in: rdi = timer; replaces `call NextStep` at the loop's back edge
    a.raw("53")                            # push rbx
    a.raw("4889FB")                        # mov rbx,rdi
    a.call(NEXTSTEP)
    a.rel("833D", STATE + ONLINE, b"\x00")  # cmp dword [ONLINE],0
    a.jcc("84", "done")
    a.raw("8B4304")                        # mov eax,[rbx+4]
    a.raw("3B4308")                        # cmp eax,[rbx+8]
    a.jcc("83", "done")                    # jae: no further step this frame
    # another step follows: redo what the frame start does before a step. A waiting step zeroes the step length
    # (SetFps(timer, 0) at 0xa4b754); the frame start's SetFps(timer, fps) restores it, so do the same here.
    a.raw("4889DF")                        # mov rdi,rbx
    a.raw("BE") ; a.b += struct.pack("<I", ONLINE_FPS)   # mov esi,R
    a.call(SETFPS)
    # then the lockstep check (are all players' inputs for this step here?)
    a.rel("488D3D", LOCKSTEP)              # lea rdi,[lockstep]
    a.raw("488B07")                        # mov rax,[rdi]
    a.raw("FF5010")                        # call [rax+0x10]      1 = wait
    a.rel("8805", GAMEMAIN + 0x3B024)      # mov [GameMain+0x3b024],al
    a.rel("8805", WAIT_COPY)               # mov [copy],al
    a.raw("0FB6F0")                        # movzx esi,al
    a.rel("488D3D", LOCKSTEP)              # lea rdi,[lockstep]
    a.call(LS_COUNT)
    a.rel("8905", GAMEMAIN + 0x3B028)      # mov [GameMain+0x3b028],eax
    a.label("done")
    a.raw("5B")                            # pop rbx
    a.raw("C3")
    return a.link()


def site_clock60():
    a = Asm(CLOCK60_SITE)
    a.rel("8B05", STATE + U60)             # mov eax,[U60]
    a.b += b"\x90" * (22 - len(a.b))
    return a.link()


def site_fps60():
    a = Asm(FPS60_SITE)
    a.raw("0FB68888010000")                # movzx ecx,byte [rax+0x188]   x (30 fps frames)
    a.rel("0FAF0D", STATE + SCALE)         # imul ecx,[SCALE]             x * fps/30 (16.16)
    a.raw("C1E910")                        # shr ecx,16
    a.raw("4489C0")                        # mov eax,r8d
    a.raw("664189 8C4208020000".replace(" ", ""))  # mov [r10+rax*2+0x208],cx
    rel8 = FPS60_CONT - (a.at() + 2)
    assert -128 <= rel8 < 128
    a.b += bytes([0xEB, rel8 & 0xFF])      # jmp short FPS60_CONT
    assert len(a.b) <= FPS60_KEEP - FPS60_SITE   # 0xa60a47 is a branch target (jne at 0xa60a09)
    return a.link()


def branch_targets(md, text, lo, hi):
    """All jmp/jcc/call targets found by linear disassembly of [lo, hi)."""
    out = set()
    for i in md.disasm(text[lo:hi], lo):
        if (i.mnemonic.startswith("j") or i.mnemonic == "call") and i.op_str.startswith("0x"):
            out.add(int(i.op_str, 16))
    return out


def check_no_branch_into(md, text, site, length):
    fs = text.rfind(b"\x55\x48\x89\xe5", 0, site + 1)
    bad = [t for t in branch_targets(md, text, fs, site + 0x4000) if site < t < site + length]
    assert not bad, "branch into patched range %#x+%d: %s" % (site, length, [hex(b) for b in bad])


def main():
    d = open(EBOOT, "rb").read()
    text = d[0x4000:0x4000 + 0x27ADA08]
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64
    md = Cs(CS_ARCH_X86, CS_MODE_64)

    def show(name, code, at, stop_at_data=True):
        print(name, file=sys.stderr)
        for i in md.disasm(code, at):
            print("  %08x: %-10s %-40s %s" % (i.address, i.mnemonic, i.op_str, i.bytes.hex()), file=sys.stderr)

    def unique(pat):
        assert len(re.findall(re.escape(pat), text)) == 1, pat.hex()
        return pat

    # call site
    assert text[CALL_SITE:CALL_SITE + 6] == bytes.fromhex("C4C17A104724")
    pat_call = unique(text[CALL_SITE - 6:CALL_SITE + 12])
    call = b"\xE8" + struct.pack("<i", FRAME_CAVE - (CALL_SITE + 5)) + b"\x90"
    # 1/60 clock
    assert text[CLOCK60_SITE:CLOCK60_SITE + 22] == bytes.fromhex("488D3DCD347E02B83C00000031D2F73F85C0410F4EC4")
    pat_c60 = unique(text[CLOCK60_SITE - 8:CLOCK60_SITE + 22])
    # fps == 60
    assert text[FPS60_SITE:FPS60_SITE + 0x10] == bytes.fromhex("488D150AA3E8018B8888010000833A3C"), text[FPS60_SITE:FPS60_SITE + 0x10].hex()
    pat_f60 = unique(text[FPS60_SITE - 10:FPS60_SITE + 0x28])
    # rapid stab
    assert text[STAB_SITE - 8:STAB_SITE + 22] == bytes.fromhex("41C7477C0E000000418A8EFC030000D0E980E1010FB6C98D044141894774")
    pat_stab = unique(text[STAB_SITE - 8:STAB_SITE + 22])
    stab_call = b"\xE8" + struct.pack("<i", STAB_CAVE - (STAB_SITE + 5)) + b"\x90" * 17
    # flip rate
    assert text[FLIP_SITE:FLIP_SITE + 5] == bytes.fromhex("BE01000000")
    flip_pat = unique(text[FLIP_SITE - 11:FLIP_SITE + 10])

    # online multi-step: BeginSteps / NextStep calls in the frame loop
    def widen(site, after=10):
        back = 10
        while len(re.findall(re.escape(text[site - back:site + after]), text)) != 1:
            back += 4
            assert back < 80
        return text[site - back:site + after], back
    for site, callee in ((BEGIN_SITE, BEGINSTEPS), (NEXT_SITE, NEXTSTEP)):
        assert text[site] == 0xE8 and site + 5 + struct.unpack_from("<i", text, site + 1)[0] == callee, hex(site)
    pat_begin, off_begin = widen(BEGIN_SITE)
    pat_next, off_next = widen(NEXT_SITE)
    begin_call = bytes([0xE8]) + struct.pack("<i", BEGIN_CAVE - (BEGIN_SITE + 5))
    next_call = bytes([0xE8]) + struct.pack("<i", NEXT_CAVE - (NEXT_SITE + 5))

    fc, sc, c60, f60 = frame_cave(), stab_cave(), site_clock60(), site_fps60()
    bc, nc = begin_cave(), next_cave()
    assert STAB_CAVE + len(sc) <= BEGIN_CAVE and BEGIN_CAVE + len(bc) <= NEXT_CAVE and NEXT_CAVE + len(nc) <= FRAME_CAVE
    assert FRAME_CAVE + len(fc) <= 0x27B0000
    for site, length in ((CALL_SITE, 6), (CLOCK60_SITE, len(c60)), (FPS60_SITE, len(f60)), (STAB_SITE, 22)):
        check_no_branch_into(md, text, site, length)
    show("frame cave", fc, FRAME_CAVE)
    show("stab cave", sc, STAB_CAVE)
    show("begin-steps cave", bc, BEGIN_CAVE)
    show("next-step cave", nc, NEXT_CAVE)
    show("1/60 clock site", c60, CLOCK60_SITE)
    show("fps==60 site", f60, FPS60_SITE)
    hexpat = lambda b: " ".join("%02X" % x for x in b)
    xml = f'''<?xml version="1.0"?>
<Patch>
    <TitleID>
        <ID>CUSA09120</ID>
    </TitleID>
    <Metadata Title="Happy Dungeons" Name="Decouple gameplay from frame rate (real time everywhere)" Author="local" Note="Every game timing follows real time at any frame rate: real delta time into the engine timer, the engine fps setting follows the measured frame rate (fixes ~100 seconds-to-frames conversions), the integer 1/60 s clock and the fps==60 special case count real time, and the rapid stab mash window is 467 ms at any fps. Online co-op is input lockstep, so there every player runs the same fixed {ONLINE_FPS} Hz tick (each step exactly 1/{ONLINE_FPS} s; faster PCs pace their frames to it, slower ones such as 30 fps run several steps per frame) and players on any frame rate stay in sync. Built by tools\\make_fps_patch.py. Caves at eboot {FRAME_CAVE:#x} / {STAB_CAVE:#x}, state at 0x5d36000." PatchVer="3.2" AppVer="01.11" AppElf="eboot.bin" isEnabled="true">
        <PatchList>
            <Line Type="bytes" Address="0x{FRAME_CAVE + 0x400000:08x}" Value="{fc.hex()}"/>
            <Line Type="bytes" Address="0x{STAB_CAVE + 0x400000:08x}" Value="{sc.hex()}"/>
            <Line Type="bytes" Address="0x{BEGIN_CAVE + 0x400000:08x}" Value="{bc.hex()}"/>
            <Line Type="bytes" Address="0x{NEXT_CAVE + 0x400000:08x}" Value="{nc.hex()}"/>
            <!-- eboot 0xa4b563: BeginSteps(timer, n) -> online: n = ticks due this frame (2 at 30 fps) -->
            <Line Type="mask" Address="{hexpat(pat_begin)}" Value="{begin_call.hex()}" Offset="{off_begin}"/>
            <!-- eboot 0xa4be09: NextStep(timer) -> online: before another step, redo the lockstep input check -->
            <Line Type="mask" Address="{hexpat(pat_next)}" Value="{next_call.hex()}" Offset="{off_next}"/>
            <!-- eboot 0xa4a71e: vmovss xmm0,[r15+0x24] -> call frame cave; nop -->
            <Line Type="mask" Address="{hexpat(pat_call)}" Value="{call.hex()}" Offset="6"/>
            <!-- eboot 0x10785c: max(60/fps,1) -> mov eax,[U60] -->
            <Line Type="mask" Address="{hexpat(pat_c60)}" Value="{c60.hex()}" Offset="8"/>
            <!-- eboot 0xa60a1f: fps==60 ? x*2 : x  ->  x*fps/30 -->
            <Line Type="mask" Address="{hexpat(pat_f60)}" Value="{f60.hex()}" Offset="10"/>
            <!-- eboot 0x452e56: history = history*2 | press -> call stab cave (shift by real 1/30 s ticks) -->
            <Line Type="mask" Address="{hexpat(pat_stab)}" Value="{stab_call.hex()}" Offset="8"/>
        </PatchList>
    </Metadata>
    <Metadata Title="Happy Dungeons" Name="Frame rate: lift the 30 fps cap (flip every vblank)" Author="local" Note="sceVideoOutSetFlipRate(handle, 1) -> 0 at eboot 0x4529a. The game renders every second vblank, so the frame rate becomes half of shadPS4's vblank_frequency (120 -> 60 fps, 240 -> 120 fps). Use together with the decouple patch." PatchVer="1.0" AppVer="01.11" AppElf="eboot.bin" isEnabled="true">
        <PatchList>
            <Line Type="mask" Address="{hexpat(flip_pat)}" Value="be00000000" Offset="11"/>
        </PatchList>
    </Metadata>
</Patch>
'''
    sys.stdout.write(xml)


if __name__ == "__main__":
    main()
