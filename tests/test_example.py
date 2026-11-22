"""End to end: the example template renders to a two-page PDF with its text in it."""
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from sla2pdf import load_values, render_document

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_example_renders(tmp_path):
    out = tmp_path / "postcard.pdf"
    res = render_document(EXAMPLES / "postcard.sla", load_values(EXAMPLES / "data.json"), out,
                          cache_dir=tmp_path / "cache")
    assert res.pages == 2
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    assert not any(line.startswith(("missing picture", "unknown variable", "overflow")) for line in res.log), res.log


def test_unknown_variable_is_logged(tmp_path):
    values = load_values(EXAMPLES / "data.json")
    values.pop("name")
    res = render_document(EXAMPLES / "postcard.sla", values, tmp_path / "o.pdf", cache_dir=tmp_path / "cache")
    assert "unknown variable %VAR_name%" in res.log


def test_only_some_pages(tmp_path):
    res = render_document(EXAMPLES / "postcard.sla", load_values(EXAMPLES / "data.json"), tmp_path / "o.pdf",
                          pages=[1], cache_dir=tmp_path / "cache")
    assert res.pages == 1
    with pytest.raises(ValueError):
        render_document(EXAMPLES / "postcard.sla", {}, tmp_path / "x.pdf", pages=[5], cache_dir=tmp_path / "cache")


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext (poppler) not installed")
def test_example_text(tmp_path):
    out = tmp_path / "postcard.pdf"
    render_document(EXAMPLES / "postcard.sla", load_values(EXAMPLES / "data.json"), out, cache_dir=tmp_path / "cache")
    text = subprocess.run(["pdftotext", str(out), "-"], capture_output=True, text=True).stdout
    assert "Hello, Alex!" in text


def test_cli(tmp_path):
    out = tmp_path / "cli.pdf"
    r = subprocess.run([sys.executable, "-m", "sla2pdf", str(EXAMPLES / "postcard.sla"),
                        "--data", str(EXAMPLES / "data.json"), "-o", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.exists() and out.stat().st_size > 1000
