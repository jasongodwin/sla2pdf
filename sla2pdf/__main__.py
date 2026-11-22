"""python -m sla2pdf template.sla --data data.json -o out.pdf"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from . import __version__
from .colors import set_cmyk_profile
from .document import render_document
from .variables import load_values


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sla2pdf", description="Render a Scribus .sla template to PDF, filling "
        "%%VAR_name%% placeholders from a flat JSON file.")
    ap.add_argument("template", type=Path, help="the .sla file")
    ap.add_argument("-o", "--output", type=Path, required=True, help="PDF to write")
    ap.add_argument("--data", type=Path, help="flat JSON object of variables")
    ap.add_argument("--pictures", type=Path, action="append", default=[], metavar="DIR",
                    help="folder searched for pictures before the template's own folder "
                         "(may be given more than once)")
    ap.add_argument("--fallback-font", default="DejaVu Sans",
                    help="font family used when a template font is not installed")
    ap.add_argument("--cmyk-profile", type=Path, metavar="ICC",
                    help="CMYK ICC profile for colour-managed CMYK -> RGB "
                         "(default: a simple formula)")
    ap.add_argument("--json", action="store_true",
                    help="print the result as one JSON line instead of plain text")
    ap.add_argument("--version", action="version", version=f"sla2pdf {__version__}")
    args = ap.parse_args(argv)
    try:
        if args.cmyk_profile:
            set_cmyk_profile(args.cmyk_profile)
        values = load_values(args.data) if args.data else {}
        # stray prints from libraries go to stderr: stdout carries only the result
        with contextlib.redirect_stdout(sys.stderr):
            result = render_document(args.template, values, args.output,
                                     picture_dirs=args.pictures,
                                     fallback_font=args.fallback_font)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        else:
            print(f"sla2pdf: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"pdf": str(result.pdf), "pages": result.pages,
                          "log": result.log, "seconds": result.seconds}))
    else:
        for line in result.log:
            print(f"  {line}", file=sys.stderr)
        print(f"{result.pdf}: {result.pages} page(s) in {result.seconds}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
