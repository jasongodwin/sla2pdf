"""Draw one page of a Scribus document into an fpdf2 PDF.

Items are drawn by layer level, then in document order, skipping layers that do not
print and items on the pasteboard. An item is drawn on every page its box touches
(a spread picture lands on both pages); the PDF page box clips the rest.

Coordinates: the document's canvas is pt, y down; each page sits at its own
(x, y) on the canvas. Everything is written as raw PDF operators (y up) through
`_Painter`, which converts the y-down page coordinates, so frame transforms can be
stacked the way Scribus does: translate to the frame's top-left, rotate clockwise
about it, then mirror about the frame's centre.

Text is drawn glyph by glyph from the layout (glyph ids, advances and offsets come
from HarfBuzz in `layout.py`), through fpdf2's font subsetting, so what is printed is
exactly what was measured.
"""
from __future__ import annotations

import hashlib
import math
import tempfile
from functools import lru_cache
from pathlib import Path

import uharfbuzz as hb
from fpdf import FPDF

from .fonts import FontFace, FontResolver
from .geometry import item_polygon
from .layout import FrameLayout, Piece, fit_frame
from .pictures import PictureResolver, write_atomically
from .shaping import _hb_font, font_metrics
from .sla import Document, Item, Page
from .styles import Style, _tint

Matrix = tuple[float, float, float, float, float, float]


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# --- PDF operators in y-down page coordinates --------------------------------------

class _Painter:
    def __init__(self, pdf: FPDF, page: Page):
        self.pdf = pdf
        self.page = page
        self.h = page.height

    def out(self, s: str) -> None:
        self.pdf._out(s)

    def y(self, v: float) -> float:
        return self.h - v

    def cm(self, m: Matrix) -> None:
        """Concatenate a y-down matrix (u' = a u + c v + e, v' = b u + d v + f)."""
        a, b, c, d, e, f = m
        H = self.h
        pdf_m = (a, -b, -c, d, c * H + e, H * (1 - d) - f)
        self.out(" ".join(_fmt(x) for x in pdf_m) + " cm")

    def path(self, points: list[tuple[float, float]]) -> str:
        ops = [f"{_fmt(points[0][0])} {_fmt(self.y(points[0][1]))} m"]
        ops += [f"{_fmt(x)} {_fmt(self.y(y))} l" for x, y in points[1:]]
        return " ".join(ops) + " h"

    def fill_color(self, rgb) -> str:
        return " ".join(_fmt(c) for c in rgb) + " rg"

    def stroke_color(self, rgb) -> str:
        return " ".join(_fmt(c) for c in rgb) + " RG"


def _frame_matrices(item: Item, page: Page) -> list[Matrix]:
    """Frame-local (y down) -> page: translate, rotate clockwise about the top-left,
    mirror about the frame's centre (applied to the content in the reverse order)."""
    a = math.radians(item.rotation)
    c, s = math.cos(a), math.sin(a)
    out: list[Matrix] = [(1, 0, 0, 1, item.x - page.x, item.y - page.y)]
    if abs(item.rotation) > 1e-9:
        out.append((c, s, -s, c, 0, 0))
    if item.flip_h:
        out.append((-1, 0, 0, 1, item.width, 0))
    if item.flip_v:
        out.append((1, 0, 0, -1, 0, item.height))
    return out


# --- fonts -------------------------------------------------------------------------

def _cff_to_truetype(font) -> None:
    """Replace CFF (PostScript) outlines by TrueType ones, keeping the glyph order.
    fpdf2 2.7.8 embeds every font as a TrueType font file; a CFF font embedded that
    way is a broken font for strict PDF readers."""
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import newTable

    order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet()
    glyf = newTable("glyf")
    glyf.glyphOrder = order
    glyf.glyphs = {}
    for name in order:
        pen = TTGlyphPen(glyph_set)
        glyph_set[name].draw(Cu2QuPen(pen, 1.0, reverse_direction=True))
        glyf.glyphs[name] = pen.glyph()
    font["glyf"] = glyf
    font["loca"] = newTable("loca")
    del font["CFF "]
    if "VORG" in font:
        del font["VORG"]
    font["head"].glyphDataFormat = 0
    maxp = font["maxp"]
    maxp.tableVersion = 0x00010000
    for attr in ("maxZones", "maxTwilightPoints", "maxStorage", "maxFunctionDefs",
                 "maxInstructionDefs", "maxStackElements", "maxSizeOfInstructions",
                 "maxComponentElements", "maxComponentDepth"):
        setattr(maxp, attr, 1 if attr == "maxZones" else 0)
    post = font["post"]
    post.formatType = 2.0
    post.extraNames = []
    post.mapping = {}
    post.glyphOrder = order
    font.sfntVersion = "\x00\x01\x00\x00"


