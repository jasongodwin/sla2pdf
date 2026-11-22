"""HarfBuzz text shaping: glyphs and advances in pt for one run of text in one font."""
from __future__ import annotations

import struct
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

import uharfbuzz as hb

from .fonts import FontFace

Direction = Literal["ltr", "rtl"]


@dataclass
class Shaped:
    glyph_ids: list[int]
    advances: list[float]  # pt, in visual (drawing) order
    width: float  # pt
    direction: str
    offsets: list[tuple[float, float]] = field(default_factory=list)  # pt (dx, dy), y up


@dataclass(frozen=True)
class FontMetrics:
    ascent: float  # pt above the baseline (positive)
    descent: float  # pt below the baseline (positive)
    height: float  # Scribus's ScFace::height (FreeType face->height)


@lru_cache(maxsize=None)
def _hb_font(path: str, index: int) -> hb.Font:
    return hb.Font(hb.Face(hb.Blob.from_file_path(path), index))


def _font(face: FontFace) -> hb.Font:
    return _hb_font(str(face.path), face.index)


def text_direction(text: str) -> Direction | None:
    """"rtl" / "ltr" from the strong letters in `text`; None when it has none."""
    rtl = ltr = 0
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi in ("R", "AL"):
            rtl += 1
        elif bidi == "L":
            ltr += 1
    if rtl == ltr == 0:
        return None
    return "rtl" if rtl > ltr else "ltr"


def has_glyph(face: FontFace, char: str) -> bool:
    return _font(face).get_nominal_glyph(ord(char)) is not None


# characters that legitimately draw nothing (everything else in the font must have an outline)
_LEGIT_EMPTY = {0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD} | set(range(0xFE00, 0xFE10))


@lru_cache(maxsize=None)
def _has_ink(path: str, index: int, code: int) -> bool:
    ext = _hb_font(path, index).get_glyph_extents(_hb_font(path, index).get_nominal_glyph(code) or 0)
    return ext is not None and (ext.width != 0 or ext.height != 0)


def has_blank_glyph(face: FontFace, char: str) -> bool:
    """True when the font maps `char` to a glyph with no outline although the character
    should be visible (a font whose cmap lists letters it does not draw)."""
    if ord(char) in _LEGIT_EMPTY or char.isspace() or not has_glyph(face, char):
        return False
    return not _has_ink(str(face.path), face.index, ord(char))


def has_visible_glyph(face: FontFace, char: str) -> bool:
    """has_glyph, but a glyph that is present and empty does not count for a visible char."""
    return has_glyph(face, char) and not has_blank_glyph(face, char)


def _i16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">h", data, offset)[0]


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


@lru_cache(maxsize=None)
def _unit_metrics(path: str, index: int) -> tuple[float, float, float]:
    """(ascent, descent, height) per em, as FreeType (>= 2.10) face->ascender/descender/
    height and so Scribus's ScFace read them: the OS/2 typo values when OS/2 sets
    USE_TYPO_METRICS (fsSelection bit 7); otherwise the hhea table, then OS/2 typo when
    hhea's ascender and descender are both 0, then OS/2 win when those are 0 too; and
    when all are 0, Scribus's own rule (ftface.cpp): ascent = bbox height = height."""
    face = _hb_font(path, index).face
    upem = face.upem
    hhea = face.reference_table("hhea").data
    os2 = face.reference_table("OS/2").data
    asc, desc, gap = (_i16(hhea, 4), _i16(hhea, 6), _i16(hhea, 8)) if len(hhea) >= 10 else (0, 0, 0)
    height = asc - desc + gap
    if len(os2) >= 78 and _u16(os2, 62) & 0x80:  # USE_TYPO_METRICS
        asc, desc, gap = _i16(os2, 68), _i16(os2, 70), _i16(os2, 72)
        height = asc - desc + gap
    elif asc == 0 and desc == 0 and len(os2) >= 78:
        asc, desc, gap = _i16(os2, 68), _i16(os2, 70), _i16(os2, 72)
        height = asc - desc + gap
        if asc == 0 and desc == 0:
            asc, desc = _u16(os2, 74), -_u16(os2, 76)
            height = asc - desc
    if asc == 0 and desc == 0:
        head = face.reference_table("head").data
        if len(head) >= 44:
            asc = height = _i16(head, 42) - _i16(head, 38)
    return asc / upem, -desc / upem, height / upem


def font_metrics(face: FontFace, size: float) -> FontMetrics:
    asc, desc, height = _unit_metrics(str(face.path), face.index)
    return FontMetrics(asc * size, desc * size, height * size)


@lru_cache(maxsize=None)
def _glyph_box(path: str, index: int, glyph: int) -> tuple[float, float]:
    font = _hb_font(path, index)
    ext = font.get_glyph_extents(glyph)
    if ext is None:
        return 0.0, 0.0
    upem = font.face.upem
    return ext.y_bearing / upem, -(ext.y_bearing + ext.height) / upem


def ink_extent(face: FontFace, size: float, glyph_ids: list[int]) -> tuple[float, float]:
    """Tallest real glyph ascent and deepest real glyph descent (pt, both measured from
    the baseline, positive = away from it) over `glyph_ids`; (0, 0) for blank glyphs."""
    asc = desc = 0.0
    for g in glyph_ids:
        a, d = _glyph_box(str(face.path), face.index, g)
        if a or d:
            asc, desc = max(asc, a * size), max(desc, d * size)
    return asc, desc


@lru_cache(maxsize=50000)
def _shape_cached(text: str, path: str, index: int, size: float, direction: str) -> Shaped:
    font = _hb_font(path, index)
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    buf.direction = direction
    hb.shape(font, buf)
    k = size / font.face.upem
    ids = [info.codepoint for info in buf.glyph_infos]
    advances = [pos.x_advance * k for pos in buf.glyph_positions]
    offsets = [(pos.x_offset * k, pos.y_offset * k) for pos in buf.glyph_positions]
    return Shaped(ids, advances, sum(advances), direction, offsets)


def shape(text: str, face: FontFace, size: float, direction: Direction | None = None) -> Shaped:
    """Shape `text` at `size` pt. Without a direction, text that is mostly Arabic/Hebrew
    letters is shaped right to left, anything else left to right."""
    direction = direction or text_direction(text) or "ltr"
    s = _shape_cached(text, str(face.path), face.index, float(size), direction)
    # Callers may scale advances; never hand out the cached lists.
    return Shaped(list(s.glyph_ids), list(s.advances), s.width, s.direction, list(s.offsets))
