# Happy Dungeons: what the Xbox/WinDurango work gives the PS4/shadPS4 port

Written 2026-11-19. Sources: earlier working notes,
`D:\dungeons project\` (READ-ME.txt, builds, findings), the WinDurango package
`D:\Desktop\HappyWars\WinDurango_HappyWars_20260917`, and the Mount itself. Nothing in those folders was changed.

## 1. Where the Xbox work lives

| What | Where |
|---|---|
| Xbox build (package `HappyDungeons` 1.15.0.344) | `%USERPROFILE%\OneDrive\Desktop\Happy Dungeons\Mount` (stock exe kept as `agemini_app_develop_Win32.exe`; patched exe `gemini_app_develop_Win32.exe` + 10 `.bak_before_*` steps) |
| All 20 exe patches, with what/why/required | `D:\dungeons project\builds\HD (WD)\patches\HappyDungeons_exe_patches.json` (+ `tools\patch_exe.py`) |
| WinDurango source that shipped | `%USERPROFILE%\windurango-source` (`HappyDungeonsServerData.cpp`, `XboxLiveRedirect.cpp`, `ExePatchesData.h`, ...) |
| Local Xbox Live emulator (package copy, Happy Wars flavoured) | `D:\Desktop\HappyWars\WinDurango_HappyWars_20260917\server\experimentalserver.py` (51 KB, 28 routes) |
| Newest local Happy Dungeons server | `D:\dungeons project\work\fps-split\server\test\run\srv_after\experimentalserver.py` (500 KB, 68 routes, 2026-09-29). The live VPS copy is newer and was not touched. |
| Real Toylogic delivery archive (events, shop, cards) | `D:\Downloads\XboxOne_aries` (139 files for 41 of the 44 slots) and `Mount\ServerData` |
| Saves (local emulator) | `...\WinDurango_HappyWars_20260917\server\data\<xuid>\slot0..3, slot9999, backup\0000\MM\DD` |

## 2. The Xbox exe patches and whether the PS4 needs them

| Xbox patch (exe RVA) | What it did | PS4 v1.11 |
|---|---|---|
| Service-end title (0x583B2A, 0x584557, 0x583B3B cave, 0x19C8778 vtable, 0x519DAE/0x583A43 config) | The final Xbox update turned the title's A/Start into `Dialog_ServiceEnd_Title`, which shuts the network down. Patched to run the real title → sign-in → save load → top menu route. | **Not needed.** The PS4 eboot has no `ServiceEnd` strings at all, and the title screen works with "Press the ✕ button" (verified in shadPS4). |
| Time bomb: `Maintenance::IsActive` → never (0xEAE05C) | `maintenance.bres` entry `MAINTENANCE_190411` (2019-04-11 → 2119-04-11) forced the service-end popup on the top menu. | **Not needed.** The PS4 `maintenance.bres` only has a disabled 2017 sample entry. |
| Canis downloader stub + status-5 skip (0x11FB7B0, 0x12E03CF) | The Xbox build's Vulcan download worker was dead-stripped, so the downloader was faked and WinDurango fed the 44 slots from `ServerData`. | **Different:** the PS4 build still has a working Vulcan HTTP downloader → serve the data from a local server instead of patching. |
| GNet connectivity level = 4 (0x1149C89, 0x114A869, 0x114A876, 0x11510B4) and Xbox Live connectivity wait (0x126DF38, 0x126DF4D) | WinRT network status calls failed on PC. | Xbox-only. On PS4 this is NetCtl/NP, handled by shadPS4. |
| Telemetry off (0x1201310, 0x1201530 → `ret`) | MSXML telemetry object was never created on PC → crash. | PS4 posts telemetry over HTTP to `telemetry.happywars.net` → redirected to 127.0.0.1 by a shadPS4 patch. |
| Hybrid title cave (0x51D38E, 0x583658, 0x19BFAB8) | Earlier "title" boot experiment. | Not relevant. |
| Later: title music, Wwise Bluetooth audio, 60 fps, resolution | PC presentation fixes. | Not relevant now. |

## 3. What the local server implements (`experimentalserver.py`, Flask :5000 + websocket :8765)

- **Xbox Live REST emulation:** title storage (saves, GET/PUT), presence, people/social, MPSD sessions + handles, SmartMatch tickets (hopper `GeneralHopper4`, host/seeker placement), RTA websocket (shoulder taps), marketplace inventory/consumables/EDS catalogue (`offerId` must be inside `OfferDisplayData`), leaderboards, achievements.
- **Happy Dungeons extras (`/windurango/*`):** identity claim/whoami (name popup, 4-XUID blocks), players + lobbies + join, friends + invites, solo mode, Discord status, server-data manifest/files with revision numbers, event switcher (admin), client updater, achievements, save export, news, and a UDP peer relay (WDR1, port 3075) that made multiplayer work without a VPN.
- **Data layout:** `data/<xuid>/slot0..3`, `slot9999` (index), `backup/0000/MM/DD`, `_sessions`, `_handles`, `_matchtickets`, `data/serverdata` (+ `manifest.json`), `_players.json`. Cron jobs on the VPS rolled daily event orders.

## 4. Findings that carry over to the PS4 (platform-independent)

- **Game data formats** (same engine "gemini"/tll10, same data tree: Xbox `wars\` = PS4 `AriesDataOrbis\`): ZRES = `ZRES` + u32 size + zlib; BRES = TLV tree (`TAG(4)+SIZE(4)`, high bit = container, rebuilding must fix every enclosing size and the FILE `PI "size"`); KPK event packs carry a version stamp the game compares with its build (`VULCAN: Version is different. Event Package (v1.x.y) != Build (v1.x.y)`); LNG text. `HappyTool.exe` (Downloads) decompresses.
- **Save format:** `dg_game_data` = zlib(text JSON with `// Serialize by tll10::json::Serializer` header), root `dungeon_SaveData` v20180329 (receiveBox, elementParamS32/U32/F32/U64/F64/Str, elementFlags, dungeonOrderSaveData). The Xbox title-storage copy is base64 of that. Known data bugs: hair overflow values render a white body; order progress is keyed by order key only (daily rolls need new keys).
- **Server-data delivery:** the 44 Canis slots and their label table (0 DungeonPatch, 1 JsonDiff, 2 Permission, 3 Presentation, 4 ShopCatalog, 5 HappyCard, 6 EventExtra, 7 EventCampaign, 8 EventTreasure, 9 EventOrder, 10 EventBonus, 11 Dictionary, 12–43 images); slots 0, 1, 6, 7 are version-keyed (they split matchmaking); events 1117/1119 are the final live set; Happy Card file 41022; `shop_catalog.bin` is ZRES with 4 docs and price rule FirstPrice → SalesPrice → FixedPrice; `permission.pak` is needed for the Customization tab; Presentation (gift) evaluator rules.
- **Game logic:** host/seeker matchmaking (host = preserveSession "always"), dungeon start needs every peer connected over GNet P2P, accepted invites are parked until the lobby screen arms the join flow, jewels are only credited inside the platform store's purchase flow.

## 5. Xbox/WinDurango-only (does not carry over)

All exe addresses (the PS4 eboot is a different build and compiler), XSAPI/EDS/MPSD/RTA wire formats, XUID identity blocks, title-storage URL scheme, the WinDurango DLLs (renderer, WinRT, portable mode, P2P association layer), Fiddler/loopback, GDK auth.

## 6. The PS4 build (new findings)

- `D:\PS4 ROMS\CUSA09120` (EU, `EP3658-CUSA09120_00-HAPPYDUNGEONS000`, app 01.11, SDK 4.508, built 2018-02-20). Another copy: `D:\Downloads\happydungeons1.11\CUSA09120`. `eboot.bin` is a decrypted ELF (45.5 MB; code at virtual address 0 = file offset 0x4000); data `AriesDataOrbis` 3.9 GB.
- **Online services it uses:** PSN through `libSceNpManager` (sign-in required: offline it stops at "You must be connected to PlayStation™Network to play the game"), **`libSceNpTus` for saves** (there is no `libSceSaveData` import: saves live in PSN title user storage), `libSceNpMatching2` (+ signalling) for lobbies, `libSceNpWebApi` (friends, entitlements, invitations), `libSceNpCommerce` (Happy Jewels store), `libSceNpParty`, `libSceNpTrophy`; plus Toylogic's own HTTP: Vulcan delivery API `https://xas-pfpf.lynca.net/v12/api?country_id=826&language_id=41&platform_id=6&project_id=7&stage_id=1&version=1300&authcode=<sha256>`, `https://telemetry.happywars.net/api/TelemetryEntity?name=Aries&Version=v001v038`, `https://dev-api.lynca.net/` (lynca_id/lynca_pw, HMAC), `https://jp.happydungeons.net/law/`.
- **The 6 Nov run (your own, with the public shadNet server):** TUS slots 1–5 read, slot 1 (181 B) and slot 2 (5,769 B) written, Happy Jewel entitlement lookup 404, Vulcan API answered 200 with 41 bytes, telemetry 500. The game got past the title into the in-game managers.
- **shadPS4:** the `sgdc3/lbp3-compat` build (v0.20.1 WIP, a01cca9) has a full shadNet client (TUS/TSS, Matching2 rooms + signalling, friends, trophies, scores, WebAPI push events) and a real `libSceHttp`. Your global shadPS4 settings point at the public `srv.shadps4.net` with account MyPlayer; the PS4 work uses its own portable copy instead (see PLAN.md).
