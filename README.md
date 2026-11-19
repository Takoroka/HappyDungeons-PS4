# Happy Dungeons (PS4) revival for shadPS4

Brings the PS4 version of Happy Dungeons (CUSA09120, EU, v01.11) back online in the
[shadPS4](https://github.com/shadps4-emu/shadPS4) emulator, on the Happy Dungeons friends server. The emulator
itself is not modified: everything here is game patches (applied in memory by shadPS4's patch system) and
server-side services. No game files, firmware or user data are included; you need your own dump of the game.

## What is in here

| Path | What |
|---|---|
| `tools/make_fps_patch.py` | Frame-rate decoupling: gameplay at real-time speed at any frame rate (30–240+ fps), the 30 fps cap lifted, and online co-op on a shared 60 Hz lockstep tick so players on different frame rates stay in sync (30 fps clients run two steps per frame). |
| `tools/make_net_patch.py` | Online fixes: the NAT check counts as done; the game's `select()` checks its socket (without it co-op never sends a packet in shadPS4). |
| `tools/make_localsave_patch.py` | Optional: keep saves on the PC (`user/download/CUSA09120`) instead of the server's cloud storage. |
| `tools/build_patch_pack.py` | Builds the patch pack (one folder for shadPS4's `patches` folder; any OS). |
| `tools/build_player_package.py`, `dist-src/` | The player package and its setup/account scripts. |
| `server/hd_ps4_gateway.py` | PS4 gateway in front of shadNet: links PS4 accounts to the Happy Dungeons server (saves, friends, events, shop), Happy Dungeons account sign-up. |
| `dungeons-server/` | The Happy Dungeons server itself (`experimentalserver.py`, the players website, VPS deploy kit, event-order tools): identities, saves, friends, lobbies, events, shop, achievements. The PS4 gateway runs next to it. |
| `deploy/` | VPS deployment (`push-vps.ps1`, `vps/install.sh`, service units, config templates with placeholders). |
| `shadps4/user/patches/` | The patch files as installed (`HappyDungeonsLocal` = local-server variant, `HappyDungeonsFps`). |
| `tools/*` (others) | Reverse-engineering and test tools used to find the fixes (hidden-desktop test clients, memory/packet probes, hardware write-watch, disassembly helpers). |
| `PLAN.md`, `DEPLOY-VPS.md` | Detailed notes: every finding with eboot addresses, test results, deployment. |

## Playing

1. Copy the `HappyDungeons` patch folder (from the patch pack) into shadPS4's `patches` folder
   (Windows `%APPDATA%\shadPS4\patches`, Linux `~/.local/share/shadPS4/patches`,
   macOS `~/Library/Application Support/shadPS4/patches`).
2. Create a Happy Dungeons login on the Happy Dungeons website (it is not your normal shadNet login).
3. In shadPS4: put that login in its own shadPS4 user (Set Default User on it when playing Happy Dungeons) and set
   the shadNet server to the Happy Dungeons server in Happy Dungeons' game-specific settings.

Normal shadNet accounts are refused by the Happy Dungeons server; its accounts exist only for Happy Dungeons.

## License

MIT (see `LICENSE`), for everything in this repository. It holds no game files: Happy Dungeons belongs to its publisher, and you need your own copy. shadPS4 and shadNet are separate projects with their own licenses (shadPS4: GPL-2.0); this repository only patches the game in memory and talks to shadNet over its protocol.
