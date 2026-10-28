"""The website and its HTTP API, over the query engine.

    GET /?q=...&language=c&license=permissive&page=1   the search page (partial=1: results only)
    GET /s/{id}                                         a snippet with every copy of its code
    GET /api/search?q=...&language=c&language=cpp&license=permissive&page=1
        license: "permissive" (default), "any", "allowed" (allowed=SPDX ids, comma-separated)
        or "compatible" (project=SPDX id: code OSADL's matrix says may go into that project)
        level: A, B, C or X, repeated; require: an identifier the code must contain, repeated
    GET /api/snippets/{id}
    POST /mcp                                           the same search as MCP tools
    GET /healthz                                        which build of the index is served

Pages are rendered on the server, so a search is a link that works without JavaScript; the
script only fetches the same results as you type. The whole query is in the URL, and every
request only reads the index: replicas of the server can serve the same index directory.
"""

import math
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from markupsafe import Markup, escape
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from provgrep import compatibility
from provgrep.attribution import reuse_snippet
from provgrep.mcp_server import server as mcp_server
from provgrep.search import Index, Page, Result, licenses_of, query_terms, terms

PAGE_SIZE = 10
MAX_PAGE = 20  # deeper than this, refine the query
MAX_CONNECTIONS = 64
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Searches a client may make: RATE a second, BURST at once. Typing in the search box asks for
# results at most four times a second, and only while typing.
RATE = 2.0
BURST = 30
FREE = ("/static/", "/healthz", "/robots.txt")  # paths that do not search
_HEADERS = [
    (name.encode(), value.encode())
    for name, value in {
        # Nothing but this site's own script, styles and requests: no third parties, no inline code.
        "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self';"
        " img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none';"
        " frame-ancestors 'none'",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",  # a link to GitHub does not say what was searched
    }.items()
]
# Search pages are endless, and each one runs a search: crawlers get the home page only.
ROBOTS = "User-agent: *\nAllow: /$\nDisallow: /\n"
LANGUAGES = {
    "c": "C",
    "cpp": "C++",
    "go": "Go",
    "java": "Java",
    "javascript": "JavaScript",
    "python": "Python",
    "rust": "Rust",
    "typescript": "TypeScript",
}
LEVELS = {
    "A": "The file's own header names this license.",
    "B": "The license file and the package manifest agree.",
    "C": "One source names it, or the nearer of two that disagree.",
    "X": "No license found.",
}
EXAMPLES = (
    "create an XRender picture for an X11 window",
    "debounce a function",
    "parse the header of an RTP packet",
    "retry a request with exponential backoff",
)
_HERE = Path(__file__).parent
_WORD = re.compile(r"\w+")
# Not marked in code: they rank, but a page of marked "the" says nothing about why it matched.
_UNMARKED = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "this",
        "to",
        "use",
        "using",
        "what",
        "when",
        "which",
        "with",
    ]
)


def app(index: Index, host: str = "127.0.0.1", public: str | None = None) -> Starlette:
    """The site over an index. host is where it listens; public, the URL it is reached at, through
    a proxy or not. MCP refuses requests addressed to any other host or sent from a page of any
    other origin (DNS rebinding protection), so listening beyond this machine needs public."""
    if public is None and host not in LOOPBACK:
        raise ValueError(f"listening on {host} needs the URL clients reach it at (--public)")
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.globals.update(projects=compatibility.PROJECTS, osadl=compatibility.SOURCE)

    def search(params: Any) -> tuple[dict[str, Any], Page]:
        """The query a request asks for, as the page shows it, and its page of results."""
        license = params.get("license", "permissive")
        allowed = params.get("allowed", "")
        project = params.get("project", "") if license == "compatible" else ""
        if license == "allowed":
            licenses: str | list[str] = allowed.split(",")
        elif license == "compatible":
            if not project:
                raise ValueError("name the license of your project")
            licenses = "any"
        else:
            licenses = licenses_of(license)
        number = params.get("page", "1")
        if not number.isdigit() or not 1 <= int(number) <= MAX_PAGE:
            raise ValueError(f"page goes from 1 to {MAX_PAGE}")
        page = int(number)
        query = {
            "q": params.get("q", ""),
            "language": params.get("language", ""),
            "license": license,
            "allowed": allowed,
            "project": project,
            "page": page,
        }
        found = index.search(
            query["q"],
            languages=[v for v in params.getlist("language") if v],  # "" is the form's "all"
            licenses=licenses,
            project=project or None,
            levels=[v for v in params.getlist("level") if v],
            require=[v for v in params.getlist("require") if v],
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
        return query, found

    def home(request: Request) -> Response:
        params = request.query_params
        query: dict[str, Any] = {"q": params.get("q", ""), "license": "permissive", "page": 1}
        found, error = None, None
        if query["q"] or params.getlist("require"):
            try:
                query, found = search(params)
            except ValueError as e:
                error = str(e)
        context = {
            "query": query,
            "found": found,
            "error": error,
            "results": [_view(r, query["q"]) for r in found.results] if found else [],
            "next": _next(request, found, query["page"]),
            "manifest": index.manifest,
            "languages": LANGUAGES,
            "examples": EXAMPLES,
        }
        if params.get("partial"):
            name = "results.html" if found or error else "intro.html"
        else:
            name = "search.html"
        return templates.TemplateResponse(request, name, context, status_code=400 if error else 200)

    def snippet_page(request: Request) -> Response:
        found = index.snippet(request.path_params["id"])
        if found is None:
            missing = {"query": {"q": "", "license": "permissive"}, "languages": LANGUAGES}
            return templates.TemplateResponse(request, "missing.html", missing, status_code=404)
        context: dict[str, Any] = {
            "query": {"q": "", "license": "permissive"},
            "languages": LANGUAGES,
            "result": _view(found.result, ""),
            "copies": [(c, LEVELS[c.license.level]) for c in found.copies],
        }
        return templates.TemplateResponse(request, "snippet.html", context)

    def api_search(request: Request) -> JSONResponse:
        try:
            query, found = search(request.query_params)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"page": query["page"], "page_size": PAGE_SIZE, **asdict(found)})

    def api_snippet(request: Request) -> JSONResponse:
        found = index.snippet(request.path_params["id"])
        if found is None:
            return JSONResponse({"error": "no such snippet"}, status_code=404)
        return JSONResponse(asdict(found))

    mcp = mcp_server(index, public)
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if public:
        url = urlsplit(public)
        hosts.append(url.netloc)
        origins.append(f"{url.scheme}://{url.netloc}")
    # Stateless and plain JSON: any replica answers any request, as it does pages. Served at
    # /mcp itself: a redirect to /mcp/ would be to http behind a proxy, and clients refuse it.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins),
        host=host,
    )

    def about(request: Request) -> Response:
        context = {"query": {"q": "", "license": "permissive"}, "languages": LANGUAGES}
        return templates.TemplateResponse(
            request, "about.html", {**context, "manifest": index.manifest}
        )

    def health(_: Request) -> JSONResponse:
        built = index.manifest.get("built")
        return JSONResponse({"index_built": built, "commit": index.manifest.get("commit")})

    def robots(_: Request) -> Response:
        return PlainTextResponse(ROBOTS)

    def no_stream(_: Request) -> Response:
        # Stateless: there is no stream of server messages to hold open, and no session to end.
        return Response(status_code=405, headers={"Allow": "POST"})

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():  # a mounted app's own lifespan does not run
            yield

    site = Starlette(
        routes=[
            Route("/", home),
            Route("/s/{id}", snippet_page),
            Route("/api/search", api_search),
            Route("/api/snippets/{id}", api_snippet),
            Route("/mcp", no_stream, methods=["GET", "DELETE"]),
            *mcp_app.routes,
            Route("/about", about),
            Route("/healthz", health),
            Route("/robots.txt", robots),
            Mount("/static", StaticFiles(directory=_HERE / "static"), name="static"),
        ],
        lifespan=lifespan,
    )
    site.add_middleware(Guard)
    return site


