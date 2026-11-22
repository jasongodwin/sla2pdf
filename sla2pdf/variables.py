"""Fill %VAR_name% placeholders from a plain dict of values."""
from __future__ import annotations

import json
import re
from pathlib import Path

_VAR = re.compile(r"%VAR_([A-Za-z0-9_]+)%")


def fill(text: str, values: dict[str, str], log: list[str]) -> str:
    """Replace every %VAR_key% in `text` by values[key]. An unknown key becomes the
    empty string (never the raw placeholder) and is reported in `log`."""
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        log.append(f"unknown variable %VAR_{key}%")
        return ""
    return _VAR.sub(repl, text)


def normalise_values(raw: dict) -> dict[str, str]:
    """A flat JSON object -> {str: str}. Numbers and booleans become text, null becomes
    ""; nested lists/objects are refused (the data must be flat)."""
    if not isinstance(raw, dict):
        raise ValueError("data must be a JSON object of name -> value")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]+", key):
            raise ValueError(f"bad variable name {key!r}: use letters, digits and _")
        if value is None:
            out[key] = ""
        elif isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            out[key] = str(value)
        else:
            raise ValueError(f"variable {key!r}: nested values are not supported")
    return out


def load_values(path: str | Path) -> dict[str, str]:
    """Read a flat JSON file of variables."""
    return normalise_values(json.loads(Path(path).read_text(encoding="utf-8")))
