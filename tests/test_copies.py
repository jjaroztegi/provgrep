import json
from pathlib import Path

from provgrep import search
from provgrep.copies import study


def test_counts(index: search.Index, tmp_path: Path) -> None:
    counts = json.loads((study(index.path, tmp_path / "results") / "counts.json").read_text())
    assert counts["snippets"] == 4
    assert counts["in more than one place"] == 1  # the picture code and its vendored copy
    assert counts["under more than one license"] == 1  # MIT here, BSD-3-Clause vendored
    assert counts["in more than one repository"] == 0
