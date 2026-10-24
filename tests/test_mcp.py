import pytest
from mcp import Client
from mcp.types import TextContent

from provgrep import search
from provgrep.mcp_server import server


@pytest.mark.anyio
async def test_tools(index: search.Index) -> None:
    async with Client(server(index)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == {"search", "snippet", "attribution"}

        found = await client.call_tool("search", {"query": "create picture for a window"})
        (result,) = found.structured_content["results"]
        assert result["location"]["license"]["expression"] == "MIT"
        assert "XRenderCreatePicture" in result["code"]

        copies = await client.call_tool("snippet", {"id": result["id"]})
        assert len(copies.structured_content["copies"]) == 2

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
