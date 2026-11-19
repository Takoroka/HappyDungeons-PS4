"""Local test of the gateway's register + friends sync (127.0.0.1 only).
1. registers a second PS4 player through the gateway (/ps4/register),
2. makes them Dungeons friends with the first linked player through the Dungeons server's own friends API,
3. signs both in through the gateway's shadNet proxy (protocol Login), which triggers the friends sync,
4. prints what shadNet's admin API now says about the first player's friends.
Usage: py -3 -I gw_friends_test.py <first xuidBase> <first secret> <admin npid> <admin password>
"""
import json
import socket
import struct
import sys
import time
import urllib.request

GW, DUNGEONS, ADMIN = "http://127.0.0.1:5100", "http://127.0.0.1:5200", "http://127.0.0.1:31350"
HEADER = struct.Struct("<BHIQ")


def http(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]


def pb(num, text):
    data = text.encode()
    return bytes([(num << 3) | 2, len(data)]) + data


def login(npid, password):
    s = socket.create_connection(("127.0.0.1", 31313), timeout=10)
    head = s.recv(15)
    s.recv(HEADER.unpack(head)[2] - 15)
    body = pb(1, npid) + pb(2, password) + pb(4, "CUSA09120") + pb(5, "Happy Dungeons")
    payload = struct.pack("<I", len(body)) + body
    s.sendall(HEADER.pack(0, 0, 15 + len(payload), 1) + payload)
    head = s.recv(15)
    rest = s.recv(HEADER.unpack(head)[2] - 15)
    return s, rest[0]


def main():
    base1, secret1, admin_npid, admin_pw = sys.argv[1:5]
    tok1 = {"Authorization": "XBL3.0 x=1;WD1.%s.%s" % (base1, secret1)}
    st, friend = http("POST", GW + "/ps4/register", {"gamertag": "Friend PS4"})
    print("register:", st, {k: v for k, v in friend.items() if k not in ("secret", "password")})
    tok2 = {"Authorization": "XBL3.0 x=1;WD1.%s.%s" % (friend["xuidBase"], friend["secret"])}
    print("friend request:", http("POST", DUNGEONS + "/windurango/friends/add",
                                  {"xuid": str(int(friend["xuidBase"]) + 1)}, tok1))
    print("friend accept:", http("POST", DUNGEONS + "/windurango/friends/add", {"xuid": str(int(base1) + 1)}, tok2))
    st, links = http("GET", GW + "/ps4/status")
    print("gateway:", links)
    s1, e1 = login("MyPlayerPS4", secret1)
    s2, e2 = login(friend["npid"], friend["password"])
    print("logins:", e1, e2)
    time.sleep(3)
    st, tok = http("POST", ADMIN + "/admin/v1/login", {"npid": admin_npid, "password": admin_pw})
    hdr = {"Authorization": "Bearer " + tok["token"]}
    st, users = http("GET", ADMIN + "/admin/v1/users", None, hdr)
    print("admin users:", st, str(users)[:400])
    for s in (s1, s2):
        s.close()


if __name__ == "__main__":
    main()
