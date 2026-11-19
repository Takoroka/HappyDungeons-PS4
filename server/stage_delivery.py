"""Stage a Vulcan delivery set for the PS4 build from the Xbox delivery archive (NOT live).

Writes server/data/staged/vulcan.json + server/data/staged/delivery/<files>. hd_services.py only serves
server/data/vulcan.json + server/data/delivery/, so nothing changes for the game until the staged set is
copied there (after testing on a hidden test account).

PS4 v1.11 = internal v1.13.0: event packs (KPK~) must carry a version stamp in [1300000, 1400000)
(eboot 0x97eaf4). Toylogic's final packs are v1.14.1 (CU0014) and are left out until their content is
checked against the PS4 base data. Service-end images (Pic_Top01 41331, Pic_imageEX01 41901) are skipped.
"""
import hashlib
import json
import os
import shutil
import sys

ARCHIVE = r"D:\Downloads\XboxOne_aries\XboxOne_aries"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "staged")

# label -> archive id. Conservative first set; see DELIVERY.md for the reasoning per slot.
CHOICE = {
    "Dictionary": 41933,      # texts (unversioned); extra 1.14 strings are harmless
    "Permission": 37816,      # DLC permission table (Customization tab needs it)
    "EventCampaign": 33232,   # the only pack stamped v1.13.0 (build 2)
    "EventOrder": 39934,      # daily/special orders
    "HappyCard": 41022,       # last live Happy Card set (7 packages run to 2119)
    "ShopCatalog": 41938,     # item / insanity / recycle shops
    "Presentation": 40284,    # gift inbox
    "Pic_Top02": 17098, "Pic_Loading01": 17317, "Pic_Loading02": 37783,
    "Pic_GameHome01": 8713, "Pic_GameHome02": 15486, "Pic_GameHome03": 14138, "Pic_GameHome07": 15641,
    "Pic_CardMain01": 33563, "Pic_CardMain02": 41033, "Pic_CardMain03": 41034, "Pic_CardMain04": 35646,
    "Pic_CardMain05": 35647, "Pic_CardMain06": 35648, "Pic_CardMain07": 35649,
    "Pic_CardPack01": 10947, "Pic_CardPack02": 36745, "Pic_CardPack03": 40253, "Pic_CardPack04": 35650,
    "Pic_CardPack05": 34759, "Pic_CardPack06": 35652, "Pic_CardPack07": 35653,
}


def main():
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(os.path.join(OUT, "delivery"))
    files = []
    for label, aid in CHOICE.items():
        meta = json.load(open(os.path.join(ARCHIVE, str(aid), "_meta.json"), encoding="utf-8"))
        stored = [s for s in meta["stored"] if s.get("label") == label and s.get("hashOK", True)]
        if not stored:
            print("skip %s: archive id %d has no stored file for it" % (label, aid))
            continue
        s = stored[0]
        src = os.path.join(ARCHIVE, str(aid), s["file"])
        data = open(src, "rb").read()
        if hashlib.sha256(data).hexdigest() != s["sha256"]:
            sys.exit("hash mismatch for %s" % src)
        ext = os.path.splitext(s["filename"])[1]
        local = "%s_%d%s" % (label, aid, ext)          # ASCII name for serving
        with open(os.path.join(OUT, "delivery", local), "wb") as f:
            f.write(data)
        files.append({"label": label, "file": local, "name": s["filename"], "id": aid})
        print("%-15s %-6d %7d B  %s" % (label, aid, len(data), s["filename"].encode("ascii", "replace").decode()))
    doc = {"list": {"Name": "local_ps4_v1.13.0_staged", "Valid": 4102444800, "Memo": "local staged set", "Id": 1},
           "files": files}
    json.dump(doc, open(os.path.join(OUT, "vulcan.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("staged %d files in %s (not live)" % (len(files), OUT))


if __name__ == "__main__":
    main()
