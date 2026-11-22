"""Read a Scribus (.sla) document into plain dataclasses."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Union

from .colors import Swatch


@dataclass
class Page:
    index: int
    x: float
    y: float
    width: float
    height: float


@dataclass
class Layer:
    number: int
    name: str
    level: int
    printable: bool
    visible: bool


@dataclass
class Run:
    text: str
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class ParaEnd:
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class LineBreak:
    pass


@dataclass
class Tab:
    pass


@dataclass
class ZeroWidthSpace:
    pass


StoryElement = Union[Run, ParaEnd, LineBreak, Tab, ZeroWidthSpace]


@dataclass
class Item:
    kind: Literal["image", "text", "shape"]
    item_id: str
    page: int
    x: float
    y: float
    width: float
    height: float
    rotation: float
    flip_h: bool
    flip_v: bool
    layer: int
    path: str
    contour_path: str
    text_flow: int
    fill: str | None
    fill_shade: float
    transparency: float  # fill see-through 0..1 (Scribus TransValue)
    image_file: str | None
    image_scale: tuple[float, float]
    image_offset: tuple[float, float]
    padding: tuple[float, float, float, float]
    columns: int
    story: list[StoryElement]
    default_style: dict[str, str]
    # Scribus FLOP: 0 real glyph height, 1 font ascent, 2 line spacing, 3 baseline grid
    first_line_offset: int = 0
    # SCALETYPE="0": the picture is scaled to the frame when loaded (Scribus
    # adjustPictScale), whatever its pixels; RATIO="1" keeps its proportions then.
    image_fit_frame: bool = False
    image_keep_ratio: bool = False


@dataclass
class Document:
    page_width: float
    page_height: float
    pages: list[Page]
    layers: dict[int, Layer]
    items: list[Item]
    colors: dict[str, tuple[float, float, float]]
    para_styles: dict[str, dict]
    char_styles: dict[str, dict]
    path: Path


def _f(el: ET.Element, name: str, default: float = 0.0) -> float:
    v = el.get(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _i(el: ET.Element, name: str, default: int = 0) -> int:
    return int(_f(el, name, default))


def _parse_color(el: ET.Element) -> tuple[float, float, float]:
    """RGB 0..1; CMYK swatches are colour-managed (see colors.py) and keep their CMYK."""
    hexval = el.get("RGB")
    if hexval and hexval.startswith("#") and len(hexval) == 7:
        return tuple(int(hexval[i:i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]
    if el.get("SPACE") == "CMYK" or el.get("C") is not None:
        c, m, y, k = (_f(el, n) / 100 for n in "CMYK")
        return Swatch.from_cmyk(c, m, y, k)
    return (_f(el, "R") / 255, _f(el, "G") / 255, _f(el, "B") / 255)


def _parse_story(obj: ET.Element) -> tuple[list[StoryElement], dict[str, str]]:
    story: list[StoryElement] = []
    default_style: dict[str, str] = {}
    st = obj.find("StoryText")
    if st is None:
        return story, default_style
    for ch in st:
        if ch.tag == "DefaultStyle":
            default_style = dict(ch.attrib)
        elif ch.tag == "ITEXT":
            attrs = dict(ch.attrib)
            text = attrs.pop("CH", "")
            story.append(Run(text, attrs))
        elif ch.tag in ("para", "trail"):
            story.append(ParaEnd(dict(ch.attrib)))
        elif ch.tag == "breakline":
            story.append(LineBreak())
        elif ch.tag == "tab":
            story.append(Tab())
        elif ch.tag == "zwspace":
            story.append(ZeroWidthSpace())
    return story, default_style


def _parse_item(o: ET.Element) -> Item:
    ptype = o.get("PTYPE")
    kind = "image" if ptype == "2" else "text" if ptype == "4" else "shape"
    story, default_style = _parse_story(o)
    fill = o.get("PCOLOR")
    return Item(
        kind=kind,
        item_id=o.get("ItemID", ""),
        page=_i(o, "OwnPage", -1),
        x=_f(o, "XPOS"),
        y=_f(o, "YPOS"),
        width=_f(o, "WIDTH"),
        height=_f(o, "HEIGHT"),
        rotation=_f(o, "ROT"),
        flip_h=o.get("FLIPPEDH", "0") == "1",
        flip_v=o.get("FLIPPEDV", "0") == "1",
        layer=_i(o, "LAYER"),
        path=o.get("path", ""),
        contour_path=o.get("copath", ""),
        text_flow=_i(o, "TEXTFLOWMODE"),
        fill=fill,
        fill_shade=_f(o, "SHADE", 100.0),
        transparency=_f(o, "TransValue"),
        image_file=o.get("PFILE") or None,
        image_scale=(_f(o, "LOCALSCX", 1.0), _f(o, "LOCALSCY", 1.0)),
        image_offset=(_f(o, "LOCALX"), _f(o, "LOCALY")),
        padding=(_f(o, "EXTRA"), _f(o, "TEXTRA"), _f(o, "BEXTRA"), _f(o, "REXTRA")),
        columns=_i(o, "COLUMNS", 1),
        story=story,
        default_style=default_style,
        first_line_offset=_i(o, "FLOP", 0),
        image_fit_frame=_i(o, "SCALETYPE", 1) == 0,
        image_keep_ratio=_i(o, "RATIO", 0) == 1,
    )


def load_document(path: str | Path) -> Document:
    path = Path(path)
    root = ET.parse(path).getroot()
    d = root.find("DOCUMENT")
    if d is None:
        raise ValueError(f"{path}: no <DOCUMENT> element")

    pages = sorted(
        (
            Page(
                index=_i(p, "NUM"),
                x=_f(p, "PAGEXPOS"),
                y=_f(p, "PAGEYPOS"),
                width=_f(p, "PAGEWIDTH"),
                height=_f(p, "PAGEHEIGHT"),
            )
            for p in d.findall("PAGE")
        ),
        key=lambda p: p.index,
    )
    layers = {
        _i(l, "NUMMER"): Layer(
            number=_i(l, "NUMMER"),
            name=l.get("NAME", ""),
            level=_i(l, "LEVEL"),
            printable=l.get("DRUCKEN", "1") == "1",
            visible=l.get("SICHTBAR", "1") == "1",
        )
        for l in d.findall("LAYERS")
    }
    colors = {c.get("NAME", ""): _parse_color(c) for c in d.findall("COLOR")}
    para_styles = {s.get("NAME", ""): dict(s.attrib) for s in d.findall("STYLE")}
    char_styles = {s.get("CNAME", ""): dict(s.attrib) for s in d.findall("CHARSTYLE")}
    items = [_parse_item(o) for o in d.findall("PAGEOBJECT")]
    return Document(
        page_width=_f(d, "PAGEWIDTH"),
        page_height=_f(d, "PAGEHEIGHT"),
        pages=pages,
        layers=layers,
        items=items,
        colors=colors,
        para_styles=para_styles,
        char_styles=char_styles,
        path=path,
    )
