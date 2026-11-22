"""Render a whole Scribus document to one PDF file."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from fpdf import FPDF

from .fonts import FontResolver
from .pictures import PictureResolver, write_atomically
from .render import render_page
from .sla import load_document


@dataclass
class RenderResult:
    pdf: Path
    pages: int
    log: list[str] = field(default_factory=list)
    seconds: float = 0.0


def render_document(template: str | Path, values: dict[str, str], out_pdf: str | Path,
                    picture_dirs: list[str | Path] | None = None,
                    fallback_font: str = "DejaVu Sans",
                    cache_dir: str | Path | None = None,
                    pages: list[int] | None = None) -> RenderResult:
    """Fill `values` into the %VAR_name% placeholders of `template` (a .sla file) and
    write every page (or only `pages`, 0-based, in that order) to `out_pdf`.

    Pictures are looked up in `picture_dirs` first, then in the template's own folder.
    The returned log lists everything worth a look: font substitutions, missing or
    blank glyphs and the fallback fonts used, missing pictures, unknown variables,
    text shrunk to fit and text that still overflows its frame."""
    started = time.time()
    template = Path(template)
    log: list[str] = []
    doc = load_document(template)
    dirs = [Path(d) for d in (picture_dirs or [])] + [template.resolve().parent]
    pictures = PictureResolver(dirs, log, cache_dir=Path(cache_dir) if cache_dir else None)
    fonts = FontResolver(fallback=fallback_font, log=log, cache_dir=pictures.cache_dir / "fonts")
    order = list(range(len(doc.pages))) if pages is None else list(pages)
    for index in order:
        if not 0 <= index < len(doc.pages):
            raise ValueError(f"page {index} out of range (the document has {len(doc.pages)})")
    pdf = FPDF(unit="pt", format=(doc.page_width, doc.page_height))
    pdf.set_auto_page_break(False)
    for index in order:
        render_page(pdf, doc, index, values, fonts, pictures, log)
    out = Path(out_pdf)
    write_atomically(out, lambda tmp: pdf.output(str(tmp)))
    return RenderResult(pdf=out, pages=len(order), log=log,
                        seconds=round(time.time() - started, 2))
