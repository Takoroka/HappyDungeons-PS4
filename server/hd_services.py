"""Local stand-in for Toylogic's Happy Dungeons HTTP services (PS4 build), on 127.0.0.1:5100.

The shadPS4 patch HappyDungeonsLocal rewrites the game's hard-coded URLs to this server:
  https://xas-pfpf.lynca.net/v12/api       -> GET  /v12/api?...        Vulcan delivery list
  https://telemetry.happywars.net/          -> POST /api/TelemetryEntity  telemetry (accepted, logged)
  https://dev-api.lynca.net/                -> /...                      (logged)
  https://jp.happydungeons.net/law/         -> /law/                     (placeholder page)
shadNet's AvatarBaseUrl also points here (/avatar/...).

Vulcan reply format (from a real Happy Wars capture, same engine):
  {"Format":{"Name":"Vulcan","Version":12},"Log":{...},"List":{"Name","Valid","Memo","Id"},
   "File":{"1":{"AssetName","Label","URL"(percent-escaped),"FileName","FileSize","Memo","FileHash","Addition","Id"},...}}
The live server answered the PS4 build with only {"Format":{"Name":"Vulcan","Version":12}} (41 bytes, no files);
that is what this server sends until a delivery set is configured in data/vulcan.json.

Run: py -3 -I hd_services.py [--port 5100]
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, quote

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
LOG_PATH = os.path.join(HERE, "hd_services.log")
_log_lock = threading.Lock()

EMPTY_VULCAN = {"Format": {"Name": "Vulcan", "Version": 12}}


def log(line):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with _log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (stamp, line))


def vulcan_reply(host):
    """Delivery list. data/vulcan.json (optional) = {"list": {...}, "files": [{"label","file",...}]}."""
    cfg_path = os.path.join(DATA, "vulcan.json")
    if not os.path.exists(cfg_path):
        return EMPTY_VULCAN
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    files = {}
    for i, f in enumerate(cfg.get("files", []), 1):
        path = os.path.join(DATA, "delivery", f["file"])
        size = os.path.getsize(path)
        import hashlib
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        url = "http://%s/delivery/%s" % (host, f["file"])
        files[str(i)] = {"AssetName": cfg["list"]["Name"], "Label": f["label"], "URL": quote(url, safe=""),
                         "FileName": f.get("name", f["file"]), "FileSize": size, "Memo": "",
                         "FileHash": digest, "Addition": f.get("addition", ""), "Id": f.get("id", 1000 + i)}
    reply = dict(EMPTY_VULCAN)
    reply["List"] = cfg["list"]
    if files:
        reply["File"] = files
    return reply


class Handler(BaseHTTPRequestHandler):
    server_version = "HDServices/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet console; we log ourselves
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":")).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/v12/api":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            reply = vulcan_reply(self.headers.get("Host", "127.0.0.1:5100"))
            log("VULCAN %s x-id-hash=%s -> %d file(s)" % (
                {k: q.get(k) for k in ("country_id", "language_id", "platform_id", "project_id", "stage_id", "version")},
                self.headers.get("x-id-hash"), len(reply.get("File", {}))))
            return self._send(200, reply)
        if u.path.startswith("/delivery/"):
            name = os.path.basename(u.path)
            path = os.path.join(DATA, "delivery", name)
            if os.path.isfile(path):
                log("DELIVERY %s" % name)
                return self._send(200, open(path, "rb").read(), "application/octet-stream")
        if u.path.startswith("/law"):
            log("LAW %s" % self.path)
            return self._send(200, "<html><body><p>Happy Dungeons (local server)</p></body></html>", "text/html")
        log("UNHANDLED GET %s" % self.path)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlsplit(self.path)
        body = self._body()
        if u.path == "/api/TelemetryEntity":
            log("TELEMETRY %s %d bytes" % (u.query, len(body)))
            with open(os.path.join(HERE, "telemetry.log"), "ab") as f:
                f.write(body + b"\n")
            return self._send(200, b"", "text/plain")
        log("UNHANDLED POST %s %d bytes: %r" % (self.path, len(body), body[:300]))
        return self._send(404, {"error": "not found"})

    do_PUT = do_POST

    def do_HEAD(self):
        return self.do_GET()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5100)
    args = ap.parse_args()
    os.makedirs(os.path.join(DATA, "delivery"), exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    log("started on 127.0.0.1:%d" % args.port)
    print("HD services on http://127.0.0.1:%d" % args.port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
