"""Search the index: the one query engine behind the command line, the HTTP API and the MCP
server.

Lexical search over each snippet's name, comments and docstrings, code and path. Identifiers are
split into words (`parseRtpHeader` and `parse_rtp_header` give parse, rtp, header) and a joined
identifier is kept whole as well, then English stemming; BM25 ranks, with a weight per field.
Filters (language, licenses, level) are part of the query, so they act before ranking.

Code is returned with its license, evidence and notices, and not at all when no license was
found: that rule is applied here, once, for every interface.
"""

import itertools
import json
import re
import shutil
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tantivy
from tantivy import Occur, Query

FIELDS = {"symbol": 2.0, "doc": 2.0, "path": 1.0, "code": 1.0}
IDENTIFIER = 3.0
LEVELS = ("A", "B", "C", "X")
# Each combination of the licenses in an allow list is one term of the query.
MAX_ALLOWED = 8
ID_LENGTH = 16  # hex digits of a snippet's hash that name it
_WORD = re.compile(r"\w+")
_ASCII_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_STOP = frozenset(
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
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "to",
        "use",
        "using",
        "via",
        "what",
        "when",
        "where",
        "which",
        "with",
        "without",
    ]
)


def terms(text: str) -> list[str]:
    """Lower-cased words: each identifier whole without its underscores, the prefixes that name
    its family, then its parts, split at `_`, case changes and digits (`XRenderFillRectangle`:
    xrenderfillrectangle, xrender, xrenderfill, x, render, fill, rectangle; `parse_rtp_header`:
    parsertpheader, parsertp, parse, rtp, header). A family is how APIs are named and asked
    for: XRender*, XShm*, epoll_*."""
    out = []
    for word in _WORD.findall(text):
        parts = _parts(word)
        if len(parts) < 2:
            out += parts
        else:
            prefixes = ["".join(parts[:n]) for n in range(2, len(parts))]
            out += ["".join(parts), *prefixes, *parts]
    return out


def identifiers(text: str) -> set[str]:
    """Every identifier in a text, whole, as terms() keeps it: lower-cased and without
    underscores (`parse_rtp_header` and `parseRtpHeader` are both parsertpheader)."""
    return {"".join(parts) for word in _WORD.findall(text) if (parts := _parts(word))}


def _parts(word: str) -> list[str]:
    if word.isascii():
        return [part.lower() for part in _ASCII_PARTS.findall(word)]
    return [part for chunk in word.split("_") if chunk for part in _split(chunk)]


def query_terms(query: str) -> list[str]:
    """The terms of a query as the query parser takes them. An identifier typed as one
    (`XRender`, `epoll_wait`) weighs IDENTIFIER times a word: whoever types it knows the name.
    The single letters an identifier splits into (`XRender`: x) are left out; most code has them.
    """
    out = []
    for word in _WORD.findall(query):
        found = terms(word)
        if len(found) > 1:
            out.append(f"{found[0]}^{IDENTIFIER}")
            found = found[1:]
        out += [t for t in found if len(t) > 1]
    return out or terms(query)


def _split(word: str) -> list[str]:
    """_ASCII_PARTS for any script: at a lower-to-upper change, between digits and the rest, and
    before the last capital of a run followed by a lower-case letter (HTTPServer: HTTP, Server)."""
    parts = []
    start = 0
    for i in range(1, len(word)):
        a, b = word[i - 1], word[i]
        after = word[i + 1] if i + 1 < len(word) else ""
        if (
            (a.islower() and b.isupper())
            or (a.isdigit() != b.isdigit())
            or (a.isupper() and b.isupper() and after.islower())
        ):
            parts.append(word[start:i].lower())
            start = i
    parts.append(word[start:].lower())
    return parts


@dataclass(frozen=True)
class License:
    expression: str | None  # SPDX; None when no license was found
    shareable: bool  # whether it lets the code be shown (licenses.shareable)
    level: str
    conflict: bool
    evidence: list[dict[str, str]]
    notices: list[str]  # copyright statements


