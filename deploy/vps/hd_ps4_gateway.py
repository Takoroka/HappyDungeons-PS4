"""Happy Dungeons PS4 gateway: lets the PS4 build (CUSA09120 v1.11 in shadPS4) use the Happy Dungeons server.

The PS4 build talks PSN through shadNet and Toylogic's Vulcan HTTP API. This gateway sits next to the Dungeons
server (experimentalserver.py) and a shadNet server, and adds, without changing either of them:

  1. shadNet proxy (TCP): the game's shadNet connection goes through here. Everything passes through unchanged,
     except the cloud-save (TUS) calls of a LINKED account for Happy Dungeons (comm id NPWR12946_00): those are
     answered from the player's Dungeons saves, through the Dungeons server's own title-storage route with the
     player's key, so quota, locking and ownership checks stay the server's.
        TUS slot 1   <-> data/<xuid>/slot9999/savedata/dg_new_index.txt,binary   (save index)
        TUS slot 2-5 <-> data/<xuid>/slot0-3/savedata/dg_game_data.txt,binary    (the four rotating saves)
     Same bytes on both builds (base64(zlib(json)) + NUL), except the save version: the PS4 build (save version
     20171130) refuses newer saves, so a newer one is labelled 20171130 on the way to the PS4 and gets its version
     back when the PS4 writes it (the PS4 keeps every field of a 1.15 save except one 1.15 progress flag).
  2. HTTP (behind Caddy on the VPS): Vulcan delivery list /v12/api + files (the server's live serverdata/manifest
     files, the rest from the Xbox client ServerData set; event packs restamped v1.15 -> v1.13 for this build),
     a telemetry sink, the legal page, and the account link API:
        POST /ps4/link     {"xuidBase", "secret", "npid"?, "password"?}  -> {"ok", "npid", "password" | "ownPassword"}
        POST /ps4/register {"gamertag", "npid"?, "password"?}             -> {"ok", "xuidBase", "secret", "npid", ...}
     With "password" the player picks a password for THIS server only (never their public shadNet password): that
     npid gets it, created if free or changed if it is already the player's account. It is never stored or logged
     here. Players' public shadNet passwords are never collected: a Happy Dungeons login is its own login, kept in a
     second shadPS4 user (one shadPS4 user has ONE login for all games).
        POST /ps4/whoami   {"xuidBase", "secret"} -> {"ok", "linked", "npid"?, ...}  (the players page; makes nothing)
        GET  /ps4/account  a page doing the same in the browser (any OS): shows the shadNet login to enter in shadPS4
     A link creates (or reuses) a shadNet account with its own random password (never the player's Dungeons key).
  3. Friends: when a linked player signs in, their Dungeons friends who are linked too become shadNet friends
     (shadNet admin API), so the PS4 friends list matches.

Run: python3 hd_ps4_gateway.py gateway.json   (stdlib only)
"""
import base64
import hashlib
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------------------------------- settings
CFG = {
    "shadnetListen": "127.0.0.1:31313",     # what the game connects to (the VPS: 0.0.0.0:31313)
    "shadnetUpstream": "127.0.0.1:31413",   # the real shadNet game port
    "shadnetAdmin": "http://127.0.0.1:31350",
    "shadnetAdminNpid": "",                 # an account listed in shadnet.cfg AdminsList (friends sync)
    "shadnetAdminPassword": "",
    "shadnetRegistrationKey": "",           # shadnet.cfg RegistrationSecretKey: only the gateway creates accounts
    "httpListen": "127.0.0.1:5101",
    "publicBase": "http://127.0.0.1:5101",  # how the game reaches httpListen (Vulcan file URLs)
    "playerShadnet": "",                    # shown on /ps4/account: shadPS4's Server / WebAPI Server settings
    "playerWebApi": "",                     # (empty: publicBase's host with the shadnetListen port, and port + 2)
    "dungeonsApi": "http://127.0.0.1:5000",
    "baseServerData": "",                   # the Xbox client ServerData set (images the server does not publish)
    "stateDir": "",                         # the gateway's own files (links.json, gateway.log)
    "comId": "NPWR12946_00",
    "overrides": {},                        # delivery file name -> file served instead (e.g. no service-end art)
}
TITLE_GROUP = "be554af1-2afa-4168-b64f-bdcb9ce7ac7b"
CONTAINER = "hd201607"
PS4_SAVE_VERSION = 20171130
SLOT_FILES = {
    1: ("slot9999/savedata/dg_new_index.txt,binary", b"dg_new_index.txt\0"),
    2: ("slot0/savedata/dg_game_data.txt,binary", b"dg_game_data.txt\0"),
    3: ("slot1/savedata/dg_game_data.txt,binary", b"dg_game_data.txt\0"),
    4: ("slot2/savedata/dg_game_data.txt,binary", b"dg_game_data.txt\0"),
    5: ("slot3/savedata/dg_game_data.txt,binary", b"dg_game_data.txt\0"),
}
# shadNet protocol v1
HEADER = struct.Struct("<BHIQ")   # type, command, size (incl. header), packet id
REQUEST, REPLY = 0, 1
CMD_LOGIN, CMD_CREATE = 0, 2
CMD_TUS_SET, CMD_TUS_GET, CMD_TUS_STATUS, CMD_TUS_DELETE = 201, 202, 206, 209
ERR_OK, ERR_MALFORMED, ERR_DBFAIL = 0, 1, 24
# Vulcan: the 44 delivery labels and their file names (PS4 eboot table, same as the Xbox build)
LABELS = ("DungeonPatch JsonDiff Permission Presentation ShopCatalog HappyCard EventExtra EventCampaign "
          "EventTreasure EventOrder EventBonus Dictionary").split() + [
    "Pic_Top01", "Pic_Top02", "Pic_Loading01", "Pic_Loading02", "Pic_Loading03", "Pic_Loading04", "Pic_eventEX",
    "Pic_eventCP"] + ["Pic_GameHome%02d" % i for i in range(1, 9)] + ["Pic_CardMain%02d" % i for i in range(1, 8)] + [
    "Pic_CardPack%02d" % i for i in range(1, 8)] + ["Pic_imageEX01", "Pic_ruleCP"]
FILE_NAMES = ["DungeonPatch.pak", "JsonDiff.pak", "permission.pak", "presentation.pak", "shop_catalog.bin",
              "happy_card.pak", "EventExtra.pak", "EventCampaign.pak", "event_gacha.pak", "event_order.pak",
              "event_bonus.pak", "Dictionary.pak"] + [l + ".jpg" for l in LABELS[12:35]] + [
    l + ".png" for l in LABELS[35:42]] + ["Pic_imageEX01.jpg", "Pic_ruleCP.jpg"]
