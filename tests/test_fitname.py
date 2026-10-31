import struct
from collections import Counter

import fitdecode
import pytest

from app.fitimport import build_activity, parse_fit
from app.fitname import crc16, with_title


def decode(data: bytes, tmp_path, name="out.fit"):
    """All data messages as (name, {field: value}), with strict CRC and decoding checks."""
    p = tmp_path / name
    p.write_bytes(data)
    out = []
    with fitdecode.FitReader(str(p), check_crc=fitdecode.CrcCheck.ENABLED,
                             error_handling=fitdecode.ErrorHandling.RAISE) as fit:
        for fr in fit:
            if fr.frame_type == fitdecode.FIT_FRAME_DATA:
                out.append((fr.name, {f.name: f.value for f in fr.fields}))
    return out


def named(msgs, name):
    return [f for n, f in msgs if n == name]


def garmin_like() -> bytes:
    """A small FIT file with what real watch files have and the demo files don't: a sport
    message named "Run", developer fields, compressed timestamp headers, and local
    message types reused for different messages."""
    def defn(local, glob, fields, dev=()):
        h = 0x40 | local | (0x20 if dev else 0)
        b = bytes([h, 0, 0]) + struct.pack("<HB", glob, len(fields))
        b += b"".join(bytes(f) for f in fields)
        if dev:
            b += bytes([len(dev)]) + b"".join(bytes(f) for f in dev)
        return b

    t0 = 1_100_000_000
    body = defn(0, 0, [(0, 1, 0), (1, 2, 0x84), (4, 4, 0x86)])
    body += bytes([0, 4]) + struct.pack("<HI", 1, t0)  # file_id: activity, garmin
    body += defn(0, 207, [(3, 1, 2)]) + bytes([0, 0])  # developer_data_id, same local type
    body += defn(0, 206, [(0, 1, 2), (1, 1, 2), (2, 1, 2), (3, 7, 7)])
    body += bytes([0, 0, 0, 0x84]) + b"power2\0"  # field_description
    body += defn(2, 12, [(0, 1, 0), (1, 1, 0), (3, 4, 7)]) + bytes([2, 1, 0]) + b"Run\0"
    body += defn(3, 20, [(253, 4, 0x86), (3, 1, 2), (5, 4, 0x86)], dev=[(0, 2, 0)])
    body += bytes([3]) + struct.pack("<IBIH", t0, 140, 0, 250)
    # the rest use compressed timestamp headers: their definition has no timestamp field
    body += defn(1, 20, [(3, 1, 2), (5, 4, 0x86)], dev=[(0, 2, 0)])
    for s in range(1, 20):
        body += bytes([0x80 | (1 << 5) | ((t0 + s) & 0x1F)]) + struct.pack("<BIH", 140 + s, s * 300, 250 + s)
    body += defn(1, 18, [(253, 4, 0x86), (2, 4, 0x86), (5, 1, 0), (6, 1, 0), (7, 4, 0x86),
                         (8, 4, 0x86), (9, 4, 0x86)])
    body += bytes([1]) + struct.pack("<IIBBIII", t0 + 19, t0, 1, 0, 19000, 19000, 5700)
    body += defn(1, 34, [(253, 4, 0x86), (1, 2, 0x84)]) + bytes([1]) + struct.pack("<IH", t0 + 19, 1)
    header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(body), b".FIT")
    data = header + struct.pack("<H", crc16(header)) + body
    return data + struct.pack("<H", crc16(data))


@pytest.fixture
def demo_fit(watch_dir) -> bytes:
    return (watch_dir / "run1.fit").read_bytes()


def without(msgs, *names):
    return [m for m in msgs if m[0] not in names]


def test_adds_workout_name(demo_fit, tmp_path):
    before = decode(demo_fit, tmp_path, "a.fit")
    after = decode(with_title(demo_fit, "A spooky run on a frightful Halloween night"), tmp_path)
    assert [w["wkt_name"] for w in named(after, "workout")] == ["A spooky run on a frightful Halloween night"]
    assert named(after, "workout")[0]["sport"] == "running"
    assert len(named(after, "workout_step")) == 1
    assert after[0][0] == "file_id"  # file_id stays first
    assert without(after, "workout", "workout_step") == before  # nothing else changed


def test_adds_sport_name(demo_fit, tmp_path):
    after = decode(with_title(demo_fit, "Pumpkin Lantern Loop", ("sport",)), tmp_path)
    assert [s["name"] for s in named(after, "sport")] == ["Pumpkin Lantern Loop"]
    assert not named(after, "workout")


def test_app_still_reads_it_the_same(demo_fit, tmp_path):
    a = tmp_path / "a.fit"
    a.write_bytes(demo_fit)
    b = tmp_path / "b.fit"
    b.write_bytes(with_title(demo_fit, "Ghosts", ("workout", "sport")))
    assert build_activity(parse_fit(a)) == build_activity(parse_fit(b))


def test_garmin_like_file(tmp_path):
    src = garmin_like()
    before = decode(src, tmp_path, "a.fit")
    after = decode(with_title(src, "Witching Hour Tempo", ("workout", "sport")), tmp_path)
    assert [s["name"] for s in named(after, "sport")] == ["Witching Hour Tempo"]  # renamed, not added
    assert [w["wkt_name"] for w in named(after, "workout")] == ["Witching Hour Tempo"]
    assert named(after, "workout")[0]["sport"] == "running"  # copied from the session
    strip = lambda ms: [(n, {k: v for k, v in f.items() if k != "name"} if n == "sport" else f)  # noqa: E731
                        for n, f in without(ms, "workout", "workout_step")]
    assert strip(after) == strip(before)
    recs = named(after, "record")
    assert len(recs) == 20 and recs[-1]["power2"] == 269 and recs[-1]["heart_rate"] == 159


def test_retitling_replaces_the_old_title(tmp_path):
    once = with_title(garmin_like(), "First title", ("workout", "sport"))
    twice = with_title(once, "Second", ("workout", "sport"))
    msgs = decode(twice, tmp_path)
    assert [w["wkt_name"] for w in named(msgs, "workout")] == ["Second"]
    assert [s["name"] for s in named(msgs, "sport")] == ["Second"]
    assert Counter(n for n, _ in msgs) == Counter(n for n, _ in decode(once, tmp_path, "o.fit"))


def test_unicode_and_long_titles(tmp_path):
    msgs = decode(with_title(garmin_like(), "Fête des morts 🎃 " + "é" * 200), tmp_path)
    name = named(msgs, "workout")[0]["wkt_name"]
    assert name.startswith("Fête des morts 🎃") and len(name.encode()) <= 100


def test_rejects_non_fit():
    with pytest.raises(ValueError):
        with_title(b"not a fit file at all", "x")
    with pytest.raises(ValueError):
        with_title(garmin_like()[:40], "x")
