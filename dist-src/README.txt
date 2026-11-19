HAPPY DUNGEONS (PS4) ON THE HAPPY DUNGEONS FRIENDS SERVER
=========================================================

This folder runs the PS4 version of Happy Dungeons in the shadPS4 emulator and connects it to the Happy Dungeons
friends server: your account, friends, saves, events, shop and daily orders are the same as in the PC
(WinDurango) version. Co-op matchmaking is between PS4 players only.

It does NOT include the game or any PlayStation system files. You need:
  * Your own decrypted dump of Happy Dungeons, European release CUSA09120, version 01.11
    (the folder with eboot.bin and sce_sys).
  * PS4 firmware modules from your own console (only these six):
      libSceAudiodec.sprx  libSceJpegDec.sprx  libSceJson2.sprx  libSceLibcInternal.sprx
      libScePngDec.sprx    libSceRtc.sprx
    If you already use shadPS4, setup finds them in %APPDATA%\shadPS4\sys_modules. Otherwise copy them into
    shadPS4\user\sys_modules in this folder.
  * Windows 10 or 11, 64-bit, and a graphics card with Vulkan 1.3.


SETUP (once)
------------
1. Unzip this folder somewhere you can write to (not Program Files), e.g. C:\Games\HappyDungeonsPS4.
2. Double-click "Setup Happy Dungeons.bat". It asks for:
     - the game folder (CUSA09120);
     - who you are on the server:
         1  you already play Happy Dungeons on PC: pick winDurango.json from your PC game
            (Mount\UserData\LocalState\WinDurango\winDurango.json). Same player, same character.
         2  you are new: type a player name.
         3  you have a happydungeons_identity.json from an earlier PS4 setup.
   A new player's key is saved as happydungeons_identity.json in this folder. KEEP A COPY of that file: it is what
   signs you in, and with it you can set up again on another PC.
   Windows may say "Windows protected your PC" for the .bat or shadPS4.exe: choose "More info" > "Run anyway".


PLAYING
-------
__LAUNCHERS__
The game runs at normal speed at any frame rate. Co-op works with players on different frame rates (30 to 240 fps):
online, everyone's game runs on a shared 60-steps-per-second clock, so a faster PC shows 60 fps during co-op and a
30 fps PC runs two steps per frame. Solo play uses your full frame rate.
The first start compiles shaders and is slow; later starts are quick.

Controls: a game controller works directly. shadPS4's keyboard layout is in shadPS4\user\input_config.

Don't play the same character on PC and PS4 at the same time: both save to the same place, the last one wins.


SAVES ON THIS PC (OPTIONAL, OFF)
--------------------------------
Normally your character is saved on the Happy Dungeons server, shared with the PC version. If you would rather keep
it on this PC, double-click "Saves on this PC - on or off.bat": it shows whether this is on or off and switches it.
When it is ON:
  * the game saves to files in shadPS4\user\download\CUSA09120 (hds_*.bin and hds_*.bak); back them up yourself,
    the server keeps no copy;
  * that character is not shared with the PC version or your other computers;
  * the first time, the game starts with a new character; your server character stays on the server, unchanged,
    and comes back when you switch this off;
  * online play (co-op, friends, events, shop) still uses the server.


CO-OP CONNECTIONS
-----------------
PS4 co-op connects players' PCs directly (peer to peer) on UDP port 3658.
  * When Windows Firewall asks about shadPS4, allow it (private and public networks).
  * If others cannot join your games, or you cannot join theirs: forward UDP port 3658 on your router to this PC.
  * Two or more players behind the SAME router/internet connection: give each PC its own port when setting up,
    e.g. powershell -ExecutionPolicy Bypass -File tools\setup.ps1 -P2PPort 3659
    (and forward each port to its PC). Their router must also support "NAT loopback"/"hairpinning" for them to
    play together; many do, some don't.
  * Everyone must be on this same server with this same package: matchmaking only pairs players whose event data
    matches.


TROUBLESHOOTING
---------------
  * "The server said no": the message says why. Player names are 1-15 letters, digits or spaces and start with a
    letter.
  * The game closes at start: check the firmware modules (above) and that eboot.bin is decrypted.
  * Network error in the lobby: check the firewall/router notes above.
  * Logs for a bug report: shadPS4\user\log\shad_log.txt.
  * To start over: delete settings.json and run setup again (your player and saves stay on the server; keep
    happydungeons_identity.json). With saves on this PC switched on, keep shadPS4\user\download too.


WHAT IS IN HERE
---------------
  shadPS4\shadPS4.exe     the emulator, unmodified build (see SOURCE.txt; license GPL-2.0, LICENSES\)
  shadPS4\user\patches    game patches, applied in memory when the game runs (your game files are not changed):
                            - HappyDungeons: Toylogic's old server addresses -> the friends server; an empty cloud
                              save slot means "no save yet" (not "corrupt"); the NAT check counts as done
                              (otherwise the lobby countdown stops at 29); the game's network wait checks its
                              socket (otherwise it never sends co-op data and other players never appear)
                            - HappyDungeonsFps: game speed follows real time at any frame rate; 30 fps cap lifted;
                              online co-op runs on a shared 60 Hz tick so players on different frame rates stay in sync
                            - HappyDungeonsSaves: optional, OFF unless you switch it on: saves go to files on this PC
                              instead of the server
  tools\setup.ps1, tools\play.ps1, tools\saves.ps1, tools\server.json   setup, start and saves-switch scripts,
                          server addresses
Nothing outside this folder is changed (except Windows' own firewall entry for shadPS4 if you allow it).
