"""The HTTP API: JSON over the query engine, for the website and any other client.

    GET /api/search?q=...&language=c&language=cpp&license=permissive&page=1
        license: "permissive" (default), "any", or SPDX ids, comma-separated
        level: A, B, C or X, repeated; require: an identifier the code must contain, repeated
    GET /api/snippets/{id}   a snippet with every copy of its code

The whole query is in the URL, so a search can be shared as a link. Every request only reads
the index: replicas of the server can serve the same index directory.
"""

from dataclasses import asdict
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from provgrep.search import Index

PAGE_SIZE = 10
MAX_PAGE = 20  # deeper than this, refine the query


def app(index: Index) -> Starlette:
    def search(request: Request) -> JSONResponse:
        params = request.query_params
        license = params.get("license", "permissive")
        try:
            page = int(params.get("page", "1"))
            if not 1 <= page <= MAX_PAGE:
                raise ValueError(f"page goes from 1 to {MAX_PAGE}")
            found = index.search(
                params.get("q", ""),
                languages=params.getlist("language"),
                licenses=license if license in ("permissive", "any") else license.split(","),
                levels=params.getlist("level"),
                require=params.getlist("require"),
                limit=PAGE_SIZE,
                offset=(page - 1) * PAGE_SIZE,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"page": page, "page_size": PAGE_SIZE, **asdict(found)})

    def snippet(request: Request) -> JSONResponse:
        found = index.snippet(request.path_params["id"])
        if found is None:
            return JSONResponse({"error": "no such snippet"}, status_code=404)
        return JSONResponse(asdict(found))

    return Starlette(
        routes=[
            Route("/api/search", search),
            Route("/api/snippets/{id}", snippet),
        ]
    )


def serve(index_path: Path, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(app(Index(index_path)), host=host, port=port)
