import json

import pytest
from mcp import Client
from mcp.types import TextContent

from provgrep import search
from provgrep.mcp_server import server


@pytest.mark.anyio
async def test_tools(index: search.Index) -> None:
    async with Client(server(index)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {"search", "snippet", "attribution"}
        for tool in tools.values():
            assert tool.title
            assert tool.annotations
            assert tool.annotations.read_only_hint
        assert tools["search"].output_schema is not None  # the shape of a Page

        found = await client.call_tool("search", {"query": "create picture for a window"})
        (result,) = found.structured_content["results"]
        assert result["location"]["license"]["expression"] == "MIT"
        assert "XRenderCreatePicture" in result["code"]
        text = found.content[0]
        assert isinstance(text, TextContent)
        assert json.loads(text.text) == found.structured_content
        assert "\n " not in text.text  # compact: an agent pays for every space

        fits = await client.call_tool("search", {"query": "fill", "project": "GPL-3.0-or-later"})
        (gpl,) = fits.structured_content["results"]  # licenses: any, once a project is given
        assert gpl["compatibility"]["answer"] == "yes"

        copies = await client.call_tool("snippet", {"id": result["id"]})
        assert len(copies.structured_content["copies"]) == 2
        first = await client.call_tool("snippet", {"id": result["id"], "copies": 1})
        assert len(first.structured_content["copies"]) == 1
        assert first.structured_content["result"]["copies"] == 2  # how many there are in all

        pasted = (await client.call_tool("attribution", {"id": result["id"]})).content[0]
        assert isinstance(pasted, TextContent)
        assert "SPDX-License-Identifier: MIT" in pasted.text

        listed = await client.call_tool(
            "search", {"query": "fill", "licenses": "any", "require": ["XRenderFillRectangle"]}
        )
        unlicensed = [
            r for r in listed.structured_content["results"] if r["location"]["repo"] == "acme/small"
        ]
        assert unlicensed[0]["code"] is None
        refused = await client.call_tool("attribution", {"id": unlicensed[0]["id"]})
        assert refused.is_error
        reason = refused.content[0]
        assert isinstance(reason, TextContent)
        assert "link to it instead" in reason.text


@pytest.mark.anyio
async def test_search_pages_and_filters(index: search.Index) -> None:
    async with Client(server(index)) as client:
        both = {"query": "fill", "licenses": "any"}
        page = (await client.call_tool("search", both)).structured_content
        assert page["total"] == 2
        second = (await client.call_tool("search", {**both, "offset": 1})).structured_content
        assert second["results"] == page["results"][1:]
        x = await client.call_tool("search", {**both, "levels": ["X"]})
        assert [r["location"]["repo"] for r in x.structured_content["results"]] == ["acme/small"]
        wrong = await client.call_tool("search", {"query": "fill", "languages": ["cobol"]})
        assert wrong.is_error  # not an empty page that looks like no code does it
        long = await client.call_tool("search", {"query": "fill " * 300})
        reason = long.content[0]
        assert isinstance(reason, TextContent)
        assert "at most 1000 characters" in reason.text
