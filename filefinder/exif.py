"""Read the date and GPS position out of photos (EXIF), without any imaging library.

Only a few small header fields are read from the start of the file. The picture
itself is never decoded, so a damaged or hostile image cannot reach an image decoder.
Supports JPEG, TIFF based raw files (DNG, CR2, NEF, ARW and similar), PNG, WebP and HEIC.
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from datetime import datetime

from .extract import open_regular

MAX_SCAN = 1024 * 1024
TIFF_STARTS = (b"II*\x00", b"MM\x00*")
_DATE = re.compile(r"(\d{4}):(\d\d):(\d\d)[ T](\d\d):(\d\d):(\d\d)")
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
_BAD_DATA = (struct.error, IndexError, ValueError, OverflowError)

@dataclass
class PhotoInfo:
    taken: float | None = None      # when the picture was taken (timestamp)
    lat: float | None = None
    lon: float | None = None

def read_photo_info(path: str) -> PhotoInfo | None:
    """Return the date taken and position of a photo, or None when there is no EXIF."""
    fd = open_regular(path)
    if fd is None:
        return None
    try:
        with open(fd, "rb", closefd=False) as fh:
            tiff = _find_tiff(fh)
    except OSError:
        return None
    finally:
        os.close(fd)
    if not tiff:
        return None
    try:
        return _parse_tiff(tiff)
    except _BAD_DATA:
        return None

def _find_tiff(fh) -> bytes | None:
    head = fh.read(4)
    fh.seek(0)
    if head[:2] == b"\xff\xd8":
        return _jpeg_exif(fh)
    blob = fh.read(MAX_SCAN)
    if blob[:4] in TIFF_STARTS:
        return blob                                   # TIFF and most raw formats
    for marker, skip in ((b"Exif\x00\x00", 6), (b"eXIf", 4), (b"EXIF", 8)):
        start = blob.find(marker)
        while start != -1:
            candidate = blob[start + skip:]
            if candidate[:6] == b"Exif\x00\x00":      # WebP may repeat the prefix
                candidate = candidate[6:]
            if candidate[:4] in TIFF_STARTS:
                return candidate
            start = blob.find(marker, start + 1)
    for pattern in (b"MM\x00*\x00\x00\x00\x08", b"II*\x00\x08\x00\x00\x00"):   # HEIC fallback
        start = blob.find(pattern)
        if start != -1:
            return blob[start:]
    return None

def _jpeg_exif(fh) -> bytes | None:
    fh.seek(2)
    for _ in range(40):                               # look at the first few segments only
        header = fh.read(4)
        if len(header) < 4 or header[0] != 0xFF:
            return None
        code, length = header[1], struct.unpack(">H", header[2:4])[0]
        if code in (0xDA, 0xD9) or length < 2:        # start of picture data: no EXIF
            return None
        size = length - 2
        if code == 0xE1 and size > 6:
            data = fh.read(size)
            if data[:6] == b"Exif\x00\x00":
                return data[6:]
        else:
            fh.seek(size, 1)
    return None

class _Tiff:
    def __init__(self, data: bytes):
        self.data = data
        self.order = "<" if data[:2] == b"II" else ">"

    def u16(self, offset: int) -> int:
        return struct.unpack_from(self.order + "H", self.data, offset)[0]

    def u32(self, offset: int) -> int:
        return struct.unpack_from(self.order + "I", self.data, offset)[0]

    def ifd(self, offset: int) -> dict:
        """Map tag number to (type, count, position of the value field)."""
        entries = {}
        for i in range(min(self.u16(offset), 500)):
            pos = offset + 2 + 12 * i
            entries[self.u16(pos)] = (self.u16(pos + 2), self.u32(pos + 4), pos + 8)
        return entries

    def _value_at(self, entry) -> int:
        kind, count, pos = entry
        return pos if _TYPE_SIZE.get(kind, 1) * count <= 4 else self.u32(pos)

    def text(self, entry) -> str:
        start = self._value_at(entry)
        raw = self.data[start:start + min(entry[1], 64)]
        return raw.split(b"\x00", 1)[0].decode("ascii", "ignore")

    def rationals(self, entry) -> list[float]:
        start, out = self._value_at(entry), []
        for i in range(min(entry[1], 3)):
            top, bottom = struct.unpack_from(self.order + "II", self.data, start + 8 * i)
            out.append(top / bottom if bottom else 0.0)
        return out

def _degrees(tiff: _Tiff, ifd: dict, value_tag: int, ref_tag: int) -> float | None:
    if value_tag not in ifd or ref_tag not in ifd:
        return None
    parts = tiff.rationals(ifd[value_tag])
    if len(parts) != 3:
        return None
    degrees = parts[0] + parts[1] / 60 + parts[2] / 3600
    return -degrees if tiff.text(ifd[ref_tag]).upper().startswith(("S", "W")) else degrees

def _parse_date(text: str) -> float | None:
    m = _DATE.search(text)
    if not m:
        return None
    try:
        when = datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None
    if not 1990 <= when.year <= datetime.now().year + 1:
        return None
    return when.timestamp()

def _parse_tiff(data: bytes) -> PhotoInfo | None:
    tiff = _Tiff(data)
    if tiff.u16(2) != 42:
        return None
    ifd0 = tiff.ifd(tiff.u32(4))
    info = PhotoInfo()
    # Each block is read on its own, so a damaged GPS block never costs you the date.
    try:
        if 0x8769 in ifd0:                                # the Exif sub block
            exif = tiff.ifd(tiff.u32(ifd0[0x8769][2]))
            for tag in (0x9003, 0x9004):                  # taken, then digitized
                if tag in exif and info.taken is None:
                    info.taken = _parse_date(tiff.text(exif[tag]))
    except _BAD_DATA:
        pass
    try:
        if info.taken is None and 0x0132 in ifd0:         # plain date time of the file
            info.taken = _parse_date(tiff.text(ifd0[0x0132]))
    except _BAD_DATA:
        pass
    try:
        if 0x8825 in ifd0:                                # the GPS sub block
            gps = tiff.ifd(tiff.u32(ifd0[0x8825][2]))
            lat, lon = _degrees(tiff, gps, 2, 1), _degrees(tiff, gps, 4, 3)
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                info.lat, info.lon = lat, lon
    except _BAD_DATA:
        pass
    if info.taken is None and info.lat is None:
        return None
    return info
