# Happy Dungeons PS4 on the live friends server: DEPLOYED 2026-11-19 (18:40-18:45 UTC)

Nothing below has been done on the VPS yet. Everything was built and tested on this PC against a byte-identical
copy of the live server (`experimentalserver.py` md5 86b36ea5…, the same file as /opt/happydungeons/server).

## What it does

A PS4 player (shadPS4 + CUSA09120) plays on the same server as the Xbox/PC players:

| Shared | How |
|---|---|
| **Account** | A PS4 online id is linked to a Dungeons identity (xuidBase + key). `tools\ps4_link.py` links an existing PC player from their `winDurango.json`, or registers a new player. |
| **Saves** | The PS4's cloud-save calls (shadNet TUS) are answered from the player's Dungeons saves, `data/<xuid>/slot0-3` and `slot9999`, through the server's own title-storage route with the player's key. The server's quota, locking and ownership checks all still apply. Version translation: 20180329 → 20171130 going to the PS4, restored on the way back. |
| **Events / shop / cards / daily orders** | The game's Vulcan download list is built from the server's `/windurango/serverdata/manifest` + files, fetched as the requesting player, exactly what PC clients download. Event packs are restamped v1.15 → v1.13. Pictures the server doesn't publish come from the Xbox client's ServerData set, with the service-end `Pic_Top01` replaced by title art. |
| **Friends** | When a linked player signs in, their Dungeons friends who also play on PS4 become shadNet friends. |

**Not shared:** matchmaking. PS4 players match only with PS4 players (shadNet Matching2 rooms, 10 worlds), as decided.

**`experimentalserver.py` is not changed.** The PS4 side is two new services next to it:
- **shadNet:** the PSN replacement, behind the gateway.
- **`hd_ps4_gateway.py`:** stdlib Python 3, one file.

## Verified locally (2026-11-19, hidden-desktop runs `runs\gw1_linked`, `gw3_serverdata_api`)

**Save sharing**
- Your Xbox save (player <your xuidBase>, Lv 15) was uploaded to a test player on the local server copy, then linked as `MyPlayerPS4`.
- The PS4 build signed in, loaded the character through the gateway, ran its item check, saved back into `data/<xuid>/slot0` with version 20180329, and reloaded that save on the next boot.
- A round-trip diff against the Xbox save showed:
  - kept: all 4,284 values and every item, except one 1.15 progress flag (`elementParamS32/00008-05_0`);
  - added by the PS4 build: its login streak bonus, gift-inbox rewards and daily order lottery (the PC build never runs these).

**Events, shop and pictures**
- Vulcan list served 39 files and the server logged the downloads as the player.
- Event Quest shows the live event (Big Boss Rumble Greatest Hits). The duel dungeon loads and plays.
- The shop, Happy Cards and gift inbox work.
- Game Home shows title art, not the service-end notice.

**Accounts and friends**
- Register, link, re-link and wrong-key refusal all behave correctly.
- Each link gets its own random shadNet password, so the Dungeons key is never sent over shadNet.
- shadNet refuses registrations that don't come from the gateway (RegistrationSecretKey).
- Friends sync added the friendship through shadNet's admin API.

**Known gap:** event quest names show as text keys (`MissionName_Ev_Duel_Fire_00`). The 1.15 game has those strings built in; the 1.13 PS4 build doesn't. The fix is a PS4 dictionary pack with the missing strings (follow-up).

## How it is pushed (run 2026-11-19 with the user's OK: check, fetch, setup, start, expose, website)

**Live state after the push (all checked from outside):**
- services hd-ps4-shadnet + hd-ps4-gateway active;
- public: 31313/tcp (gateway), 31314/udp (STUN), 31315/tcp (WebAPI);
- closed from outside: 31413, 31350, 5101;
- Caddy: PS4 blocks in, backup `Caddyfile.before-ps4-20261119-*`;
- players page: PS4 box in, backup `web/index.html.before-ps4-20261119-184356`;
- the Dungeons server is untouched (md5 86b36ea5..., happydungeons.service active).

**Smoke test:** a shadNet-only throwaway account (`HDSmokeTest`, no Dungeons player) signed in through
107.170.39.185:31313 (code 0). A wrong password and an unknown id got 8. The account was deleted afterwards.
The pages, `/ps4/whoami` and `/v12/api` (39 files) all answered.