class Guard:
    """Every response with headers that keep the page to this site, and searches rationed per
    client: past its budget, a client gets 429 and when to come back. Behind a proxy the
    client is the address the proxy forwards for (uvicorn trusts it from 127.0.0.1)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.rate = RATE
        self.burst = BURST
        self._budgets: dict[str, tuple[float, float]] = {}  # client: searches left, when

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def guarded(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *_HEADERS]
            await send(message)

        client = scope["client"][0] if scope.get("client") else ""
        if not scope["path"].startswith(FREE) and (wait := self._spend(client)):
            busy = PlainTextResponse(
                "Too many searches at once: try again in a few seconds.",
                status_code=429,
                headers={"Retry-After": str(math.ceil(wait))},
            )
            await busy(scope, receive, guarded)
            return
        await self.app(scope, receive, guarded)

    def _spend(self, client: str) -> float:
        """Spend one of a client's searches: 0, or the seconds until it has one."""
        now = time.monotonic()
        left, then = self._budgets.get(client, (self.burst, now))
        left = min(self.burst, left + (now - then) * self.rate)
        if left < 1:
            self._budgets[client] = (left, now)
            return (1 - left) / self.rate
        self._budgets[client] = (left - 1, now)
        if len(self._budgets) > 100_000:  # forget clients whose budget is full again
            full = now - self.burst / self.rate
            self._budgets = {c: b for c, b in self._budgets.items() if b[1] > full}
        return 0.0


def _view(result: Result, query: str) -> dict[str, Any]:
    """What a result card shows."""
    lic = result.location.license
    category = "link" if not lic.shareable else "permissive" if lic.permissive else "copyleft"
    lines = []
    if result.code is not None:
        wanted = {t.split("^")[0] for t in query_terms(query)} - _UNMARKED
        for n, line in enumerate(result.code.split("\n"), result.location.start):
            lines.append((n, _marked(line, wanted)))
    return {
        "r": result,
        "loc": result.location,
        "lic": lic,
        "category": category,
        "level_text": LEVELS[lic.level],
        "language": LANGUAGES.get(result.language, result.language),
        "lines": lines,
        "attribution": reuse_snippet(result),
    }


def _marked(line: str, wanted: set[str]) -> Markup:
    """A line of code, escaped, with the identifiers that match the query marked."""
    if not wanted:
        return escape(line)
    out = []
    last = 0
    for m in _WORD.finditer(line):
        if wanted & set(terms(m.group())):
            out += [escape(line[last : m.start()]), Markup("<mark>"), escape(m.group())]
            out.append(Markup("</mark>"))
            last = m.end()
    out.append(escape(line[last:]))
    return Markup("").join(out)


def _next(request: Request, found: Page | None, page: int) -> str | None:
    """The link to the next page of the same search, if there is one."""
    if found is None or page * PAGE_SIZE >= found.total or page >= MAX_PAGE:
        return None
    kept = [(k, v) for k, v in request.query_params.multi_items() if k not in ("page", "partial")]
    return "/?" + urlencode([*kept, ("page", str(page + 1))])


def serve(index_path: Path, host: str, port: int, public: str | None) -> None:
    import uvicorn

    site = app(Index(index_path), host, public)
    # Every search scores every partition: past this many connections, a 503 rather than a
    # queue. No access log: a request's URL is what someone searched for.
    uvicorn.run(site, host=host, port=port, limit_concurrency=MAX_CONNECTIONS, access_log=False)