@lru_cache(maxsize=None)
def _font_file(path: str, index: int, cache_dir: Path) -> Path:
    """A TrueType file fpdf2 can embed for face `index` of `path`. fpdf2 always reads
    face 0 of a collection and embeds outlines as TrueType, so other faces of a .ttc
    and fonts with CFF outlines are saved as their own TrueType file first (glyph ids
    stay the same, so the layout's glyphs still match). Those files are kept in
    `cache_dir`, named after the source file's path, size, date and face, and made
    once (atomically, so documents made at the same time can share them)."""
    from fontTools.ttLib import TTCollection, TTFont

    p = Path(path)
    collection = p.suffix.lower() in (".ttc", ".otc")
    font = TTCollection(str(p)).fonts[index] if collection else TTFont(str(p), lazy=True)
    if "CFF " not in font and (index == 0 or not collection):
        return p
    st = p.stat()
    key = hashlib.sha1(f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}|{index}".encode()).hexdigest()
    out = Path(cache_dir) / f"{p.stem}-{index}-{key[:16]}.ttf"
    if out.is_file():
        return out
    if "CFF " in font:
        _cff_to_truetype(font)
    write_atomically(out, lambda tmp: font.save(str(tmp)))
    return out


def _pdf_font(pdf: FPDF, face: FontFace):
    """The fpdf2 font object for `face`, added to this PDF the first time it is used."""
    registry: dict = pdf.__dict__.setdefault("_sla2pdf_fonts", {})
    key = (str(face.path), face.index)
    if key not in registry:
        family = f"slafont{len(registry)}"
        cache_dir = pdf.__dict__.get("_sla2pdf_font_cache") or Path(tempfile.gettempdir()) / "sla2pdf-fonts"
        pdf.add_font(family, "", str(_font_file(*key, Path(cache_dir))))
        registry[key] = pdf.fonts[family]
    return registry[key]


def _glyph_texts(piece: Piece) -> list[tuple[int, ...]]:
    """The characters behind each glyph of the piece (for copy / search in the PDF):
    each HarfBuzz cluster's text goes with its first glyph."""
    shaped, text, style = piece.shaped, piece.text, piece.style
    font = _hb_font(str(style.font.path), style.font.index)
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    buf.direction = shaped.direction
    hb.shape(font, buf)
    infos = buf.glyph_infos
    n = len(shaped.glyph_ids)
    if [i.codepoint for i in infos] != shaped.glyph_ids:
        return [tuple(map(ord, text))] + [()] * (n - 1)
    starts = sorted({i.cluster for i in infos})
    ends = {s: (starts[k + 1] if k + 1 < len(starts) else len(text)) for k, s in enumerate(starts)}
    seen: set[int] = set()
    out = []
    for info in infos:
        if info.cluster in seen:
            out.append(())
        else:
            seen.add(info.cluster)
            out.append(tuple(ord(ch) for ch in text[info.cluster:ends[info.cluster]]))
    return out


def _glyph_codes(pdf: FPDF, piece: Piece) -> list[int | None]:
    """fpdf2 subset codes for the piece's glyphs (None for .notdef: not drawn)."""
    font = _pdf_font(pdf, piece.style.font)
    hmtx = font.ttfont["hmtx"].metrics
    codes: list[int | None] = []
    for gid, chars in zip(piece.shaped.glyph_ids, _glyph_texts(piece)):
        if gid == 0:
            codes.append(None)
            continue
        name = font.ttfont.getGlyphName(gid)
        glyph = font.subset.get_glyph(glyph=gid, unicode=chars, glyph_name=name,
                                      glyph_width=round(font.scale * hmtx[name][0]))
        codes.append(font.subset.pick_glyph(glyph))
    return codes


# --- the drawing ----------------------------------------------------------------------

