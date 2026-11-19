# Happy Dungeons on PS4/shadPS4: plan

Goal: the PS4 build (CUSA09120 v1.11) running in shadPS4 against servers on this PC only.
The real Toylogic hosts, Sony's PSN and the public shadNet server are never contacted.

## How it fits together

```
shadPS4 (portable copy, D:\dungeons project\ps4\shadps4)
  ├─ PSN calls (sign-in, TUS saves, Matching2 lobbies, signalling, friends, WebAPI)
  │     └─ shadNet server on 127.0.0.1  (TCP 31313 game, 31315 WebAPI, UDP 31314 signalling)
  └─ Toylogic HTTP calls (URLs rewritten by the shadPS4 patch HappyDungeonsLocal)
        └─ Happy Dungeons services on 127.0.0.1:5100 (Python, reusing experimentalserver.py pieces):
           Vulcan delivery API (events, shop, cards), telemetry sink, legal page
```

## Steps

1. **Boot to the title screen, isolated. DONE (2026-11-19).**
   - Portable shadPS4 (copy of the `sgdc3/lbp3-compat` build you last used) with its own `user` folder:
     network off, shadNet off (server fields already set to 127.0.0.1), mute, no Discord, controller input
     only when focused. Your `%APPDATA%\shadPS4` settings are untouched (hashes checked before/after).
   - shadPS4 patch `user\patches\HappyDungeonsLocal\HappyDungeons.xml`: byte-pattern patches that rewrite the four
     hard-coded Toylogic URLs to `http://127.0.0.1:5100/...` (all four applied, per the log).
   - Service-end and time-bomb: the PS4 v1.11 build has neither (no ServiceEnd code, maintenance data disabled),
     so no exe patch is needed for them.
   - Tested on a hidden desktop: title screen renders; pressing ✕ shows "You must be connected to
     PlayStation™Network". Screenshots: `runs\boot3_offline\`.
