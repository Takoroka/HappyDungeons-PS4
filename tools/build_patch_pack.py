"""Builds the cross-platform Happy Dungeons patch pack for a normal shadPS4 install (Windows, macOS, Linux).

  py -3 -I build_patch_pack.py [--server vps|local]

The pack is one folder, HappyDungeons, that goes into shadPS4's patches folder:
  HappyDungeons.xml       ALL the patches in one file (shadPS4 applies only one file per patch folder):
                          server redirect (Toylogic hosts -> the friends server, or 127.0.0.1:5100 for --server local),
                          empty-cloud-slot save fix, NAT check, select() fix (from the player package's patch file);
                          frame-rate decoupling + shared online tick, 30 fps cap lifted (tools/make_fps_patch.py);
                          optional, off by default: keep saves on this PC (tools/make_localsave_patch.py), when built
  files.json              maps HappyDungeons.xml to CUSA09120 (how shadPS4 finds patches for a game)
plus INSTALL.txt (dist-src/patch-pack/INSTALL.txt) and happydungeons_account.py (gets a player's shadNet login
from the server, any OS; the same as the gateway's /ps4/account page). Output: dist/HappyDungeons-patches[-local]/ and a zip next to it.
The patches only change the game in memory while it runs; shadPS4 itself is not modified (any build with shadNet,
e.g. the official v0.21 pre-release, works)."""
import json
import os
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVERS = {
    "vps": {"xml": os.path.join(ROOT, "deploy", "client", "HappyDungeons.xml"), "suffix": "",
            "shadnet": "107.170.39.185:31313", "webapi": "http://107.170.39.185:31315",
            "link": "https://107-170-39-185.sslip.io"},
    "local": {"xml": os.path.join(ROOT, "shadps4", "user", "patches", "HappyDungeonsLocal", "HappyDungeons.xml"),
              "suffix": "-local", "shadnet": "127.0.0.1:31313", "webapi": "http://127.0.0.1:31315",
              "link": "http://127.0.0.1:5100"},
}
FPS_XML = os.path.join(ROOT, "shadps4", "user", "patches", "HappyDungeonsFps", "HappyDungeons_fps.xml")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import make_localsave_patch  # noqa: E402  (optional local-saves patch, built from the game's eboot, off by default)

SRC = os.path.join(ROOT, "dist-src", "patch-pack")   # INSTALL.txt, happydungeons_account.py


def main():
    server = "vps"
    if "--server" in sys.argv:
        server = sys.argv[sys.argv.index("--server") + 1]
    s = SERVERS[server]
    out = os.path.join(ROOT, "dist", "HappyDungeons-patches" + s["suffix"])
    if os.path.isdir(out):
        shutil.rmtree(out)
    pack = os.path.join(out, "HappyDungeons")
    os.makedirs(pack)
    # ONE patch file: shadPS4 applies only the first file in a patch folder whose files.json entry lists the game
    # (common/memory_patcher.cpp: "break;" after the first match), so the fps and saves patches must be in the same
    # file as the server redirect, or they are silently skipped (the launcher still lists and ticks them).
    sources = [open(p, encoding="utf-8").read() for p in (s["xml"], FPS_XML)]
    sources.append(make_localsave_patch.patch_xml(enabled=False))
    saves_note = (
        "  (in HappyDungeons.xml)                   OPTIONAL, off by default: \"Saves: keep them on this PC\" keeps\n"
        "                                           your saves on this PC instead of the server (tick it in Cheats /\n"
        "                                           Patches). Your character is then no longer shared with the PC\n"
        "                                           version or other computers, and the server does not keep it.\n"
        "                                           The save files are hds_*.bin / .bak in the shadPS4 folder >\n"
        "                                           download > CUSA09120; copy them to back up.\n")
    head, blocks = None, []
    for data in sources:
        assert "CUSA09120" in data and 'AppVer="01.11"' in data
        start, end = data.index("</TitleID>") + len("</TitleID>"), data.rindex("</Patch>")
        if head is None:
            head = data[:start]
        blocks.append(data[start:end].strip("\n"))
    merged = head + "\n" + "\n".join(blocks) + "\n</Patch>\n"
    assert merged.count("<Metadata") == sum(data.count("<Metadata") for data in sources)
    files = {"HappyDungeons.xml": merged}
    with open(os.path.join(pack, "HappyDungeons.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(merged)
    with open(os.path.join(pack, "files.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"HappyDungeons.xml": ["CUSA09120"]}, f)
    fill = {"__SHADNET__": s["shadnet"], "__WEBAPI__": s["webapi"], "__LINK_API__": s["link"],
            "__ACCOUNT_PAGE__": s["link"] + "/ps4/account", "__SAVES__": saves_note}

    def filled(name):
        data = open(os.path.join(SRC, name), encoding="utf-8").read()
        for key, value in fill.items():
            data = data.replace(key, value)
        assert "__" not in data.replace("__name__", "").replace("__main__", ""), name + ": placeholder left"
        return data

    text = filled("INSTALL.txt")
    with open(os.path.join(out, "happydungeons_account.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(filled("happydungeons_account.py"))
    with open(os.path.join(out, "INSTALL.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(text)
    z = out + "-" + time.strftime("%Y%m%d") + ".zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for dp, _, fs in os.walk(out):
            for fn in fs:
                p = os.path.join(dp, fn)
                zf.write(p, os.path.relpath(p, os.path.dirname(out)))
    print(out)
    print(z, os.path.getsize(z), "bytes;", ", ".join(sorted(files)))


if __name__ == "__main__":
    main()