class _PageRenderer:
    def __init__(self, pdf: FPDF, doc: Document, page: Page, values: dict[str, str],
                 fonts: FontResolver, pictures: PictureResolver, log: list[str]):
        self.pdf, self.doc, self.page = pdf, doc, page
        self.values, self.fonts, self.pictures, self.log = values, fonts, pictures, log
        self.p = _Painter(pdf, page)
        self._layouts: dict[int, FrameLayout] = {}

    def _log_once(self, msg: str) -> None:
        if msg not in self.log:
            self.log.append(msg)

    def _color(self, name: str | None, shade: float, item: Item):
        if name in (None, "", "None"):
            return None
        rgb = self.doc.colors.get(name)
        if rgb is None:
            self._log_once(f"unknown colour '{name}' in item {item.item_id}, not filled")
            return None
        return _tint(rgb, shade)

    def _page_polygon(self, item: Item) -> list[tuple[float, float]]:
        return [(x - self.page.x, y - self.page.y) for x, y in item_polygon(item)]

    def _layout(self, item: Item) -> FrameLayout:
        """The frame's layout (after shrink-to-fit), made once per page."""
        if id(item) not in self._layouts:
            self._layouts[id(item)] = fit_frame(self.doc, item, self.values, self.fonts,
                                                None, log=self.log)
        return self._layouts[id(item)]

    def draw(self, item: Item) -> None:
        with self.pdf.local_context():
            self._fill(item)
            if item.kind == "image":
                self._image(item)
            elif item.kind == "text":
                self._text(item)

    def _fill(self, item: Item) -> None:
        """The item's fill, see-through by its TransValue: only the fill, as Scribus
        does (text and pictures stay opaque)."""
        rgb = self._color(item.fill, item.fill_shade, item)
        if rgb is None:
            return
        polygon = self._page_polygon(item)
        fill = f"{self.p.fill_color(rgb)} {self.p.path(polygon)} f"
        t = max(0.0, min(1.0, item.transparency))
        if t > 0:
            with self.pdf.local_context(fill_opacity=1 - t):
                self.p.out(fill)
        else:
            self.p.out(fill)

    # -- pictures -----------------------------------------------------------------
    def _image(self, item: Item) -> None:
        if not item.image_file:
            return
        path = self.pictures.resolve(item.image_file, self.values)
        if path is None:
            return
        x, y, w, h = _picture_box(item, self.pictures.pixel_size(path))
        k = self.pdf.k
        self.p.out(f"q {self.p.path(self._page_polygon(item))} W n")
        for m in _frame_matrices(item, self.page):
            self.p.cm(m)
        # fpdf flips image y with its own page height; the painter flips with the
        # template page's. They differ when a bigger output page is asked for.
        y += self.pdf.h * k - self.p.h
        self.pdf.image(str(path), x=x / k, y=y / k, w=w / k, h=h / k)
        self.p.out("Q")

    # -- text -----------------------------------------------------------------------
    def _text(self, item: Item) -> None:
        if not item.story:
            return
        layout = self._layout(item)
        self.p.out("q")
        for m in _frame_matrices(item, self.page):
            self.p.cm(m)
        self._backgrounds(layout)
        for line in layout.lines:
            for piece in line.pieces:
                self._piece(piece, line.baseline)
        self.p.out("Q")

    def _backgrounds(self, layout: FrameLayout) -> None:
        for line in layout.lines:
            for piece in line.pieces:
                st = piece.style
                if st.bg_color is None or not piece.shaped.width:
                    continue
                fm = font_metrics(st.font, st.size)
                top = line.baseline - st.baseline_shift - fm.ascent
                self.p.out(f"{self.p.fill_color(st.bg_color)} {_fmt(piece.x)} "
                           f"{_fmt(self.p.y(top + fm.ascent + fm.descent))} "
                           f"{_fmt(piece.shaped.width)} {_fmt(fm.ascent + fm.descent)} re f")

    def _piece(self, piece: Piece, baseline: float) -> None:
        st: Style = piece.style
        fill = _tint(st.color, st.shade)
        if fill is None and not st.outline and st.shadow is None:
            return
        codes = _glyph_codes(self.pdf, piece)
        y = baseline - st.baseline_shift
        if st.shadow is not None:
            dx, dy = st.shadow
            self._glyphs(piece, codes, piece.x + dx, y + dy,
                         f"0 Tr {self.p.fill_color(st.stroke_color)}")
        if st.outline > 0:
            mode = "2 Tr" if fill is not None else "1 Tr"
            state = (f"{mode} {_fmt(st.outline)} w 1 j {self.p.stroke_color(st.stroke_color)}"
                     + (f" {self.p.fill_color(fill)}" if fill is not None else ""))
            self._glyphs(piece, codes, piece.x, y, state)
        elif fill is not None:
            self._glyphs(piece, codes, piece.x, y, f"0 Tr {self.p.fill_color(fill)}")

    def _glyphs(self, piece: Piece, codes: list[int | None], x: float, y: float,
                state: str) -> None:
        st = piece.style
        font = _pdf_font(self.pdf, st.font)
        ops = [f"q {state} BT /F{font.i} {_fmt(st.size)} Tf"]
        if st.scale_h != 1.0:
            ops.append(f"{_fmt(st.scale_h * 100)} Tz")
        pen = x
        offsets = piece.shaped.offsets or [(0.0, 0.0)] * len(codes)
        for code, adv, (dx, dy) in zip(codes, piece.shaped.advances, offsets):
            if code is not None:
                ops.append(f"1 0 0 1 {_fmt(pen + dx)} {_fmt(self.p.y(y) + dy)} Tm <{code:04X}> Tj")
            pen += adv
        ops.append("ET Q")
        self.p.out(" ".join(ops))


