"""Write a title into a copy of a FIT activity file.

FIT has no "activity name" field. The closest are the workout name (`workout.wkt_name`,
what a watch shows for a structured workout) and the sport profile name (`sport.name`,
"Run"). This rewrites or adds those messages and leaves every other byte as it was.

FIT data messages refer to the latest definition for their local message type, so a
message whose layout changes is written as: new definition, the message, then the
original definition again, so that later messages still decode as before.
"""
import struct
from dataclasses import dataclass

WORKOUT, WORKOUT_STEP, SPORT, SESSION, FILE_ID = 26, 27, 12, 18, 0
STRING, ENUM, UINT16 = 0x07, 0x00, 0x84
TARGETS = {"workout": (WORKOUT, 8), "sport": (SPORT, 3)}  # message -> its name field
MAX_BYTES = 100

_CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def crc16(data: bytes, crc: int = 0) -> int:
    for b in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[b & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[(b >> 4) & 0xF]
    return crc


@dataclass
class _Def:
    local: int
    arch: int
    glob: int
    fields: list  # [(num, size, base_type)]
    dev: list  # [(num, size, dev_index)]
    raw: bytes  # the definition record as it appears in the file

    def encode(self, fields: list) -> bytes:
        endian = ">" if self.arch else "<"
        out = bytes([0x40 | self.local | (0x20 if self.dev else 0), 0, self.arch])
        out += struct.pack(endian + "HB", self.glob, len(fields))
        out += b"".join(bytes(f) for f in fields)
        if self.dev:
            out += bytes([len(self.dev)]) + b"".join(bytes(f) for f in self.dev)
        return out


def _records(data: bytes, start: int, end: int):
    """Yield (definition, record bytes, is_definition) for each record."""
    defs = {}
    pos = start
    while pos < end:
        h = data[pos]
        if h & 0x80:  # compressed timestamp header: data message, local type 0-3
            d = defs[(h >> 5) & 0x3]
            size = 1 + sum(f[1] for f in d.fields) + sum(f[1] for f in d.dev)
            yield d, data[pos:pos + size], False
        elif h & 0x40:
            arch = data[pos + 2]
            glob, n = struct.unpack((">" if arch else "<") + "HB", data[pos + 3:pos + 6])
            p = pos + 6
            fields = [tuple(data[p + 3 * i:p + 3 * i + 3]) for i in range(n)]
            p += 3 * n
            dev = []
            if h & 0x20:
                nd = data[p]
                dev = [tuple(data[p + 1 + 3 * i:p + 4 + 3 * i]) for i in range(nd)]
                p += 1 + 3 * nd
            d = _Def(h & 0x0F, arch, glob, fields, dev, data[pos:p])
            defs[d.local] = d
            size = p - pos
            yield d, d.raw, True
        else:
            d = defs[h & 0x0F]
            size = 1 + sum(f[1] for f in d.fields) + sum(f[1] for f in d.dev)
            yield d, data[pos:pos + size], False
        if pos + size > end:
            raise ValueError("record runs past the end of the data")
        pos += size


def _split(d: _Def, rec: bytes) -> tuple[list, bytes]:
    """Field values of a data record as [(num, size, base_type, bytes)], plus developer data."""
    out, p = [], 1
    for num, size, bt in d.fields:
        out.append((num, size, bt, rec[p:p + size]))
        p += size
    return out, rec[p:]


def _string(value: str) -> bytes:
    raw = value.replace("\0", "").encode()[:MAX_BYTES]
    return raw.decode(errors="ignore").encode() + b"\0"  # don't cut a character in half


def _with_name(d: _Def, rec: bytes, field: int, title: bytes) -> bytes:
    """Re-encode one data record with `field` set to the title string."""
    values, dev_data = _split(d, rec)
    if not any(v[0] == field for v in values):
        values.append((field, 0, STRING, b""))
    values = [(n, len(title), STRING, title) if n == field else (n, s, bt, b)
              for n, s, bt, b in values]
    new_def = d.encode([(n, s, bt) for n, s, bt, _ in values])
    return new_def + rec[:1] + b"".join(v[3] for v in values) + dev_data + d.raw


def _new_message(local: int, glob: int, fields: list) -> bytes:
    """A little-endian definition plus one data record. fields: [(num, base_type, bytes)]."""
    d = _Def(local, 0, glob, [], [], b"")
    return (d.encode([(n, len(b), bt) for n, bt, b in fields])
            + bytes([local]) + b"".join(b for _, _, b in fields))


def with_title(data: bytes, title: str, into: tuple = ("workout",)) -> bytes:
    """Return a copy of a FIT file with `title` as the workout name and/or sport name."""
    if len(data) < 14 or data[8:12] != b".FIT":
        raise ValueError("not a FIT file")
    hsize = data[0]
    end = hsize + struct.unpack("<I", data[4:8])[0]
    if end + 2 > len(data):
        raise ValueError("FIT file is truncated")
    name = _string(title)
    targets = {TARGETS[k] for k in into}

    out = bytearray()
    insert_at = file_id = None
    found = set()
    sport = sub_sport = 0
    for d, rec, is_def in _records(data, hsize, end):
        target = next((t for t in targets if t[0] == d.glob), None)
        if not is_def and target:
            out += _with_name(d, rec, target[1], name)
            found.add(target)
            continue
        out += rec
        if not is_def and d.glob == FILE_ID and insert_at is None:
            insert_at, file_id = len(out), d
        if not is_def and d.glob == SESSION and not (sport or sub_sport):
            values = {n: b for n, _, _, b in _split(d, rec)[0]}
            sport = values.get(5, b"\0")[0]
            sub_sport = values.get(6, b"\0")[0]

    missing = targets - found
    if missing:
        if insert_at is None:
            raise ValueError("no file_id message")
        local = file_id.local  # borrow file_id's local type, then give it back
        extra = b""
        if (WORKOUT, 8) in missing:
            extra += _new_message(local, WORKOUT, [(4, ENUM, bytes([sport])),
                                                   (6, UINT16, struct.pack("<H", 1)),
                                                   (8, STRING, name)])
            # one open-ended step, as a watch writes for a simple workout
            extra += _new_message(local, WORKOUT_STEP, [(254, UINT16, struct.pack("<H", 0)),
                                                        (1, ENUM, b"\x05"), (3, ENUM, b"\x02"),
                                                        (7, ENUM, b"\x00")])
        if (SPORT, 3) in missing:
            extra += _new_message(local, SPORT, [(0, ENUM, bytes([sport])),
                                                 (1, ENUM, bytes([sub_sport])),
                                                 (3, STRING, name)])
        out[insert_at:insert_at] = extra + file_id.raw

    header = bytearray(data[:hsize])
    header[4:8] = struct.pack("<I", len(out))
    if hsize >= 14:
        header[12:14] = struct.pack("<H", crc16(bytes(header[:12])))
    body = bytes(header) + bytes(out)
    # anything after this file's CRC is a chained FIT file: keep it as it was
    return body + struct.pack("<H", crc16(body)) + data[end + 2:]
