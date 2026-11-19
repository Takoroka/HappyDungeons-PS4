# Event / shop data for the PS4 build (Vulcan delivery)

How the PS4 build gets it: at boot it asks `GET /v12/api?country_id&language_id&platform_id=6&project_id=7&stage_id=1&version=1300&authcode=<sha256>`
(rewritten to `http://127.0.0.1:5100`). The auth code is salted with `leo-a9qtcbjlvsreNQ4P`; our server ignores it.
The reply lists files per label (`{"Format":{"Name":"Vulcan","Version":12},"List":{...},"File":{"1":{"Label","URL","FileName","FileSize","FileHash",...}}}`),
then the game downloads each URL. Today the live Toylogic server and our server both answer with no files.

Labels = the eboot's table, same as the Xbox archive's `_meta.json` labels: DungeonPatch, JsonDiff, Permission,
Presentation, ShopCatalog, HappyCard, EventExtra, EventCampaign, EventTreasure, EventOrder, EventBonus,
Dictionary, Pic_* (32 images).

**Version check (eboot 0x97eaf4):** event packs (`KPK~`, version u32 at byte 8 = minor*100000 + patch*1000 + build)
must be in [1300000, 1400000), i.e. v1.13.x. The PS4 v1.11 build is internal v1.13.0 (it also sends version=1300).
The Xbox archive's final packs are v1.14.1 (content update CU0014, Xbox only); the only v1.13 pack is
EventCampaign id 33232 (`CERT_1.13.0_05 ... CU0013_00_002`, v1.13.0 build 2). There is no v1.13 DungeonPatch.

**Staged set** (`server\stage_delivery.py` → `server\data\staged\`, not live): Dictionary 41933, Permission 37816,
EventCampaign 33232, EventOrder 39934, HappyCard 41022, ShopCatalog 41938, Presentation 40284 and 21 images
(service-end notices Pic_Top01 41331 / Pic_imageEX01 41901 left out). Not staged: DungeonPatch and JsonDiff
(CU0014 balance/data patches for a newer build), EventExtra / EventBonus / Pic_Loading04 (not in the archive).

**Before going live:** test on a hidden test account (HDFresh01): titles load, Happy Cards / shops open,
no missing-item crashes. Risk: the Happy Card, shop and gift tables are from the 1.14 era and may name items the
1.13 base data lacks; check them against `AriesDataOrbis\dungeon\json\item` if anything looks off.
To go live: copy `data\staged\vulcan.json` → `data\vulcan.json` and `data\staged\delivery\*` → `data\delivery\`.
