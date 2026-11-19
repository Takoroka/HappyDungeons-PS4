"""Build the player package (Happy Dungeons PS4 for other players): dist\\HappyDungeonsPS4\\ and a zip of it.

  py -3 -I build_player_package.py              the friends server (107.170.39.185)  -> dist\\HappyDungeonsPS4-<date>.zip
  py -3 -I build_player_package.py --local      this PC's test servers (127.0.0.1)   -> dist\\HappyDungeonsPS4-localtest\\ (no zip)
  --fps 30   (or e.g. --fps 60,120,240, the default): which frame rates get a Play launcher (one rate = one
             launcher). Co-op works across frame rates (fps patch v3.2: shared 60 Hz online tick)

Sources: shadps4\\shadPS4.exe (unmodified sgdc3/shadPS4 a01cca9 build), shadps4\\user\\config.json and users.json
(cleaned: no paths, devices or accounts from this PC), the save fix, NAT/countdown and select() patches from
shadps4\\user\\patches\\HappyDungeonsLocal (REQUIRED_BLOCKS; the build stops if one is missing or an unknown one
appears), the fps patch from shadps4\\user\\patches\\HappyDungeonsFps, the optional local-saves patch
(tools\\make_localsave_patch.py, OFF; "Saves on this PC - on or off.bat" switches it), and
dist-src\\ (README, scripts). The URL patch is generated for the chosen server. Never included: the game, firmware
modules, keys, saves, logs, shader caches.
Also writes deploy\\client\\HappyDungeons.xml (the same patch file as in the friends-server package).
"""
import datetime
import json
import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMU = os.path.join(ROOT, "shadps4")
SRC = os.path.join(ROOT, "dist-src")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import make_localsave_patch  # noqa: E402  (optional local-saves patch, built from the game's eboot)
GPL = r"C:\Program Files\Git\mingw64\share\licenses\xz\COPYING.GPLv2"
SHADPS4_SOURCE = "https://github.com/sgdc3/shadPS4/tree/a01cca9c3e29a660d2a8aa7aaefe79b74c7f8b4e"
SERVERS = {
    "vps": {"base": "http://107.170.39.185", "linkApi": "https://107-170-39-185.sslip.io",
            "shadnet": "107.170.39.185:31313", "webapi": "http://107.170.39.185:31315"},
    "local": {"base": "http://127.0.0.1:5100", "linkApi": "http://127.0.0.1:5100",
              "shadnet": "127.0.0.1:31313", "webapi": "http://127.0.0.1:31315"},
}
# the game's hard-coded Toylogic URLs -> path kept on the new server
URLS = [("https://xas-pfpf.lynca.net/v12/api", "/v12/api", "Vulcan delivery API (event/shop data)"),
        ("https://telemetry.happywars.net/", "/", "Telemetry"),
        ("https://dev-api.lynca.net/", "/", "Lynca dev API"),
        ("https://jp.happydungeons.net/law/", "/law/", "Legal page link")]
# blocks taken from shadps4\user\patches\HappyDungeonsLocal\HappyDungeons.xml (by the start of their Name)
REQUIRED_BLOCKS = ["Saves: empty cloud slot",                       # save fix
                   "Network: treat the NAT check as done",          # lobby countdown stuck at 29
                   "Network: select() checks the game's socket"]    # no P2P packets at all without it
HEADER = '<?xml version="1.0"?>\n<Patch>\n    <TitleID>\n        <ID>CUSA09120</ID>\n    </TitleID>\n'


def url_block(base):
    host = base.split("//", 1)[1]
    lines = []
    for old, path, what in URLS:
        new = base + path
        if len(new) > len(old):
            sys.exit("%s is longer than %s: it does not fit in the game" % (new, old))
        address = " ".join("%02X" % b for b in old.encode() + b"\0")
        value = (new.encode() + b"\0" * (len(old) + 1 - len(new))).hex()
        lines.append('            <!-- %s: %s -> %s -->\n            <Line Type="mask" Address="%s" Value="%s" Offset="0"/>'
                     % (what, old, new, address, value))
    return ('    <Metadata Title="Happy Dungeons" Name="Server: redirect Toylogic hosts to %s" Author="local" '
            'Note="Points the game\'s hard-coded Toylogic URLs (Vulcan delivery API, telemetry, Lynca dev API, legal page) '
            'at the Happy Dungeons server so it never contacts Toylogic\'s hosts. PSN features go through shadNet." '
            'PatchVer="1.0" AppVer="01.11" AppElf="eboot.bin" isEnabled="true">\n        <PatchList>\n%s\n'
            '        </PatchList>\n    </Metadata>\n' % (host, "\n".join(lines)))


