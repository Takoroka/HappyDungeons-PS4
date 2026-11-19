"""Happy Dungeons (PS4) on shadPS4: get your shadNet login for the Happy Dungeons friends server.

Works on Windows, macOS and Linux with Python 3.6 or newer (no extra modules). The web page does the same:
__ACCOUNT_PAGE__

  python3 happydungeons_account.py new "Your Name"          a new player (saves happydungeons_identity.json here:
                                                           KEEP IT, it is your player's key)
  python3 happydungeons_account.py pc  path/to/winDurango.json
                                                           the same player as your PC (Xbox/WinDurango) game
  python3 happydungeons_account.py key path/to/happydungeons_identity.json
                                                           a player you made before (new PC, lost the login)
On Windows type "py" instead of "python3".

Happy Dungeons has its OWN shadNet login on the friends server: never your public shadNet password (the friends
server must never get it). The server makes one with a random password, or you pick a NEW one for it:
  --pick-login         asks for a NEW password, only for Happy Dungeons (not shown while you type; at least 8
                       characters; NOT the one of your public shadNet account or of anything else). The Account ID
                       comes from --npid NAME, or it asks.
  --npid NAME          the Account ID you would like (3-16 letters, digits, - or _)

shadPS4 has ONE shadNet login per user, used for every game, so put the Happy Dungeons login in a shadPS4 user of
its own (Settings > Manage Users > Create User > ShadNet...) and make that user the default (Set Default User) when
you play Happy Dungeons.

  --shadps4 [FOLDER]   also set up shadPS4 for you (close shadPS4 and its launcher first): writes the login into
                       FOLDER/users.json - into --user NAME/ID, or else a user that has no other shadNet login (your
                       default user only if it has none) - and creates Happy Dungeons' game-specific settings
                       FOLDER/custom_configs/CUSA09120.json from your global settings (server, WebAPI server,
                       shadNet, network connected, UPnP). FOLDER defaults to shadPS4's folder for this OS. The old
                       files are kept as *.before-happydungeons.
  --fps N              with --shadps4: the frame rate for Happy Dungeons (30, 60, 120 or 240; vblank = 2 x N).
                       Default for new game-specific settings: 60.
"""
import argparse
import getpass
import json
import os
import shutil
import ssl
import sys
import urllib.error
import urllib.request

# filled in when the pack is built: the friends server's account API, and shadPS4's Network > Server / WebAPI Server
LINK_API = "__LINK_API__"
SHADNET = "__SHADNET__"
WEBAPI = "__WEBAPI__"
TITLE = "CUSA09120"


def post(path, body):
    req = urllib.request.Request(LINK_API.rstrip("/") + path, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            answer = json.loads(error.read().decode("utf-8"))
        except ValueError:
            answer = {}
        reason = answer.get("reason") or answer.get("error") or "HTTP %d" % error.code
        if answer.get("xuidBase") and answer.get("secret"):
            return dict(answer, failed=reason)   # a new player was made, but no login: keep the key anyway
        sys.exit("The server said no: %s" % reason)
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLError) and sys.platform == "darwin":
            sys.exit("Python has no web certificates yet: run 'Install Certificates.command' in your Python folder "
                     "(Applications > Python 3.x), or use the web page: %s/ps4/account" % LINK_API.rstrip("/"))
        sys.exit("Cannot reach the server (%s): %s" % (LINK_API, error.reason))


def read_key(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as error:
        sys.exit("Cannot read %s: %s" % (path, error))
    base, secret = str(data.get("xuidBase") or ""), str(data.get("secret") or "")
    if not base or not secret:
        sys.exit("%s has no player key (xuidBase/secret). Has the PC game signed in to the server yet?" % path)
    return base, secret


def keep_identity(answer, base, secret):
    """Save the player's key as happydungeons_identity.json in the current folder (never over another player's)."""
    identity = {"server": LINK_API, "gamertag": answer.get("gamertag") or "", "xuidBase": base, "secret": secret}
    path = "happydungeons_identity.json"
    n = 2
    while os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                if str(json.load(f).get("xuidBase")) == base:
                    return os.path.abspath(path)
        except (OSError, ValueError):
            pass
        path = "happydungeons_identity-%d.json" % n
        n += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(identity, f, indent=2)
        f.write("\n")
    return os.path.abspath(path)


def default_folder():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "shadPS4")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/shadPS4")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "shadPS4")


