from pathlib import Path

import pytest

from provgrep import search


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


def test_search_filters_and_withholds_unlicensed_code(index: search.Index) -> None:
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
    assert index.search("fill", licenses=search.licenses_of("Any"), languages=["C"]).total == 2
    with pytest.raises(ValueError, match="at most"):
        index.search("fill " * 500)


def test_snippet_lists_every_copy(index: search.Index) -> None:
    first = index.search("picture").results[0]
    snippet = index.snippet(first.id)
    assert snippet is not None
    assert [c.path for c in snippet.copies] == ["third_party/x/pic.h", "src/pic.c"]
    assert [c.license.expression for c in snippet.copies] == ["BSD-3-Clause", "MIT"]
    assert index.snippet("0" * search.ID_LENGTH) is None


def test_shards_page_as_one_index(
    index: search.Index, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search, "SHARD_SIZE", 1)  # a shard per snippet, up to MAX_SHARDS
    monkeypatch.setattr(search, "CHUNK", 1)
    sharded = tmp_path / "sharded"
    sharded.mkdir()
    for name in ("manifest.json", "records.sqlite"):
        (sharded / name).symlink_to(index.path / name)
    search.write_lexical(index.path / "records.sqlite", sharded / "lexical", jobs=2)
    split = search.Index(sharded)
    assert [s.num_docs for s in split._searchers] == [1, 1, 1, 1]
    query = "fill picture render"
    whole = split.search(query, licenses="any")
    assert whole.total == index.search(query, licenses="any").total == 4
    assert {r.id for r in whole.results} == {
        r.id for r in index.search(query, licenses="any").results
    }
    pages = [split.search(query, licenses="any", limit=1, offset=n).results for n in range(4)]
    assert [r.id for page in pages for r in page] == [r.id for r in whole.results]