**Fixes found during the push:**
- the release zip holds a tar.gz, which install.sh now unpacks;
- push-vps.ps1 now uses ErrorActionPreference Continue, because PowerShell 5.1 turned curl's stderr into an
  abort. The remaining steps were run over ssh from bash.

Everything is scripted; one step at a time, each with a `-DryRun` first:

```
powershell -ExecutionPolicy Bypass -File deploy\push-vps.ps1 -Step check     # read-only report
powershell -ExecutionPolicy Bypass -File deploy\push-vps.ps1 -Step fetch     # shadNet zip + Qt runtime (downloads)
powershell -ExecutionPolicy Bypass -File deploy\push-vps.ps1 -Step setup     # user, folders, files, secrets, units
powershell -ExecutionPolicy Bypass -File deploy\push-vps.ps1 -Step start     # shadNet, admin account, gateway, checks
powershell -ExecutionPolicy Bypass -File deploy\push-vps.ps1 -Step expose    # Caddy blocks + 3 ufw ports
```
- `push-vps.ps1` rebuilds `deploy\hd-ps4-vps-bundle.tar.gz` (`tools\build_vps_bundle.py`, ~5 MB), copies it to
  `/root/hd-ps4-bundle` and runs `bash install.sh <step>` there. It is not deploy.ps1 and touches nothing else.