def _picture_box(item: Item, pixels: tuple[int, int]) -> tuple[float, float, float, float]:
    """Frame-local (x, y, w, h) of the picture, as Scribus draws it: scale x its own
    pixels (LOCALSCX already holds the 72/dpi from when it was placed), top-left at
    offset x scale. A frame set to scale its picture to the frame (SCALETYPE 0) gets
    its scale from the frame and the original's pixels when loaded (adjustPictScale);
    proportions kept with RATIO 1, the offsets in the file kept."""
    px_w, px_h = pixels
    sx, sy = item.image_scale
    if item.image_fit_frame and px_w and px_h:
        sx, sy = item.width / px_w, item.height / px_h
        if item.image_keep_ratio:
            sx = sy = min(sx, sy)
    ox, oy = item.image_offset
    return ox * sx, oy * sy, px_w * sx, px_h * sy


def _layer_order(doc: Document, item: Item) -> tuple[int, bool]:
    layer = doc.layers.get(item.layer)
    if layer is None:
        return item.layer, True
    return layer.level, layer.printable


def _touches(item: Item, left: float, top: float, right: float, bottom: float) -> bool:
    """Does the item's box overlap the canvas rectangle (left, top, right, bottom)?"""
    poly = item_polygon(item)
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return min(xs) < right and max(xs) > left and min(ys) < bottom and max(ys) > top


def render_page(pdf: FPDF, doc: Document, page_index: int, values: dict[str, str],
                fonts: FontResolver, pictures: PictureResolver, log: list[str],
                page_size: tuple[float, float] | None = None,
                page_offset: tuple[float, float] = (0.0, 0.0)) -> None:
    """Add page `page_index` of `doc` to `pdf` and draw everything that prints on it.

    `page_size` (pt) is an output page different from the template page, with the
    template page's top-left at `page_offset` (pt) on it; items are then drawn at their
    template coordinates + offset and only the output page clips them."""
    page = doc.pages[page_index]
    pdf.__dict__.setdefault("_sla2pdf_font_cache", pictures.cache_dir / "fonts")
    out_w, out_h = page_size or (page.width, page.height)
    off_x, off_y = page_offset
    pdf.add_page(format=(out_w / pdf.k, out_h / pdf.k))
    if page_size or off_x or off_y:
        # painter coordinates are y-down on the template page; move that origin onto the output page
        pdf._out(f"1 0 0 1 {_fmt(off_x)} {_fmt(out_h - off_y - page.height)} cm")
    items = []
    for n, item in enumerate(doc.items):
        level, printable = _layer_order(doc, item)
        if printable and item.page >= 0 and _touches(
                item, page.x - off_x, page.y - off_y,
                page.x - off_x + out_w, page.y - off_y + out_h):
            items.append((level, n, item))
    renderer = _PageRenderer(pdf, doc, page, values, fonts, pictures, log)
    for _level, _n, item in sorted(items, key=lambda t: (t[0], t[1])):
        renderer.draw(item)
