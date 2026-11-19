"""Builds the cross-platform Happy Dungeons patch pack for a normal shadPS4 install (Windows, macOS, Linux).

  py -3 -I build_patch_pack.py [--server vps|local]

The pack is one folder, HappyDungeons, that goes into shadPS4's patches folder:
  HappyDungeons.xml       server redirect (Toylogic hosts -> the friends server, or 127.0.0.1:5100 for --server local),
                          empty-cloud-slot save fix, NAT check, select() fix (from the player package's patch file)
  HappyDungeons_fps.xml   frame-rate decoupling + shared online tick (tools/make_fps_patch.py), 30 fps cap lifted
  HappyDungeons_saves.xml optional (off by default): keep saves on this PC instead of the server, when built
  files.json              maps the files to CUSA09120 (how shadPS4 finds patches for a game)
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
SAVES_XML = os.path.join(ROOT, "runs", "saves_local.xml")   # tools/make_localsave_patch.py output, when present

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
    files = {"HappyDungeons.xml": s["xml"], "HappyDungeons_fps.xml": FPS_XML}
    saves_note = ""
    if os.path.exists(SAVES_XML):
        files["HappyDungeons_saves.xml"] = SAVES_XML
        saves_note = (
            "  HappyDungeons/HappyDungeons_saves.xml    OPTIONAL, off by default: keep your saves on this PC instead of\n"
            "                                           the server (tick it in Cheats / Patches). Your character is then\n"
            "                                           no longer shared with the PC version or other computers, and the\n"
            "                                           server does not keep it. The save files are hds_*.bin / .bak in\n"
            "                                           the shadPS4 folder > download > CUSA09120; copy them to back up.\n")
    for name, src in files.items():
        data = open(src, encoding="utf-8").read()
        assert "CUSA09120" in data and 'AppVer="01.11"' in data, src
        with open(os.path.join(pack, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
    with open(os.path.join(pack, "files.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({name: ["CUSA09120"] for name in files}, f)
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