- `install.sh` (in `deploy\vps\`) is idempotent: each step only changes what is not in place yet; secrets are made
  once (random) and reused; files it replaces are kept as `.before-<stamp>`; the Caddyfile is backed up as
  `Caddyfile.before-ps4-<stamp>`, the new one is `caddy validate`d before it is installed, and its PS4 blocks sit
  between `# >>> happydungeons-ps4` / `# <<< happydungeons-ps4` markers. Tested here: adding twice gives the same
  file, removing gives back the original byte for byte.
- `status` and `rollback` are there too. Rollback stops/disables the two services, removes the Caddy blocks and the
  three ufw rules; files, accounts and links stay.
- **setup.sh rewrites the whole Caddyfile.** After running it again, run
  `bash /opt/happydungeons/ps4/install.sh expose` on the VPS to put the PS4 blocks back.

## Changes on the VPS, each needing your OK

1. **Downloads onto the VPS**
   - `shadnet-linux64-2026-11-16-a53dad5.zip` (1.26 MB, sha256 `a3bb6ea3a364d3a2d6f6980e684c9daa77b64b727e42b187c59653e8b562d899`) from https://github.com/shadps4-emu/shadNet/releases. It contains only the `shadnet` binary, built against Qt 6.10.3. Ubuntu 24.04's Qt 6.4 can't run it.
   - Qt 6.10.3 Linux desktop kit with `qthttpserver` + `qtwebsockets`, via `aqtinstall` (pip package from PyPI, installed in its own venv `/opt/happydungeons/ps4/aqt-venv`; the kit is roughly 100–250 MB) into `/opt/happydungeons/ps4/qt-kit`, linked as `/opt/happydungeons/ps4/qt`. This is what shadNet's own CI does. Nothing goes into the system Qt, pip or apt (the VPS already has python3-venv).
2. **Uploads from this PC** (the bundle, ~5 MB)
   - `hd_ps4_gateway.py`, `install.sh` and the configs in `deploy\vps\`.
   - The Xbox client ServerData pictures and packs (39 files, from `Mount\ServerData`).
   - The title-art `Pic_Top01.jpg`.
3. **New user and folders:** user `hdps4`; `/opt/happydungeons/ps4/{shadnet,qt,gateway,state,base_serverdata,overrides}`. The existing `/opt/happydungeons/data` is not written by the PS4 side except through the server's normal save route.
4. **Two systemd services:** `hd-ps4-shadnet.service`, `hd-ps4-gateway.service` (files in `deploy\vps\`).
5. **Caddy:** add the blocks in `deploy\vps\Caddyfile.ps4-site` and `Caddyfile.ps4-route`:
   - an `http://107.170.39.185` site for `/v12/api`, `/vulcan/*`, telemetry and `/law`;
   - `/ps4/*` inside the sslip.io site for linking.
   - Then `caddy validate` + `systemctl reload caddy` (game websockets survive a reload).
   - Note: the Caddyfile says it's managed by `setup.sh`, and a setup.sh rerun drops the blocks. Run `install.sh expose` again after it.
6. **ufw, opening three ports:**
   - 31313/tcp: the gateway, where the PS4 game connects;
   - 31314/udp: shadNet STUN / signalling;
   - 31315/tcp: shadNet WebAPI, for friends lists, presence and sessions.
   - 31413 (shadNet's own game port) stays closed. 8763/8764 (kytyNET) are not touched.

## Order (each step reversible; = the install.sh steps)

1. `fetch`: download the shadNet zip (sha256 checked) + aqtinstall/Qt into `/opt/happydungeons/ps4`; `ldd` check that shadnet resolves every Qt library, and the Qt SQLite driver is there.
2. `setup`: user `hdps4`, folders, gateway, configs (random RegistrationSecretKey + admin password, the same in `shadnet.cfg` and `gateway.json`), `worlds.cfg`, base ServerData and the override, the two units (enabled, not started).
3. `start`: start shadNet, create the `HDGateway` admin account with the key (`hd_ps4_gateway.py --create-admin`), restart shadNet once if the account is new (AdminsList), start the gateway. It then checks `http://127.0.0.1:5101/ps4/status` and the Vulcan list. Before any player links, the list is the base set; afterwards it is the live rev (73 today).
4. `expose`: Caddy blocks + ufw ports, then `http://107.170.39.185/v12/api` from the VPS itself.
4b. `website`: the "PS4 (shadPS4)" box on the players page (`/windurango/`), added by
   `deploy\vps\players_page_ps4.py` to `/opt/happydungeons/server/web/index.html`.
   - It is built and tested against the live file, md5 `d649e577…`.
   - Exact anchors, marker `ps4_account_patch v1`, a `.before-ps4-<time>` backup, written in place.
   - The file is static, so the Dungeons server doesn't restart.
   - **What the box does:**
     - shows the signed-in player's PS4 login (`POST /ps4/whoami`, which makes nothing);
     - makes one, with a random password or a picked Happy Dungeons-only one;
     - "Show" reveals a random password; "Pick a new password" changes it;
     - says to put the login in a second shadPS4 user.
   - `website-remove` (and `rollback`) takes it out. Removing it was tested to give back the original
     byte-for-byte.
   - Tested locally: the live page served by livecopy, with a one-origin test proxy standing in for Caddy.
5. Players: `dist\HappyDungeonsPS4-<date>.zip` (see "Player package"). For your own profile on this PC: copy `deploy\client\HappyDungeons.xml` (URLs → `http://107.170.39.185`, save fix, NAT fix, select() fix) into the profile's patches instead of HappyDungeonsLocal, then run `tools\ps4_link.py --windurango <your winDurango.json>` and boot. Your real character, same save as PC.

**Rollback:**
- Stop and disable the two services, remove the Caddy blocks, close the three ports.
- The Dungeons server and its data are untouched apart from the saves PS4 players wrote through the normal route, which are kept with the server's usual daily backups.

## Multiplayer findings that shape the setup (2026-11-19)

- **STUN port = shadNet port + 1.** shadPS4 sends its STUN ping to the port after `shadnet_server`'s. Players connect
  to the gateway on 31313, so shadNet's `MatchingUdpPort` must be 31314 (it is, in `deploy\vps\shadnet.cfg`).
  Pointing a client straight at 31413 sends the ping to 31414, shadNet never learns the player's address, and the
  lobby fails with "continuous communication cannot be maintained".
- **Player addresses come from the STUN ping, not from the TCP connection** (shadNet `GetSelfSignalingAddr`: the
  UDP sender address recorded per online id; the TCP peer address is only a fallback). The STUN UDP goes straight to
  shadNet, so the gateway in front of the TCP port does not hide players' real addresses. (Only if a ping never
  arrives would shadNet hand out 127.0.0.1.)
- **Matchmaking only pairs players with the same delivered event data** (search attribute 0x4e is a hash of it),
  so every player must use this server's Vulcan list. After the server's ServerData rev changes, players who
  signed in before and after can't match until the earlier ones restart the game.
- **Players behind the same router** (or several clients on one PC) are given each other's public address, so
  their router must support NAT loopback (hairpinning), and each PC needs its own `p2p_port`. Testing 4 clients
  on one PC against the VPS depends on this; the local 127.0.0.1 test does not.
- **New players per hour:** the Dungeons server allows 10 new players per address per hour. The gateway now
  passes the player's own address (from Caddy's X-Forwarded-For) on `/ps4/register`, so PS4 sign-ups count per
  player and not all against 127.0.0.1.

## Normal shadPS4 (main route for players, 2026-11-19)

