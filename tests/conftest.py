"""An index built from staged files written here, without the network."""

import gzip
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from provgrep import build, github, search, stage
from provgrep.chunks import units

PICTURE = """static Picture window_picture(Display *dpy, Window w)
{
  XWindowAttributes wa;
  XGetWindowAttributes(dpy, w, &wa);
  XRenderPictFormat *fmt = XRenderFindVisualFormat(dpy, wa.visual);
  return XRenderCreatePicture(dpy, w, fmt, 0, NULL);
}
"""
OWN = """void own_render_wait(Display *dpy)
{
  XSync(dpy, False);
  epoll_wait(0, 0, 0, 0);
}
"""
FILL = """void fill(Display *dpy, Picture p)
{
  XRenderColor c = {0xffff, 0, 0, 0xffff};
  XRenderFillRectangle(dpy, PictOpSrc, p, &c, 0, 0, 10, 10);
}
"""


def _file(
    path: str,
    code: str,
    expression: str | None,
    level: str,
    vendored: bool = False,
    language: str = "c",
) -> dict[str, Any]:
    return {
        "path": path,
        "language": language,
        "vendored": vendored,
        "expression": expression,
        "level": level,
        "evidence": [{"kind": "header", "path": path, "expression": expression}]
        if expression
        else [],
        "conflict": False,
        "notices": ["Copyright (c) 2020 Acme"] if expression else [],
        "units": [asdict(u) for u in units(code, "c")],
    }


@pytest.fixture
def index(tmp_path: Path) -> search.Index:
    """An index of two repositories: a function with a vendored copy under another license, one
    under the GPL, one with no license found and one under a proprietary license."""
    repos = [
        github.Repo("acme/big", "a" * 40, "2026-01-01T00:00:00Z", 900, "C", "MIT"),
        github.Repo("acme/small", "b" * 40, "2026-01-01T00:00:00Z", 10, "C", None),
    ]
    files = {
        "acme/big": [
            # A vendored copy comes first; the location shown is the project's own one.
            _file("third_party/x/pic.h", PICTURE, "BSD-3-Clause", "C", True, "cpp"),
            _file("src/pic.c", PICTURE, "MIT", "B"),
            _file("src/fill.c", FILL, "GPL-2.0-or-later", "A"),
        ],
        "acme/small": [
            _file("fill_copy.c", FILL.replace("fill(", "fill2("), None, "X"),
            _file("own.c", OWN, "LicenseRef-scancode-proprietary-license", "A"),
        ],
    }
    staging = tmp_path / "staging"
    staging.mkdir()
    for repo in repos:
        with gzip.open(stage.path(staging, repo), "wt", encoding="utf-8") as f:
            f.writelines(json.dumps(line) + "\n" for line in [*files[repo.name], {"kinds": {}}])
    counts = build._records(repos, staging, tmp_path / "records.sqlite")
    assert counts["snippets"] == 4
    assert counts["snippets_whose_copies_differ_in_license"] == 1
    search.write_lexical(tmp_path / "records.sqlite", tmp_path / "lexical")
    (tmp_path / "manifest.json").write_text("{}")
    return search.Index(tmp_path)
