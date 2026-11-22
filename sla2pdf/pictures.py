"""Find the picture file a frame asks for.

A frame's PFILE (with any %VAR_x% filled in) is looked up, in order:
  1. as written, relative to each picture folder (``images/photo.jpg``);
  2. by its file name alone in each picture folder.
The first file found is used. Absolute paths in a template are reduced to their file
name, so a template made on another machine still finds its pictures.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

from PIL import Image

from .variables import fill

_UMASK = os.umask(0)  # read once at import (setting it back right away), not per file:
os.umask(_UMASK)  # changing the umask while other threads make files is not safe


def write_atomically(out: Path, write: Callable[[Path], None]) -> None:
    """Make `out` by calling `write(tmp)` on a temp file of its own in the same folder,
    then renaming it into place. Several processes making the same file at once each
    write their own temp file; the last rename wins and every one of them is whole."""
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(name)
    try:
        write(tmp)
        os.chmod(tmp, 0o666 & ~_UMASK)  # like a normally created file, not mkstemp's 0600
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


class PictureResolver:
    """`picture_dirs` are searched in order (see the module docstring)."""

    def __init__(self, picture_dirs: list[Path], log: list[str],
                 cache_dir: Path | None = None):
        self.picture_dirs = [Path(d) for d in picture_dirs]
        self.log = log
        # converted font files are kept here (see render._font_file)
        self.cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "sla2pdf-cache"
        self._resolved: dict[str, Path | None] = {}
        self._pixels: dict[Path, tuple[int, int]] = {}

    def resolve(self, image_file: str, values: dict[str, str]) -> Path | None:
        """The file to embed for a frame's PFILE (variables filled), or None when it is
        in none of the picture folders (logged as a missing picture)."""
        wanted = fill(image_file, values, self.log)
        if wanted not in self._resolved:
            self._resolved[wanted] = self._find(wanted)
        return self._resolved[wanted]

    def pixel_size(self, path: Path) -> tuple[int, int]:
        if path not in self._pixels:
            with Image.open(path) as img:
                self._pixels[path] = img.size
        return self._pixels[path]

    def _candidates(self, wanted: str) -> list[Path]:
        win = PureWindowsPath(wanted)
        posix = PurePosixPath(wanted.replace("\\", "/"))
        name = posix.name
        out: list[Path] = []
        for d in self.picture_dirs:
            if not (posix.is_absolute() or win.drive):
                out.append(d / Path(*posix.parts) if posix.parts else d)
            out.append(d / name)
        return out

    def _find(self, wanted: str) -> Path | None:
        name = PurePosixPath(wanted.replace("\\", "/")).name
        found = next((p for p in self._candidates(wanted) if p.is_file()), None)
        if found is None:
            self.log.append(f"missing picture {name}")
            return None
        try:
            with Image.open(found) as img:
                self._pixels[found] = img.size
        except Exception as e:  # unreadable: left out like a missing picture, the rest goes on
            self.log.append(f"bad picture {name}: {e}")
            return None
        return found
