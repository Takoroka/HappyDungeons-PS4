"""Build the VPS bundle for the Happy Dungeons PS4 side: deploy\\hd-ps4-vps-bundle.tar.gz.

  py -3 -I build_vps_bundle.py

Contents (unpacked into one folder on the VPS, then "bash install.sh <step>"):
  install.sh, hd_ps4_gateway.py (from server\\), shadnet.cfg, worlds.cfg, clientversions.cfg, gateway.json
  (templates: install.sh fills in random secrets), the two systemd units, the two Caddy blocks,
  base_serverdata\\ (the Xbox client's ServerData pictures/packs the server does not publish) and
  overrides\\Pic_Top01.jpg (title art instead of the service-end notice).
Text files go in with LF line endings. Nothing is uploaded by this script.
"""
import hashlib
import io
import os
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VPS = os.path.join(ROOT, "deploy", "vps")
OUT = os.path.join(ROOT, "deploy", "hd-ps4-vps-bundle.tar.gz")
SERVERDATA = r"%USERPROFILE%\OneDrive\Desktop\Happy Dungeons\Mount\ServerData"
OVERRIDE_TOP = os.path.join(ROOT, "livecopy", "overrides", "Pic_Top01.jpg")
TEXT = ["install.sh", "shadnet.cfg", "worlds.cfg", "clientversions.cfg", "gateway.json", "hd-ps4-shadnet.service",
        "hd-ps4-gateway.service", "Caddyfile.ps4-site", "Caddyfile.ps4-route",
        "players_page_ps4.py"]
# what the gateway can deliver from the base set (DungeonPatch/JsonDiff are never sent to the PS4)
BASE_NAMES = (["permission.pak", "presentation.pak", "shop_catalog.bin", "happy_card.pak", "EventExtra.pak",
               "EventCampaign.pak", "event_gacha.pak", "event_order.pak", "event_bonus.pak", "Dictionary.pak"]
              + ["Pic_Top01.jpg", "Pic_Top02.jpg"] + ["Pic_Loading%02d.jpg" % i for i in range(1, 5)]
              + ["Pic_eventEX.jpg", "Pic_eventCP.jpg"] + ["Pic_GameHome%02d.jpg" % i for i in range(1, 9)]
              + ["Pic_CardMain%02d.jpg" % i for i in range(1, 8)] + ["Pic_CardPack%02d.png" % i for i in range(1, 8)]
              + ["Pic_imageEX01.jpg", "Pic_ruleCP.jpg"])


def add_bytes(tar, name, data, mode=0o644):
    info = tarfile.TarInfo("hd-ps4-bundle/" + name)
    info.size, info.mode, info.mtime = len(data), mode, int(os.path.getmtime(VPS))
    tar.addfile(info, io.BytesIO(data))


def main():
    gateway = open(os.path.join(ROOT, "server", "hd_ps4_gateway.py"), "rb").read()
    deployed = open(os.path.join(VPS, "hd_ps4_gateway.py"), "rb").read()
    if gateway != deployed:
        open(os.path.join(VPS, "hd_ps4_gateway.py"), "wb").write(gateway)
        print("deploy\\vps\\hd_ps4_gateway.py updated from server\\")
    with tarfile.open(OUT, "w:gz") as tar:
        for name in TEXT:
            data = open(os.path.join(VPS, name), "rb").read().replace(b"\r\n", b"\n")
            add_bytes(tar, name, data, 0o755 if name.endswith(".sh") else 0o644)
        add_bytes(tar, "hd_ps4_gateway.py", gateway.replace(b"\r\n", b"\n"))
        count = 0
        for name in BASE_NAMES:
            path = os.path.join(SERVERDATA, name)
            if os.path.isfile(path):
                add_bytes(tar, "base_serverdata/" + name, open(path, "rb").read())
                count += 1
        add_bytes(tar, "overrides/Pic_Top01.jpg", open(OVERRIDE_TOP, "rb").read())
    digest = hashlib.sha256(open(OUT, "rb").read()).hexdigest()
    print("%s: %d bytes, %d base ServerData files, sha256 %s" % (OUT, os.path.getsize(OUT), count, digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
