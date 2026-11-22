"""Lay out the story of one Scribus text frame into lines of shaped pieces.

Coordinates are frame-local pt (origin at the frame's top-left, x right, y down,
before the frame's rotation), so a rotated frame is laid out as if upright and the
drawing applies the rotation once. Text that does not fit is never dropped: it keeps
going below the frame at full frame width and the layout says `overflow=True`.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field, replace

from .fonts import FontResolver
from .geometry import free_segments, frame_obstacles, item_polygon, obstacle_polygon, to_frame_local
from .shaping import Shaped, font_metrics, has_blank_glyph, has_glyph, has_visible_glyph, ink_extent, shape, text_direction
from .sla import Document, Item, LineBreak, ParaEnd, Run, Tab, ZeroWidthSpace
from .styles import Style, StyleResolver
from .variables import fill

__all__ = ["Style", "Piece", "Line", "FrameLayout", "layout_frame", "fit_frame"]

TAB_WIDTH = 36.0
EPS = 0.01
MIN_SCALE, SCALE_STEP = 0.80, 0.02
_NEWLINE = re.compile(r"\r\n|\r|\n")
_HYPHENS = "-‐"
_NO_BREAK_SPACES = "   "


@dataclass
class Piece:
    text: str  # logical order
    style: Style
    x: float  # left edge, frame-local pt
    shaped: Shaped  # advances already include the style's horizontal scale


@dataclass
class Line:
    baseline: float
    x: float  # left edge of the text
    width: float  # text width, trailing spaces not counted
    pieces: list[Piece]  # left to right


@dataclass
class FrameLayout:
    item: Item
    lines: list[Line]
    overflow: bool
    scale: float


# --- words -------------------------------------------------------------------

@dataclass
class _Frag:
    text: str
    style: Style
    width: float = 0.0
    ink_ascent: float = 0.0  # tallest real glyph above the baseline, baseline shift included
    ink_descent: float = 0.0  # deepest real glyph below the baseline


def _make_frag(text: str, style: Style, direction: str) -> _Frag:
    frag = _Frag(text, style)
    _measure_frag(frag, direction)
    return frag


def _measure_frag(frag: _Frag, direction: str) -> None:
    shaped = _shape(frag.text, frag.style, direction)
    frag.width = shaped.width
    asc, desc = ink_extent(frag.style.font, frag.style.size, shaped.glyph_ids)
    if asc or desc:
        shift = frag.style.baseline_shift
        asc, desc = asc + shift, desc - shift
    frag.ink_ascent, frag.ink_descent = asc, desc


@dataclass
class _Word:
    kind: str = "word"  # "word", "tab" or "br"
    style: Style | None = None  # tab / br: the style that sizes an otherwise empty line
    parts: list[_Frag] = field(default_factory=list)
    spaces: list[_Frag] = field(default_factory=list)
    break_after: bool = False  # ends in a hyphen
    direction: str = "ltr"

    @property
    def width(self) -> float:
        return sum(f.width for f in self.parts)

    @property
    def space_width(self) -> float:
        return sum(f.width for f in self.spaces)

    def frags(self):
        return [*self.parts, *self.spaces] if self.kind == "word" else []


def _is_space(ch: str) -> bool:
    return ch not in _NO_BREAK_SPACES and (ch == " " or unicodedata.category(ch) == "Zs")


def _append(frags: list[_Frag], ch: str, style: Style) -> None:
    if frags and frags[-1].style is style:
        frags[-1].text += ch
    else:
        frags.append(_Frag(ch, style))


class _WordBuilder:
    """Splits a paragraph's text into words: break after spaces, after a hyphen
    that follows a letter, and at zero-width spaces, tabs and line breaks."""

    def __init__(self):
        self.words: list[_Word] = []
        self.cur: _Word | None = None
        self.force_new = False

    def text(self, text: str, style: Style) -> None:
        for ch in text:
            if _is_space(ch) and self.cur is not None and self.cur.parts:
                _append(self.cur.spaces, ch, style)
                continue
            if (self.cur is None or self.cur.spaces or self.cur.break_after or self.force_new):
                self.cur = _Word()
                self.words.append(self.cur)
                self.force_new = False
            prev = self.cur.parts[-1].text[-1] if self.cur.parts else ""
            _append(self.cur.parts, ch, style)
            if ch in _HYPHENS and prev.isalpha():
                self.cur.break_after = True

    def mark(self, kind: str, style: Style) -> None:
        self.words.append(_Word(kind=kind, style=style))
        self.cur = None

    def break_here(self) -> None:
        self.force_new = True


@dataclass
class _Para:
    words: list[_Word]
    style: Style  # paragraph settings + the style of an empty line
    direction: str


# --- the layout ----------------------------------------------------------------

@dataclass
class _Metrics:
    """Heights of one line. Font values come from the fonts' ascent/descent, ink values
    from the real glyph outlines on the line (0 when it has no visible glyphs)."""
    font_ascent: float
    font_descent: float
    ink_ascent: float
    ink_descent: float

    def covers(self, other: "_Metrics") -> bool:
        return all(b <= a + EPS for a, b in zip(self._values(), other._values()))

    def grow(self, other: "_Metrics") -> "_Metrics":
        return _Metrics(*(max(a, b) for a, b in zip(self._values(), other._values())))

    def _values(self):
        return (self.font_ascent, self.font_descent, self.ink_ascent, self.ink_descent)


class _FrameLayouter:
    def __init__(self, doc: Document, item: Item, values: dict[str, str], fonts: FontResolver,
                 obstacles: list[Item] | None, scale: float, log: list[str]):
        self.item, self.values, self.fonts, self.log = item, values, fonts, log
        self.styles = StyleResolver(doc, item, fonts, scale, self._log_once)
        self._fallback_styles: dict[tuple, Style] = {}
        self.poly = to_frame_local(item, item_polygon(item))
        if obstacles is None:
            canvas_obstacles = frame_obstacles(doc, item)
        else:
            canvas_obstacles = [p for p in (obstacle_polygon(o) for o in obstacles) if p]
        self.obstacles = [to_frame_local(item, p) for p in canvas_obstacles]
        left, top, bottom, right = item.padding
        self.left_pad, self.top_pad, self.right_pad = left, top, right
        xs, ys = [p[0] for p in self.poly], [p[1] for p in self.poly]
        self.column = (min(xs) + left, max(min(xs) + left + 1.0, max(xs) - right))
        self.bottom = max(ys) - bottom
        if item.columns > 1:
            self._log_once(f"item {item.item_id}: {item.columns} columns laid out as one")
        self.lines: list[Line] = []
        self.prev_baseline: float | None = None
        self.prev_row_bottom = 0.0  # previous baseline + its font descent (Scribus lastLineY)
        self.max_ink_descent = 0.0  # deepest real glyph descent so far (Scribus realDesc)
        self.overflow = False

    def _log_once(self, msg: str) -> None:
        if msg not in self.log:
            self.log.append(msg)

    # -- story -> paragraphs ----------------------------------------------------
    def paragraphs(self) -> list[_Para]:
        story = self.item.story
        if not story:
            return []
        paras, chunk = [], []
        for el in story:
            if isinstance(el, ParaEnd):
                paras.append((chunk, el.attrs))
                chunk = []
            else:
                chunk.append(el)
        # Scribus writes the last paragraph's style as <trail>: style only, no break.
        # So a story ending in ParaEnd has no empty paragraph after it.
        if chunk or not isinstance(story[-1], ParaEnd):
            paras.append((chunk, {}))
        out = []
        last_run: dict = {}  # an empty paragraph mark takes the previous run's style
        for elements, para_attrs in paras:
            builder = _WordBuilder()
            texts = []
            for el in elements:
                if isinstance(el, Run):
                    last_run = el.attrs
                    style = self.styles.resolve(el.attrs, para_attrs)
                    text = self._fill(el.text)
                    if style.allcaps:
                        text = text.upper()
                    # a newline in a filled value (text typed on two lines) is a line
                    # break, as <breakline/>; HarfBuzz would give U+000A a .notdef, not a break
                    for i, line in enumerate(_NEWLINE.split(text)):
                        if i:
                            builder.mark("br", style)
                        texts.append(line)
                        for part, part_style in self._font_runs(line, style):
                            builder.text(part, part_style)
                elif isinstance(el, Tab):
                    builder.mark("tab", self.styles.resolve(last_run, para_attrs))
                elif isinstance(el, LineBreak):
                    builder.mark("br", self.styles.resolve(last_run, para_attrs))
                elif isinstance(el, ZeroWidthSpace):
                    builder.break_here()
            style = self.styles.resolve(last_run, para_attrs)
            direction = text_direction("".join(texts)) or "ltr"
            for w in builder.words:
                self._measure(w, direction)
            out.append(_Para(builder.words, style, direction))
        return out

    def _fill(self, text: str) -> str:
        tmp: list[str] = []
        text = fill(text, self.values, tmp)
        for msg in tmp:
            self._log_once(msg)
        return text

    def _font_runs(self, text: str, style: Style) -> list[tuple[str, Style]]:
        """Split `text` so only the characters missing from the style's font use a
        fallback font. Spaces stay with the text before them (they draw nothing), so
        a fully substituted word run does not alternate fonts at every space."""
        out: list[tuple[str, Style]] = []
        for ch in text:
            use = style
            if _is_space(ch):
                if out and has_glyph(out[-1][1].font, ch):
                    use = out[-1][1]
            elif not (unicodedata.category(ch) in ("Cc", "Cf") or has_visible_glyph(style.font, ch)):
                use = self._fallback(ch, style)
            if out and out[-1][1] is use:
                out[-1] = (out[-1][0] + ch, use)
            else:
                out.append((ch, use))
        return out

    def _fallback(self, ch: str, style: Style) -> Style:
        fb = self.fonts.fallback_for(ch)
        code = f"U+{ord(ch):04X}"
        if not has_visible_glyph(fb, ch):
            self._log_once(f"no font has {code}")
            return style
        have, use = style.font.family, fb.family
        if have == use:  # e.g. a bold face without Arabic falling back to the regular one
            have, use = f"{have} ({style.font.path.name})", f"{use} ({fb.path.name})"
        kind = "blank" if has_blank_glyph(style.font, ch) else "missing"
        if kind == "blank" and have == style.font.family:
            have = f"{have} ({style.font.path.name})"
        self._log_once(f"{kind} glyph {code} in {have} → {use}")
        key = (id(style), str(fb.path), fb.index)
        if key not in self._fallback_styles:
            self._fallback_styles[key] = replace(style, font=fb)
        return self._fallback_styles[key]

    def _measure(self, word: _Word, para_dir: str) -> None:
        text = "".join(f.text for f in word.parts)
        direction = text_direction(text)
        if direction is None:
            digits = any(unicodedata.bidirectional(ch) in ("EN", "AN") for ch in text)
            direction = "ltr" if digits else para_dir
        word.direction = direction
        for f in word.frags():
            _measure_frag(f, direction)

    # -- lines ----------------------------------------------------------------------
    @staticmethod
    def _metrics(styles: list[Style], frags: list[_Frag]) -> _Metrics:
        """Scribus's line ascent is the font ascent (plus baseline shift) of the tallest
        style; the line descent is that same style's font descent."""
        font_asc = font_desc = 0.0
        for st in styles:
            m = font_metrics(st.font, st.size)
            if m.ascent + st.baseline_shift > font_asc or not font_asc:
                font_asc, font_desc = m.ascent + st.baseline_shift, m.descent
        ink_asc = max([f.ink_ascent for f in frags] or [0.0])
        ink_desc = max([f.ink_descent for f in frags] or [0.0])
        return _Metrics(font_asc, font_desc, ink_asc, ink_desc)

    def _ink_bottom(self, baseline: float, m: _Metrics) -> float | None:
        """Lowest real glyph edge Scribus checks against the frame bottom: it keeps
        realDesc for the whole frame, so the deepest glyph of any line so far counts.
        None for a line with no visible glyphs."""
        if not (m.ink_ascent or m.ink_descent):
            return None
        return baseline + max(m.ink_descent, self.max_ink_descent)

    def _segments(self, para: _Para, baseline: float, m: _Metrics) -> list[tuple[float, float]]:
        """Free space for the line. The band is the real glyphs' extent in automatic
        spacing (Scribus realAsce / realDesc) and the font ascent/descent in fixed
        spacing; the frame bottom is tested with the real glyphs in both modes
        (isEndOfCol(realDesc))."""
        ink_bottom = self._ink_bottom(baseline, m)
        if para.style.line_spacing_mode != 0 and ink_bottom is not None:
            top, bottom = baseline - m.ink_ascent, ink_bottom
        else:
            top, bottom = baseline - m.font_ascent, baseline + m.font_descent
        lowest = ink_bottom if ink_bottom is not None else bottom
        if self.overflow or lowest > self.bottom + EPS:
            self.overflow = True
            return [self.column]
        # the font descent may reach past the frame bottom; that part does not need room
        bottom = max(top + EPS, min(bottom, self.bottom))
        return free_segments(self.poly, self.obstacles, top, bottom,
                             self.left_pad, self.right_pad)

    def _first_baseline(self, para: _Para, m: _Metrics) -> float:
        flop = self.item.first_line_offset
        if flop == 0:  # FLOPRealGlyphHeight (font ascent for an empty first line)
            offset = m.ink_ascent or m.font_ascent
        elif flop == 2:  # FLOPLineSpacing
            offset = para.style.line_spacing
        else:  # 1 FLOPFontAscent; 3 baseline grid is not supported
            if flop != 1:
                self._log_once(f"item {self.item.item_id}: first line offset {flop} "
                               "laid out as font ascent")
            offset = m.font_ascent
        # a glyph taller than the offset pushes the first line down (Scribus `diff`)
        return self.top_pad + max(offset, m.ink_ascent)

    def layout(self) -> None:
        prev_after = 0.0  # space after of the paragraph before (Scribus gapAfter)
        for n, para in enumerate(self.paragraphs()):
            # Scribus adds gapBefore above a paragraph's first line unless it is the
            # first line of the frame, and gapAfter of the paragraph before it
            gap = 0.0 if n == 0 else prev_after + para.style.space_before
            prev_after = para.style.space_after
            words = list(para.words)
            if not words:
                self._band(para, [_Word(kind="br", style=para.style)], 0, gap)
                continue
            i = 0
            while i < len(words):
                i = self._band(para, words, i, gap if i == 0 else 0.0)
                if i == len(words) and words[-1].kind == "br":
                    # a line break at the end of a paragraph leaves an empty line
                    self._band(para, [_Word(kind="br", style=words[-1].style)], 0)

    def _band(self, para: _Para, words: list[_Word], i: int, gap: float = 0.0) -> int:
        """Place one line band (all free segments at one baseline); returns the next word."""
        first = words[i]
        if first.kind == "word":
            est = self._metrics([f.style for f in first.frags()], first.frags())
        else:
            est = self._metrics([first.style], [])
        for _ in range(6):
            baseline, placed, nxt = self._try_band(para, words, i, est, gap)
            frags = [f for _x, w, _sp in (e for seg in placed for e in seg[1]) for f in w.frags()]
            if not frags or est.covers(self._metrics([f.style for f in frags], frags)):
                break
            est = est.grow(self._metrics([f.style for f in frags], frags))
        self._commit(para, baseline, placed)
        self.prev_baseline = baseline
        self.prev_row_bottom = baseline + est.font_descent
        if placed:
            self.max_ink_descent = max(self.max_ink_descent, est.ink_descent)
        return nxt

    def _try_band(self, para: _Para, words: list[_Word], i: int, m: _Metrics,
                  gap: float = 0.0):
        if self.prev_baseline is None:
            baseline = self._first_baseline(para, m)
        else:
            # paragraph space before / after is part of yPos before the clearance check
            baseline = self.prev_baseline + para.style.line_spacing + gap
            if para.style.line_spacing_mode != 0:
                # automatic spacing: glyphs must clear the previous line's descent
                baseline = max(baseline, self.prev_row_bottom + m.ink_ascent)
        if words[i].kind == "br":  # empty line: needs height, not room
            return baseline, [], i + 1
        while True:
            segs = self._segments(para, baseline, m)
            if segs:
                placed, nxt = self._fill_segments(segs, words, i)
                if nxt != i:
                    return baseline, placed, nxt
            baseline += 1.0  # no room on this band: move down like Scribus does

    def _fill_segments(self, segs, words: list[_Word], i: int):
        """Put words into the segments left to right. Each placed element is
        (logical x, word, trailing space counted)."""
        placed = []
        col_width = self.column[1] - self.column[0]
        for seg in segs:
            seg_w = seg[1] - seg[0]
            elems: list[list] = []
            pos = 0.0
            while i < len(words):
                w = words[i]
                if w.kind == "br":
                    if elems:
                        placed.append((seg, elems))
                    return placed, i + 1
                gap = elems[-1][1].space_width if elems and elems[-1][1].kind == "word" else 0.0
                if w.kind == "tab":
                    stop = (math.floor((pos + gap) / TAB_WIDTH + EPS) + 1) * TAB_WIDTH
                    if stop > seg_w + EPS and elems:
                        break
                    if elems:
                        elems[-1][2] = gap > 0
                    elems.append([stop, w, False])  # x = where the next word starts
                    pos, i = stop, i + 1
                    continue
                if pos + gap + w.width <= seg_w + EPS:
                    if elems:
                        elems[-1][2] = gap > 0
                    elems.append([pos + gap, w, False])
                    pos, i = pos + gap + w.width, i + 1
                    continue
                if elems:
                    break
                if w.width <= col_width + EPS:
                    break  # fits a wider place: try the next segment / band
                head, tail = _split_word(w, seg_w)
                words[i:i + 1] = [head, tail]
                elems.append([0.0, head, False])
                i += 1
                break
            if elems:
                placed.append((seg, elems))
            if i >= len(words):
                break
        return placed, i

    def _commit(self, para: _Para, baseline: float, placed) -> None:
        if not placed:
            self.lines.append(Line(baseline, self.column[0], 0.0, []))
            return
        for seg, elems in placed:
            self.lines.append(self._line(para, baseline, seg, elems))

    def _line(self, para: _Para, baseline: float, seg, elems) -> Line:
        width = max([x + w.width for x, w, _ in elems if w.kind == "word"] or [0.0])
        width = max(width, max([x for x, w, _ in elems if w.kind == "tab"] or [0.0]))
        align = para.style.align
        if para.direction == "rtl" and align == "L":
            align = "R"
        seg_w = seg[1] - seg[0]
        offset = {"L": 0.0, "C": (seg_w - width) / 2, "R": seg_w - width}[align]
        line_x = seg[0] + offset

        # The line's text in logical order, cut where the reading direction changes.
        logical: list[tuple[float, _Frag, str]] = []  # (logical x, frag, direction)
        frags = [(x, f) for x, w, with_space in elems if w.kind == "word"
                 for f in (w.parts + w.spaces if with_space else w.parts)]
        dirs = _char_directions("".join(f.text for _, f in frags), para.direction)
        k, prev_word_x, x = 0, None, 0.0
        for word_x, f in frags:
            if word_x != prev_word_x:
                x, prev_word_x = word_x, word_x
            start = 0
            for end in range(1, len(f.text) + 1):
                if end == len(f.text) or dirs[k + end] != dirs[k + start]:
                    text, d = f.text[start:end], dirs[k + start]
                    piece = _make_frag(text, f.style, d)
                    logical.append((x, piece, d))
                    x += piece.width
                    start = end
            k += len(f.text)

        # Visual order (a simplified Unicode bidi): mirror right-to-left paragraphs,
        # then put each run against the paragraph direction back in its own order.
        visual = [[lx if para.direction == "ltr" else width - lx - f.width, f, d]
                  for lx, f, d in logical]
        k = 0
        while k < len(visual):
            j = k
            while (j < len(visual) and visual[j][2] != para.direction
                   and (j == k or abs(logical[j - 1][0] + logical[j - 1][1].width
                                      - logical[j][0]) < EPS)):
                j += 1
            if j > k + 1:
                lo = min(v[0] for v in visual[k:j])
                hi = max(v[0] + v[1].width for v in visual[k:j])
                for v in visual[k:j]:
                    v[0] = lo + hi - v[0] - v[1].width
            k = max(j, k + 1)
        visual.sort(key=lambda v: v[0])
        return Line(baseline, line_x, width, _pieces(visual, line_x))


