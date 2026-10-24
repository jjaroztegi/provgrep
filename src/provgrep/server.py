"""The website and its HTTP API, over the query engine.

    GET /?q=...&language=c&license=permissive&page=1   the search page (partial=1: results only)
    GET /s/{id}                                         a snippet with every copy of its code
    GET /api/search?q=...&language=c&language=cpp&license=permissive&page=1
        license: "permissive" (default), "any", or SPDX ids, comma-separated
        level: A, B, C or X, repeated; require: an identifier the code must contain, repeated
    GET /api/snippets/{id}

Pages are rendered on the server, so a search is a link that works without JavaScript; the
script only fetches the same results as you type. The whole query is in the URL, and every
request only reads the index: replicas of the server can serve the same index directory.
"""

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from markupsafe import Markup, escape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from provgrep.attribution import reuse_snippet
from provgrep.search import Index, Page, Result, query_terms, terms

PAGE_SIZE = 10
MAX_PAGE = 20  # deeper than this, refine the query
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


def app(index: Index) -> Starlette:
    templates = Jinja2Templates(directory=_HERE / "templates")

    def search(params: Any) -> tuple[dict[str, Any], Page]:
        """The query a request asks for, as the page shows it, and its page of results."""
        license = params.get("license", "permissive")
        allowed = params.get("allowed", "")
        if license == "allowed":
            licenses: str | list[str] = allowed.split(",")
        else:
            licenses = license if license in ("permissive", "any") else license.split(",")
        page = int(params.get("page", "1"))
        if not 1 <= page <= MAX_PAGE:
            raise ValueError(f"page goes from 1 to {MAX_PAGE}")
        query = {
            "q": params.get("q", ""),
            "language": params.get("language", ""),
            "license": license,
            "allowed": allowed,
            "page": page,
        }
        found = index.search(
            query["q"],
            languages=[v for v in params.getlist("language") if v],  # "" is the form's "all"
            licenses=licenses,
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
        name = "results.html" if params.get("partial") else "search.html"
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

    return Starlette(
        routes=[
            Route("/", home),
            Route("/s/{id}", snippet_page),
            Route("/api/search", api_search),
            Route("/api/snippets/{id}", api_snippet),
            Mount("/static", StaticFiles(directory=_HERE / "static"), name="static"),
        ]
    )


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


def serve(index_path: Path, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(app(Index(index_path)), host=host, port=port)
