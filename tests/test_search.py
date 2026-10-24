import gzip
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from provgrep import build, github, search, server, stage
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


def _index(tmp_path: Path) -> search.Index:
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


def test_terms_keep_identifiers_whole_with_their_family_and_parts() -> None:
    assert search.terms("XRenderCreatePicture(dpy)") == [
        *("xrendercreatepicture", "xrender", "xrendercreate"),
        *("x", "render", "create", "picture", "dpy"),
    ]
    assert search.terms("parse_rtp_header utf8 naïveBayes") == [
        *("parsertpheader", "parsertp", "parse", "rtp", "header"),
        *("utf8", "utf", "8"),
        *("naïvebayes", "naïve", "bayes"),
    ]
    # A typed identifier weighs more; its single letters go.
    assert search.query_terms("XRender fill") == ["xrender^3.0", "render", "fill"]


def test_search_filters_and_withholds_unlicensed_code(tmp_path: Path) -> None:
    index = _index(tmp_path)

    (only,) = index.search("create picture for a window").results
    assert (only.location.path, only.location.license.expression) == ("src/pic.c", "MIT")
    assert (only.copies, only.repos, only.licenses) == (2, 1, 2)
    assert only.language == "c"  # of the copy shown, not of the vendored one seen first
    assert only.code is not None
    assert "XRenderCreatePicture" in only.code
    assert only.location.license.notices == ["Copyright (c) 2020 Acme"]

    assert index.search("fill rectangle").results == []  # GPL is not permissive
    (gpl,) = index.search("fill rectangle", licenses=["GPL-2.0-or-later", "MIT"]).results
    assert gpl.location.path == "src/fill.c"

    found = index.search("fill rectangle", licenses="any").results
    unlicensed = next(r for r in found if r.location.repo == "acme/small")
    assert unlicensed.code is None  # no license found: a link, not the code
    assert unlicensed.location.url.endswith("/fill_copy.c#L1-L5")

    assert index.search("fill", licenses="any", levels=["A"]).total == 1
    (own,) = index.search("", licenses="any", require=["own_render_wait"]).results
    assert own.code is None  # a license, but not one that lets the code be shared
    assert own.location.license.expression == "LicenseRef-scancode-proprietary-license"
    # Whole identifiers: `wait` is not in code that has only `epoll_wait`.
    assert index.search("", licenses="any", require=["wait"]).total == 0
    assert index.search("", licenses="any", require=["epoll_wait|nothing"]).total == 1
    assert index.search("fill", licenses=["GPL-2.0-or-later ", " MIT"]).total == 1
    assert index.search("fill", licenses="any", languages=["go"]).total == 0


def test_snippet_lists_every_copy(tmp_path: Path) -> None:
    index = _index(tmp_path)
    first = index.search("picture").results[0]
    snippet = index.snippet(first.id)
    assert snippet is not None
    assert [c.path for c in snippet.copies] == ["third_party/x/pic.h", "src/pic.c"]
    assert [c.license.expression for c in snippet.copies] == ["BSD-3-Clause", "MIT"]
    assert index.snippet("0" * search.ID_LENGTH) is None


def test_api(tmp_path: Path) -> None:
    client = TestClient(server.app(_index(tmp_path)))
    found = client.get("/api/search", params={"q": "fill", "license": "any", "level": "X"}).json()
    assert (found["total"], found["results"][0]["code"]) == (1, None)
    gpl = client.get("/api/search", params={"q": "fill", "license": "GPL-2.0-or-later"}).json()
    assert gpl["results"][0]["location"]["license"]["expression"] == "GPL-2.0-or-later"
    picture = client.get("/api/search", params={"q": "picture"}).json()["results"][0]
    copies = client.get(f"/api/snippets/{picture['id']}").json()["copies"]
    assert len(copies) == 2
    assert client.get("/api/search", params={"q": "x", "page": "0"}).status_code == 400
    assert client.get("/api/snippets/nope").status_code == 404