def _char_directions(text: str, para_dir: str) -> list[str]:
    """Reading direction of each character: letters by their script, digits left to
    right, everything else (spaces, punctuation, marks) follows the letters on both
    sides when they agree and the paragraph otherwise."""
    strong: list[str | None] = []
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        strong.append("rtl" if bidi in ("R", "AL") else "ltr" if bidi in ("L", "EN", "AN")
                      else None)
    following: list[str] = [para_dir] * len(text)
    last = para_dir
    for k in range(len(text) - 1, -1, -1):
        following[k] = last
        last = strong[k] or last
    out, prev = [], para_dir
    for k, d in enumerate(strong):
        if d is None:
            d = prev if prev == following[k] else para_dir
        else:
            prev = d
        out.append(d)
    return out


def _shape(text: str, style: Style, direction: str) -> Shaped:
    s = shape(text, style.font, style.size, direction)  # type: ignore[arg-type]
    if style.scale_h != 1.0:
        k = style.scale_h
        s.advances = [a * k for a in s.advances]
        s.offsets = [(dx * k, dy) for dx, dy in s.offsets]
        s.width *= k
    return s


def _pieces(frags, line_x: float) -> list[Piece]:
    """Merge neighbouring fragments with the same style and direction into pieces."""
    groups: list[list] = []
    for x, f, d in frags:
        g = groups[-1] if groups else None
        if g and g[1] == f.style and g[2] == d and abs(g[0] + g[4] - x) < EPS:
            g[3].append(f)
            g[4] += f.width
        else:
            groups.append([x, f.style, d, [f], f.width])
    out = []
    for x, style, d, fs, _w in groups:
        visual_text = [f.text for f in fs]
        text = "".join(reversed(visual_text) if d == "rtl" else visual_text)
        out.append(Piece(text, style, line_x + x, _shape(text, style, d)))
    return out


