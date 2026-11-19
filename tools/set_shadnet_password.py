"""Sets the password of a shadNet account on the LOCAL Happy Dungeons server (this PC), e.g. to match the password
the same name uses on the public shadNet, so one shadPS4 login works for every game.

  py -3 tools\\set_shadnet_password.py MyPlayer

You type the new password yourself (hidden, twice); it is never shown or logged. The tool
  1. asks shadNet's admin API (127.0.0.1:31350, the gateway's admin account from livecopy\\gateway.json) to set it
     (shadNet hashes it itself),
  2. checks a real shadNet sign-in with it (127.0.0.1:31413),
  3. updates the copies that must match: the portable shadPS4 profile's users.json (used by the "Play Happy
     Dungeons (PS4)" .bat launchers) and the gateway's link record, if the account is linked.
Note: shadNet's game sign-in is not encrypted (also on the public shadNet), so the password reaches every server
this account signs in to.
Options: --profile <folder> (default shadps4), --password-file <file> (tests only)."""
import getpass
import json
import os
import socket
import sqlite3
import struct
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY_CFG = os.path.join(ROOT, "livecopy", "gateway.json")
DB = os.path.join(ROOT, "shadnet", "db", "shadnet.db")
HEADER = struct.Struct("<BHIQ")
CMD_LOGIN = 0


def pb_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_str(num, value):
    value = value.encode("utf-8")
    return pb_varint((num << 3) | 2) + pb_varint(len(value)) + value if value else b""


def recv_exact(s, n):
    out = b""
    while len(out) < n:
        chunk = s.recv(n - len(out))
        if not chunk:
            raise ConnectionError("closed")
        out += chunk
    return out


def read_packet(s):
    head = recv_exact(s, HEADER.size)
    ptype, cmd, size, pid = HEADER.unpack(head)
    return ptype, cmd, head + recv_exact(s, size - HEADER.size)


def try_login(hostport, npid, password):
    """shadNet Login (command 0): the reply's error code (0 = signed in)."""
    host, _, port = hostport.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=15) as s:
        read_packet(s)                                     # ServerInfo
        proto = pb_str(1, npid) + pb_str(2, password)
        req = struct.pack("<I", len(proto)) + proto
        s.sendall(HEADER.pack(0, CMD_LOGIN, HEADER.size + len(req), 1) + req)
        while True:
            ptype, cmd, packet = read_packet(s)
            if ptype == 1 and cmd == CMD_LOGIN:
                return packet[HEADER.size] if len(packet) > HEADER.size else -1


def http_json(method, url, body=None, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:200].decode("utf-8", "replace")


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        sys.exit(__doc__)
    npid = args[0]
    profile = args[args.index("--profile") + 1] if "--profile" in args else "shadps4"
    cfg = json.load(open(GATEWAY_CFG, encoding="utf-8"))
    row = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True).execute(
        "select user_id, username from account where username = ? collate nocase", (npid,)).fetchone()
    if not row:
        sys.exit("No account %r on the local shadNet." % npid)
    user_id, npid = row
    if "--password-file" in args:
        password = open(args[args.index("--password-file") + 1], encoding="utf-8").read().strip()
    else:
        print("New password for %s on the local Happy Dungeons server (typing is hidden)." % npid)
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Again:    "):
            sys.exit("The two entries differ; nothing changed.")
    if not password:
        sys.exit("Empty password; nothing changed.")

    status, login = http_json("POST", cfg["shadnetAdmin"] + "/admin/v1/login",
                              {"npid": cfg["shadnetAdminNpid"], "password": cfg["shadnetAdminPassword"]})
    if status != 200:
        sys.exit("shadNet admin sign-in failed (%s); nothing changed." % status)
    token = login.get("token")
    path = cfg["shadnetAdmin"] + "/admin/v1/users/%d/password" % user_id
    for method in ("POST", "PUT"):
        status, answer = http_json(method, path, {"password": password}, token)
        if status not in (404, 405):
            break
    if status != 200:
        sys.exit("shadNet did not accept the change (%s %s); nothing changed." % (status, answer))
    code = try_login(cfg["shadnetUpstream"], npid, password)
    print("shadNet: password set; sign-in check %s." % ("OK" if code == 0 else "FAILED (error %s)" % code))
    if code != 0:
        sys.exit(1)

    users = os.path.join(ROOT, profile, "user", "users.json")
    if os.path.exists(users):
        u = json.load(open(users, encoding="utf-8"))
        hits = [x for x in u["Users"]["user"] if x.get("shadnet_npid", "").lower() == npid.lower()]
        for x in hits:
            x["shadnet_password"] = password
            x["shadnet_token"] = ""
        if hits:
            tmp = users + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(u, f, indent=4)
            os.replace(tmp, users)
        print("%s\\user\\users.json: %s." % (profile, "updated" if hits else "no user with this name (unchanged)"))
    links = os.path.join(cfg["stateDir"], "links.json")
    if os.path.exists(links):
        data = json.load(open(links, encoding="utf-8"))
        link = data.get(npid.lower())
        if link:
            link["password"] = password
            tmp = links + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, links)
        print("gateway link record: %s." % ("updated" if link else "not linked (nothing to update)"))
    print("Done. Sign in with this password in shadPS4 (and on the public shadNet if it is the same).")


if __name__ == "__main__":
    main()
