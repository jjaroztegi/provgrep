"""The website and its HTTP API, over the query engine.

    GET /?q=...&language=c&license=permissive&page=1   the search page (partial=1: results only)
    GET /s/{id}                                         a snippet with every copy of its code
    GET /api/search?q=...&language=c&language=cpp&license=permissive&page=1
        license: "permissive" (default), "any", "allowed" (allowed=SPDX ids, comma-separated)
        or "compatible" (project=SPDX id: code OSADL's matrix says may go into that project)
        level: A, B, C or X, repeated; require: an identifier the code must contain, repeated
    GET /api/snippets/{id}?copies=100                  a snippet and up to that many copies
    POST /mcp                                           the same search as MCP tools
    GET /healthz                                        which build of the index is served
    GET /robots.txt, /sitemap.xml, /opensearch.xml, /favicon.ico   for crawlers and browsers

Pages are rendered on the server, so a search is a link that works without JavaScript; the
script only fetches the same results as you type. The whole query is in the URL, and every
request only reads the index: replicas of the server can serve the same index directory.
"""

import hashlib
import html
import math
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from markupsafe import Markup
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import PathLike, StaticFiles
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from provgrep import compatibility, highlight
from provgrep.attribution import reuse_snippet
from provgrep.mcp_server import server as mcp_server
from provgrep.search import Index, License, Page, Result, licenses_of, query_terms, terms

PAGE_SIZE = 10
MAX_PAGE = 20  # deeper than this, refine the query
COPIES = 100  # listed on a snippet's page, and by the API unless it asks for more
MAX_CONNECTIONS = 64
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Searches a client may make: RATE a second, BURST at once. Typing in the search box asks for
# results at most four times a second, and only while typing.
RATE = 2.0
BURST = 30
# Paths that do not search
FREE = ("/static/", "/healthz", "/robots.txt", "/sitemap.xml", "/opensearch.xml", "/favicon.ico")
_HEADERS = [
    (name.encode(), value.encode())
    for name, value in {
        # Nothing but this site's own script, styles and requests: no third parties, no inline code.
        "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self';"
        " font-src 'self'; img-src 'self' data:; connect-src 'self'; manifest-src 'self';"
        " form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",  # a link to GitHub does not say what was searched
    }.items()
]
# A static file asked for by its content's hash (asset) does not change; anything else, for a day.
YEAR = "public, max-age=31536000, immutable"
DAY = "public, max-age=86400"
# Search pages are endless, and each one runs a search: crawlers get the home and about pages,
# and what they need to show them (styles, fonts, the icon).
ROBOTS = """User-agent: *
Allow: /$
Allow: /about$
Allow: /static/
Allow: /favicon.ico$
Disallow: /
Sitemap: {base}/sitemap.xml
"""
SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{base}/</loc></url>
<url><loc>{base}/about</loc></url>
</urlset>
"""
# What a browser needs to offer the site as a search engine.
OPENSEARCH = """<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
<ShortName>provgrep</ShortName>
<Description>Open-source code by what it does, with the license it is under</Description>
<InputEncoding>UTF-8</InputEncoding>
<Image width="32" height="32" type="image/x-icon">{base}/favicon.ico</Image>
<Url type="text/html" method="get" template="{base}/?q={{searchTerms}}"/>
</OpenSearchDescription>
"""
# Every language the index cuts (chunks.LANGUAGES), as a reader names it, in the filters' order.
LANGUAGES = {
    "ada": "Ada",
    "c": "C",
    "c3": "C3",
    "commonlisp": "Common Lisp",
    "cpp": "C++",
    "d": "D",
    "erlang": "Erlang",
    "fortran": "Fortran",
    "glsl": "GLSL",
    "go": "Go",
    "haskell": "Haskell",
    "jai": "Jai",
    "java": "Java",
    "javascript": "JavaScript",
    "julia": "Julia",
    "lua": "Lua",
    "nim": "Nim",
    "ocaml": "OCaml",
    "odin": "Odin",
    "pascal": "Pascal",
    "python": "Python",
    "racket": "Racket",
    "rust": "Rust",
    "teal": "Teal",
    "typescript": "TypeScript",
    "vala": "Vala",
    "zig": "Zig",
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

    def base(request: Request) -> str:
        """The site's URL, for links that leave the page (sitemap, previews, search engines)."""
        return (public or str(request.base_url)).rstrip("/")

    templates = Jinja2Templates(
        directory=_HERE / "templates", context_processors=[lambda r: {"base": base(r)}]
    )
    templates.env.globals.update(
        projects=compatibility.PROJECTS, osadl=compatibility.SOURCE, asset=_asset
    )
    plain = {"query": {"q": "", "license": "permissive"}, "languages": LANGUAGES}  # not a search

    def search(params: Any) -> tuple[dict[str, Any], Page]:
        """The query a request asks for, as the page shows it, and its page of results."""
        query = _query(params)
        license, allowed, project = query["license"], query["allowed"], query["project"]
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
        query["page"] = page
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
        query = _query(params)
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
            "api": "/api/search?"
            + urlencode([(k, v) for k, v in params.multi_items() if v and k != "partial"]),
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
        found = index.snippet(request.path_params["id"], COPIES)
        if found is None:
            missing = {**plain, "snippet": True}
            return templates.TemplateResponse(request, "missing.html", missing, status_code=404)
        context: dict[str, Any] = {
            **plain,
            "result": _view(found.result, ""),
            "copies": [(c, LEVELS[c.license.level], _category(c.license)) for c in found.copies],
        }
        return templates.TemplateResponse(request, "snippet.html", context)

    def api_search(request: Request) -> JSONResponse:
        try:
            query, found = search(request.query_params)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"page": query["page"], "page_size": PAGE_SIZE, **asdict(found)})

    def api_snippet(request: Request) -> JSONResponse:
        wanted = request.query_params.get("copies", str(COPIES))
        if not wanted.isdigit():
            return JSONResponse({"error": "copies is a number"}, status_code=400)
        found = index.snippet(request.path_params["id"], int(wanted))
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
        return templates.TemplateResponse(
            request, "about.html", {**plain, "manifest": index.manifest}
        )

    def not_found(request: Request, _: Exception) -> Response:
        return templates.TemplateResponse(request, "missing.html", plain, status_code=404)

    def health(_: Request) -> JSONResponse:
        built = index.manifest.get("built")
        return JSONResponse({"index_built": built, "commit": index.manifest.get("commit")})

    def robots(request: Request) -> Response:
        return PlainTextResponse(ROBOTS.format(base=base(request)))

    def sitemap(request: Request) -> Response:
        return Response(SITEMAP.format(base=html.escape(base(request))), media_type="text/xml")

    def opensearch(request: Request) -> Response:
        xml = OPENSEARCH.format(base=html.escape(base(request)))
        return Response(xml, media_type="application/opensearchdescription+xml")

    def favicon(_: Request) -> Response:
        return FileResponse(_HERE / "static" / "favicon.ico", headers={"cache-control": DAY})

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
            Route("/sitemap.xml", sitemap),
            Route("/opensearch.xml", opensearch),
            Route("/favicon.ico", favicon),
            Mount("/static", Static(directory=_HERE / "static"), name="static"),
        ],
        exception_handlers={404: not_found},
        lifespan=lifespan,
    )
    site.add_middleware(Guard)
    return site