def _clusters(text: str) -> list[int]:
    """Indices where a character may be split off (not before a combining mark)."""
    return [k for k in range(1, len(text)) if not unicodedata.combining(text[k])
            and unicodedata.category(text[k]) not in ("Mn", "Mc", "Me")]


def _split_word(word: _Word, avail: float) -> tuple[_Word, _Word]:
    """Cut a word too long for any line: as many characters as fit (at least one)."""
    head = _Word(direction=word.direction)
    used = 0.0
    parts = list(word.parts)
    while parts:
        f = parts[0]
        if used + f.width <= avail + EPS:
            head.parts.append(f)
            used += f.width
            parts.pop(0)
            continue
        cut = 0
        for k in _clusters(f.text):
            if used + _shape(f.text[:k], f.style, word.direction).width > avail + EPS:
                break
            cut = k
        if cut == 0 and not head.parts:
            cut = (_clusters(f.text) or [len(f.text)])[0]
        if cut:
            a, b = f.text[:cut], f.text[cut:]
            head.parts.append(_make_frag(a, f.style, word.direction))
            if b:
                parts[0] = _make_frag(b, f.style, word.direction)
            else:
                parts.pop(0)
        break
    tail = _Word(parts=parts, spaces=word.spaces,
                 break_after=word.break_after, direction=word.direction)
    return head, tail


