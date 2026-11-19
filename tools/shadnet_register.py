"""Create an account on the LOCAL shadNet server (protocol v1, command 2 = Create).

Usage: py -3 -I shadnet_register.py <npid> <password> <email> [host] [port] [registration key]
Only meant for 127.0.0.1. Prints the server's reply code (0 = created, 11 = name taken).
The local shadNet now has a RegistrationSecretKey (shadnet\\shadnet.cfg / livecopy\\gateway.json
"shadnetRegistrationKey"); without it the reply is 23. With no key argument the tool reads it from livecopy\\gateway.json.
"""
import json
import os
import socket
import struct
import sys

ERRORS = {0: "NoError (account created)", 1: "Malformed", 3: "InvalidInput", 10: "CreationError",
          11: "CreationExistingUsername", 12: "CreationBannedEmailProvider", 13: "CreationExistingEmail",
          23: "Unauthorized (registration secret key required)"}


def pb_string(field, text):
    data = text.encode("utf-8")
    out = bytearray([(field << 3) | 2])
    n = len(data)
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + data


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed the connection")
        buf += chunk
    return buf


def read_packet(sock):
    head = recv_exact(sock, 15)
    ptype, cmd, size, pid = struct.unpack("<BHIQ", head)
    body = recv_exact(sock, size - 15) if size > 15 else b""
    return ptype, cmd, pid, body


def main():
    npid, password, email = sys.argv[1:4]
    host = sys.argv[4] if len(sys.argv) > 4 else "127.0.0.1"
    port = int(sys.argv[5]) if len(sys.argv) > 5 else 31313
    key = sys.argv[6] if len(sys.argv) > 6 else ""
    if not key:
        try:
            gw = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "livecopy", "gateway.json")
            key = json.load(open(gw, encoding="utf-8")).get("shadnetRegistrationKey", "")
        except (OSError, ValueError):
            key = ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("refusing: this tool is for the local server only")
    with socket.create_connection((host, port), timeout=10) as s:
        ptype, cmd, pid, body = read_packet(s)
        if ptype == 3:
            print("server protocol version", struct.unpack("<I", body[:4])[0])
        msg = pb_string(1, npid) + pb_string(2, password) + pb_string(4, email) + (pb_string(5, key) if key else b"")
        payload = struct.pack("<I", len(msg)) + msg
        s.sendall(struct.pack("<BHIQ", 0, 2, 15 + len(payload), 1) + payload)
        while True:
            ptype, cmd, pid, body = read_packet(s)
            if ptype == 1 and cmd == 2:
                code = body[0] if body else -1
                print("reply:", code, ERRORS.get(code, "?"))
                return 0 if code in (0, 11) else 1


if __name__ == "__main__":
    sys.exit(main())
