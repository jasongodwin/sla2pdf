import json

import pytest

from sla2pdf.variables import fill, load_values, normalise_values


def test_fill_known_and_unknown():
    log = []
    assert fill("Hello %VAR_name%, %VAR_age%!", {"name": "Alex"}, log) == "Hello Alex, !"
    assert log == ["unknown variable %VAR_age%"]


def test_normalise_values_types():
    assert normalise_values({"a": 1, "b": 2.5, "c": True, "d": None, "e": "x"}) == {
        "a": "1", "b": "2.5", "c": "true", "d": "", "e": "x"}


@pytest.mark.parametrize("raw", [[1, 2], {"bad key": 1}, {"nested": {"x": 1}}])
def test_normalise_values_refuses(raw):
    with pytest.raises(ValueError):
        normalise_values(raw)


def test_load_values(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"name": "Lucía"}), encoding="utf-8")
    assert load_values(p) == {"name": "Lucía"}
