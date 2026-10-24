from starlette.testclient import TestClient

from provgrep import search, server
from provgrep.attribution import reuse_snippet


def test_api(index: search.Index) -> None:
    client = TestClient(server.app(index))
    found = client.get("/api/search", params={"q": "fill", "license": "any", "level": "X"}).json()
    assert (found["total"], found["results"][0]["code"]) == (1, None)
    gpl = client.get("/api/search", params={"q": "fill", "license": "GPL-2.0-or-later"}).json()
    assert gpl["results"][0]["location"]["license"]["expression"] == "GPL-2.0-or-later"
    picture = client.get("/api/search", params={"q": "picture"}).json()["results"][0]
    copies = client.get(f"/api/snippets/{picture['id']}").json()["copies"]
    assert len(copies) == 2
    assert client.get("/api/search", params={"q": "x", "page": "0"}).status_code == 400
    assert client.get("/api/snippets/nope").status_code == 404


def test_pages(index: search.Index) -> None:
    client = TestClient(server.app(index))
    home = client.get("/").text
    assert "What should the code do?" in home
    page = client.get("/", params={"q": "create picture"}).text
    assert "<mark>XRenderCreatePicture</mark>" in page
    assert "Copyright (c) 2020 Acme" in page
    assert "Same code in 2 places" in page
    withheld = client.get("/", params={"q": "fill", "license": "any", "level": "X"}).text
    assert "The code is not shown here" in withheld
    assert "XRenderFillRectangle" not in withheld
    allowed = client.get(
        "/", params={"q": "fill", "license": "allowed", "allowed": "GPL-2.0-or-later"}
    )
    assert "src/fill.c" in allowed.text
    partial = client.get("/", params={"q": "picture", "partial": "1"}).text
    assert "<html" not in partial
    assert "src/pic.c" in partial
    hostile = client.get("/", params={"q": "<script>alert(1)</script>"}).text
    assert "<script>alert(1)</script>" not in hostile
    picture = index.search("picture").results[0]
    copies = client.get(f"/s/{picture.id}").text
    assert "third_party/x/pic.h" in copies
    assert client.get("/s/nope").status_code == 404


def test_attribution(index: search.Index) -> None:
    picture = index.search("picture").results[0]
    assert picture.code is not None
    assert reuse_snippet(picture) == (
        "// SPDX-SnippetBegin\n"
        "// SPDX-SnippetCopyrightText: 2020 Acme\n"
        "// SPDX-License-Identifier: MIT\n"
        f"// Source: {picture.location.url}\n"
        f"{picture.code.rstrip()}\n"
        "// SPDX-SnippetEnd\n"
    )
    (unlicensed,) = [
        r for r in index.search("fill", licenses="any").results if r.location.repo == "acme/small"
    ]
    assert reuse_snippet(unlicensed) is None
