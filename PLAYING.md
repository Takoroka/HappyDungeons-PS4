# How to play Happy Dungeons (PS4) on the friends server

Happy Dungeons' PS4 version, played in the shadPS4 emulator, online on the Happy Dungeons friends server: your
player, character, friends, saves, events and shop are the same as in the PC (WinDurango) version. Co-op
matchmaking is between PS4 players.

## What you need

- **Your own decrypted dump of Happy Dungeons: European release CUSA09120, version 01.11.** Other regions or
  versions don't work (the patches are made for this exact build). Nothing here includes the game.
- **Six firmware modules from your own PS4** (`system/common/lib`): `libSceAudiodec.sprx`, `libSceJpegDec.sprx`,
  `libSceJson2.sprx`, `libSceLibcInternal.sprx`, `libScePngDec.sprx`, `libSceRtc.sprx`.
- A PC with a Vulkan 1.3 graphics card.

## 1. Download

From the latest release, <https://github.com/Takoroka/HappyDungeons-PS4/releases/latest>, take ONE of:

| Download | For | Account |
|---|---|---|
| `HappyDungeons-patches-<date>.zip` (patch pack) | Windows, macOS or Linux, with your own shadPS4 (official v0.21 pre-release or newer) | you make it on the website (step 2A) |
| `HappyDungeonsPS4-<date>.zip` (Windows package) | Windows only; shadPS4 is included | the setup script makes it for you (step 2B) |

## 2A. Patch pack (any OS, your own shadPS4)

1. **shadPS4:** install it with its launcher and choose the **Pre-release** build (v0.21 or newer; older builds
   lack part of the online code). Add your Happy Dungeons dump, and put the six firmware modules in the
   `sys_modules` folder of the shadPS4 folder.
2. **Patches:** copy the `HappyDungeons` folder from the zip into the `patches` folder of the shadPS4 folder:
   - Windows: `%APPDATA%\shadPS4\patches`
   - Linux: `~/.local/share/shadPS4/patches`
   - macOS: `~/Library/Application Support/shadPS4/patches`

   In the launcher (right-click the game > Cheats / Patches > Patches) they are listed; leave them as they are.
3. **Register on the portal:** open <https://107-170-39-185.sslip.io/ps4/account>.
   - *Who are you?* **I am new** (type a player name), **I play Happy Dungeons on PC** (pick your
     `winDurango.json` from `Mount\UserData\LocalState\WinDurango\`; same character and friends on both), or
     **I have a happydungeons_identity.json from before**.
   - *Your login:* **Make one for me** (random password), or pick an Account ID and a NEW password used only for
     Happy Dungeons. Never use your normal shadNet password: normal shadNet accounts are refused by this server.
   - Click **Get my login** ONCE (each click makes a new player). Write down the **Account ID** and **Password**,
     and click **Download happydungeons_identity.json** and keep that file: it is your player's key, and with it
     you can get your login again on any PC.
4. **Put the login in its own shadPS4 user:** Settings > Manage Users > Create User (e.g. "HappyDungeons"). Select
   it > **ShadNet...** > tick *Enable ShadNet for this user*, enter the Account ID and Password. Then, with that
   user selected, click **Set Default User** (the default user is the one that signs in). For your other games,
   set your normal user as default again.
5. **Happy Dungeons' own settings:** right-click the game > Game-specific Settings... > *Create Game-specific
   Settings from Global Settings*, then *Configure Game-specific Settings*:
   - Network tab: Server `107.170.39.185:31313`, WebAPI Server `http://107.170.39.185:31315`; tick *Set Network
     Connected Status as True*, *Enable shadNet* and *Enable UPNP*.
   - Experimental tab: Vblank Frequency = twice the frame rate you want (120 = 60 fps, 240 = 120 fps,
     480 = 240 fps; 60 = 30 fps).
6. **Play:** start Happy Dungeons. The first start compiles shaders and is slow.

`INSTALL.txt` in the zip has the same steps in more detail, plus `happydungeons_account.py`, which does steps 3-5
from the command line (Python 3).

## 2B. Windows package

1. Unzip it somewhere you can write to (not Program Files), e.g. `C:\Games\HappyDungeonsPS4`.
2. Double-click **Setup Happy Dungeons.bat**. It asks for the game folder and who you are (1 = PC player with
   `winDurango.json`, 2 = new player, 3 = `happydungeons_identity.json`), and makes your account on the server
   itself: no website needed. If the firmware modules are missing it says where to put them.
3. Keep a copy of `happydungeons_identity.json` (in that folder): it is your player's key.
4. Double-click **Play Happy Dungeons.bat** (60 fps), or the 120 / 240 fps one.

If Windows says "Windows protected your PC": More info > Run anyway.

## Co-op

- Players on different frame rates (30 to 240 fps) can play together: online, everyone runs on a shared
  60-steps-per-second clock.
- Co-op connects players' PCs directly on **UDP port 3658**. Allow shadPS4 through your firewall. If others can't
  join you (or you can't join them) and UPnP doesn't open it, forward UDP 3658 on your router to your PC.
- Two PCs behind the same router: give each its own P2P port (e.g. 3659) and forward each; the router must
  support NAT loopback.
- Everyone must use the same release: matchmaking only pairs players with the same event data. After a server
  event update, restart the game.

Don't play the same character on PC and PS4 at the same time: the last one to save wins.

## Optional: saves on your PC

Off by default: your character is saved on the server. To keep it on your PC instead, tick *Saves: keep them on
this PC* in the Patches list (patch pack), or double-click **Saves on this PC - on or off.bat** (Windows package).
That character is then not shared with the PC version or other computers, and the server keeps no copy. The
first start makes a new character; your server character comes back when you switch it off.

## Problems

- **Not signed in / network error:** check the Account ID and password in the shadPS4 user, that this user is the
  default user, and the Server / WebAPI Server settings.
- **The game runs too fast:** the patches are not applied. The `HappyDungeons` folder must be directly in
  `patches` and hold only `HappyDungeons.xml` and `files.json`.
- **The game closes at start:** check the firmware modules and that the dump is decrypted (and is CUSA09120
  v01.11).
- **Lost your login:** on the portal choose *I have a happydungeons_identity.json from before*.
- **Logs for a bug report:** `log/shad_log.txt` in the shadPS4 folder (Windows package:
  `shadPS4\user\log\shad_log.txt`).