def game_patch(base):
    local = open(os.path.join(EMU, "user", "patches", "HappyDungeonsLocal", "HappyDungeons.xml"), encoding="utf-8").read()
    blocks = re.findall(r"    <Metadata .*?</Metadata>\n", local, flags=re.S)
    keep = []
    for prefix in REQUIRED_BLOCKS:
        found = [b for b in blocks if ('Name="%s' % prefix) in b]
        if len(found) != 1:
            sys.exit("HappyDungeonsLocal\\HappyDungeons.xml: expected one block named '%s...', found %d" % (prefix, len(found)))
        keep += found
    extra = [b for b in blocks if b not in keep and 'Name="Local server:' not in b]
    if extra:
        sys.exit("HappyDungeonsLocal\\HappyDungeons.xml has blocks the package does not know about: %s"
                 % [re.search(r'Name="([^"]*)"', b).group(1) for b in extra])
    return HEADER + url_block(base) + "".join(keep) + "</Patch>\n"


def clean_config(server, vblank):
    cfg = json.load(open(os.path.join(EMU, "user", "config.json"), encoding="utf-8"))
    cfg["General"].update(install_dirs=[], addon_install_dir="", sys_modules_dir="", home_dir="", font_dir="",
                          volume_slider=100)
    cfg["GPU"].update(vblank_frequency=vblank, full_screen=False)
    for key in ("sdl_main_output_device", "sdl_padSpk_output_device"):
        cfg["Audio"][key] = "Default Device"
    cfg["Input"]["default_controller_id"] = ""
    cfg["Network"].update(connected_to_network=True, shad_net_enabled=True, shadnet_server=server["shadnet"],
                          shadnet_webapi_server=server["webapi"], p2p_port=3658, signaling_info="")
    return json.dumps(cfg, indent=4) + "\n"


def clean_users():
    users = json.load(open(os.path.join(EMU, "user", "users.json"), encoding="utf-8"))
    for entry in users["Users"]["user"]:
        entry.update(shadnet_email="", shadnet_enabled=False, shadnet_npid="", shadnet_password="", shadnet_token="",
                     user_name="shadPS4-%d" % entry["player_index"])
    return json.dumps(users, indent=4) + "\n"


