"""PS4 import NIDs: py -3 -I nid.py name [name...] -> prints each name's NID (and the eboot stub if imported)."""
import base64, hashlib, struct, subprocess, sys
SUFFIX = bytes.fromhex("518D64A635DED8C1E6B039B1C3E55230")
def nid(name):
    h = hashlib.sha1(name.encode() + SUFFIX).digest()[:8]
    v = struct.unpack("<Q", h)[0]
    s = base64.b64encode(struct.pack(">Q", v)).decode().rstrip("=")
    return s.replace("/", "-")
imp = subprocess.run([sys.executable, "-I", __file__.replace("nid.py", "imports.py")], capture_output=True, text=True).stdout
table = {l.split()[1].split("#")[0]: l.split()[0] for l in imp.splitlines() if " " in l}
for n in sys.argv[1:]:
    k = nid(n)
    print("%-40s %s  %s" % (n, k, table.get(k, "-")))
