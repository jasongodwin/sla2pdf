"""Resolve the character + paragraph style of a run of text the way Scribus 1.5 does.

Lookup order for one attribute (first layer that has it wins):
  1. the run's own ITEXT attributes (character attributes only)
  2. the paragraph's attributes (the <para>/<trail> element ending the paragraph)
  3. if the paragraph names a PARENT: that style, then its PARENT chain;
     otherwise: the story's DefaultStyle, then its PARENT chain
     (Scribus StyleContextProxy: an empty parent name means "the story default",
     a named parent is looked up in the document's styles, skipping the story default)
  4. the document's default paragraph style
  5. the document's default character style (character attributes only)
A layer with CPARENT pulls in that character style's chain right after itself.
FEATURES strings ("inherit shadowed") add up down the chain while they say "inherit",
so "inherit shadowed" takes its shadow offsets from wherever TXTSHX/TXTSHY are set
further down, normally the Default Character Style.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .colors import shade as shade_color
from .fonts import FontFace, FontResolver
from .shaping import font_metrics
from .sla import Document, Item

# Scribus "automatic" line spacing = the paragraph style font's height x AUTOL/100.
# Scribus' default AUTOL is 100; sla.py does not read the document's AUTOL setting.
AUTO_LINE_SPACING = 1.0

_ALIGN = {"0": "L", "1": "C", "2": "R"}  # 3/4 (justified) are laid out as left
_BLACK = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class Style:
    """Everything needed to measure and draw a run. Sizes are pt, already multiplied
    by the layout scale; colours are RGB 0..1."""
    font: FontFace
    size: float
    color: tuple | None  # FCOLOR before shading; None = no fill ("None" colour)
    shade: float  # FSHADE percent (100 = colour as is, 0 = white)
    outline: float  # outline stroke width in pt; 0 = no outline effect
    shadow: tuple[float, float] | None  # (dx, dy) pt, y down; None = no shadow effect
    stroke_color: tuple  # SCOLOR with SSHADE applied (outline and shadow colour)
    scale_h: float  # 1.0 = normal width
    baseline_shift: float  # pt, positive raises the text
    bg_color: tuple | None  # BGCOLOR with BGSHADE applied
    align: Literal["L", "C", "R"]
    line_spacing_mode: int  # 0 fixed, 1 automatic (2 baseline grid is treated as 1)
    line_spacing: float  # pt: LINESP (fixed) or the paragraph font's height x AUTOL (automatic)
    allcaps: bool = False
    space_before: float = 0.0  # pt, Scribus VOR (scaled like the font sizes)
    space_after: float = 0.0  # pt, Scribus NACH


def _tint(rgb: tuple | None, shade: float) -> tuple | None:
    if rgb is None:
        return None
    return shade_color(rgb, shade)


def _num(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


class StyleResolver:
    def __init__(self, doc: Document, item: Item, fonts: FontResolver, scale: float,
                 log_once):
        self.doc = doc
        self.item = item
        self.fonts = fonts
        self.scale = scale
        self._log = log_once
        self._cache: dict[tuple, Style] = {}
        self.default_para = self._default(doc.para_styles, "Default Paragraph Style")
        self.default_char = self._default(doc.char_styles, "Default Character Style")

    @staticmethod
    def _default(styles: dict[str, dict], name: str) -> dict:
        for s in styles.values():
            if s.get("DefaultStyle") == "1":
                return s
        return styles.get(name, {})

    # -- layer chains ---------------------------------------------------------
    def _chain(self, styles: dict[str, dict], name: str, parent_key: str) -> list[dict]:
        out, seen = [], set()
        while name and name in styles and name not in seen:
            seen.add(name)
            out.append(styles[name])
            name = styles[name].get(parent_key, "")
        if name and name not in styles:
            self._log(f"unknown style '{name}' in item {self.item.item_id}")
        return out

    def _para_layers(self, para_attrs: dict) -> list[dict]:
        parent = para_attrs.get("PARENT", "")
        if parent:
            rest = self._chain(self.doc.para_styles, parent, "PARENT")
        else:
            story = self.item.default_style
            rest = [story] + self._chain(self.doc.para_styles, story.get("PARENT", ""), "PARENT")
        return [para_attrs, *rest, self.default_para]

    def _with_char_parents(self, layers: list[dict]) -> list[dict]:
        out = []
        for layer in layers:
            out.append(layer)
            if layer.get("CPARENT"):
                out.extend(self._chain(self.doc.char_styles, layer["CPARENT"], "CPARENT"))
        return out

    # -- colours ----------------------------------------------------------------
    def _color(self, name: str | None) -> tuple | None:
        if name in (None, "", "None"):
            return None
        if name in self.doc.colors:
            return self.doc.colors[name]
        self._log(f"unknown colour '{name}' in item {self.item.item_id}, using black")
        return _BLACK

    # -- resolve ----------------------------------------------------------------
    def resolve(self, run_attrs: dict, para_attrs: dict) -> Style:
        key = (tuple(sorted(run_attrs.items())), tuple(sorted(para_attrs.items())))
        if key not in self._cache:
            self._cache[key] = self._build(run_attrs, para_attrs)
        return self._cache[key]

    def _build(self, run_attrs: dict, para_attrs: dict) -> Style:
        para = self._para_layers(para_attrs)
        char = self._with_char_parents([run_attrs, *para, self.default_char])

        def c(name, default=None):
            return next((l[name] for l in char if l.get(name, "") != ""), default)

        def p(name, default=None):
            return next((l[name] for l in para if l.get(name, "") != ""), default)

        # the paragraph style's own character settings (no run attributes)
        para_char = self._with_char_parents([*para, self.default_char])

        def pc(name, default=None):
            return next((l[name] for l in para_char if l.get(name, "") != ""), default)

        features: set[str] = set()
        for layer in char:
            if "FEATURES" in layer:
                words = layer["FEATURES"].split()
                features.update(w for w in words if w != "inherit")
                if "inherit" not in words:
                    break

        size = _num(c("FONTSIZE"), 12.0) * self.scale
        font = self.fonts.resolve(c("FONT", "DejaVu Sans Book"))
        mode = int(_num(p("LINESPMode"), 1))
        if mode == 0:
            spacing = _num(p("LINESP"), 15.0) * self.scale
        else:
            # Scribus calculateLineSpacing: the paragraph style's font, not the run's
            mode = 1
            para_font = self.fonts.resolve(pc("FONT", "DejaVu Sans Book"))
            para_size = _num(pc("FONTSIZE"), 12.0) * self.scale
            spacing = font_metrics(para_font, para_size).height * AUTO_LINE_SPACING
        shadow = None
        if "shadowed" in features:
            shadow = (size * _num(c("TXTSHX"), 5) / 100, -size * _num(c("TXTSHY"), -5) / 100)
        outline = size * _num(c("TXTOUT"), 1) / 100 if "outline" in features else 0.0
        return Style(
            font=font,
            size=size,
            color=self._color(c("FCOLOR", "Black")),
            shade=_num(c("FSHADE"), 100),
            outline=outline,
            shadow=shadow,
            stroke_color=_tint(self._color(c("SCOLOR", "Black")), _num(c("SSHADE"), 100)) or _BLACK,
            scale_h=_num(c("SCALEH"), 100) / 100,
            baseline_shift=size * _num(c("BASEO"), 0) / 100,
            bg_color=_tint(self._color(c("BGCOLOR")), _num(c("BGSHADE"), 100)),
            align=_ALIGN.get(p("ALIGN", "0"), "L"),
            line_spacing_mode=mode,
            line_spacing=spacing,
            allcaps="allcaps" in features,
            space_before=_num(p("VOR"), 0.0) * self.scale,
            space_after=_num(p("NACH"), 0.0) * self.scale,
        )
