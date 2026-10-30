# The server with what it would otherwise download, for an index mounted at /index. It needs no
# network and writes nothing, so it runs with a read-only filesystem as an unprivileged user:
#   docker run --read-only -p 127.0.0.1:8000:8000 -v "$PWD/index/github:/index" IMAGE
# The mount is not :ro because tantivy opens its lock to read an index too (docs/build.md).
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv PROVGREP_CACHE=/app/cache XDG_CACHE_HOME=/app/cache
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable
# What the server would otherwise download on first use: the query model and the tokenizer,
# checked by their sha256, and the grammars that cut and colour code, for every language indexed
RUN /app/.venv/bin/python -c "import tree_sitter_language_pack as pack; from provgrep import chunks, dense; \
dense.file(*dense.QUERIES); dense.file(dense.MODEL.repo, dense.MODEL.revision, *dense.MODEL.tokenizer); \
pack.download(sorted({spec.grammar for spec in chunks.LANGUAGES.values()}))"

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
COPY --from=build /app/.venv /app/.venv
# The grammars' cache must be its user's alone (0700), as the language pack checks
COPY --from=build --chown=65532:65532 /app/cache /app/cache
ENV PATH=/app/.venv/bin:$PATH PROVGREP_CACHE=/app/cache XDG_CACHE_HOME=/app/cache \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 8000
HEALTHCHECK --start-period=30s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)"]
ENTRYPOINT ["provgrep"]
CMD ["serve", "--index", "/index", "--host", "0.0.0.0", "--port", "8000"]