@dataclass(frozen=True)
class Location:
    repo: str
    commit: str
    stars: int
    path: str
    start: int
    end: int
    symbol: str
    vendored: bool
    url: str  # the lines at the pinned commit
    license: License


@dataclass(frozen=True)
class Result:
    id: str
    score: float
    language: str
    code: str | None  # None when no license was found: link only
    location: Location  # where the code shown is from
    copies: int  # locations of the same code in the corpus
    repos: int
    licenses: int  # distinct licenses among the copies


@dataclass(frozen=True)
class Page:
    total: int
    results: list[Result]


@dataclass(frozen=True)
class Snippet:
    result: Result
    copies: list[Location]


class Index:
    """An index directory, opened read-only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        self._db = sqlite3.connect(
            f"{(path / 'records.sqlite').resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
        )
        self._lexical = tantivy.Index.open(str(path / "lexical"))
        self._schema = self._lexical.schema
        self._searcher = self._lexical.searcher()

    def search(
        self,
        query: str,
        *,
        languages: Sequence[str] = (),
        licenses: str | Sequence[str] = "permissive",
        levels: Sequence[str] = (),
        require: Sequence[str] = (),
        limit: int = 10,
        offset: int = 0,
    ) -> Page:
        """Snippets for a query, best first. licenses: "permissive", "any", or the SPDX ids
        allowed (a snippet passes if one way to comply with its license needs only those).
        require: identifiers the code must contain, whole; `A|B` for either."""
        if limit < 1 or offset < 0:
            raise ValueError("limit must be at least 1 and offset at least 0")
        words = query_terms(query)
        clauses = []
        if words:
            text = self._lexical.parse_query(" ".join(words), list(FIELDS), FIELDS)
            clauses.append((Occur.Must, text))
        filters = []
        for identifier in require:
            whole = [w for alt in identifier.split("|") for w in identifiers(alt)]
            if not whole:
                raise ValueError(f"not an identifier: {identifier!r}")
            filters.append(self._any("identifiers", whole))
        if not words and not filters:
            return Page(0, [])
        if languages:
            filters.append(self._any("language", languages))
        if licenses == "permissive":
            filters.append(Query.term_query(self._schema, "permissive", 1))
        elif licenses != "any":
            allowed = sorted({spdx.strip().lower() for spdx in licenses if spdx.strip()})
            if not 0 < len(allowed) <= MAX_ALLOWED:
                raise ValueError(f"allow 1 to {MAX_ALLOWED} licenses, not {len(allowed)}")
            combinations = [
                " + ".join(c)
                for n in range(1, len(allowed) + 1)
                for c in itertools.combinations(allowed, n)
            ]
            filters.append(self._any("license", combinations))
        if levels:
            filters.append(self._any("level", levels))
        clauses += [(Occur.Must, Query.const_score_query(f, 0.0)) for f in filters]
        found = self._searcher.search(Query.boolean_query(clauses), limit, True, offset=offset)
        ranked = [(score, self._searcher.doc(address)["id"][0]) for score, address in found.hits]
        rows = self._rows([snippet for _, snippet in ranked])
        total = found.count  # type: ignore[attr-defined]  # missing from tantivy's stub
        return Page(total, [_result(rows[snippet], score) for score, snippet in ranked])

    def snippet(self, id: str) -> Snippet | None:
        """A snippet by its id, with every location of its code."""
        if len(id) != ID_LENGTH or not all(c in "0123456789abcdef" for c in id):
            return None
        row = self._db.execute(
            "SELECT id FROM snippets WHERE hash >= ? AND hash < ?", (id, id + "g")
        ).fetchone()
        if row is None:
            return None
        copies = self._db.execute(
            f"SELECT {_LOCATION} FROM locations l JOIN files f ON f.id = l.file"
            " JOIN repos r ON r.id = f.repo WHERE l.snippet = ? ORDER BY l.id",
            (row[0],),
        ).fetchall()
        return Snippet(_result(self._rows([row[0]])[row[0]], 0.0), [_location(c) for c in copies])

    def _any(self, field: str, values: Sequence[str]) -> Query:
        return Query.boolean_query(
            [(Occur.Should, Query.term_query(self._schema, field, v)) for v in values]
        )

    def _rows(self, snippets: list[int]) -> dict[int, tuple[Any, ...]]:
        if not snippets:
            return {}
        marks = ",".join("?" * len(snippets))
        rows = self._db.execute(
            f"SELECT {_LOCATION}, s.id, s.hash, s.language, s.code, s.copies, s.repos, s.licenses"
            " FROM snippets s JOIN locations l ON l.id = s.location JOIN files f ON f.id = l.file"
            f" JOIN repos r ON r.id = f.repo WHERE s.id IN ({marks})",
            snippets,
        ).fetchall()
        return {row[_LOCATION_COLUMNS]: row for row in rows}


_LOCATION = (
    "r.name, r.commit_id, r.stars, f.path, l.start, l.end, l.symbol, f.vendored,"
    " f.license, f.shareable, f.level, f.conflict, f.evidence, f.notices"
)
_LOCATION_COLUMNS = 14


def _location(row: tuple[Any, ...]) -> Location:
    repo, commit, stars, path, start, end, symbol, vendored = row[:8]
    expression, shareable, level, conflict, evidence, notices = row[8:_LOCATION_COLUMNS]
    return Location(
        repo,
        commit,
        stars,
        path,
        start,
        end,
        symbol,
        bool(vendored),
        f"https://github.com/{repo}/blob/{commit}/{path}#L{start}-L{end}",
        License(
            expression,
            bool(shareable),
            level,
            bool(conflict),
            json.loads(evidence),
            json.loads(notices),
        ),
    )


def _result(row: tuple[Any, ...], score: float) -> Result:
    location = _location(row[:_LOCATION_COLUMNS])
    hash_, language, code, copies, repos, licenses = row[_LOCATION_COLUMNS + 1 :]
    # No license that lets it be shared (none found means all rights reserved): linked, not shown.
    shown = code if location.license.shareable else None
    return Result(hash_[:ID_LENGTH], score, language, shown, location, copies, repos, licenses)


def write_lexical(records: Path, out: Path) -> None:
    """Write the lexical index of every snippet in records.sqlite into out, replacing it whole."""
    part = out.with_name(out.name + ".part")
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    index = tantivy.Index(_schema(), path=str(part))
    writer = index.writer(heap_size=1 << 30)
    db = sqlite3.connect(records)
    rows = db.execute(
        "SELECT s.id, s.language, s.code, s.doc, l.symbol, r.name, f.path, f.permissive,"
        " f.level, f.choices FROM snippets s JOIN locations l ON l.id = s.location"
        " JOIN files f ON f.id = l.file JOIN repos r ON r.id = f.repo ORDER BY s.id"
    )
    for snippet, language, code, doc_text, symbol, repo, path, permissive, level, choices in rows:
        doc = tantivy.Document()
        doc.add_unsigned("id", snippet)
        doc.add_text("symbol", " ".join(terms(symbol)))
        doc.add_text("doc", " ".join(terms(doc_text)))
        doc.add_text("path", " ".join(terms(f"{repo}/{path}")))
        doc.add_text("code", " ".join(terms(code)))
        doc.add_text("language", language)
        doc.add_unsigned("permissive", permissive)
        doc.add_text("level", level)
        for identifier in identifiers(code):
            doc.add_text("identifiers", identifier)
        for choice in json.loads(choices):
            doc.add_text("license", " + ".join(sorted(spdx.lower() for spdx in choice)))
        writer.add_document(doc)
    writer.commit()
    writer.wait_merging_threads()
    db.close()
    shutil.rmtree(out, ignore_errors=True)
    part.replace(out)


def _schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    builder.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    for field in FIELDS:
        builder.add_text_field(field, tokenizer_name="en_stem", index_option="freq")
    for field in ("language", "level", "license", "identifiers"):
        builder.add_text_field(field, tokenizer_name="raw", index_option="basic")
    builder.add_unsigned_field("permissive", indexed=True)
    return builder.build()
