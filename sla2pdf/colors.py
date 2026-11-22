"""Colours the way Scribus converts them for PDF export.

CMYK swatches are converted to RGB with the naive formula
``r = (1 - c) * (1 - k)`` (and so on) unless an ICC profile is set with
`set_cmyk_profile` (or the ``SLA2PDF_CMYK_PROFILE`` environment variable): then they go
profile -> sRGB, relative colorimetric with black point compensation, which is what a
colour-managed Scribus does. No profile ships with this package.

Shading follows Scribus' ScColorEngine::getShadeColorRGB: CMYK colours are scaled on
their CMYK values and then converted; RGB colours are shaded in HSV
(s*k, v = 1-(1-v)*k).
"""
from __future__ import annotations

import colorsys
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageCms

_INTENT_RELATIVE = 1
_FLAG_BPC = 0x2000  # integer: older Pillow versions have no ImageCms.Flags

_profile: Path | None = None
_transform = None


def set_cmyk_profile(path: str | Path | None) -> None:
    """Use this CMYK ICC profile for CMYK -> RGB (None: the simple formula).
    Call it before loading documents: swatches are converted when read."""
    global _profile, _transform
    _profile = Path(path) if path else None
    if _profile is not None and not _profile.is_file():
        raise FileNotFoundError(f"ICC profile not found: {_profile}")
    _transform = None
    _convert.cache_clear()


def cmyk_profile() -> Path | None:
    return _profile


def _get_transform():
    global _transform
    if _transform is None:
        _transform = ImageCms.buildTransform(
            ImageCms.getOpenProfile(str(_profile)), ImageCms.createProfile("sRGB"),
            "CMYK", "RGB", renderingIntent=_INTENT_RELATIVE, flags=_FLAG_BPC)
    return _transform


@lru_cache(maxsize=None)
def _convert(c8: int, m8: int, y8: int, k8: int) -> tuple[float, float, float]:
    if _profile is None:
        c, m, y, k = (v / 255 for v in (c8, m8, y8, k8))
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    img = Image.new("CMYK", (1, 1), (c8, m8, y8, k8))
    r, g, b = ImageCms.applyTransform(img, _get_transform()).getpixel((0, 0))
    return (r / 255, g / 255, b / 255)


def cmyk_to_rgb(c: float, m: float, y: float, k: float) -> tuple[float, float, float]:
    """CMYK 0..1 -> RGB 0..1 (cached per colour)."""
    return _convert(*(round(min(max(v, 0.0), 1.0) * 255) for v in (c, m, y, k)))


def shade_cmyk(cmyk: tuple, shade: float) -> tuple:
    """Scribus shades CMYK by scaling every channel (shade in percent)."""
    k = shade / 100.0
    return tuple(v * k for v in cmyk)


def shade_rgb(rgb: tuple, shade: float) -> tuple:
    """Scribus shades RGB in HSV: saturation scaled, value moved towards white."""
    k = shade / 100.0
    h, s, v = colorsys.rgb_to_hsv(*rgb)
    return colorsys.hsv_to_rgb(h, s * k, 1.0 - (1.0 - v) * k)


class Swatch(tuple):
    """An RGB (0..1) colour that remembers its CMYK source, so shading starts from the
    swatch's own colour space. Compares and unpacks as a plain RGB tuple."""
    cmyk: tuple | None = None

    @classmethod
    def from_cmyk(cls, c: float, m: float, y: float, k: float) -> "Swatch":
        sw = cls(cmyk_to_rgb(c, m, y, k))
        sw.cmyk = (c, m, y, k)
        return sw


def shade(color: tuple, shade_percent: float) -> tuple:
    """Shade a colour like Scribus; CMYK swatches (Swatch.cmyk set) shade in CMYK."""
    cmyk = getattr(color, "cmyk", None)
    if cmyk is not None:
        return cmyk_to_rgb(*shade_cmyk(cmyk, shade_percent))
    return shade_rgb(color, shade_percent)


if os.environ.get("SLA2PDF_CMYK_PROFILE"):
    set_cmyk_profile(os.environ["SLA2PDF_CMYK_PROFILE"])
