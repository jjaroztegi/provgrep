"""Search the index: the one query engine behind the command line, the HTTP API and the MCP
server.

Lexical search over each snippet's name, comments and docstrings, code and path. Identifiers are
split into words (`parseRtpHeader` and `parse_rtp_header` give parse, rtp, header) and a joined
identifier is kept whole as well, then English stemming; BM25 ranks, with a weight per field.
Filters (language, licenses, level) are part of the query, so they act before ranking.

With embeddings of the code (dense.py), the first RERANK results are then ordered by meaning:
how close each one's code is to the query.

tantivy-py scores every document a query matches (it collects without block-max pruning), so the
time a search takes is the number of documents it scores. Two things keep that down: a document
must have one of the query's rarer terms (common ones only add to the score of those), and the
index is cut into partitions searched at once, each a sample of the whole so their scores
compare (build.py).

Code is returned with its license, evidence and notices, and not at all when no license was
found: that rule is applied here, once, for every interface. Asked for a project's license, a
search keeps the code OSADL's matrix says may go into it, and says why (compatibility.py).
"""

import itertools
import json
import re
import sqlite3
from collections.abc import Sequence
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import tantivy
from tantivy import Occur, Query

from provgrep import compatibility, dense
from provgrep.compatibility import Verdict

FIELDS = {"symbol": 2.0, "doc": 2.0, "path": 1.0, "code": 1.0}
IDENTIFIER = 3.0
LEVELS = ("A", "B", "C", "X")
# Each combination of the licenses in an allow list is one term of the query.
MAX_ALLOWED = 8
MAX_QUERY = 1000  # characters: a question, not a file
MAX_REQUIRED = 8
ID_LENGTH = 16  # hex digits of a snippet's hash that name it
MAX_COPIES = 1000  # listed at most for a snippet: one has 3,069, all in one repository
HASH_BYTES = 16  # of a code's sha256, kept to tell functions apart
BUCKETS = 256  # by the first byte of the hash: a partition is a run of them
# A term in more than this share of a field's documents does not make a document match on its
# own, as in Lucene's common terms query. At 0.01 the intent suites rank as well as with no cut,
# and a search scores a third of the documents.
COMMON = 0.01
RERANK = 100  # lexical results ordered by meaning, when the index has embeddings
# en_stem as tantivy defines it: the tokens the text fields hold.
_ANALYZER = (
    tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
    .filter(tantivy.Filter.remove_long(40))
    .filter(tantivy.Filter.lowercase())
    .filter(tantivy.Filter.stemmer("english"))
    .build()
)
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