class Static(StaticFiles):
    """The site's own files, which browsers and the caches between may keep (YEAR, DAY)."""

    def file_response(
        self, full_path: PathLike, stat_result: os.stat_result, scope: Scope, status_code: int = 200
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["cache-control"] = YEAR if b"v=" in scope["query_string"] else DAY
        return response


@lru_cache
def _asset(path: str) -> str:
    """A static file's URL, which changes when the file does. Relative to the site, so that it is
    https behind a proxy whatever the proxy says the scheme is."""
    digest = hashlib.sha256((_HERE / "static" / path).read_bytes()).hexdigest()[:12]
    return f"/static/{path}?v={digest}"


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
    lines = []
    if result.code is not None:
        wanted = {t.split("^")[0] for t in query_terms(query)} - _UNMARKED
        lines = _lines(result.location.path, result.code, result.location.start, wanted)
    return {
        "r": result,
        "loc": result.location,
        "lic": lic,
        "category": _category(lic),
        "level_text": LEVELS[lic.level],
        "language": LANGUAGES.get(result.language, result.language),
        "lines": lines,
        "attribution": reuse_snippet(result),
    }


def _query(params: Any) -> dict[str, Any]:
    """The search a request asks for, as the page shows it."""
    license = params.get("license", "permissive")
    return {
        "q": params.get("q", ""),
        "language": params.get("language", ""),
        "level": params.get("level", ""),
        "license": license,
        "allowed": params.get("allowed", ""),
        "project": params.get("project", "") if license == "compatible" else "",
        "page": 1,
    }


def _category(lic: License) -> str:
    """Which colour a license is shown in: whether its code may be shared, and on what terms."""
    return "link" if not lic.shareable else "permissive" if lic.permissive else "copyleft"


def _lines(path: str, code: str, first: int, wanted: set[str]) -> list[tuple[int, Markup]]:
    """The code's lines, numbered, escaped, coloured by its syntax, with the identifiers that
    match the query marked. Built as plain strings: a page has thousands of pieces."""
    marks = [
        (m.start(), m.end())
        for m in _WORD.finditer(code if wanted else "")
        if not wanted.isdisjoint(_terms(m.group()))
    ]
    lines: list[list[str]] = [[]]
    at = 0  # where in the code the next piece starts
    i = 0  # the first mark that does not end before it
    for cls, text in highlight.runs(path, code):
        for n, part in enumerate(text.split("\n")):
            if n:
                lines.append([])
                at += 1
            if not part:
                continue
            end = at + len(part)
            out, last = [], at
            while i < len(marks) and marks[i][0] < end:
                start, stop = max(marks[i][0], at), min(marks[i][1], end)
                out += [_escape(code[last:start]), "<mark>", _escape(code[start:stop]), "</mark>"]
                last = stop
                if marks[i][1] > end:  # a word the syntax cut in two goes on in the next piece
                    break
                i += 1
            out.append(_escape(code[last:end]))
            piece = "".join(out)
            lines[-1].append(f'<span class="{cls}">{piece}</span>' if cls else piece)
            at = end
    return [(n, Markup("".join(parts))) for n, parts in enumerate(lines, first)]


def _escape(text: str) -> str:
    return html.escape(text, quote=False)  # text inside an element: quotes need no escaping


_terms = lru_cache(maxsize=65536)(terms)  # code repeats its identifiers


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