def write(path, text, crlf=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = text.replace("\r\n", "\n")
    if crlf:
        data = data.replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(data)


def fps_choice():
    """--fps 30 or --fps 60,120,240: the frame rates that get a Play launcher (the first one is the plain
    "Play Happy Dungeons.bat"). Co-op works with players on different frame rates (30 to 240 fps): the fps patch
    runs online play on a shared 60 Hz tick."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a.startswith("--fps"):
            text = a.split("=", 1)[1] if "=" in a else args[i + 1]
            rates = [int(x) for x in text.split(",") if x.strip()]
            if not rates or any(r not in (30, 60, 120, 240) for r in rates):
                sys.exit("--fps takes 30, 60, 120 and/or 240")
            return rates
    return [60, 120, 240]


def main():
    variant = "local" if "--local" in sys.argv[1:] else "vps"
    server = SERVERS[variant]
    rates = fps_choice()
    name = "HappyDungeonsPS4" if variant == "vps" else "HappyDungeonsPS4-localtest"
    out = os.path.join(ROOT, "dist", name)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "shadPS4", "user", "sys_modules"))
    shutil.copy2(os.path.join(EMU, "shadPS4.exe"), os.path.join(out, "shadPS4", "shadPS4.exe"))
    user = os.path.join(out, "shadPS4", "user")
    write(os.path.join(user, "config.json"), clean_config(server, rates[0] * 2))
    write(os.path.join(user, "users.json"), clean_users())
    patch = game_patch(server["base"])
    write(os.path.join(user, "patches", "HappyDungeons", "HappyDungeons.xml"), patch)
    write(os.path.join(user, "patches", "HappyDungeons", "files.json"), json.dumps({"HappyDungeons.xml": ["CUSA09120"]}))
    fps_dir = os.path.join(EMU, "user", "patches", "HappyDungeonsFps")
    for fname in ("HappyDungeons_fps.xml", "files.json"):
        write(os.path.join(user, "patches", "HappyDungeonsFps", fname), open(os.path.join(fps_dir, fname), encoding="utf-8").read())
    # own folder: shadPS4 applies only the first patch file of a folder that lists the game
    write(os.path.join(user, "patches", "HappyDungeonsSaves", "HappyDungeons_saves.xml"),
          make_localsave_patch.patch_xml(enabled=False))
    write(os.path.join(user, "patches", "HappyDungeonsSaves", "files.json"),
          json.dumps({"HappyDungeons_saves.xml": ["CUSA09120"]}))
    write(os.path.join(user, "sys_modules", "PUT PS4 FIRMWARE MODULES HERE.txt"),
          "Copy these from your own PS4's firmware (system/common/lib), or leave this folder empty if you already\n"
          "have them in %APPDATA%\\shadPS4\\sys_modules:\n  libSceAudiodec.sprx libSceJpegDec.sprx libSceJson2.sprx\n"
          "  libSceLibcInternal.sprx libScePngDec.sprx libSceRtc.sprx\n", crlf=True)
    for fname in ("setup.ps1", "play.ps1", "saves.ps1"):
        write(os.path.join(out, "tools", fname), open(os.path.join(SRC, "tools", fname), encoding="utf-8").read(), crlf=True)
    write(os.path.join(out, "tools", "server.json"),
          json.dumps({"linkApi": server["linkApi"], "shadnet": server["shadnet"], "webapi": server["webapi"]}, indent=1))
    launchers = [("Play Happy Dungeons%s.bat" % ("" if i == 0 else " - %d fps" % fps), fps) for i, fps in enumerate(rates)]
    width = max(len(n) for n, _ in launchers)
    readme = open(os.path.join(SRC, "README.txt"), encoding="utf-8").read().replace(
        "__LAUNCHERS__", "\n".join("  %-*s  %d fps" % (width, n, fps) for n, fps in launchers))
    write(os.path.join(out, "README.txt"), readme, crlf=True)
    write(os.path.join(out, "Setup Happy Dungeons.bat"),
          '@echo off\nrem First-time setup (game folder, your player). Run again to change something.\n'
          'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\\setup.ps1" %*\npause\n', crlf=True)
    write(os.path.join(out, "Saves on this PC - on or off.bat"),
          '@echo off\nrem Optional: keep your saves on this PC instead of the server (off unless you switch it on).\n'
          'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\\saves.ps1" %*\npause\n', crlf=True)
    for fname, fps in launchers:
        write(os.path.join(out, fname),
              '@echo off\nrem Happy Dungeons at %d fps. Co-op works with players on different frame rates (30 to 240 fps).\n'
              'powershell -NoProfile -ExecutionPolicy Bypass -File "%%~dp0tools\\play.ps1" -Vblank %d\n'
              'if errorlevel 1 pause\n' % (fps, fps * 2), crlf=True)
    os.makedirs(os.path.join(out, "LICENSES"))
    shutil.copy2(GPL, os.path.join(out, "LICENSES", "shadPS4-GPL-2.0.txt"))
    write(os.path.join(out, "SOURCE.txt"),
          "shadPS4.exe is an unmodified build of shadPS4 (GPL-2.0, see LICENSES\\shadPS4-GPL-2.0.txt).\n"
          "Its source code: %s\n(fork of https://github.com/shadps4-emu/shadPS4, commit a01cca9, v0.20.1 WIP).\n\n"
          "The game patches in shadPS4\\user\\patches are plain text (XML): each line says what it changes and why.\n"
          "The setup and play scripts in tools\\ are plain PowerShell.\n" % SHADPS4_SOURCE, crlf=True)
    if variant == "vps":
        write(os.path.join(ROOT, "deploy", "client", "HappyDungeons.xml"), patch)
        stamp = datetime.date.today().strftime("%Y%m%d")
        zpath = os.path.join(ROOT, "dist", "HappyDungeonsPS4-%s.zip" % stamp)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for folder, _, files in os.walk(out):
                for fname in files:
                    full = os.path.join(folder, fname)
                    z.write(full, os.path.relpath(full, os.path.dirname(out)))
        print("%s (%d bytes)" % (zpath, os.path.getsize(zpath)))
    print("built %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