# --- public API ----------------------------------------------------------------

def layout_frame(doc: Document, item: Item, values: dict[str, str], fonts: FontResolver,
                 obstacles: list[Item] | None, scale: float = 1.0,
                 log: list[str] | None = None) -> FrameLayout:
    """Lay out `item`'s story at `scale` x its font sizes. `obstacles` are the items
    text flows around (None: work them out from the document)."""
    lay = _FrameLayouter(doc, item, values, fonts, obstacles, scale,
                         log if log is not None else [])
    lay.layout()
    return FrameLayout(item, lay.lines, lay.overflow, scale)


def fit_frame(doc: Document, item: Item, values: dict[str, str], fonts: FontResolver,
              obstacles: list[Item] | None, scale: float = 1.0,
              log: list[str] | None = None) -> FrameLayout:
    """`layout_frame`, shrinking the text in 2 % steps down to 80 % until it fits.
    Still too long at 80 %: that layout, overflowing below the frame."""
    log = log if log is not None else []
    where = f"{doc.path.stem} p{item.page} item{item.item_id}"
    steps = round((scale - MIN_SCALE) / SCALE_STEP)
    out = None
    for n in range(max(steps, 0) + 1):
        s = round(scale - n * SCALE_STEP, 2)
        out = layout_frame(doc, item, values, fonts, obstacles, s, log)
        if not out.overflow:
            break
    if out.scale < scale:
        log.append(f"shrink {where} {out.scale:.2f}")
    if out.overflow:
        log.append(f"overflow {where} at {out.scale:.2f}: text continues below the frame")
    return out
