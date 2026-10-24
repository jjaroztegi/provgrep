"""The search engine as MCP tools, for agents: the same index, filters and rules as the website.

`provgrep mcp` serves them over stdio; `provgrep serve` also mounts them at /mcp over
Streamable HTTP, stateless, so replicas of the server answer them as they answer pages.
"""

from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from provgrep.attribution import reuse_snippet
from provgrep.search import Index, Page, Snippet, licenses_of

MAX_RESULTS = 20
INSTRUCTIONS = """\
Searches functions from the most-starred public repositories on GitHub (C, C++, Go, Java,
JavaScript, Python, Rust, TypeScript) and returns real code with the license it is under, the
evidence for that license (level A: the file's own header; B: license file and manifest agree;
C: one source; X: none), and its copyright notices. Name the APIs you expect the code to use: an
identifier in the query weighs more than a word, and `require` asks for it outright. Pass the
licenses your project can take. Code no license lets be shared comes without its code, as a link.
Licenses are as detected, not legal advice. Before pasting code, take `attribution`."""
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def server(index: Index) -> MCPServer:
    mcp = MCPServer("provgrep", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=_READ_ONLY)
    def search(
        query: Annotated[str, Field(description="What the code should do, and the APIs it uses")],
        languages: Annotated[
            list[str],
            Field(description="c, cpp, go, java, javascript, python, rust or typescript"),
        ] = [],  # noqa: B006 - the SDK copies defaults; this one is never mutated
        licenses: Annotated[
            str,
            Field(description='"permissive", "any", or the SPDX ids allowed, comma-separated'),
        ] = "permissive",
        require: Annotated[
            list[str], Field(description="Identifiers the code must contain; A|B for either")
        ] = [],  # noqa: B006
        limit: Annotated[int, Field(ge=1, le=MAX_RESULTS)] = 10,
    ) -> Page:
        """Functions that do what the query asks, best first, each with its license, evidence
        level, copyright notices, the lines at their commit on GitHub, and how many copies of the
        same code the index has."""
        try:
            return index.search(
                query,
                languages=languages,
                licenses=licenses_of(licenses),
                require=require,
                limit=limit,
            )
        except ValueError as e:  # a reason the agent can act on, not "Error executing tool"
            raise ToolError(str(e)) from e

    @mcp.tool(annotations=_READ_ONLY)
    def snippet(id: Annotated[str, Field(description="A result's id")]) -> Snippet:
        """One result with every copy of its code in the index, each under its own license."""
        found = index.snippet(id)
        if found is None:
            raise ToolError(f"no snippet {id!r} in this index")
        return found

    @mcp.tool(annotations=_READ_ONLY)
    def attribution(id: Annotated[str, Field(description="A result's id")]) -> str:
        """The code between REUSE snippet tags (copyright, SPDX license, source), to paste into
        a file as it is."""
        found = index.snippet(id)
        if found is None:
            raise ToolError(f"no snippet {id!r} in this index")
        text = reuse_snippet(found.result)
        if text is None:
            raise ToolError("no license found lets this code be shared; link to it instead")
        return text

    return mcp