def licenses_of(text: str | None, project: str | None = None) -> str | list[str]:
    """The licenses filter as people write it: "permissive", "any", or SPDX ids, comma-separated.
    Unsaid, it is permissive, or any when the project's license already filters."""
    if not text:
        return "any" if project else "permissive"
    word = text.strip().lower()
    return word if word in ("permissive", "any") else text.split(",")


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
    choices: list[list[str]]  # the ways to comply with it, each the licenses it takes together
    permissive: bool  # whether a permissive filter lets it through (licenses.permissive)
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
    score: float  # the cosine of its code with the query among the first RERANK, else BM25
    language: str
    code: str | None  # None when no license was found: link only
    location: Location  # where the code shown is from
    copies: int  # locations of the same code in the corpus
    repos: int
    licenses: int  # distinct licenses among the copies
    compatibility: Verdict | None = None  # with the project's license the search was asked for


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
        self.partitions = len(list((path / "partitions").iterdir()))
        self._dbs = [connect(path, n) for n in range(self.partitions)]
        self._dictionary = dictionary(path)
        self._searchers = [
            tantivy.Index.open(str(path / "partitions" / str(n) / "lexical")).searcher()
            for n in range(self.partitions)
        ]
        self._schema = schema()
        self._documents = sum(searcher.num_docs for searcher in self._searchers)
        self._pool = ThreadPoolExecutor(self.partitions)  # tantivy-py releases the GIL
        self._compatible: dict[str, list[str]] = {}  # license terms, by project license
        self.vectors = None
        if (path / "dense.json").exists():
            self.vectors = dense.Vectors(path, self.manifest["built"], self.partitions)
            # A query asked again (another filter, the next page) is not embedded again
            self._meaning = lru_cache(maxsize=1024)(dense.Encoder(queries=True).query)

    def search(
        self,
        query: str,
        *,
        languages: Sequence[str] = (),
        licenses: str | Sequence[str] = "permissive",
        project: str | None = None,
        levels: Sequence[str] = (),
        require: Sequence[str] = (),
        limit: int = 10,
        offset: int = 0,
    ) -> Page:
        """Snippets for a query, best first. licenses: "permissive", "any", or the SPDX ids
        allowed (a snippet passes if one way to comply with its license needs only those).
        project: the license of the project the code is for; only code OSADL's matrix says may
        go into it passes, each result with the verdict. require: identifiers the code must
        contain, whole; `A|B` for either."""
        if limit < 1 or offset < 0:
            raise ValueError("limit must be at least 1 and offset at least 0")
        if len(query) > MAX_QUERY:
            raise ValueError(f"a query has at most {MAX_QUERY} characters, not {len(query)}")
        if len(require) > MAX_REQUIRED:
            raise ValueError(f"require at most {MAX_REQUIRED} identifiers, not {len(require)}")
        languages = [language.lower() for language in languages]
        if project is not None:
            project = compatibility.project(project)
        clauses = []
        text = self._text(query_terms(query))
        if text is not None:
            clauses.append((Occur.Must, text))
        filters = []
        for identifier in require:
            whole = [w for alt in identifier.split("|") for w in identifiers(alt)]
            if not whole:
                raise ValueError(f"not an identifier: {identifier!r}")
            filters.append(self._any("identifiers", whole))
        if text is None and not filters:
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
        if project is not None:
            fits = self._fits(project)
            filters.append(Query.term_set_query(self._schema, "license", fits))
        if levels:
            filters.append(self._any("level", levels))
        clauses += [(Occur.Must, Query.const_score_query(f, 0.0)) for f in filters]
        combined = Query.boolean_query(clauses)
        vectors = self.vectors if text is not None else None  # nothing to order by without words
        depth = max(offset + limit, RERANK) if vectors is not None else offset + limit
        searches = [self._pool.submit(s.search, combined, depth) for s in self._searchers]
        # The query is embedded while the shards are searched: it needs none of their results.
        meaning = self._meaning(query) if vectors is not None else None
        found = [future.result() for future in searches]
        hits = sorted(
            ((score, n, address) for n, part in enumerate(found) for score, address in part.hits),
            key=lambda hit: -hit[0],
        )[:depth]
        # A snippet is (partition, its id there).
        ranked = [
            (score, (n, self._searchers[n].doc(address)["id"][0])) for score, n, address in hits
        ]
        if vectors is not None:
            head = [snippet for _, snippet in ranked[:RERANK]]
            cosines = vectors.similarity(meaning, head)
            ranked = sorted(zip(cosines, head, strict=True), key=lambda h: -h[0]) + ranked[RERANK:]
        ranked = ranked[offset : offset + limit]
        rows = self._rows([snippet for _, snippet in ranked])
        total = sum(part.count for part in found)  # type: ignore[attr-defined]  # not in its stubs
        return Page(total, [_result(rows[snippet], score, project) for score, snippet in ranked])

    def snippet(self, id: str, copies: int = MAX_COPIES) -> Snippet | None:
        """A snippet by its id, with the first `copies` locations of its code (at most
        MAX_COPIES); its result's `copies` counts them all."""
        found = self._find(id)
        if found is None:
            return None
        n, snippet = found
        listed = (
            self._dbs[n]
            .execute(
                f"SELECT {_LOCATION} FROM locations l JOIN files f ON f.id = l.file"
                " JOIN repos r ON r.id = f.repo WHERE l.snippet = ? ORDER BY l.id LIMIT ?",
                (snippet, max(0, min(copies, MAX_COPIES))),
            )
            .fetchall()
        )
        result = _result(self._rows([found])[found], 0.0)
        return Snippet(result, [_location(c) for c in listed])

    def code_of(self, id: str) -> str | None:
        """A snippet's code whatever its license: to measure search with, never to show."""
        found = self._find(id)
        if found is None:
            return None
        n, snippet = found
        (code,) = self._dbs[n].execute("SELECT code FROM snippets WHERE id = ?", (snippet,))
        return self._code(code[0])

    def _find(self, id: str) -> tuple[int, int] | None:
        """The partition a snippet id is in, and its id there."""
        if len(id) != ID_LENGTH or not all(c in "0123456789abcdef" for c in id):
            return None
        n = partition(int(id[:2], 16), self.partitions)
        low = bytes.fromhex(id)
        high = (int(id, 16) + 1).to_bytes(ID_LENGTH // 2) if id != "f" * ID_LENGTH else b"\xff" * 9
        row = (
            self._dbs[n]
            .execute("SELECT id FROM snippets WHERE hash >= ? AND hash < ?", (low, high))
            .fetchone()
        )
        return None if row is None else (n, row[0])

    def _code(self, blob: bytes) -> str:
        return zstd.decompress(blob, zstd_dict=self._dictionary).decode()

    def _text(self, words: list[str]) -> Query | None:
        """The words in every field, weighted; a document must have one of the rarer ones (see
        COMMON), unless they are all common."""
        rare: list[tuple[Occur, Query]] = []
        common: list[tuple[Occur, Query]] = []
        for word in words:
            term, _, weight = word.partition("^")
            for token in _ANALYZER.analyze(term):
                for field, boost in FIELDS.items():
                    documents = sum(s.doc_freq(field, token) for s in self._searchers)
                    if not documents:
                        continue
                    found = Query.term_query(self._schema, field, token, index_option="freq")
                    clause = (Occur.Should, Query.boost_query(found, boost * float(weight or 1)))
                    (common if documents > COMMON * self._documents else rare).append(clause)
        if not rare or not common:
            return Query.boolean_query(rare or common) if rare or common else None
        return Query.boolean_query(
            [(Occur.Must, Query.boolean_query(rare)), (Occur.Should, Query.boolean_query(common))]
        )

    def _fits(self, project: str) -> list[str]:
        """The license terms in the index (a way to comply, its SPDX ids lower-cased, joined by
        " + ") whose verdict for the project is yes."""
        if project not in self._compatible:
            indexed = {t for s in self._searchers for t, _ in s.terms_with_prefix("license", "")}
            self._compatible[project] = sorted(
                term
                for term in indexed
                if compatibility.verdict(project, [term.split(" + ")]).answer == "yes"
            )
        return self._compatible[project]

    def _any(self, field: str, values: Sequence[str]) -> Query:
        return Query.boolean_query(
            [(Occur.Should, Query.term_query(self._schema, field, v)) for v in values]
        )

    def _rows(self, snippets: list[tuple[int, int]]) -> dict[tuple[int, int], tuple[Any, ...]]:
        """The rows of snippets, by (partition, id), with their code decompressed."""
        out = {}
        for n in {n for n, _ in snippets}:
            ids = [snippet for m, snippet in snippets if m == n]
            rows = self._dbs[n].execute(
                f"SELECT {_LOCATION}, s.id, s.hash, s.language, s.code, s.copies, s.repos,"
                " s.licenses FROM snippets s JOIN locations l ON l.id = s.location"
                " JOIN files f ON f.id = l.file JOIN repos r ON r.id = f.repo"
                f" WHERE s.id IN ({','.join('?' * len(ids))})",
                ids,
            )
            for row in rows:
                code = self._code(row[_LOCATION_COLUMNS + 3])
                out[(n, row[_LOCATION_COLUMNS])] = (*row[: _LOCATION_COLUMNS + 3], code, *row[-3:])
        return out


_LOCATION = (
    "r.name, r.commit_id, r.stars, f.path, l.start, l.end, l.symbol, f.vendored,"
    " f.license, f.choices, f.permissive, f.shareable, f.level, f.conflict, f.evidence, f.notices"
)
_LOCATION_COLUMNS = 16


def _location(row: tuple[Any, ...]) -> Location:
    repo, commit, stars, path, start, end, symbol, vendored = row[:8]
    expression, choices, permissive, shareable, level, conflict, evidence, notices = row[8:16]
    return Location(
        repo,
        commit,
        stars,
        path,
        start,
        end,
        symbol,
        bool(vendored),
        f"https://github.com/{repo}/blob/{commit}/{quote(path)}#L{start}-L{end}",
        License(
            expression,
            json.loads(choices),
            bool(permissive),
            bool(shareable),
            level,
            bool(conflict),
            json.loads(evidence),
            json.loads(notices),
        ),
    )


def _result(row: tuple[Any, ...], score: float, project: str | None = None) -> Result:
    location = _location(row[:_LOCATION_COLUMNS])
    hash_, language, code, copies, repos, licenses = row[_LOCATION_COLUMNS + 1 :]
    # No license that lets it be shared (none found means all rights reserved): linked, not shown.
    shown = code if location.license.shareable else None
    fits = compatibility.verdict(project, location.license.choices) if project else None
    id = hash_.hex()[:ID_LENGTH]
    return Result(id, score, language, shown, location, copies, repos, licenses, fits)


def buckets(n: int, partitions: int) -> range:
    """The buckets of partition n of an index cut into that many."""
    return range(n * BUCKETS // partitions, (n + 1) * BUCKETS // partitions)


def partition(bucket: int, partitions: int) -> int:
    """The partition a bucket is in: the last whose buckets start at it or before."""
    return ((bucket + 1) * partitions + BUCKETS - 1) // BUCKETS - 1


def connect(index: Path, n: int) -> sqlite3.Connection:
    """Partition n's records, read-only, with the index's sources attached as `sources`."""

    def uri(name: Path) -> str:
        return f"{name.resolve().as_uri()}?mode=ro&immutable=1"

    records = index / "partitions" / str(n) / "records.sqlite"
    db = sqlite3.connect(uri(records), uri=True, check_same_thread=False)
    db.execute("ATTACH DATABASE ? AS sources", (uri(index / "sources.sqlite"),))
    return db


def dictionary(index: Path) -> zstd.ZstdDict | None:
    """The zstd dictionary an index's code is compressed with (none for very little code)."""
    path = index / "code.zdict"
    return zstd.ZstdDict(path.read_bytes()) if path.exists() else None


def document(
    snippet: int,
    language: str,
    code: str,
    text: str,
    symbol: str,
    path: str,
    permissive: bool,
    level: str,
    choices: list[list[str]],
) -> tantivy.Document:
    """A snippet as the lexical index holds it. text: its comments and docstrings; path: its
    repository's name and its file's path."""
    doc = tantivy.Document()
    doc.add_unsigned("id", snippet)
    doc.add_text("symbol", " ".join(terms(symbol)))
    doc.add_text("doc", " ".join(terms(text)))
    doc.add_text("path", " ".join(terms(path)))
    doc.add_text("code", " ".join(terms(code)))
    doc.add_text("language", language)
    doc.add_unsigned("permissive", permissive)
    doc.add_text("level", level)
    for identifier in identifiers(code):
        doc.add_text("identifiers", identifier)
    for choice in choices:
        doc.add_text("license", " + ".join(sorted(spdx.lower() for spdx in choice)))
    return doc


def schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    builder.add_unsigned_field("id", stored=True, indexed=True, fast=True)
    for field in FIELDS:
        builder.add_text_field(field, tokenizer_name="en_stem", index_option="freq")
    for field in ("language", "level", "license", "identifiers"):
        builder.add_text_field(field, tokenizer_name="raw", index_option="basic")
    builder.add_unsigned_field("permissive", indexed=True)
    return builder.build()