2. **Local shadNet (PSN replacement). DONE (2026-11-19).**
   - `shadnet\` = `shadnet-win64-2026-11-16-a53dad5.zip` (server 0.1.9, sha256 verified; the bundled
     `vc_redist.x64.exe` was not extracted). `shadnet.cfg`: `Host=127.0.0.1` (every listener: TCP 31313 game,
     31315 WebAPI, 31350 admin; UDP 31314 signalling), `Matching2Enabled=true`, stats off, avatar URL → :5100.
     Data in `shadnet\db\shadnet.db` (saves = TUS rows).
   - Local accounts: `MyPlayer` (main, user 1000 in the portable shadPS4) and `HDFresh01` (test). Created with
     `tools\shadnet_register.py` (protocol command 2); passwords live only in `shadps4\user\users.json`.
   - `server\hd_services.py` on 127.0.0.1:5100: Vulcan list (same 41-byte "no files" reply the live server
     gives), telemetry sink, legal page.
   - Result: sign-in, WebAPI, presence, Vulcan and telemetry all local; the game creates and loads its save.
   - **Save fix:** shadNet answers an empty cloud slot with "success, 0 bytes", but the game's save loader
     (eboot 0x9db590) only treats error 0x8055084A (user storage data not found) as "no save yet", so a new
     account got "The saved game is corrupt". Second patch in `HappyDungeons.xml` makes a 0-byte read take the
     not-found path. Verified: a fresh account goes straight to "A new Happy Dungeons save file will be
     created", and the next boot loads that save with no prompt (`runs\savefix1_fresh`, `runs\savefix2_reload`).
     (The MyPlayer save was made before the fix, through the "corrupt → Yes" prompt; it is an ordinary new save.)
   - To play: `Play Happy Dungeons (PS4).bat`.
3. **Event/shop data (Vulcan delivery).** Reply format known (Happy Wars capture); the PS4 build is internal
   v1.13.0 and only accepts v1.13.x event packs. A conservative set is staged, not live — see DELIVERY.md.
   Next: test it on the HDFresh01 test account, then go live; later decide whether to re-stamp v1.14 events
   after checking their content against the PS4 base data.
4. **Saves.** Find the PS4 TUS slot layout (slot 1 = 181 B, slot 2 = 5,769 B in your 6 Nov run), then
   bring an Xbox save over (the inner format is the same zlib JSON) if you want your Xbox character on PS4.
   The save from your 6 Nov run is on the public shadNet server; it is only reachable through your
   own account there, so that one is yours to export if you want it.
5. **Store and shops.** Happy Jewels (NpCommerce + WebAPI entitlements, 404 today), free shop via the
   delivered `shop_catalog.bin` like on Xbox.
6. **Multiplayer.** Matching2 rooms + UDP signalling through the local shadNet; test with two shadPS4
   instances (users 1000 and 1001) on this PC, then LAN/tailnet friends.
   - **Matchmaking fix (2026-11-19):** the game picks its world by INDEX from `DungeonLobbyMenuParam.json`
     (`lobbyMatching_`: Story SIEJA/SIEA/SIEE = 0/1/2, Mugen 3/4/5, Event 6/7/8) and needs ten worlds on
     server 1. shadNet's default is one world, so the lobby looped on GetWorldInfoList ("Searching for players")
     forever. `shadnet\worlds.cfg` now maps CUSA09120 to group `happydungeons` with worlds 1–10 (read at
     shadNet start). Verified: 10 worlds → SearchRoom world 3 → no rooms → CreateJoinRoom (room 1, 4 slots) +
     WebAPI session + room attributes. Not yet tested: a second player finding and joining that room.
   - **Players can now play together (2026-11-19).** Four fixes were needed, found with test-only probes
     (`tools\make_netprobe_patch.py` + `tools\netprobe.py`: counters on the game's sendto/recvfrom/connect/select
     call sites, signaling listeners, event-bus hops and member/session tables, readable live):
     1. STUN port: shadPS4 sends its STUN ping to the shadNet port + 1. Clients pointed straight at 31413 pinged
        31414 (nothing there); shadNet never learned peer addresses. Clients must use 31313 (gateway), whose +1 is
        shadNet's STUN port 31314. The fps launchers set `Network.shadnet_server=127.0.0.1:31313`.
     2. Same data server: matchmaking filters on search attribute 0x4e = a hash of the delivered event data, so all
        players must get Vulcan data from the same server (the 5110 stand-in vs the 5100 gateway never matched).
     3. **select() nfds = 0** (the real blocker): the game's only select() (wrapper 0x8efc00, used by the P2P send
        pump 0x879160) passes nfds 0; PS4 ignores it, shadPS4 honours it, so the socket never reads writable and the
        game never sends one P2P packet (lobby: others without character/ready, then "continuous communication
        cannot be maintained" - this hit every shadPS4 player, not only same-PC tests). Patch block "Network:
        select() checks the game's socket (online co-op)" (tools\make_net_patch.py, cave 0x27ae300) passes fd + 1.
     4. Same-PC testing: each instance needs its own `Network.p2p_port` (3658/3660/3662/...). The game's stream
        connect (0x8efa00) and one sendto (0x8efec0) hard-code port 3658 but are not used in the Story flow.
     Game network layout found on the way: GNet sessions (`GNetConsole`, modes local/TCP/UDP/Redirect,
     set-mode 0xd64820, state machine 0xd67c50 field +0x94, 0xc1a = connected) live in the manager's list
     +0x110; per-member slots ([0x504dc58]+0x98, 0x190 bytes, addr +0x13c/+0x144, port +0x148) are filled by
     0x8fbc10 and turned into sessions by 0x875e90 -> manager vfunc +0x60 (0x8779d0).
     Verified: HDFps01 + HDMp02 (30 fps each, same PC) see each other in the lobby and play Cookie Thief together.
   - **Mixed frame rates fixed (fps patch v3, 2026-11-19).** Co-op is INPUT LOCKSTEP (every client simulates every
     player from exchanged pad inputs; lockstep object eboot 0x5754100, wait check 0x97e3b0 at the frame-loop start
     -> GameMain[0x3b024], step = game update 0x251250 with dt = timer+0x24, step clock 0x107380 -> GameMain+0x3f820/
     +0x3f828). Found with tools\hwwatch.py (hardware write-watch on a test client) and tools\syncmon.py. With v2 each
     client stepped with its own frame time, so the 120 fps host ran 30 steps/s x 1/120 s (1/4 speed, clocks 132 s vs
     546 s). v3 (tools\make_fps_patch.py point 6): while the lockstep runs, every client paces frames to a shared
     60 Hz tick and each step is exactly 1/60 s (fps globals 60, rapid-stab shift from the shared step number).
     Solo / alone in Game Home keeps the variable rate. v3.2: clients below 60 fps run several steps per rendered
     frame (the frame loop's BeginSteps/NextStep step loop, retail-clamped to 1; before every extra step the step
     length is restored - a waiting step zeroes it via SetFps(timer, 0) - and the lockstep input check is redone),
     so 30 fps players keep the shared tick too: 30 + 240 fps verified with identical synced clocks. Verified (v3): 60 + 240 fps for 12 min
     and 60/240/120/60 fps four players for 6 min - identical synced clock everywhere, 60 steps/s = real time.
   - (history) **Mixed frame rates before v3.** 30 fps + 120 fps clients connect and play, but the game's net sync is
     per frame: each client sends one packet per rendered frame (120 fps sent 32544, 30 fps 8224), the 120 fps
     host's quest timer runs at 30/120 speed (its synced clock advances with the 30 fps peer's frames but counts
     them as 1/120 s), and after ~4.5 min the 120 fps client left the room. Next: run the network sync on a fixed
     real-time tick (like the T30 counter of the frame-rate patch) instead of per rendered frame. Quest timer chain:
     HUD 0x1a49880 <- double-buffered state 0x1839060 (obj+0x2fd450) <- 0x18389e0 <- 0x424070.
     Solo at 120 fps the quest timer runs at real speed, so the slowdown comes from the net sync. Packet capture
     (test-only ring, tools\netring.py): in the dungeon every client sends one packet per rendered frame, but all
     counters inside (two +4 fields, two +0x10 fields, the u16 frame number at the end) advance per 1/30 s on both
     30 and 120 fps clients - the 120 fps client just repeats each value ~4 times. So the payload is fps-independent;
     the receiver probably counts received packets as its own frames (30 packets/s from a 30 fps peer = 30 of the
     120 fps host's 1/120 s frames -> 30/120 speed). Next: send the sync packet once per 1/30 s (T30) instead of per
     frame, or fix the receive-side counter; then a mixed 30/60/120 dungeon run of 10+ minutes.
   - Test clients: shadps4-fps (HDFps01, p2p 3660), shadps4-mp2/3/4 (HDMp02/03/04, p2p 3662/3664/3666), all with a
     copy of MyPlayer's save; tools\hd_fps.ps1 / hd_mp2.ps1 / hd_mp3.ps1 / hd_mp4.ps1; runs\mp_pair.sh, mp_quad.sh.
7. **Packaging.** One `Play Happy Dungeons (PS4).bat`: start shadNet + the Python services, launch the
   portable shadPS4.
8. **The live friends server (VPS).** Built and tested locally; deploy awaiting the user's OK - see DEPLOY-VPS.md.
   - `server\hd_ps4_gateway.py` (stdlib Python, no change to experimentalserver.py): shadNet proxy that answers a
     linked player's TUS saves from their Dungeons saves (via the server's title-storage route + their key; version
     20180329 <-> 20171130), Vulcan list from the server's ServerData API (+ base pictures, service-end Pic_Top01
     replaced, event packs restamped v1.13), friends sync (shadNet admin API), link API (/ps4/link, /ps4/register).
   - Local layout now: shadNet 127.0.0.1:31413 (RegistrationSecretKey set, admin `HDGateway`), gateway
     127.0.0.1:31313 + :5100, local copy of the live server `livecopy\server` on 127.0.0.1:5200 (same md5 as
     live). Config `livecopy\gateway.json`, links/logs `livecopy\gateway-state\`. The Play launcher starts all three.
   - Verified: Xbox save (Lv 15) loads/saves on PS4 through it; live event + shop + gifts work; register/link/
     friends work. Gap: event quest names show as text keys (needs a PS4 dictionary pack with 1.15 strings).
   - Player side: `deploy\client\HappyDungeons.xml` (URLs -> http://107.170.39.185) + `tools\ps4_link.py`.

## The download step 2 used (approved 2026-11-19)

`shadnet-win64-2026-11-16-a53dad5.zip`, 24.7 MB, sha256 `bf9e5bba34f4e6251ce89dc635e870f2bdfd3fba9d0ed3f8e947cf4a8d22da82`,
from the shadNet project's own pre-release:
https://github.com/shadps4-emu/shadNet/releases/download/pre-release-shadnet-2026-11-16-a53dad52ae04aba321e62bcff8dc3743ba3e4814/shadnet-win64-2026-11-16-a53dad5.zip

It would go into its own folder `D:\dungeons project\ps4\shadnet\`, checked against that hash, and only
ever run bound to 127.0.0.1.

Alternatives if you'd rather not download a binary:
- build shadNet from source: needs the source plus the protobuf/abseil submodules and the Qt HttpServer and
  WebSockets modules (your Qt 6.10.3 lacks those two), so it means more downloads, not fewer;
- write our own small shadNet-compatible server in Python from the protocol (no download, but Matching2
  and signalling make it the slowest route).

## Tools made for this

- `tools\hd.ps1 start | key <cross|circle|up|down|...> | shot | status | stop`: runs the game on a hidden
  desktop, presses keys in it, takes screenshots with shadPS4's own hotkey (F12, rebound in the portable
  input config). Nothing shows on your screen.
- `tools\run_hidden.ps1`: timed run with log capture. `tools\HiddenDesk.cs`: the hidden-desktop helper.

## Frame rate: gameplay decoupled from fps (2026-11-19)

- Engine: one gameplay update per rendered frame, advanced by a fixed step from the global timer (eboot 0x5782ea4:
  +0x14 fps 30, +0x18 1/fps, +0x20 timescale, +0x24 step*timescale, +0x2c dt). Frame loop 0xa4a640 calls SetFps
  (0xa46940) then hands `[timer+0x24]` to SetDelta (0xa469f0) and many subsystems. Stock = frame-locked: 60 fps
  plays at 2x speed.
- Patch `HappyDungeonsFps` (built by tools\make_fps_patch.py, in shadps4\ and shadps4-fps\ profiles):
  0xa4a71e `vmovss xmm0,[r15+0x24]` -> call to a cave (eboot 0x27ae100, unused code tail) that measures real time
  since the last frame (clock_gettime MONOTONIC, clamped 1-100 ms) and writes it into +0x18/+0x24; state at
  0x5d36000 (unused data tail). shadPS4 `bytes` patch addresses = eboot va + 0x400000.
- Frame rate = half of shadPS4's vblank_frequency (the game waits two vblanks per frame): 60 -> 30 fps,
  120 -> 60, 240 -> 120, 480 -> 240. `Play Happy Dungeons (PS4) - 60 fps.bat` sets 120 (or pass another rate);
  the normal launcher sets 60.
- Verified (runs\abg_A_30_dt vs abg_B_60_dt, one screenshot per second): every intro-cutscene event is exactly
  2.0 s earlier at 60 fps (faster loading), intervals identical (first subtitle -> knight = 19.0 s both);
  unpatched 60 fps ran about 2x fast (runs\ab_C_60_nodt).
- **v2 "real time everywhere" (2026-11-19).** v1 only fixed systems that read the timer's delta; the rest of the
  engine converts with its fps setting (globals 0x28ead30 int / 0x28ead34 float, fixed at 30): ~100 sites do
  seconds*fps -> frame waits (e.g. `call 0xb1bca0`, the waitFrame/FrameWait path), fps*5 -> 5 s timers, 1/fps ->
  per-frame steps. At 60 fps those all ran 2x fast, and rapid stab needed twice the mash rate. The engine was built
  for that setting to change (the Xbox build runs it at 60), so v2 makes it follow the measured frame rate:
  - frame cave now at 0x27ae400: real dt as before; fps globals + timer+0x14 = round(1/smoothed frame time)
    (hitch frames > 2x the average barely count; the setting moves only when the measured rate is 0.75+ fps away, so it never sticks at 59 for 60; clamped 10-1000); U60/T30 = whole
    1/60 s and 1/30 s of real time this frame, remainders carried (state 0x5d36000: +0 last ns, +8/+0x10
    remainders, +0x18 U60, +0x1c T30, +0x20 smoothed frame time, +0x24 fps/30 in 16.16).
  - 0x10785c integer 1/60 s countdown (player status manager 0x107380: `max(60/fps,1)` per frame, only right
    when fps divides 60) -> subtracts U60.
  - 0xa60a1f the one `fps == 60` special case (x*2 at 60, x otherwise) -> x * (fps/30 in 16.16, kept by the
    frame cave), so no ceiling.
  - rapid stab: basic-attack state 0x4528c0 keeps a mash history at [state+0x74], 14-bit window ([state+0x7c],
    set at 0x452e4e), 4 presses -> StRushAttack. The shift at 0x452e56 -> cave 0x27ae600: shift by T30, then
    record the press. Window = 467 ms at any fps (Xbox build: same gate, its only step-counted one).
  - Checked and left alone: timer frame counter +0 (only "done this frame" stamps), timer +0x30 (debug display).
  - Verified live (tools\fpsmon.py + per-frame poll): at 60 fps the globals read 60, U60 totals 60/s and T30
    30/s (sequence 0,1,0,1...); same at 30 (globals 30, U60 2/frame = stock) and 120 (T30 0,0,0,1); at vblank
    480 the game runs ~240 fps, globals ~240, clocks still 60/30 per s, fps/30 factor 8.0. Old v1 file kept as
    runs\HappyDungeons_fps_v1_backup.xml.
  - The user's playtest at 60 fps (2026-11-19): rapid stab works.
- Still to check: a sweep for remaining per-frame counters with fixed (30 fps) limits in enemy AI / gimmicks / UI,
  then a mixed 30/60 two-client dungeon run.

## Normal shadPS4 builds, patch pack, local saves (2026-11-19)

- No change to shadPS4 itself: everything is game patches + servers. The tested portable shadPS4.exe is byte-identical
  to the user's `shadps4-lbp3-win64` build. The official v0.21.1 pre-release (Qt launcher "Pre-release") also works:
  all patches apply, 30 + 240 fps co-op stayed in sync (5 min soak). v0.18.0 lacks part of the online code.
- Per-game settings work: custom_configs/CUSA09120.json overrides Network.shadnet_server and GPU.vblank_frequency
  for Happy Dungeons only (the file must be a FULL config copy; a partial file crashed the pre-release at start).
- Patch pack for any shadPS4 (Windows/macOS/Linux): `tools\build_patch_pack.py [--server vps|local]` ->
  dist\HappyDungeons-patches[-local]\HappyDungeons (HappyDungeons.xml, HappyDungeons_fps.xml,
  HappyDungeons_saves.xml, files.json) + INSTALL.txt, zipped.
- Optional local saves (`tools\make_localsave_patch.py`, off by default): the five TUS calls (GetDataA x3, SetDataA,
  DeleteMultiSlotDataA) go to file caves at 0x27af600; files /download0/hds_<account>_<slot>.bin/.bak
  (= user/download/CUSA09120). Verified: loads a seeded save, saves back (no server writes), reloads its own save,
  fresh start = new player. The PS4 savedata folder (home/<user>/savedata) is not reachable: the game never imports
  libSceSaveData and a patch cannot add imports or create folders (no mkdir import).

## Open: bombs freeze in mid-air above 60 fps (solo, variable rate) - parked

- Reported at 240 fps; bombs work at 30 and 60 fps (online co-op runs at 60 Hz, so it is fine there).
- Likely cause: bouncing bullets (`behaviorType_` "BoundStraight", type 4) keep `dampingRate_` of their speed per
  bounce and stop when slower than `thresholdVelocity_` (0.05-0.6), tuned per 30 fps frame. With smaller steps a bomb
  touching a wall/stake reads as "slow enough" and stops where it is. Bullet data: AriesDataOrbis/dungeon/json/bullet/
  bulletParam.json (ZRES = 8-byte header + zlib). Users: player action PAC_00010_5thDemension_01 (bullet
  PA_5thDemension_Bomb, every class), Item_Bomb (tool), HWJack (Halloween event enemy), grenades, P_Banana.
- Code so far: BoundStraight parameter parser 0xa9f3f0 (vtable 0x2809768); in each bullet definition the block is at
  +0xe0 (damping +0xe8, threshold +0xec; constructor 0xa9ea00). The compare against the threshold is not found yet
  (no direct compare with [reg+0xec]; likely loaded into a register or squared). Next: throw that bomb on a test
  client and catch the bounce code with a read watchpoint on the bullet's threshold (tools/hwwatch.py, RW=11).

