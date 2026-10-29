import json

import pytest
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
    one = client.get(f"/api/snippets/{picture['id']}", params={"copies": "1"}).json()
    assert (len(one["copies"]), one["result"]["copies"]) == (1, 2)
    assert client.get(f"/api/snippets/{picture['id']}", params={"copies": "-1"}).status_code == 400
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
    assert "What should the code do" not in client.get("/", params={"partial": "1"}).text
    assert "Real code for what you want to do" in client.get("/", params={"partial": "1"}).text
    assert client.get("/", params={"q": "x", "page": "1_0"}).status_code == 400
    fits = client.get("/", params={"q": "fill", "license": "compatible", "project": "GPL-3.0-only"})
    assert "Fits a project under GPL-3.0-only" in fits.text
    assert "src/fill.c" in fits.text  # GPL-2.0-or-later goes into GPL-3.0-only
    assert client.get("/", params={"q": "fill", "license": "compatible"}).status_code == 400
    with TestClient(server.app(index)) as live:  # runs the lifespan MCP needs
        assert live.get("/mcp").status_code == 405
        assert live.delete("/mcp").status_code == 405


def test_mcp_behind_proxy(index: search.Index) -> None:
    client_info = {"name": "test", "version": "0"}
    params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": client_info}
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
    accept = {"accept": "application/json, text/event-stream"}
    proxied = server.app(index, "0.0.0.0", public="https://search.example")
    with TestClient(proxied, base_url="https://search.example") as client:
        answer = client.post("/mcp", json=initialize, headers=accept)
        assert answer.status_code == 200
        assert "true" not in json.dumps(answer.json()["result"]["capabilities"])  # nothing changes
        page = {**accept, "origin": "https://elsewhere.example"}  # a page's script, rebinding
        assert client.post("/mcp", json=initialize, headers=page).status_code == 403
    with TestClient(server.app(index), base_url="https://search.example") as client:
        assert client.post("/mcp", json=initialize, headers=accept).status_code == 421
    with pytest.raises(ValueError, match="--public"):
        server.app(index, "0.0.0.0")


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


def test_public_serving_guards(index: search.Index, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "BURST", 3)
    monkeypatch.setattr(server, "RATE", 0.001)  # none come back during the test
    client = TestClient(server.app(index))
    page = client.get("/", params={"q": "picture"})
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert page.headers["referrer-policy"] == "no-referrer"
    assert [client.get("/api/search", params={"q": "x"}).status_code for _ in range(3)] == [
        200,
        200,
        429,  # the page above spent one
    ]
    busy = client.get("/", params={"q": "picture"})
    assert busy.status_code == 429
    assert int(busy.headers["retry-after"]) > 0
    assert "nosniff" in busy.headers["x-content-type-options"]
    assert client.get("/static/site.css").status_code == 200  # not a search
    assert client.get("/healthz").json()["index_built"] is None  # the fixture's manifest is {}
    assert client.get("/robots.txt").text.startswith("User-agent: *\nAllow: /$\nDisallow: /")


def test_about_says_what_is_kept(index: search.Index) -> None:
    page = TestClient(server.app(index)).get("/about").text
    assert "keeps no log of what is searched" in page
    assert "template=removal.yml" in page