NOT_DELIVERED = {"DungeonPatch.pak", "JsonDiff.pak"}   # CU0014 balance patches for a newer build: never to the PS4
PS4_EVENT_VERSION = 1300000                              # KPK stamp range the build accepts: 1300000..1399999

LOG_LOCK = threading.Lock()
LINKS_LOCK = threading.Lock()


def log(line):
    text = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), line)
    with LOG_LOCK:
        print(text, flush=True)
        try:
            with open(os.path.join(CFG["stateDir"], "gateway.log"), "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass


def hostport(text):
    host, _, port = text.rpartition(":")
    return host, int(port)


# ---------------------------------------------------------------------------------------------------- protobuf
def pb_varint(n):
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_field(num, value):
    """bytes/str -> length-delimited, bool/int -> varint; None and empty/zero values are left out (proto3)."""
    if value is None:
        return b""
    if isinstance(value, bool):
        return pb_varint(num << 3) + pb_varint(1) if value else b""
    if isinstance(value, int):
        return pb_varint(num << 3) + pb_varint(value) if value else b""
    if isinstance(value, str):
        value = value.encode("utf-8")
    return pb_varint((num << 3) | 2) + pb_varint(len(value)) + bytes(value) if value else b""


def pb_read_varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def pb_parse(buf):
    """field number -> list of raw values (int for varints, bytes for length-delimited)."""
    out, i = {}, 0
    while i < len(buf):
        key, i = pb_read_varint(buf, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            val, i = pb_read_varint(buf, i)
        elif wt == 2:
            n, i = pb_read_varint(buf, i)
            val, i = bytes(buf[i:i + n]), i + n
        elif wt == 1:
            val, i = struct.unpack_from("<Q", buf, i)[0], i + 8
        elif wt == 5:
            val, i = struct.unpack_from("<I", buf, i)[0], i + 4
        else:
            raise ValueError("wire type %d" % wt)
        out.setdefault(num, []).append(val)
    return out


def pb_int(fields, num, default=0, signed=True):
    v = fields.get(num, [default])[-1]
    if isinstance(v, bytes):
        return default
    return v - (1 << 64) if signed and v >= 1 << 63 else v


def pb_str(fields, num):
    v = fields.get(num, [b""])[-1]
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""


def pb_ints(fields, num):
    """A repeated int32 field, packed or not."""
    out = []
    for v in fields.get(num, []):
        if isinstance(v, bytes):
            i = 0
            while i < len(v):
                x, i = pb_read_varint(v, i)
                out.append(x)
        else:
            out.append(v)
    return out


def blob(proto):
    return struct.pack("<I", len(proto)) + proto


def take_blob(body, offset):
    n = struct.unpack_from("<I", body, offset)[0]
    return body[offset + 4:offset + 4 + n]


# ---------------------------------------------------------------------------------------------------- links
def links_path():
    return os.path.join(CFG["stateDir"], "links.json")


def load_links():
    try:
        with open(links_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_links(links):
    tmp = links_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(links, f, indent=1, sort_keys=True)
    os.replace(tmp, links_path())


def link_for(npid):
    with LINKS_LOCK:
        return load_links().get((npid or "").lower())


# ---------------------------------------------------------------------------------------------------- Dungeons API
def dungeons(method, path, link=None, body=None, content_type="application/octet-stream", player_address=None):
    """(status, bytes) from the Dungeons server; with a link, as that player (their own key). player_address: the
    player's own IP, passed on as X-Forwarded-For like Caddy does (the server's per-address limits, e.g. new players
    per hour, then count that player and not this gateway's 127.0.0.1)."""
    req = urllib.request.Request(CFG["dungeonsApi"] + path, data=body, method=method)
    if link:
        req.add_header("Authorization", "XBL3.0 x=1;WD1.%s.%s" % (link["base"], link["secret"]))
    if player_address:
        req.add_header("X-Forwarded-For", player_address)
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def save_url(link, slot):
    xuid = int(link["base"]) + 1   # the game plays as the block's local user 1
    return "/users/xuid(%d)/storage/titlestorage/titlegroups/%s/data/%s/%s" % (
        xuid, TITLE_GROUP, CONTAINER, urllib.parse.quote(SLOT_FILES[slot][0], safe="/,"))


VERSION_RE = re.compile(rb'("dungeon_SaveData"\s*:\s*\{\s*"version"\s*:\s*)(\d+)')


def recode_version(data, new_version=None):
    """(bytes, version found) - with new_version, the save's version replaced (re-encoded)."""
    try:
        text = zlib.decompress(base64.b64decode(data.rstrip(b"\0")))
    except (ValueError, zlib.error):
        return data, None
    match = VERSION_RE.search(text)
    if not match:
        return data, None
    found = int(match.group(2))
    if new_version is None or new_version == found:
        return data, found
    text = text[:match.start(2)] + str(new_version).encode() + text[match.end(2):]
    return base64.b64encode(zlib.compress(text, 9)) + b"\0", found


def rtc_tick(seconds=None):
    """SceRtcTick: microseconds since 0001-01-01 (what shadNet stores as lastChangedDate)."""
    return int(((seconds if seconds is not None else time.time()) + 62135596800) * 1_000_000)


# ---------------------------------------------------------------------------------------------------- TUS
class Session:
    def __init__(self, peer):
        self.peer = peer
        self.npid = None
        self.account_id = 0
        self.link = None
        self.save_version = None   # the newest save version this session read from the server


def data_status(session, slot, data):
    fields = [pb_field(1, session.npid), pb_field(2, data is not None)]
    if data is not None:
        fields += [pb_field(3, rtc_tick()), pb_field(4, session.npid), pb_field(5, len(data)),
                   pb_field(6, SLOT_FILES[slot][1])]
    fields += [pb_field(7, session.account_id), pb_field(8, session.account_id if data is not None else 0)]
    return b"".join(fields)


def read_slot(session, slot):
    """The slot's bytes as the PS4 must see them, or None when it holds nothing."""
    status, body = dungeons("GET", save_url(session.link, slot), session.link)
    if status == 404:
        return None
    if status != 200:
        raise IOError("Dungeons server answered %d for slot %d" % (status, slot))
    if slot >= 2:
        _, found = recode_version(body)
        if found and found > PS4_SAVE_VERSION:
            session.save_version = max(session.save_version or 0, found)
            body, _ = recode_version(body, PS4_SAVE_VERSION)
    return body


def is_self(session, fields, npid_field, account_field, vuser_field):
    owner = pb_str(fields, npid_field)
    account = pb_int(fields, account_field)
    return not pb_str(fields, vuser_field) and (account in (0, session.account_id)) and owner in ("", session.npid)


def handle_tus(session, cmd, body):
    """(error, reply body) for a linked player's Happy Dungeons TUS call, or None to pass it to shadNet."""
    if session.link is None or len(body) < 16 or body[:12].rstrip(b"\0").decode("ascii", "replace") != CFG["comId"]:
        return None
    try:
        fields = pb_parse(take_blob(body, 12))
    except (ValueError, IndexError, struct.error):
        return ERR_MALFORMED, b""
    if cmd == CMD_TUS_GET:
        slot = pb_int(fields, 2)
        if slot not in SLOT_FILES or not is_self(session, fields, 1, 3, 4):
            return None
        data = read_slot(session, slot)
        resp = pb_field(1, data_status(session, slot, data)) + pb_field(2, data or b"")
        log("TUS GET  %s slot %d -> %s" % (session.npid, slot, "%d bytes" % len(data) if data else "nothing"))
        return ERR_OK, blob(resp)
    if cmd == CMD_TUS_SET:
        slot = pb_int(fields, 2)
        if slot not in SLOT_FILES or not is_self(session, fields, 1, 5, 6):
            return None
        data = fields.get(3, [b""])[-1]
        note = ""
        if slot >= 2 and session.save_version:
            data, found = recode_version(data, session.save_version)
            note = " (version %s -> %s)" % (found, session.save_version)
        status, _ = dungeons("PUT", save_url(session.link, slot), session.link, data)
        log("TUS SET  %s slot %d %d bytes%s -> %d" % (session.npid, slot, len(data), note, status))
        return (ERR_OK if status in (200, 201) else ERR_DBFAIL), b""
    if cmd == CMD_TUS_STATUS:
        slots = pb_ints(fields, 2)
        if not is_self(session, fields, 1, 3, 4) or any(s not in SLOT_FILES for s in slots):
            return None
        statuses = b"".join(pb_field(1, data_status(session, s, read_slot(session, s))) for s in slots)
        log("TUS STAT %s slots %s" % (session.npid, slots))
        return ERR_OK, blob(statuses + pb_field(2, len(slots)))
    if cmd == CMD_TUS_DELETE:
        slots = pb_ints(fields, 2)
        if not is_self(session, fields, 1, 3, 4):
            return None
        # The Dungeons server keeps saves (and their daily backups); it has no delete. The game writes a new save
        # over the slots right after, so nothing is lost by keeping them.
        log("TUS DEL  %s slots %s (kept on the server)" % (session.npid, slots))
        return ERR_OK, b""
    return None


# ---------------------------------------------------------------------------------------------------- shadNet proxy
def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def read_packet(sock):
    head = recv_exact(sock, HEADER.size)
    ptype, cmd, size, pid = HEADER.unpack(head)
    if size < HEADER.size or size > 0x800000:
        raise ConnectionError("bad packet size %d" % size)
    return ptype, cmd, pid, head + recv_exact(sock, size - HEADER.size)


def proxy_client(client, peer):
    session = Session(peer)
    upstream = socket.create_connection(hostport(CFG["shadnetUpstream"]), timeout=15)
    upstream.settimeout(None)
    client.settimeout(None)
    write_lock = threading.Lock()

    def to_client(packet):
        with write_lock:
            client.sendall(packet)

    def server_to_client():
        try:
            while True:
                ptype, cmd, pid, packet = read_packet(upstream)
                if ptype == REPLY and cmd == CMD_LOGIN and len(packet) > HEADER.size + 5 \
                        and packet[HEADER.size] == ERR_OK:
                    session.account_id = pb_int(pb_parse(take_blob(packet, HEADER.size + 1)), 2)
                    log("LOGIN %s from %s: account %d, %s" % (session.npid, peer, session.account_id,
                                                               "linked to %s" % session.link["base"]
                                                               if session.link else "not linked"))
                    if session.link:
                        note_login(session.npid, session.account_id)
                        threading.Thread(target=sync_friends, args=(session.npid, session.link, session.account_id),
                                         daemon=True).start()
                to_client(packet)
        except (ConnectionError, OSError):
            pass
        finally:
            for s in (client, upstream):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    threading.Thread(target=server_to_client, daemon=True).start()
    try:
        while True:
            ptype, cmd, pid, packet = read_packet(client)
            body = packet[HEADER.size:]
            if ptype == REQUEST and cmd == CMD_LOGIN:
                try:   # only the Account ID: the password in a Login is never read, logged or kept here
                    session.npid = pb_str(pb_parse(take_blob(body, 0)), 1)
                    session.link = link_for(session.npid)
                except (ValueError, IndexError, struct.error):
                    pass
            elif ptype == REQUEST and cmd in (CMD_TUS_SET, CMD_TUS_GET, CMD_TUS_STATUS, CMD_TUS_DELETE):
                try:
                    answer = handle_tus(session, cmd, body)
                except Exception as error:   # never pass a linked player's save call to the wrong store
                    log("TUS %d failed for %s: %s" % (cmd, session.npid, error))
                    answer = (ERR_DBFAIL, b"") if session.link else None
                if answer is not None:
                    err, reply = answer
                    to_client(HEADER.pack(REPLY, cmd, HEADER.size + 1 + len(reply), pid) + bytes([err]) + reply)
                    continue
            upstream.sendall(packet)
    except (ConnectionError, OSError):
        pass
    finally:
        # shutdown() first: on Linux, close() alone does not end a TCP connection that the other thread is still
        # blocked reading (server_to_client), so shadNet never saw the player leave and refused their next sign-in
        # with "already logged in" (error 6). shutdown sends the FIN and wakes that reader.
        for s in (client, upstream):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for s in (client, upstream):
            try:
                s.close()
            except OSError:
                pass


def run_proxy():
    srv = socket.create_server(hostport(CFG["shadnetListen"]), reuse_port=False)
    log("shadNet proxy on %s -> %s" % (CFG["shadnetListen"], CFG["shadnetUpstream"]))
    while True:
        client, addr = srv.accept()
        threading.Thread(target=proxy_client, args=(client, "%s:%d" % addr), daemon=True).start()


def shadnet_create(npid, password, email):
    """shadNet Create (protocol command 2) straight to the real server: the reply's error code."""
    with socket.create_connection(hostport(CFG["shadnetUpstream"]), timeout=15) as s:
        read_packet(s)   # ServerInfo
        req = blob(pb_field(1, npid) + pb_field(2, password) + pb_field(4, email)
                   + pb_field(5, CFG["shadnetRegistrationKey"] or None))
        s.sendall(HEADER.pack(REQUEST, CMD_CREATE, HEADER.size + len(req), 1) + req)
        while True:
            ptype, cmd, pid, packet = read_packet(s)
            if ptype == REPLY and cmd == CMD_CREATE:
                return packet[HEADER.size] if len(packet) > HEADER.size else -1


# ---------------------------------------------------------------------------------------------------- friends
ADMIN = {"token": None, "until": 0}


def admin_call(method, path, body=None):
    if not CFG["shadnetAdminNpid"]:
        return None, None
    if not ADMIN["token"] or time.time() > ADMIN["until"]:
        status, data = http_json("POST", CFG["shadnetAdmin"] + "/admin/v1/login",
                                 {"npid": CFG["shadnetAdminNpid"], "password": CFG["shadnetAdminPassword"]})
        if status != 200:
            log("shadNet admin login failed: %s %s" % (status, data))
            return status, data
        ADMIN.update(token=data.get("token"), until=time.time() + max(60, int(data.get("expiresInSeconds", 600)) - 60))
    return http_json(method, CFG["shadnetAdmin"] + path, body, {"Authorization": "Bearer " + ADMIN["token"]})


def http_json(method, url, body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw[:200]
    except (OSError, ValueError) as error:
        return None, str(error)


def dungeons_friends(link):
    """The player's own Dungeons friends list (their xuids), through the server's people route with their key."""
    xuid = int(link["base"]) + 1
    status, raw = dungeons("GET", "/users/xuid(%d)/people?maxItems=1000" % xuid, link)
    try:
        return [str(p["xuid"]) for p in json.loads(raw).get("people", [])] if status == 200 else []
    except (ValueError, AttributeError, KeyError, TypeError):
        return []


def sync_friends(npid, link, account_id):
    """Dungeons friends who are linked too -> shadNet friends (adds only; never removes)."""
    if not CFG["shadnetAdminNpid"]:
        return
    with LINKS_LOCK:
        links = load_links()
    by_xuid = {str(int(v["base"]) + 1): k for k, v in links.items()}
    wanted = [links[by_xuid[x]]["npid"] for x in dungeons_friends(link) if x in by_xuid]
    if not wanted:
        return
    status, current = admin_call("GET", "/admin/v1/users/%d/friends" % account_id)
    have = json.dumps(current).lower() if status == 200 else ""
    for friend in wanted:
        if '"%s"' % friend.lower() in have:
            continue
        status, answer = admin_call("POST", "/admin/v1/users/%d/friends" % account_id, {"npid": friend})
        log("FRIENDS %s + %s -> %s %s" % (npid, friend, status, answer))


# ---------------------------------------------------------------------------------------------------- Vulcan
def kpk_for_ps4(data):
    """An event pack restamped into the PS4 build's version range (KPK~, version u32 at byte 8)."""
    if len(data) >= 12 and data[:3] == b"KPK" and data[3:4] == b"~":
        version = struct.unpack_from("<I", data, 8)[0]
        if not PS4_EVENT_VERSION <= version < PS4_EVENT_VERSION + 100000:
            data = data[:8] + struct.pack("<I", PS4_EVENT_VERSION + version % 1000) + data[12:]
    return data


ACCOUNT_HASHES = {}   # upper-hex sha256(str(shadNet account id)) (the game's x-id-hash header) -> npid
LAST_LINKED = {"npid": None}
LIVE_CACHE = {"rev": None, "files": {}, "at": 0}   # the Dungeons server's ServerData, as its API serves it
LIVE_LOCK = threading.Lock()


def note_login(npid, account_id):
    ACCOUNT_HASHES[hashlib.sha256(str(account_id).encode()).hexdigest().upper()] = npid
    LAST_LINKED["npid"] = npid


def live_serverdata(link):
    """{file name: bytes} of the Dungeons server's current ServerData (the same manifest + files the Xbox/PC
    client downloads), fetched as this linked player; cached until the manifest's rev changes."""
    with LIVE_LOCK:
        if link is None:
            return LIVE_CACHE["rev"] or 0, dict(LIVE_CACHE["files"])
        if time.time() - LIVE_CACHE["at"] < 30 and LIVE_CACHE["rev"] is not None:
            return LIVE_CACHE["rev"], dict(LIVE_CACHE["files"])
        status, raw = dungeons("GET", "/windurango/serverdata/manifest", link)
        try:
            manifest = json.loads(raw) if status == 200 else None
        except ValueError:
            manifest = None
        if not manifest:
            log("ServerData manifest: %s" % status)
            return LIVE_CACHE["rev"] or 0, dict(LIVE_CACHE["files"])
        if manifest.get("rev") != LIVE_CACHE["rev"]:
            files = {}
            for entry in manifest.get("files", []):   # {"name", "sha256", "size"} (a plain name in older servers)
                name = entry.get("name") if isinstance(entry, dict) else entry
                want = entry.get("sha256") if isinstance(entry, dict) else None
                if not isinstance(name, str) or not name:
                    continue
                st, data = dungeons("GET", "/windurango/serverdata/files/%s" % urllib.parse.quote(name), link)
                if st != 200:
                    log("ServerData %s: %s" % (name, st))
                elif want and hashlib.sha256(data).hexdigest() != want:
                    log("ServerData %s: sha256 does not match the manifest, left out" % name)
                else:
                    files[name] = data
            LIVE_CACHE.update(rev=manifest.get("rev"), files=files)
            log("ServerData rev %s from the Dungeons server: %s" % (manifest.get("rev"), ", ".join(sorted(files))))
        LIVE_CACHE["at"] = time.time()
        return LIVE_CACHE["rev"], dict(LIVE_CACHE["files"])


def delivery_set(link=None):
    """[(label, file name, bytes)] - an override first, then the Dungeons server's ServerData (what the Xbox/PC
    client gets), then the base set (the Xbox client's ServerData folder) for the slots the server does not fill."""
    try:
        rev, live = live_serverdata(link)
    except Exception as error:   # the server unreachable or odd: last good set (or none), never a failed reply
        log("ServerData fetch failed: %s" % error)
        rev, live = LIVE_CACHE["rev"] or 0, dict(LIVE_CACHE["files"])
    live_lower = {name.lower(): data for name, data in live.items()}
    out = []
    for label, name in zip(LABELS, FILE_NAMES):
        if name in NOT_DELIVERED:
            continue
        data = None
        if CFG["overrides"].get(name) and os.path.isfile(CFG["overrides"][name]):
            with open(CFG["overrides"][name], "rb") as f:
                data = f.read()
        elif name.lower() in live_lower:
            data = live_lower[name.lower()]
        elif CFG["baseServerData"] and os.path.isfile(os.path.join(CFG["baseServerData"], name)):
            with open(os.path.join(CFG["baseServerData"], name), "rb") as f:
                data = f.read()
        if data is not None:
            out.append((label, name, kpk_for_ps4(data)))
    return out, int(rev or 0)


def vulcan_reply(link=None):
    files, rev = delivery_set(link)
    entries = {}
    for i, (label, name, data) in enumerate(files, 1):
        digest = hashlib.sha256(data).hexdigest()
        url = "%s/vulcan/files/%s?h=%s" % (CFG["publicBase"], name, digest[:16])
        entries[str(i)] = {"AssetName": "hd_ps4_rev%d" % rev, "Label": label, "URL": urllib.parse.quote(url, safe=""),
                           "FileName": name, "FileSize": len(data), "Memo": "", "FileHash": digest, "Addition": "",
                           "Id": 100000 + rev * 100 + i}
    reply = {"Format": {"Name": "Vulcan", "Version": 12},
             "Log": {"Code": hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()},
             "List": {"Name": "hd_ps4_rev%d" % rev, "Valid": 4102444800, "Memo": "Happy Dungeons server", "Id": rev}}
    if entries:
        reply["File"] = entries
    return reply, files


# ---------------------------------------------------------------------------------------------------- HTTP
NPID_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,15}$")


def secrets_token():
    import secrets
    return secrets.token_urlsafe(18)


def npid_from(gamertag):
    base = re.sub(r"[^A-Za-z0-9_-]", "", gamertag or "")
    if not base or not base[0].isalpha():
        base = "HD" + base
    return (base + "___")[:max(3, min(16, len(base)))] if len(base) < 3 else base[:16]


# A password the player picks for this server: shadNet's admin API (used to change one) refuses fewer than 8.
OWN_PASSWORD_MIN, OWN_PASSWORD_MAX = 8, 128


def shadnet_account(npid):
    """(admin API status, [the shadNet account row named npid, any case] or [])."""
    status, found = admin_call("GET", "/admin/v1/users?" + urllib.parse.urlencode({"search": npid, "limit": 500}))
    users = found.get("users", []) if status == 200 and isinstance(found, dict) else []
    return status, [u for u in users if str(u.get("npid", "")).lower() == npid.lower()]


def own_login_problem(npid, password):
    """Before a NEW player is made with a login they picked (for this server only): why it cannot work, or None."""
    if not npid or not NPID_OK.match(npid):
        return "give the Account ID (NPID) that goes with your password: 3-16 letters, digits, - or _, starting " \
               "with a letter"
    if not OWN_PASSWORD_MIN <= len(password) <= OWN_PASSWORD_MAX:
        return "passwords on this server are %d-%d characters" % (OWN_PASSWORD_MIN, OWN_PASSWORD_MAX)
    with LINKS_LOCK:
        linked = npid.lower() in load_links()
    if linked or npid.lower() == (CFG["shadnetAdminNpid"] or "").lower():
        return "the Account ID %s is taken on this server" % npid
    status, rows = shadnet_account(npid)
    if status != 200:
        return "cannot check the Account ID right now (shadNet admin API %s)" % status
    if rows:
        return "the Account ID %s is taken on this server" % npid
    return None


def shadnet_set_password(npid, password):
    """Give an existing shadNet account a new password (admin API; it also signs that account out): None or why not.
    The password is only sent to shadNet, never logged or stored here."""
    status, match = shadnet_account(npid)
    if not match:
        return "shadNet has no account %s (admin API %s)" % (npid, status)
    status, answer = admin_call("POST", "/admin/v1/users/%d/password" % int(match[0]["userId"]), {"password": password})
    if status != 200:
        error = answer.get("error", answer) if isinstance(answer, dict) else answer
        reason = (error.get("message") or error.get("code")) if isinstance(error, dict) else error
        return "shadNet did not change the password (%s %s)" % (status, reason)
    return None


def link_answer(link, base, who):
    answer = {"ok": True, "npid": link["npid"], "xuidBase": str(base), "gamertag": who.get("gamertag")}
    if link.get("ownPassword"):
        answer["ownPassword"] = True     # the player chose it: the gateway does not know it
    else:
        answer["password"] = link.get("password", link.get("secret"))   # (the oldest links used the key itself)
    return answer


def keep_link(links, base, name, record, who, note=""):
    """Store base <-> name as the player's ONE link (an earlier link of the same player is dropped from links.json;
    its shadNet account stays as it is, unlinked)."""
    for key in [k for k, v in links.items() if v["base"] == str(base) and k != name.lower()]:
        log("UNLINK %s (xuidBase %s moved to %s)" % (links[key]["npid"], base, name))
        del links[key]
    links[name.lower()] = dict(record, npid=name, base=str(base),
                               linked=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    save_links(links)
    log("LINK %s <-> xuidBase %s (%s)%s" % (name, base, who.get("gamertag"), note))


def verified_player(base, secret):
    """The Dungeons server's whoami for a player key, or None if the key is not theirs."""
    status, raw = dungeons("GET", "/windurango/identity/whoami", {"base": base, "secret": secret})
    try:
        who = json.loads(raw)
    except ValueError:
        return None
    if status != 200 or who.get("verified") is not True or str(who.get("xuidBase")) != str(base):
        return None
    return who


def link_status(base, secret):
    """For the players page: this player's PS4 login, if any (makes nothing): (http status, answer)."""
    who = verified_player(base, secret)
    if who is None:
        return 403, {"ok": False, "reason": "that is not a Happy Dungeons player key"}
    with LINKS_LOCK:
        links = load_links()
    mine = sorted((v for v in links.values() if v["base"] == str(base)), key=lambda v: v.get("linked", ""),
                  reverse=True)
    if not mine:
        return 200, {"ok": True, "linked": False, "gamertag": who.get("gamertag")}
    return 200, dict(link_answer(mine[0], base, who), linked=True)


def do_link(base, secret, wanted=None, own_password=None):
    """Link a verified Dungeons identity to a shadNet account: (http status, answer).

    Normally the account gets a random password made here (returned to the player, kept in links.json). With
    own_password the player picked a password for THIS server only: the account `wanted` gets it if the name is
    free (created) or already this player's link (changed). Never stored or logged by the gateway. An account that
    exists but is linked to nobody is never handed over."""
    if own_password is not None:
        if not wanted or not NPID_OK.match(wanted):
            return 400, {"ok": False, "reason": "give the Account ID (NPID) that goes with your password: 3-16 "
                                                "letters, digits, - or _, starting with a letter"}
        if not OWN_PASSWORD_MIN <= len(own_password) <= OWN_PASSWORD_MAX:
            return 400, {"ok": False, "reason": "passwords on this server are %d-%d characters"
                                                % (OWN_PASSWORD_MIN, OWN_PASSWORD_MAX)}
    who = verified_player(base, secret)
    if who is None:
        return 403, {"ok": False, "reason": "that is not a Happy Dungeons player key"}
    with LINKS_LOCK:
        links = load_links()
        mine = sorted((k for k, v in links.items() if v["base"] == str(base)),
                      key=lambda k: links[k].get("linked", ""), reverse=True)
        if own_password is not None:
            existing = links.get(wanted.lower())
            if existing and existing["base"] != str(base):
                return 409, {"ok": False, "reason": "the Account ID %s is taken on this server by another player"
                                                    % wanted}
            if existing:
                name = existing["npid"]
                why = shadnet_set_password(name, own_password)
                if why:   # e.g. shadNet's own rule: a new password needs 8+ characters
                    return (400 if "(400 " in why else 502), {"ok": False, "reason": why}
            else:
                name = wanted
                if name.lower() == (CFG["shadnetAdminNpid"] or "").lower():
                    return 409, {"ok": False, "reason": "the Account ID %s is taken on this server" % wanted}
                code = shadnet_create(name, own_password, "%s.%s@ps4.happydungeons.local" % (base, name.lower()))
                if code == 11:
                    return 409, {"ok": False, "reason": "the Account ID %s is taken on this server" % wanted}
                if code != 0:
                    return 502, {"ok": False, "reason": "shadNet refused the account (code %s)" % code}
            keep_link(links, base, name, {"secret": secret, "ownPassword": True}, who, ", password the player picked")
            return 200, link_answer(links[name.lower()], base, who)
        if mine and (not wanted or wanted.lower() in mine):
            return 200, link_answer(links[mine[0] if not wanted else wanted.lower()], base, who)
        npid = wanted or npid_from(who.get("gamertag"))
        if not NPID_OK.match(npid):
            return 400, {"ok": False, "reason": "online ids are 3-16 letters, digits, - or _, starting with a letter"}
        candidates = [npid] + ["%s%d" % (npid[:14], i) for i in range(2, 100)]
        for name in candidates:
            if name.lower() in links:
                continue
            # The shadNet game protocol is not encrypted, so the account gets its own random password: the
            # Dungeons key (which also signs the player in on PC) never travels over it.
            password = secrets_token()
            code = shadnet_create(name, password, "%s.%s@ps4.happydungeons.local" % (base, name.lower()))
            if code == 0:
                keep_link(links, base, name, {"secret": secret, "password": password}, who)
                return 200, link_answer(links[name.lower()], base, who)
            if code != 11:
                return 502, {"ok": False, "reason": "shadNet refused the account (code %s)" % code}
        return 409, {"ok": False, "reason": "no free online id - pick another with \"npid\""}


# ---------------------------------------------------------------------------------------------------- account page
# GET /ps4/account: the same link/register calls as setup.ps1 / happydungeons_account.py, for any OS with a browser.
# Key files are read in the browser; only xuidBase + secret are sent (to /ps4/link, as the scripts do).
ACCOUNT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Happy Dungeons PS4 sign-in</title>
<style>
:root{--bg:#f6f4ef;--fg:#1d1c1a;--muted:#5f5b53;--card:#fff;--line:#d9d4c9;--accent:#2f6f4f;--warn:#8a4b00}
@media (prefers-color-scheme:dark){:root{--bg:#171614;--fg:#ece8df;--muted:#a49e92;--card:#22201d;--line:#3a3732;
--accent:#7cc39c;--warn:#f0b35a}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:46rem;margin:0 auto;padding:1.5rem 1rem 3rem}
h1{font-size:1.6rem;margin:0 0 .25rem}h2{font-size:1.15rem;margin:0 0 .75rem}h3{font-size:1rem;margin:1.25rem 0 .25rem}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem 1.1rem;margin-top:1rem}
label.who{display:block;margin:.35rem 0}small,.muted{color:var(--muted)}
.field{margin:.75rem 0 0 1.6rem}.field input[type=text]{font:inherit;padding:.35rem .5rem;width:14rem;max-width:100%}
button{font:inherit;padding:.4rem .9rem;border-radius:6px;border:1px solid var(--line);background:var(--bg);
color:var(--fg);cursor:pointer}button.main{background:var(--accent);border-color:var(--accent);color:var(--card)}
#go{margin-top:1rem}#err{color:var(--warn);min-height:1.5em;margin:.5rem 0 0}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.5rem 1rem;align-items:center;margin:0}
dt{color:var(--muted)}dd{margin:0;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
code{font:15px ui-monospace,Consolas,monospace;background:var(--bg);border:1px solid var(--line);border-radius:4px;
padding:.1rem .35rem;overflow-wrap:anywhere}.warn{color:var(--warn)}li{margin:.35rem 0}[hidden]{display:none!important}
</style></head>
<body><main>
<h1>Happy Dungeons on shadPS4</h1>
<p class="muted">Get your shadNet login for the Happy Dungeons friends server. You enter it in shadPS4 once.</p>
<section id="pick">
<h2>Who are you?</h2>
<label class="who"><input type="radio" name="who" value="new" checked> I am new</label>
<label class="who"><input type="radio" name="who" value="pc"> I play Happy Dungeons on PC (Xbox / WinDurango)</label>
<label class="who"><input type="radio" name="who" value="key"> I have a happydungeons_identity.json from before</label>
<div class="field" data-for="new"><label>Player name <input type="text" id="name" maxlength="15" autocomplete="off"></label><br>
<small>1-15 letters, digits or spaces, starting with a letter. Each click makes a NEW player, so do it once.</small></div>
<div class="field" data-for="pc" hidden><label>winDurango.json <input type="file" id="wd" accept=".json"></label><br>
<small>From your PC game: Mount\\UserData\\LocalState\\WinDurango\\winDurango.json. Same player, character, friends and
saves on both.</small></div>
<div class="field" data-for="key" hidden><label>happydungeons_identity.json <input type="file" id="idf" accept=".json">
</label></div>
<h2 style="margin-top:1.25rem">Your Happy Dungeons login</h2>
<p class="muted" style="margin:0 0 .5rem">Happy Dungeons has its own shadNet login on this server. It is NOT your
normal shadNet login: this server never asks for, and must never get, your public shadNet password.</p>
<label class="who"><input type="radio" name="login" value="made" checked> Make one for me (random password)</label>
<label class="who"><input type="radio" name="login" value="own"> I'll pick the Account ID and a new password for
Happy Dungeons</label>
<div class="field" data-for-login="own" hidden>
<label>Account ID (NPID) <input type="text" id="ownid" maxlength="16" autocomplete="off"></label><br>
<label>New password for Happy Dungeons <input type="password" id="ownpw" maxlength="128" autocomplete="new-password">
</label><br>
<label>Again <input type="password" id="ownpw2" maxlength="128" autocomplete="new-password"></label><br>
<small>3-16 letters, digits, - or _ for the Account ID; at least 8 characters for the password.<br>
<span class="warn">Use a NEW password, only for Happy Dungeons: NOT the one of your public shadNet account or of
anything else.</span> shadPS4 sends it unencrypted when it signs in. It is not kept by this page or the gateway, only
by this server's shadNet, like any password.</small>
</div>
<button class="main" id="go">Get my login</button>
<p id="err" role="alert"></p>
<p id="keyonly" hidden><button class="save">Download happydungeons_identity.json</button></p>
</section>
<section id="done" hidden>
<h2>Your shadNet login</h2>
<dl><dt>Account ID (NPID)</dt><dd><code id="npid"></code><button data-copy="npid">Copy</button></dd>
<dt>Password</dt><dd id="pwrow"><code id="pw"></code><button data-copy="pw">Copy</button></dd>
<dd id="ownrow" hidden>the one you picked (not shown again)</dd></dl>
<p id="keep"><button class="save">Download happydungeons_identity.json</button><br>Keep this file: it is your player's key.
With it you can get this login again, on any PC ("I have a happydungeons_identity.json").</p>
<h3>Put it in its own shadPS4 user</h3>
<p class="muted" style="margin:0">shadPS4 has ONE shadNet login per user, used for every game, so Happy Dungeons gets
a user of its own and your normal user keeps your normal login.</p>
<ol>
<li>Settings &gt; Manage Users &gt; <b>Create User</b> (e.g. "HappyDungeons"). Pick that user, click
<b>ShadNet...</b>, tick <b>Enable ShadNet for this user</b> and enter the Account ID and Password above.</li>
<li>Before you play Happy Dungeons, pick that user and click <b>Set Default User</b> (the default user is player 1).
For your other games, set your normal user as default again.</li>
<li>Happy Dungeons' own settings (right-click the game &gt; Game-specific Settings...), Network tab:
Server <code>__SHADNET__</code>, WebAPI Server <code>__WEBAPI__</code>, and tick Enable shadNet, Set Network Connected
Status as True and Enable UPNP.</li>
</ol>
<p class="muted">Don't use shadNet for anything else? Then you can put the login in your normal user instead. Or keep
a separate, portable shadPS4 just for Happy Dungeons.</p>
<p class="muted">No launcher? Copy the two values into users.json in shadPS4's folder (shadnet_enabled true,
shadnet_npid, shadnet_password), or use happydungeons_account.py from the patch pack.</p>
</section>
</main>
<script>
"use strict";
const $ = id => document.getElementById(id);
let identity = null;
const who = () => document.querySelector("input[name=who]:checked").value;
const login = () => document.querySelector("input[name=login]:checked").value;
document.querySelectorAll("input[name=who]").forEach(r => r.addEventListener("change", () => {
  document.querySelectorAll("[data-for]").forEach(d => { d.hidden = d.dataset.for !== who(); });
  $("err").textContent = "";
}));
document.querySelectorAll("input[name=login]").forEach(r => r.addEventListener("change", () => {
  document.querySelectorAll("[data-for-login]").forEach(d => { d.hidden = d.dataset.forLogin !== login(); });
  $("err").textContent = "";
}));
function ownLogin() {
  if (login() !== "own") return {};
  const npid = $("ownid").value.trim(), password = $("ownpw").value;
  if (!/^[A-Za-z][A-Za-z0-9_-]{2,15}$/.test(npid))
    throw new Error("Account IDs are 3-16 letters, digits, - or _, starting with a letter.");
  if (password.length < 8) throw new Error("Pick a new password of at least 8 characters.");
  if (password !== $("ownpw2").value) throw new Error("The two passwords differ.");
  return {npid: npid, password: password};
}
async function readKey(input) {
  const f = input.files[0];
  if (!f) throw new Error("Choose the file first.");
  let j;
  try { j = JSON.parse((await f.text()).replace(/^\\uFEFF/, "")); } catch (e) { throw new Error("That file is not JSON."); }
  if (!j || !j.xuidBase || !j.secret)
    throw new Error("That file has no player key (xuidBase / secret). Has the PC game signed in to the server yet?");
  return {xuidBase: String(j.xuidBase), secret: String(j.secret)};
}
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
                               body: JSON.stringify(body), cache: "no-store"});
  let a = {};
  try { a = await r.json(); } catch (e) {}
  if (!r.ok || !a.ok) {
    const e = new Error("The server said no: " + (a.reason || a.error || ("HTTP " + r.status)));
    e.answer = a;
    throw e;
  }
  return a;
}
function showKeyOnly(a, message) {
  // a new player was made but got no login: keep their key, they can link it later
  identity = {server: location.origin, gamertag: a.gamertag || "", xuidBase: String(a.xuidBase), secret: String(a.secret)};
  $("err").textContent = message + " Your new player was saved: download its key below and try again later with "
    + "\\"I have a happydungeons_identity.json\\".";
  $("keyonly").hidden = false;
}
$("go").addEventListener("click", async () => {
  $("err").textContent = "";
  $("go").disabled = true;
  try {
    let a, key;
    const own = ownLogin();
    if (who() === "new") {
      const name = $("name").value.trim();
      if (!name) throw new Error("Type a player name.");
      a = await post("register", Object.assign({gamertag: name}, own));
      key = {xuidBase: String(a.xuidBase), secret: String(a.secret)};
    } else {
      key = await readKey(who() === "pc" ? $("wd") : $("idf"));
      a = await post("link", Object.assign({}, key, own));
    }
    $("ownpw").value = "";
    $("ownpw2").value = "";
    identity = {server: location.origin, gamertag: a.gamertag || "", xuidBase: key.xuidBase, secret: key.secret};
    $("npid").textContent = a.npid;
    $("pw").textContent = a.ownPassword ? "" : a.password;
    $("pwrow").hidden = !!a.ownPassword;
    $("ownrow").hidden = !a.ownPassword;
    $("keep").className = who() === "new" ? "warn" : "";
    $("pick").hidden = true;
    $("done").hidden = false;
  } catch (e) {
    if (e.answer && e.answer.xuidBase && e.answer.secret) showKeyOnly(e.answer, e.message);
    else $("err").textContent = e.message;
  } finally {
    $("go").disabled = false;
  }
});
document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", async () => {
  const text = $(b.dataset.copy).textContent;
  try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; }
  catch (e) { getSelection().selectAllChildren($(b.dataset.copy)); b.textContent = "Press Ctrl+C"; }
  setTimeout(() => { b.textContent = "Copy"; }, 2000);
}));
document.querySelectorAll("button.save").forEach(b => b.addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(identity, null, 2) + "\\n"], {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "happydungeons_identity.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}));
</script></body></html>
"""
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
}


def player_servers():
    """(shadNet server, WebAPI server) as players enter them in shadPS4."""
    host = urllib.parse.urlsplit(CFG["publicBase"]).hostname or "127.0.0.1"
    listen_host, port = hostport(CFG["shadnetListen"])
    shadnet = CFG.get("playerShadnet") or "%s:%d" % (host if listen_host in ("0.0.0.0", "::") else listen_host, port)
    webapi = CFG.get("playerWebApi") or "http://%s:%d" % (shadnet.rsplit(":", 1)[0], port + 2)
    return shadnet, webapi


def account_page():
    import html
    shadnet, webapi = player_servers()
    return ACCOUNT_PAGE.replace("__SHADNET__", html.escape(shadnet)).replace("__WEBAPI__", html.escape(webapi))


class Handler(BaseHTTPRequestHandler):
    server_version = "HDPS4Gateway/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":")).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def player_address(self):
        """The player's IP: Caddy (on this machine) puts it in X-Forwarded-For, replacing anything the player sent."""
        peer = self.client_address[0]
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
        if peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1") and forwarded:
            return forwarded
        return peer

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/v12/api":
            # The game's x-id-hash names its shadNet account: fetch the server's ServerData as that player (the
            # first request of a boot can come without it - then as the player who signed in last).
            npid = ACCOUNT_HASHES.get((self.headers.get("x-id-hash") or "").upper()) or LAST_LINKED["npid"]
            link = link_for(npid) if npid else None
            if link is None:   # an unlinked player: the ServerData is the same for everyone, fetch it as any link
                with LINKS_LOCK:
                    any_link = next(iter(load_links().values()), None)
                link = any_link
            reply, files = vulcan_reply(link)
            log("VULCAN for %s: %d file(s), ServerData rev %s" % (npid, len(files), reply["List"]["Id"]))
            return self.send(200, reply)
        if u.path.startswith("/vulcan/files/"):
            name = urllib.parse.unquote(u.path[len("/vulcan/files/"):])
            for label, fname, data in delivery_set()[0]:
                if fname == name:
                    return self.send(200, data, "application/octet-stream")
            return self.send(404, {"error": "not found"})
        if u.path.startswith("/law"):
            return self.send(200, "<html><body><p>Happy Dungeons</p></body></html>", "text/html")
        if u.path == "/ps4/status":
            with LINKS_LOCK:
                n = len(load_links())
            return self.send(200, {"ok": True, "links": n})
        if u.path == "/ps4/account":
            return self.send(200, account_page(), "text/html; charset=utf-8", PAGE_HEADERS)
        if u.path in ("/ps4", "/ps4/"):
            return self.send(302, b"", "text/plain", {"Location": "/ps4/account"})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/api/TelemetryEntity":
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return self.send(200, b"", "text/plain")
        # JSON only (a cross-site form cannot send application/json without the browser asking first)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        body = self.body_json() if ctype == "application/json" else None
        if not isinstance(body, dict):
            return self.send(400, {"ok": False, "reason": "send JSON"})
        npid = str(body.get("npid") or "").strip() or None
        own = body.get("password")
        own = str(own) if own not in (None, "") else None    # a player-chosen password (their own login)
        if u.path == "/ps4/whoami":   # the players page: is this player linked yet? (makes nothing)
            status, answer = link_status(str(body.get("xuidBase", "")), str(body.get("secret", "")))
            return self.send(status, answer, headers={"Cache-Control": "no-store"})
        if u.path == "/ps4/link":
            status, answer = do_link(str(body.get("xuidBase", "")), str(body.get("secret", "")), npid, own)
            return self.send(status, answer, headers={"Cache-Control": "no-store"})
        if u.path == "/ps4/register":
            if own is not None:   # check the login first: no new Dungeons player for a login that cannot work
                why = own_login_problem(npid, own)
                if why:
                    return self.send(409 if "taken" in why else 400, {"ok": False, "reason": why},
                                     headers={"Cache-Control": "no-store"})
            status, raw = dungeons("POST", "/windurango/identity/claim", None,
                                   json.dumps({"gamertag": body.get("gamertag", "")}).encode(), "application/json",
                                   self.player_address())
            try:
                claim = json.loads(raw)
            except ValueError:
                claim = {}
            if status != 200 or not claim.get("ok"):
                return self.send(status if status >= 400 else 502, claim or {"ok": False})
            status, answer = do_link(claim["xuidBase"], claim["secret"], npid, own)
            answer.update(secret=claim["secret"], xuidBase=claim["xuidBase"])   # kept even if the link failed
            return self.send(status, answer, headers={"Cache-Control": "no-store"})
        return self.send(404, {"error": "not found"})


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            CFG.update(json.load(f))
    if "--create-admin" in sys.argv[2:]:
        # one-off at install time: create the shadNet admin account (shadnet.cfg AdminsList) with the
        # registration key. 0 = created, 11 = already exists; both are fine.
        code = shadnet_create(CFG["shadnetAdminNpid"], CFG["shadnetAdminPassword"], "admin@ps4.happydungeons.local")
        print("shadNet create %s: %s" % (CFG["shadnetAdminNpid"], code))
        sys.exit(0 if code in (0, 11) else 1)
    os.makedirs(CFG["stateDir"], exist_ok=True)
    threading.Thread(target=run_proxy, daemon=True).start()
    host, port = hostport(CFG["httpListen"])
    log("HTTP on %s (public base %s), Dungeons API %s" % (CFG["httpListen"], CFG["publicBase"], CFG["dungeonsApi"]))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        sys.exit(1)
