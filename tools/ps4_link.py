"""Link a portable shadPS4 (Happy Dungeons PS4) to a Happy Dungeons server account. Run once per PC.

  py -3 ps4_link.py --profile "<shadPS4 folder>" --windurango "<Mount>\\UserData\\LocalState\\WinDurango\\winDurango.json"
        same player as your Xbox/PC game (same character, friends and saves)
  py -3 ps4_link.py --profile "<shadPS4 folder>" --gamertag "Your Name"
        a new player (PS4 only); the new player's key is saved in <shadPS4 folder>\\happydungeons_identity.json -
        keep that file, it is what signs you in.

Options: --server URL (the Happy Dungeons server, default https://107-170-39-185.sslip.io),
         --shadnet HOST:PORT (default: the server's host, port 31313), --npid NAME (your online id, optional).
Writes user 1 of <shadPS4 folder>\\user\\users.json (online id + password) and the shadNet server fields of
<shadPS4 folder>\\user\\config.json. Nothing else on the PC is changed.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        except ValueError:
            return error.code, {"reason": "HTTP %d" % error.code}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, help="portable shadPS4 folder (the one with user\\)")
    ap.add_argument("--server", default="https://107-170-39-185.sslip.io")
    ap.add_argument("--shadnet", default=None)
    ap.add_argument("--webapi", default=None)
    ap.add_argument("--windurango", default=None, help="winDurango.json of your Xbox/PC game")
    ap.add_argument("--gamertag", default=None, help="name for a new player")
    ap.add_argument("--npid", default=None)
    args = ap.parse_args()

    user_dir = os.path.join(args.profile, "user")
    if not os.path.isfile(os.path.join(user_dir, "users.json")):
        sys.exit("No user\\users.json in %s - start shadPS4 from that folder once first." % args.profile)
    host = urllib.parse.urlsplit(args.server).hostname
    shadnet = args.shadnet or "%s:31313" % host
    webapi = args.webapi or "http://%s:31315" % shadnet.rsplit(":", 1)[0]

    if args.windurango:
        with open(args.windurango, encoding="utf-8-sig") as f:
            wd = json.load(f)
        base, secret = str(wd.get("xuidBase") or ""), str(wd.get("secret") or "")
        if not base or not secret:
            sys.exit("%s has no xuidBase/secret (has the game signed you in yet?)" % args.windurango)
        status, answer = post(args.server.rstrip("/") + "/ps4/link",
                              {"xuidBase": base, "secret": secret, "npid": args.npid})
    elif args.gamertag:
        status, answer = post(args.server.rstrip("/") + "/ps4/register", {"gamertag": args.gamertag, "npid": args.npid})
        if status == 200:
            keep = os.path.join(args.profile, "happydungeons_identity.json")
            with open(keep, "w", encoding="utf-8") as f:
                json.dump({"server": args.server, "gamertag": answer.get("gamertag"), "xuidBase": answer["xuidBase"],
                           "secret": answer["secret"]}, f, indent=1)
            print("Your new player's key is in %s - keep it." % keep)
    else:
        sys.exit("Give --windurango (your existing player) or --gamertag (a new player).")
    if status != 200 or not answer.get("ok"):
        sys.exit("The server said no: %s" % answer.get("reason", answer))

    users_path = os.path.join(user_dir, "users.json")
    with open(users_path, encoding="utf-8") as f:
        users = json.load(f)
    for entry in users["Users"]["user"]:
        if entry.get("user_id") == 1000:
            entry.update(shadnet_enabled=True, shadnet_npid=answer["npid"], shadnet_password=answer["password"],
                         shadnet_email="", shadnet_token="", user_name=answer["npid"])
    with open(users_path, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=4)
    cfg_path = os.path.join(user_dir, "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("Network", {}).update(connected_to_network=True, shad_net_enabled=True,
                                          shadnet_server=shadnet, shadnet_webapi_server=webapi)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)
    print("Linked: online id %s = Happy Dungeons player %s (%s). shadNet %s." % (
        answer["npid"], answer.get("gamertag"), answer["xuidBase"], shadnet))


if __name__ == "__main__":
    main()