Players use their own shadPS4 on Windows, macOS or Linux: the official v0.21 pre-release or newer, with the Qt
launcher. We verified the official v0.21.1 pre-release (34a1d4d): the patches apply, and 30 + 240 fps
co-op held through a 5-minute soak.

**Patch pack:** `dist\HappyDungeons-patches-<date>.zip`, built by `tools\build_patch_pack.py [--server vps|local]`;
the player text and script sources are in `dist-src\patch-pack\`. It contains:
- the `HappyDungeons` folder (patches + files.json), copied into shadPS4's `patches` folder;
- `INSTALL.txt`;
- `happydungeons_account.py`.

**Account setup (any OS):**
- **Web page** `https://107-170-39-185.sslip.io/ps4/account`, served by the gateway at `GET /ps4/account`. Caddy's
  existing `/ps4/*` route on the sslip site covers it, so it is HTTPS only. The page:
  - offers new player / PC player (winDurango.json) / earlier player (happydungeons_identity.json);
  - reads key files in the browser and sends only xuidBase + secret, as setup.ps1 does;
  - shows the NPID and password with copy buttons;
  - offers the key as a download;
  - if a new player was made but the shadNet link failed, still hands out the key.
  - Headers: strict CSP, no-store, nosniff, no-referrer.
  - `/ps4/link` and `/ps4/register` now accept `Content-Type: application/json` only (blocks cross-site form
    posts) and answer `Cache-Control: no-store`. setup.ps1, ps4_link.py and the script already send JSON.
- **Script** `happydungeons_account.py new "Name" | pc winDurango.json | key happydungeons_identity.json`:
  Python 3.6+, stdlib only. Optional `--shadps4 [FOLDER] [--user NAME/ID] [--fps N]` does two things, each with a
  `*.before-happydungeons` backup:
  - writes the login into `users.json` (the default user = player_index 1);
  - creates `custom_configs/CUSA09120.json` as a FULL copy of `config.json` with the Network fields and vblank.
- **Where players type the login:** Settings > Manage Users > user > ShadNet... > "Enable ShadNet for this user",
  Account ID (NPID), Password (shadps4-qtlauncher `user_manager_dialog.cpp`). Per-game server settings live under
  right-click > Game-specific Settings... (Network tab). Vblank Frequency is on the Experimental tab, which the
  launcher shows only in game-specific settings.
- **Rule (the user, 2026-11-19): players' public shadNet passwords are never stored on, or knowingly collected by,
  our servers** ("we dont want their passwords on our servers people will definitely get upset at that").
  - **A Happy Dungeons login is its own login on our shadNet.** It is created and linked together with the
    player's Dungeons identity (page, script, or the Windows setup), with either:
    - a random password from the gateway (default, returned to the player and kept in links.json); or
    - a password the player picks for this server only, 8-128 characters, labelled everywhere as NOT their
      public shadNet password. It is never stored or logged by the gateway (`ownPassword: true` in the link).
  - **Players keep it in a second shadPS4 user** (Manage Users > Create User > ShadNet...) and Set Default User
    while playing Happy Dungeons; their normal user keeps their normal login.
  - **Removed after a short-lived attempt the same day:**
    - auto-creating accounts from sign-ins through the proxy;
    - "use the login your shadPS4 user already has" on the page;
    - the script's `--same-login`, which read users.json;
    - linking an existing unlinked account by proving its password.

    The proxy again reads only the Account ID of a Login. A sign-in for an unknown name, or with a wrong password,
    is refused by shadNet as before. An existing account linked to nobody is never handed over (409).
  - **Changing the password** of the player's OWN linked account goes through the admin API
    (`POST /admin/v1/users/<id>/password`, which signs that account out). The player proves who they are with
    their Dungeons key.
  - **Script:** `--pick-login [--npid NAME]` asks for a new Happy Dungeons-only password (getpass, twice).
    `--shadps4` writes the login into `--user`, or else into a user with no other shadNet login, never over
    another login without `--user`. It then tells the player to Set Default User on it.
  - **Tested** on the second local gateway:
    - unknown sign-ins create nothing;
    - register with a picked login works (protocol sign-in 0); a short one gets 400;
    - an existing unlinked id gets 409 for register and for link;
    - changing the own linked password works (old refused);
    - the page with a picked password (mismatch caught);
    - script: a made login went to free user 2 while user 1's fake normal login stayed untouched; `--pick-login`
      into `--user shadPS4-3` signs in; a typo is caught.
  - **Leftover local test accounts:** AutoNew1-4, ShortPw2, PickedOne, PagePick1, ScriptMade, ScriptPick1 and
    others. Made-up passwords on the local shadNet only.
