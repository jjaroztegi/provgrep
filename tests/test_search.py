import json
from compression import zstd
from pathlib import Path

import pytest

from provgrep import build, github, search


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
    assert search.query_terms("X11 window") == ["x11", "11", "window"]  # a name, not an API


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
    # A name of several (`io.open`) needs every one of them.
    assert index.search("", licenses="any", require=["XSync.epoll_wait"]).total == 1
    assert index.search("", licenses="any", require=["epoll_wait.nothing"]).total == 0
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


@pytest.mark.skipif(not Path("/proc/self/smaps").exists(), reason="Linux only")
def test_the_index_is_read_a_page_at_a_time(index: search.Index) -> None:
    advised, mapped = 0, 0
    current = ""
    for line in Path("/proc/self/smaps").read_text().splitlines():
        fields = line.split()
        if "-" in fields[0] and len(fields) >= 6:
            current = fields[5]
        elif fields[0] == "VmFlags:" and current.startswith(str(index.path.resolve())):
            mapped += 1
            advised += "rr" in fields  # MADV_RANDOM
    assert mapped
    assert advised == mapped


def test_partitions_page_as_one_index(
    index: search.Index, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build, "PARTITION_SIZE", 1)  # a partition per location
    out = tmp_path / "cut"
    out.mkdir()
    (out / "manifest.json").write_bytes((index.path / "manifest.json").read_bytes())
    repos = github.load(index.path.parent / "corpus.json")  # what the fixture staged
    assert build.write(repos, index.path.parent / "staging", out, jobs=2)["partitions"] == 5
    cut = search.Index(out)
    assert sum(s.num_docs for s in cut._searchers) == 4
    assert sum(s.num_docs > 0 for s in cut._searchers) > 1  # a search over several
    query = "fill picture render"
    whole = cut.search(query, licenses="any")
    assert whole.total == index.search(query, licenses="any").total == 4
    assert {r.id for r in whole.results} == {
        r.id for r in index.search(query, licenses="any").results
    }
    pages = [cut.search(query, licenses="any", limit=1, offset=n).results for n in range(4)]
    assert [r.id for page in pages for r in page] == [r.id for r in whole.results]
    for result in whole.results:  # found in the partition its id says
        found = cut.snippet(result.id)
        assert found is not None
        assert found.result.code == result.code


def test_buckets_cut_into_partitions() -> None:
    for partitions in (1, 3, 8, 255, 256):
        cut = [search.buckets(n, partitions) for n in range(partitions)]
        assert [b for r in cut for b in r] == list(range(search.BUCKETS))
        assert all(search.partition(b, partitions) == n for n, r in enumerate(cut) for b in r)


def test_search_for_a_project_keeps_what_fits_it(index: search.Index) -> None:
    # GPL-2.0-or-later code does not go into an MIT project; MIT code goes into a GPL one.
    assert index.search("fill", licenses="any", project="mit").total == 0
    found = index.search("fill picture", licenses="any", project="GPL-2.0-or-later").results
    assert sorted(r.location.license.expression or "" for r in found) == [
        "GPL-2.0-or-later",
        "MIT",
    ]
    verdict = found[0].compatibility
    assert verdict is not None
    assert (verdict.project, verdict.answer) == ("GPL-2.0-or-later", "yes")
    assert verdict.source.startswith("OSADL License Compatibility Matrix, 20")
    assert index.search("fill").results == []  # without a project, no verdict and no change
    with pytest.raises(ValueError, match="no license 'Proprietary'"):
        index.search("fill", project="Proprietary")


def test_a_snippet_lists_as_many_copies_as_asked(index: search.Index) -> None:
    picture = index.search("picture").results[0].id
    first = index.snippet(picture, copies=1)
    assert first is not None
    assert [c.path for c in first.copies] == ["third_party/x/pic.h"]
    assert first.result.copies == 2  # how many there are in all


def test_what_was_asked_to_be_removed_is_left_out(index: search.Index, tmp_path: Path) -> None:
    picture = index.search("picture").results[0].id
    asked = tmp_path / "removed.json"
    asked.write_text(
        json.dumps(
            {
                "removed": [
                    {"repo": "Acme/Small", "issue": "1"},
                    {"repo": "acme/big", "path": "src/fill.c/", "issue": "2"},
                    {"function": picture, "issue": "3"},
                ]
            }
        )
    )
    out = tmp_path / "left"
    out.mkdir()
    (out / "manifest.json").write_bytes((index.path / "manifest.json").read_bytes())
    repos = github.load(index.path.parent / "corpus.json")
    counts = build.write(repos, index.path.parent / "staging", out, 1, build.Removed.load(asked))
    assert counts["removed"] == {"files": 1, "functions": 2}  # the picture and its vendored copy
    assert counts["repos"] == 1
    left = search.Index(out)
    assert left.search("fill picture render own", licenses="any").total == 0
    assert left.snippet(picture) is None


def test_code_is_read_back_from_its_blocks(tmp_path: Path) -> None:
    here = tmp_path / "partitions" / "0"
    here.mkdir(parents=True)
    writer = search.CodeWriter(here, zstd.ZstdCompressor(3))
    code = [f"int f{i}(void) {{ return {i}; }} // añadido\n" * (i % 5 + 1) for i in range(1, 40)]
    for c in code:
        writer.add(c)
    writer.close()
    read = search.Code(tmp_path, 0)
    wanted = [39, 1, 16, 17, 5]  # either side of a block's edge, and in the last, short one
    assert read(wanted) == {i: code[i - 1] for i in wanted}
    empty = tmp_path / "empty"
    (empty / "partitions" / "0").mkdir(parents=True)
    search.CodeWriter(empty / "partitions" / "0", zstd.ZstdCompressor(3)).close()
    assert search.Code(empty, 0)([]) == {}
