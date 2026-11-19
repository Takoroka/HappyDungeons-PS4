"""Read and write the Happy Dungeons .pak data files (ZRES -> BRES -> embedded files).

Format, as observed in the shipped files (payload/ServerData/*.pak):

  ZRES container      b'ZRES' + u32 uncompressed_size + raw zlib stream
  BRES tree           chunks of  tag[4] + u32 raw_size
                        - bit 31 of raw_size set  => the chunk's payload is itself a chunk list
                        - the low 31 bits are the payload length in bytes
                      chunk payloads are padded so every chunk starts on a 4-byte boundary
  tags seen           BRES (root, has children)
                        INFO  8 bytes
                        ASET  (has children)
                          PS    "name\0RES\0"        - a key/value string pair
                          PS    "target\0Win32\0"
                          FILE  (has children)   one per embedded file
                            PI    "size\0" + u32   - the payload length
                            PS    "name\0<filename>\0"
                            FB    the file bytes (JSON text, NUL padded to 4)

Nothing here re-serialises JSON: an embedded file is handled as bytes, so a build that
changes one file leaves every other byte in the pak exactly as it was.
"""

import struct
import zlib

ZRES_MAGIC = b"ZRES"
ALIGN = 4


class Chunk:
    """One BRES chunk. Either `data` (leaf) or `children` (branch) is set."""

    __slots__ = ("tag", "data", "children")

    def __init__(self, tag, data=None, children=None):
        assert len(tag) == 4, tag
        self.tag = tag
        self.data = data
        self.children = children

    @property
    def is_branch(self):
        return self.children is not None

    def __repr__(self):
        if self.is_branch:
            return "<Chunk %r %d children>" % (self.tag, len(self.children))
        return "<Chunk %r %d bytes>" % (self.tag, len(self.data))


def _pad(n):
    return (-n) % ALIGN


def parse_chunks(buf, start, end):
    out = []
    off = start
    while off + 8 <= end:
        tag = buf[off:off + 4]
        raw = struct.unpack_from("<I", buf, off + 4)[0]
        size = raw & 0x7FFFFFFF
        branch = bool(raw >> 31)
        body_start = off + 8
        body_end = body_start + size
        if body_end > end:
            raise ValueError("chunk %r at %#x overruns its parent" % (tag, off))
        if branch:
            out.append(Chunk(tag, children=parse_chunks(buf, body_start, body_end)))
        else:
            out.append(Chunk(tag, data=bytes(buf[body_start:body_end])))
        off = body_end + _pad(size)
    return out


def build_chunks(chunks):
    parts = []
    for c in chunks:
        if c.is_branch:
            body = build_chunks(c.children)
            flag = 0x80000000
        else:
            body = c.data
            flag = 0
        parts.append(c.tag)
        parts.append(struct.pack("<I", len(body) | flag))
        parts.append(body)
        parts.append(b"\0" * _pad(len(body)))
    return b"".join(parts)


def zres_decompress(blob):
    if blob[:4] != ZRES_MAGIC:
        raise ValueError("not a ZRES file (magic %r)" % blob[:4])
    declared = struct.unpack_from("<I", blob, 4)[0]
    out = zlib.decompress(blob[8:])
    if len(out) != declared:
        raise ValueError("ZRES says %d bytes, got %d" % (declared, len(out)))
    return out


def zres_compress(raw, level=9):
    # Deterministic: the same `raw` and `level` always give the same bytes.
    return ZRES_MAGIC + struct.pack("<I", len(raw)) + zlib.compress(raw, level)


class Pak:
    """A ZRES/BRES pak, held as its chunk tree plus the order of its embedded files."""

    def __init__(self, root):
        self.root = root

    # -- loading / saving -------------------------------------------------

    @classmethod
    def load(cls, path):
        with open(path, "rb") as fh:
            return cls.from_bytes(fh.read())

    @classmethod
    def from_bytes(cls, blob):
        raw = zres_decompress(blob)
        chunks = parse_chunks(raw, 0, len(raw))
        if len(chunks) != 1 or chunks[0].tag != b"BRES":
            raise ValueError("expected a single BRES root, got %r" % (chunks,))
        return cls(chunks[0])

    def to_bytes(self, level=9):
        return zres_compress(build_chunks([self.root]), level=level)

    def save(self, path, level=9):
        blob = self.to_bytes(level=level)
        with open(path, "wb") as fh:
            fh.write(blob)
        return blob

    # -- embedded files ---------------------------------------------------

    def _aset(self):
        for c in self.root.children:
            if c.tag == b"ASET":
                return c
        raise ValueError("no ASET chunk")

    def file_chunks(self):
        return [c for c in self._aset().children if c.tag == b"FILE"]

    @staticmethod
    def _file_name(file_chunk):
        for c in file_chunk.children:
            if c.tag == b"PS  " and c.data.startswith(b"name\0"):
                return c.data[5:].split(b"\0", 1)[0].decode("ascii")
        raise ValueError("FILE chunk has no name")

    @staticmethod
    def _fb(file_chunk):
        for c in file_chunk.children:
            if c.tag == b"FB  ":
                return c
        raise ValueError("FILE chunk has no FB payload")

    @staticmethod
    def _pi(file_chunk):
        for c in file_chunk.children:
            if c.tag == b"PI  " and c.data.startswith(b"size\0"):
                return c
        raise ValueError("FILE chunk has no size")

    def names(self):
        return [self._file_name(f) for f in self.file_chunks()]

    def read_file(self, name):
        """The embedded file's bytes, exactly as stored (NUL padding included)."""
        for f in self.file_chunks():
            if self._file_name(f) == name:
                return self._fb(f).data
        raise KeyError(name)

    def declared_size(self, name):
        for f in self.file_chunks():
            if self._file_name(f) == name:
                return struct.unpack_from("<I", self._pi(f).data, 5)[0]
        raise KeyError(name)

    def write_file(self, name, payload):
        """Replace an embedded file.

        `payload` is the file's real bytes. The FILE's PI "size" is set to that length and
        the FB chunk holds the payload NUL-padded up to a 4-byte boundary - which is what
        every shipped file does (checked on all 39 embedded files across the five paks in
        the installer payload: PI size == unpadded length, FB size == ceil4(PI size))."""
        padded = payload + b"\0" * _pad(len(payload))
        for f in self.file_chunks():
            if self._file_name(f) == name:
                self._fb(f).data = padded
                pi = self._pi(f)
                pi.data = pi.data[:5] + struct.pack("<I", len(payload)) + pi.data[9:]
                return
        raise KeyError(name)


def text_of(payload):
    """An embedded JSON file as text, with the trailing NUL padding removed."""
    return payload.rstrip(b"\0").decode("utf-8")


def strip_comments(text):
    """The game's JSON dialect allows // line comments. json.loads does not."""
    return "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("//"))


def load_json(payload):
    import json
    return json.loads(strip_comments(text_of(payload)))