- **shadPS4 folders** (from shadPS4 `path_util.cpp`): `%APPDATA%\shadPS4`, `$XDG_DATA_HOME/shadPS4` or
  `~/.local/share/shadPS4`, `~/Library/Application Support/shadPS4`. A portable `user` folder in the working
  directory wins.
- **New gateway config:** `playerShadnet` / `playerWebApi` (shown on the page). The VPS gateway.json has
  107.170.39.185:31313 and http://107.170.39.185:31315; empty means derived. The local gateway still runs the old
  code, which serves no page, but the script works against it (same API).

**Tested locally** with a second gateway (new code) on 127.0.0.1:5102/31323, sharing livecopy's state:
- page: rejected name, a key file without a secret, re-link of PkgTest, new player `PageTest`;
- script: key + `--shadps4` on a copy of the settings files (default user and `--user shadPS4-2`; vblank kept on
  rerun; backup kept), new player `ScriptTest`, a rejected name, a bad file, a missing folder, an unreachable
  server;
- the script also works against the running old gateway on 5100.

Not tested: a game boot with the pack in the official build (tested separately).

## Bundled Windows package (optional)

`dist\HappyDungeonsPS4-<date>.zip` (built by `tools\build_player_package.py`, sources in `dist-src\`):
- shadPS4 (the unmodified sgdc3 a01cca9 build, GPL-2.0 text and source link included);
- a clean `user\` (no paths, devices or accounts from this PC);
- the patches: server URLs → 107.170.39.185, save fix, NAT/countdown fix, select() fix (without it the game
  sends no P2P packets in shadPS4), fps decoupling. The builder takes the save/NAT/select() blocks from the main
  profile's `HappyDungeonsLocal` patch by name and stops if one is missing or an unknown one appears;

- `Setup Happy Dungeons.bat`, `Play Happy Dungeons.bat` (60 fps), the 120 and 240 fps versions, and a README.

Setup (PowerShell 5.1, nothing to install):
- asks for the game folder and checks that it is CUSA09120 with a decrypted eboot;
- finds the six firmware modules (in its own `user\sys_modules` or `%APPDATA%\shadPS4\sys_modules`);
- links the player through `https://107-170-39-185.sslip.io/ps4/link` or `/ps4/register`:
  - PC players pick their winDurango.json;
  - new players type a name, and the key is saved as `happydungeons_identity.json`.

**Not included:** the game, firmware, keys, saves, logs, shader caches.

**Mixed frame rates work in co-op (fps patch v3.2):** co-op is input lockstep, so online play now runs on a shared
60 Hz tick:
- faster PCs pace their frames to it;
- 30 fps runs 2 steps per frame;
- solo keeps the variable rate.

Tested on 4 local clients:
- 60 + 240 fps for 12 min;
- 30 + 240 fps for 10 min;
- 30/60/120/240 together for 8 min.

The README says co-op works across 30 to 240 fps. The default build has 60, 120 and 240 fps launchers. `--fps`
still picks which launchers are built (e.g. `--fps 30` for one 30 fps launcher). Rebuild after any change to
`HappyDungeons_fps.xml`; the zip of 2026-11-19 contains v3.2.

`--local` builds `dist\HappyDungeonsPS4-localtest\`, the same package pointed at this PC's servers. Setup was tested with it here:
- new player, rerun, re-link from the key file;
- refused name, and a wrong game folder.

Not tested yet: a game boot from the package. It waits until the multiplayer tests are done, because a third game on the local shadNet could join their lobbies.

## Things to know before saying yes

- **Don't play the same character on PC and PS4 at the same time.** The save store has no cross-build lock; the last writer wins.
- **The shadNet game protocol is unencrypted**, same as the public shadNet. The gateway gives each PS4 account its own random password, so a sniffed one only exposes that PS4 login (which can reach that player's saves), never their Dungeons key.
- **PS4↔PS4 co-op uses direct peer connections** (UDP port 3658 on players' PCs, UPnP or a forwarded port). There is no relay like the PC build's port 3075, so strict NATs may fail to connect.
- **Every linked PS4 sign-in fetches server data with the player's own key.** It shows in the server's log as `[SERVERDATA] <gamertag> downloaded …`, like PC clients.
