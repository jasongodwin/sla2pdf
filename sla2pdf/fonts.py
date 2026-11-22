"""Map Scribus font names ("Family Style") to font files through fontconfig."""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .pictures import write_atomically

_SEP = "|"
_FMT = "%{file}|%{index}|%{family}|%{style}"
_STYLE_WORDS = {
    "regular", "normal", "roman", "book", "bold", "italic", "oblique", "medium",
    "light", "thin", "heavy", "black", "demi", "semibold", "extrabold",
    "ultralight", "ultra", "condensed", "extralight", "demibold",
}


@dataclass
class FontFace:
    family: str
    path: Path
    index: int  # face index inside a .ttc collection (0 for plain files)


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def _readable(path: Path, index: int) -> bool:
    """True when the tables fpdf2 subsetting needs can all be read from the face."""
    from fontTools.ttLib import TTFont

    try:
        with TTFont(str(path), fontNumber=index, lazy=True) as font:
            outlines = ["glyf", "loca"] if "glyf" in font else ["CFF "]
            for tag in outlines + ["cmap", "hmtx", "head", "hhea", "maxp"]:
                font[tag]
            if "glyf" in font:
                font["glyf"].glyphs  # decompile: a cut-off file fails here
        return True
    except Exception:
        return False


def _rebuild(path: Path, index: int, out: Path) -> None:
    """Save a copy of the face at `out` with a glyf table rebuilt glyph by glyph: every
    glyph that decodes is kept, the rest (a cut-off end of the table) become empty.
    Raises when nothing usable (no TrueType outlines,
    no cmap, no glyph) can be made."""
    from fontTools.ttLib import TTCollection, TTFont, newTable
    from fontTools.ttLib.tables._g_l_y_f import Glyph

    collection = path.suffix.lower() in (".ttc", ".otc")
    font = TTCollection(str(path), lazy=True).fonts[index] if collection else TTFont(str(path), lazy=True)
    if "glyf" not in font:
        raise ValueError("no glyf table")
    if not any(t.cmap for t in font["cmap"].tables):
        raise ValueError("no cmap")
    raw = font.reader["glyf"]
    locations = font["loca"].locations
    order = font.getGlyphOrder()
    glyf = newTable("glyf")
    glyf.glyphOrder = order
    glyf.glyphs = {}
    kept = 0
    for gid, name in enumerate(order):
        start, end = locations[gid], locations[gid + 1]
        glyph = Glyph(raw[start:end] if end <= len(raw) else b"")
        try:
            glyph.expand(glyf)
            kept += 1 if end > start else 0
        except Exception:
            glyph = Glyph()
        glyf.glyphs[name] = glyph
    if kept == 0:
        raise ValueError("no glyph decodes")
    font["glyf"] = glyf
    write_atomically(out, lambda tmp: font.save(str(tmp)))


def repair_font(path: Path, index: int, cache_dir: Path, log: list[str]) -> Path | None:
    """A readable copy of a truncated-but-mostly-intact TrueType face, kept in `cache_dir`
    (named after the source's path, size, date and face, made once, atomically); None
    when it cannot be repaired."""
    try:
        st = path.stat()
        key = hashlib.sha1(f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{index}".encode()).hexdigest()
        out = Path(cache_dir) / f"{path.stem}-{index}-repaired-{key[:16]}.ttf"
        if not out.is_file():
            _rebuild(path, index, out)
        if _readable(out, 0):
            from fontTools.ttLib import TTFont
            with TTFont(str(out), lazy=True) as f:
                log.append(f"repaired font {path} ({f['maxp'].numGlyphs} glyphs)")
            return out
    except Exception:
        pass
    return None


class FontResolver:
    def __init__(self, fallback: str = "DejaVu Sans", log: list[str] | None = None,
                 cache_dir: Path | None = None):
        self.fallback = fallback
        self.cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "sla2pdf-fonts"
        self.log = log if log is not None else []
        self._cache: dict[str, FontFace] = {}
        self._char_cache: dict[str, FontFace] = {}
        self._families: set[str] | None = None
        self._verdicts: dict[tuple[Path, int], bool] = {}

    # -- fontconfig helpers -------------------------------------------------
    def _known_families(self) -> set[str]:
        if self._families is None:
            fams: set[str] = set()
            for line in _run(["fc-list", ":", "family"]).splitlines():
                for name in line.split(","):
                    if name.strip():
                        fams.add(name.strip().lower())
            self._families = fams
        return self._families

    def _match(self, pattern: str) -> tuple[FontFace, str]:
        out = _run(["fc-match", "-f", _FMT, pattern]).strip()
        path, index, family, _style = out.split(_SEP, 3)
        names = [n.strip() for n in family.split(",") if n.strip()]
        face = FontFace(family=names[0] if names else family, path=Path(path), index=int(index or 0))
        return face, family.lower()

    # -- public API ---------------------------------------------------------
    def resolve(self, scribus_name: str) -> FontFace:
        if scribus_name in self._cache:
            return self._cache[scribus_name]
        words = scribus_name.split()
        known = self._known_families()
        face = None
        for n in range(len(words), 0, -1):
            family = " ".join(words[:n])
            if family.lower() not in known:
                continue
            style = " ".join(words[n:])
            pattern = f"{family}:style={style}" if style else family
            face, resolved = self._match(pattern)
            if family.lower() not in [r.strip() for r in resolved.split(",")]:
                self.log.append(f"font substitution: '{scribus_name}' -> {face.family}")
            break
        if face is None:
            # Unknown family: keep any trailing style words for the fallback.
            i = len(words)
            while i > 0 and words[i - 1].lower() in _STYLE_WORDS:
                i -= 1
            style = " ".join(words[i:]) or "Regular"
            face, _ = self._match(f"{self.fallback}:style={style}")
            self.log.append(f"font substitution: '{scribus_name}' -> {face.family} ({style})")
        if not self._usable(face):
            repaired = repair_font(face.path, face.index, self.cache_dir, self.log)
            if repaired is not None:
                face = FontFace(face.family, repaired, 0)
        if not self._usable(face):
            i = len(words)
            while i > 0 and words[i - 1].lower() in _STYLE_WORDS:
                i -= 1
            style = " ".join(words[i:]) or "Regular"
            bad = face.path
            face, _ = self._match(f"{self.fallback}:style={style}")
            self.log.append(f"unreadable font {bad} -> {face.family} ({style})")
        self._cache[scribus_name] = face
        return face

    def _usable(self, face: FontFace) -> bool:
        key = (face.path, face.index)
        if key not in self._verdicts:
            self._verdicts[key] = _readable(face.path, face.index)
        return self._verdicts[key]

    def fallback_for(self, char: str) -> FontFace:
        """A face that has a glyph for `char` (fontconfig charset match)."""
        if char not in self._char_cache:
            face, _ = self._match(f":charset={ord(char):x}")
            self._char_cache[char] = face
        return self._char_cache[char]