def backup(path):
    keep = path + ".before-happydungeons"
    if os.path.exists(path) and not os.path.exists(keep):
        shutil.copy2(path, keep)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def load_user(folder, wanted_user, npid):
    """(users.json path, its data, the user for the Happy Dungeons login) - or a message why not. Without
    wanted_user: a user that has no other shadNet login (the default user first), so nobody's normal login is
    overwritten."""
    path = os.path.join(folder, "users.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        users = data["Users"]["user"]
    except (OSError, ValueError, KeyError, TypeError):
        return "no usable users.json in %s (start shadPS4 once first)" % folder
    if wanted_user:
        pick = [u for u in users if str(u.get("user_id")) == wanted_user or u.get("user_name") == wanted_user]
        if not pick:
            return "no user %r in %s. Users: %s" % (wanted_user, path, ", ".join(
                "%s (%s)" % (u.get("user_name"), u.get("user_id")) for u in users))
        return path, data, pick[0]
    free = [u for u in sorted(users, key=lambda u: u.get("player_index") != 1)
            if not u.get("shadnet_npid") or str(u.get("shadnet_npid")).lower() == npid.lower()]
    if not free:
        return ("every shadPS4 user already has a shadNet login: add one for Happy Dungeons (Settings > Manage Users >"
                " Create User), then run again with --user NAME")
    return path, data, free[0]


def set_login(folder, wanted_user, npid, password):
    found = load_user(folder, wanted_user, npid)
    if isinstance(found, str):
        print("! %s. Or enter the login by hand." % found)
        return False
    path, data, user = found
    old = user.get("shadnet_npid") or ""
    if old and old.lower() != npid.lower():
        print("  User %s signed in to shadNet as %s before; that login is replaced (kept in users.json.before-"
              "happydungeons)." % (user.get("user_name"), old))
    user.update(shadnet_enabled=True, shadnet_npid=npid, shadnet_password=password, shadnet_email="", shadnet_token="")
    backup(path)
    write_json(path, data)
    print("  %s: user %s (%s) signs in as %s" % (path, user.get("user_name"), user.get("user_id"), npid))
    if user.get("player_index") != 1:
        print("  That is not your default user: before you play Happy Dungeons, pick %s in Settings > Manage Users"
              " and click Set Default User (switch back for your other games)." % user.get("user_name"))
    return True


def set_game_settings(folder, fps):
    path = os.path.join(folder, "custom_configs", TITLE + ".json")
    fresh = not os.path.exists(path)
    source = os.path.join(folder, "config.json") if fresh else path
    try:
        with open(source, encoding="utf-8-sig") as f:
            config = json.load(f)
    except (OSError, ValueError):
        print("! No usable %s (start shadPS4 once first). Make the game-specific settings in the launcher." % source)
        return False
    net, gpu = config.get("Network"), config.get("GPU")
    if not isinstance(net, dict) or "shadnet_server" not in net or not isinstance(gpu, dict):
        print("! This shadPS4 keeps its settings differently. Make the game-specific settings in the launcher.")
        return False
    net.update(connected_to_network=True, shad_net_enabled=True, enable_upnp=True, shadnet_server=SHADNET,
               shadnet_webapi_server=WEBAPI)
    if fps or fresh:
        gpu["vblank_frequency"] = 2 * (fps or 60)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup(path)
    write_json(path, config)
    print("  %s: Happy Dungeons' own settings (server %s, %d fps). Your other games keep theirs."
          % (path, SHADNET, gpu.get("vblank_frequency", 120) // 2))
    return True


def pick_login(npid):
    """An Account ID and a NEW password for Happy Dungeons only (asked; the password is not shown)."""
    npid = npid or input("Account ID (NPID) for Happy Dungeons (3-16 letters, digits, - or _): ").strip()
    print("Now a NEW password, only for Happy Dungeons - NOT the one of your public shadNet account or of anything "
          "else. At least 8 characters.")
    password = getpass.getpass("New Happy Dungeons password (not shown): ")
    if password != getpass.getpass("Again: "):
        sys.exit("The two passwords differ.")
    if len(password) < 8:
        sys.exit("Pick at least 8 characters.")
    return npid, password


def main():
    ap = argparse.ArgumentParser(description="Get your Happy Dungeons shadNet login (see the top of this file).")
    ap.add_argument("who", choices=["new", "pc", "key"], help="new player / PC player / earlier PS4 player")
    ap.add_argument("what", help="new: your player name; pc: winDurango.json; key: happydungeons_identity.json")
    ap.add_argument("--npid", default=None)
    ap.add_argument("--pick-login", action="store_true")
    ap.add_argument("--shadps4", nargs="?", const="", default=None, metavar="FOLDER")
    ap.add_argument("--user", default=None)
    ap.add_argument("--fps", type=int, choices=[30, 60, 120, 240], default=None)
    ap.add_argument("--server", default=None, help=argparse.SUPPRESS)   # another account API (tests)
    args = ap.parse_args()
    global LINK_API
    LINK_API = args.server or LINK_API
    if "://" not in LINK_API:
        sys.exit("This copy has no server address: use the one from the Happy Dungeons patch pack.")
    folder = os.path.normpath(args.shadps4 or default_folder())

    login = {"npid": args.npid or ""}
    if args.pick_login:
        npid, password = pick_login(args.npid)
        login = {"npid": npid, "password": password}

    if args.who == "new":
        answer = post("/ps4/register", dict(login, gamertag=args.what))
        base, secret = str(answer["xuidBase"]), str(answer["secret"])
    else:
        base, secret = read_key(args.what)
        answer = post("/ps4/link", dict(login, xuidBase=base, secret=secret))
    kept = args.what
    if args.who == "new":
        kept = keep_identity(answer, base, secret)
        print("Your player's key: %s\n  KEEP A COPY of this file: with it you get your login again on any PC." % kept)
    if answer.get("failed"):
        sys.exit("Your new player was made, but the login was not: %s\nTry again later with: key %s"
                 % (answer["failed"], kept))

    own = bool(answer.get("ownPassword"))
    print()
    print("Happy Dungeons player: %s" % (answer.get("gamertag") or "?"))
    print("  Account ID (NPID): %s" % answer["npid"])
    password = login.get("password") if own and args.pick_login else answer.get("password")
    if own:
        print("  Password:          %s" % ("the new one you just picked" if args.pick_login
                                           else "the one you picked for it (run with --pick-login to change it)"))
    else:
        print("  Password:          %s" % answer["password"])
    print()
    if args.shadps4 is not None:
        if not os.path.isdir(folder):
            sys.exit("No shadPS4 folder at %s (start shadPS4 once, or give --shadps4 FOLDER)." % folder)
        ok = set_login(folder, args.user, answer["npid"], password) if password else False
        ok = set_game_settings(folder, args.fps) and ok
        if ok:
            print("Done: start Happy Dungeons in shadPS4.")
            return
    print("In shadPS4: Settings > Manage Users > Create User (e.g. HappyDungeons) > pick it > ShadNet... > tick")
    print("'Enable ShadNet for this user' and enter the Account ID and Password. Before you play Happy Dungeons, make")
    print("that user the default (Set Default User); switch back for your other games.")
    print("Happy Dungeons' game-specific settings, Network tab:")
    print("  Server %s, WebAPI Server %s, Enable shadNet, Set Network Connected Status as True, Enable UPNP."
          % (SHADNET, WEBAPI))


if __name__ == "__main__":
    main()
