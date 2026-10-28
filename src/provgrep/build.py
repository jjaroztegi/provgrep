"""Build an index from a pinned corpus: every function of every repository, the license it is
under with the evidence for it, its notices, and where else the same code is found.

An index is cut into partitions by the hash of each function's code, so that it grows by adding
partitions, each about the size one core searches in tens of milliseconds:

- every copy of a function falls in the same partition, so each one finds its copies, picks the
  one to show and counts them alone, and partitions are built at once, each in memory a fraction
  of the corpus needs;
- each holds a random sample of the corpus, so its BM25 statistics are the whole index's.

The build writes `<out>.part/`, in steps, then puts it in place of out:

1. Staging (stage.py): one file per repository, kept in the cache under a key of the code and
   tools that staged it. A repository staged under the current key is not fetched again; staged
   files under another key are removed.
2. sources.sqlite: the repositories and their files, each with its license, evidence and
   notices. The functions are routed meanwhile, by the first byte of their hash, to BUCKETS
   spool files; a partition is a run of buckets.
3. partitions/<n>/: records.sqlite, the distinct functions (code compressed with code.zdict, a
   zstd dictionary trained on the corpus) and their locations; lexical/, the search index over
   them (search.py). The location shown for a function is its first copy outside a vendored
   directory, in corpus order (most-starred repository first): the same corpus gives the same
   index.
4. manifest.json: what was built from what, with which versions, and what was left out.

What people asked to have removed (corpus/removed.json) is left out of every build: whole
repositories, files or directories of one, and functions by id, wherever their code is.
"""

import gzip
import json
import multiprocessing
import pickle
import random
import shutil
import sqlite3
import sys
import time
from collections import Counter
from compression import zstd
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import Any

import tantivy

from provgrep import github, search, source_commit, stage
from provgrep.fetch import cache_dir, sha256_of
from provgrep.licenses import choices, permissive, shareable

# Repositories a staging worker stages before a new one takes its place. A worker keeps the
# memory of the largest repository it has staged (the allocator rarely gives it back): twelve that
# lived through the whole corpus held 44 GB, one of them 10 GB. A new one loads ScanCode's license
# index in 3.4 s.
TASKS_PER_WORKER = 8
# Locations (copies of functions) per partition. T1, the 500 most-starred repositories of each
# language, has 26.5 M in 21.3 M functions: 8 partitions of 2.7 M functions, searched in 20 ms.
PARTITION_SIZE = 3_400_000
DICTIONARY_SIZE = 64 << 10  # 2.9 times smaller code at level 9, against 2.3 without one
SAMPLES = 20_000  # functions the dictionary is trained on
CODE_LEVEL = 9
SOURCES = """
CREATE TABLE repos (
    id INTEGER PRIMARY KEY,  -- corpus order
    name TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    committed TEXT NOT NULL,
    stars INTEGER NOT NULL,
    language TEXT NOT NULL,  -- what the repository was chosen for
    github_license TEXT
);
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    repo INTEGER NOT NULL REFERENCES repos,
    path TEXT NOT NULL,
    language TEXT NOT NULL,
    vendored INTEGER NOT NULL,
    license TEXT,  -- SPDX expression, NULL when none was found
    level TEXT NOT NULL,
    conflict INTEGER NOT NULL,
    permissive INTEGER NOT NULL,
    shareable INTEGER NOT NULL,  -- whether the code may be shown (licenses.shareable)
    choices TEXT NOT NULL,  -- JSON: the ways to comply, each a list of SPDX ids
    evidence TEXT NOT NULL,  -- JSON
    notices TEXT NOT NULL  -- JSON: copyright statements
);
"""
RECORDS = """
CREATE TABLE snippets (
    id INTEGER PRIMARY KEY,  -- order of first appearance in the partition
    hash BLOB NOT NULL UNIQUE,  -- the first 16 bytes of the sha256 of the normalized code
    language TEXT NOT NULL,
    code BLOB NOT NULL,  -- as found at the location shown, zstd with code.zdict
    location INTEGER NOT NULL,  -- the one shown
    copies INTEGER NOT NULL,
    repos INTEGER NOT NULL,
    licenses INTEGER NOT NULL  -- distinct license expressions among the copies
);
CREATE TABLE locations (
    id INTEGER PRIMARY KEY,
    snippet INTEGER NOT NULL REFERENCES snippets,
    file INTEGER NOT NULL,  -- in sources.sqlite
    symbol TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL
);
CREATE INDEX locations_snippet ON locations (snippet);
"""


def build(
    corpus: Path, out: Path, jobs: int, removed: Path, limit: int | None = None
) -> dict[str, Any]:
    """Build the index of a pinned corpus into out, without what removed lists; return the
    manifest. With limit, only the first `limit` repositories of each language."""
    started = time.monotonic()
    commit = source_commit()  # the code that runs is the code at the start
    repos = load(corpus, limit)
    left_out = Removed.load(removed)
    repos = [r for r in repos if r.name.lower() not in left_out.repos]  # not even fetched
    staging, failed = stage_corpus(repos, jobs)

    part = out.with_name(out.name + ".part")
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    counts = write(repos, staging, part, jobs, left_out)
    manifest = {
        "corpus": {
            "path": corpus.as_posix(),
            "sha256": sha256_of(corpus),
            "repos": len(repos),
            "limit_per_language": limit,
        },
        "removed": {"path": removed.as_posix(), "sha256": sha256_of(removed)}
        if removed.exists()
        else None,
        "built": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": commit,
        "stage_key": staging.name,
        "versions": {name: version(name) for name in (*stage.TOOLS, "tantivy")},
        "counts": counts,
        "failed": failed,
        "seconds": round(time.monotonic() - started),
    }
    (part / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    shutil.rmtree(out, ignore_errors=True)
    part.replace(out)
    return manifest


def load(corpus: Path, limit: int | None = None) -> list[github.Repo]:
    """The repositories of a pinned corpus; with limit, the first `limit` of each language."""
    repos = github.load(corpus)
    if limit is None:
        return repos
    taken: Counter[str] = Counter()
    kept = []
    for repo in repos:
        taken[repo.language] += 1
        if taken[repo.language] <= limit:
            kept.append(repo)
    return kept


def stage_corpus(repos: list[github.Repo], jobs: int) -> tuple[Path, dict[str, str]]:
    """Stage the repositories the cache does not have under the current key; return where they
    are staged, and the ones that failed, with why. Staged files are named by repository and
    commit, so machines that stage parts of a corpus merge them by copying the files."""
    staging = cache_dir() / "staging" / stage.key()
    staging.mkdir(parents=True, exist_ok=True)
    for old in staging.parent.iterdir():
        if old != staging:
            shutil.rmtree(old)
    todo = [r for r in repos if not stage.path(staging, r).exists()]
    return staging, stage_all(todo, staging, jobs, cache_dir() / "work")


@dataclass(frozen=True)
class Removed:
    """What the build leaves out, as asked: repositories (owner/name, lower-cased), paths in
    one (a file, or a directory and all under it), and functions by id."""

    repos: frozenset[str] = frozenset()
    paths: dict[str, tuple[str, ...]] = field(default_factory=dict)
    functions: frozenset[str] = frozenset()

    @classmethod
    def load(cls, path: Path) -> Removed:
        """corpus/removed.json: {"removed": [{"repo": ..., "path": ..., "issue": ...},
        {"function": ..., "issue": ...}]}; a missing file removes nothing."""
        if not path.exists():
            return cls()
        asked = json.loads(path.read_text(encoding="utf-8"))["removed"]
        paths: dict[str, list[str]] = {}
        for entry in asked:
            if "path" in entry:
                paths.setdefault(entry["repo"].lower(), []).append(entry["path"].strip("/"))
        return cls(
            frozenset(e["repo"].lower() for e in asked if "repo" in e and "path" not in e),
            {repo: tuple(prefixes) for repo, prefixes in paths.items()},
            frozenset(e["function"] for e in asked if "function" in e),
        )

    def file(self, repo: str, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self.paths.get(repo.lower(), ()))


NOTHING = Removed()


def write(
    repos: list[github.Repo], staging: Path, out: Path, jobs: int, removed: Removed = NOTHING
) -> dict[str, Any]:
    """Write the sources, dictionary and partitions of the staged repositories into out, without
    what was removed; return the counts."""
    spool = out / "spool"
    counts, sizes = _route(
        [r for r in repos if r.name.lower() not in removed.repos], staging, out, spool, removed
    )
    partitions = max(1, -(-sum(sizes) // PARTITION_SIZE))
    totals: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    levels: Counter[str] = Counter()
    with ProcessPoolExecutor(min(jobs, partitions)) as pool:
        done = [
            pool.submit(_reduce, spool, out, n, search.buckets(n, partitions))
            for n in range(partitions)
        ]
        for future in done:
            numbers, by_language, by_level = future.result()
            totals.update(numbers)
            languages.update(by_language)
            levels.update(by_level)
    shutil.rmtree(spool)
    return {
        **counts,
        **totals,
        "by_language": dict(sorted(languages.items())),
        "by_level": dict(sorted(levels.items())),
        "partitions": partitions,
    }


def _route(
    repos: list[github.Repo], staging: Path, out: Path, spool: Path, removed: Removed
) -> tuple[dict[str, Any], list[int]]:
    """Write sources.sqlite and code.zdict from the staged repositories, in corpus order, and
    each file's functions to the spool of their bucket; return the counts and the locations in
    each bucket."""
    db = sqlite3.connect(out / "sources.sqlite")
    db.executescript(SOURCES)
    spool.mkdir()
    buckets = [zstd.open(spool / f"{b:02x}", "wb", level=1) for b in range(search.BUCKETS)]
    sizes = [0] * search.BUCKETS
    kinds: Counter[str] = Counter()
    skipped: dict[str, str] = {}
    samples: list[bytes] = []
    pick = random.Random(0)
    file_id = routed = 0
    staged = 0
    left_out: Counter[str] = Counter()
    for repo_id, repo in enumerate(repos, 1):
        staged_path = stage.path(staging, repo)
        if not staged_path.exists():
            continue  # failed; listed in the manifest
        staged += 1
        db.execute(
            "INSERT INTO repos VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                repo_id,
                repo.name,
                repo.commit,
                repo.committed,
                repo.stars,
                repo.language,
                repo.license,
            ),
        )
        with gzip.open(staged_path, "rt", encoding="utf-8") as lines:
            for line in lines:
                f = json.loads(line)
                if "skipped" in f:
                    skipped[repo.name] = f["skipped"]
                if "kinds" in f:
                    kinds.update(f["kinds"])
                    continue
                if removed.file(repo.name, f["path"]):
                    left_out["files"] += 1
                    continue
                file_id += 1
                expression = f["expression"]
                ways = [list(c) for c in choices(expression)] if expression else []
                db.execute(
                    "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        file_id,
                        repo_id,
                        f["path"],
                        f["language"],
                        f["vendored"],
                        expression,
                        f["level"],
                        f["conflict"],
                        permissive(expression),
                        shareable(expression),
                        json.dumps(ways),
                        json.dumps(f["evidence"]),
                        json.dumps(f["notices"]),
                    ),
                )
                units: dict[int, list[tuple[Any, ...]]] = {}
                for u in f["units"]:
                    if u["hash"][: search.ID_LENGTH] in removed.functions:
                        left_out["functions"] += 1
                        continue
                    units.setdefault(int(u["hash"][:2], 16), []).append(
                        (u["hash"], u["code"], u["doc"], u["symbol"], u["start"], u["end"])
                    )
                    routed += 1
                    if len(samples) < SAMPLES:  # a uniform sample: reservoir sampling
                        samples.append(u["code"].encode())
                    elif (n := pick.randrange(routed)) < SAMPLES:
                        samples[n] = u["code"].encode()
                copy = _File(
                    repo_id,
                    repo.name,
                    file_id,
                    f["path"],
                    f["language"],
                    f["vendored"],
                    expression,
                    permissive(expression),
                    f["level"],
                    ways,
                )
                for b, found in units.items():
                    pickle.dump((copy, found), buckets[b])
                    sizes[b] += len(found)
    for bucket in buckets:
        bucket.close()
    db.commit()
    db.close()
    try:
        dictionary = zstd.train_dict(samples, DICTIONARY_SIZE).dict_content
    except ValueError, zstd.ZstdError:  # too little code to train on: compressed without one
        pass
    else:
        (out / "code.zdict").write_bytes(dictionary)
    counts = {"repos": staged, "skipped": skipped, "files": dict(kinds), "locations": routed}
    return counts | {"removed": dict(left_out)}, sizes


@dataclass(frozen=True)
class _File:
    """What a file's functions need of it in their partition."""

    repo: int
    repo_name: str
    id: int
    path: str
    language: str
    vendored: bool
    license: str | None
    permissive: bool
    level: str
    choices: list[list[str]]


@dataclass
class _Snippet:
    id: int
    hash: str
    location: int  # the one shown, and what it was found as
    file: _File
    code: str
    doc: str
    symbol: str
    copies: int = 1
    repos: set[int] = field(default_factory=set)
    licenses: set[str] = field(default_factory=set)


def _reduce(
    spool: Path, out: Path, n: int, buckets: range
) -> tuple[Counter[str], Counter[str], Counter[str]]:
    """Write partition n from the spools of its buckets; return its counts, of functions by
    language and by level. One bucket at a time is in memory."""
    here = out / "partitions" / str(n)
    (here / "lexical").mkdir(parents=True)
    db = sqlite3.connect(here / "records.sqlite")
    db.executescript(RECORDS)
    writer = tantivy.Index(search.schema(), path=str(here / "lexical")).writer(heap_size=1 << 29)
    compressor = zstd.ZstdCompressor(CODE_LEVEL, zstd_dict=search.dictionary(out))
    counts: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    levels: Counter[str] = Counter()
    snippets = locations = 0
    for b in buckets:
        seen: dict[str, _Snippet] = {}
        found: list[tuple[int, int, int, str, int, int]] = []
        with zstd.open(spool / f"{b:02x}", "rb") as f:
            while True:
                try:
                    copy, units = pickle.load(f)  # written by _route, in this build
                except EOFError:
                    break
                for hash_, code, doc, symbol, start, end in units:
                    locations += 1
                    s = seen.get(hash_)
                    if s is None:
                        snippets += 1
                        s = seen[hash_] = _Snippet(
                            snippets, hash_, locations, copy, code, doc, symbol
                        )
                    else:
                        s.copies += 1
                        if s.file.vendored and not copy.vendored:  # shown from here instead
                            s.location, s.file, s.code, s.doc = locations, copy, code, doc
                            s.symbol = symbol
                    s.repos.add(copy.repo)
                    s.licenses.add(copy.license or "")
                    found.append((locations, s.id, copy.id, symbol, start, end))
        db.executemany("INSERT INTO locations VALUES (?, ?, ?, ?, ?, ?)", found)
        for s in seen.values():
            row = (
                s.id,
                bytes.fromhex(s.hash[: 2 * search.HASH_BYTES]),
                s.file.language,
                compressor.compress(s.code.encode(), zstd.ZstdCompressor.FLUSH_FRAME),
                s.location,
                s.copies,
                len(s.repos),
                len(s.licenses),
            )
            db.execute("INSERT INTO snippets VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
            writer.add_document(
                search.document(
                    s.id,
                    s.file.language,
                    s.code,
                    s.doc,
                    s.symbol,
                    f"{s.file.repo_name}/{s.file.path}",
                    s.file.permissive,
                    s.file.level,
                    s.file.choices,
                )
            )
            counts["snippets_with_copies"] += s.copies > 1
            counts["snippets_whose_copies_differ_in_license"] += len(s.licenses) > 1
            languages[s.file.language] += 1
            levels[s.file.level] += 1
        counts["snippets"] += len(seen)
    db.commit()
    db.close()
    writer.commit()
    writer.wait_merging_threads()
    return counts, languages, levels


def stage_all(repos: list[github.Repo], staging: Path, jobs: int, work: Path) -> dict[str, str]:
    """Stage repositories in parallel; return the ones that failed, with why. Workers unpack
    under work, which is emptied whenever none is running.

    A worker that dies (a crash in native code, the kernel ending it for memory) takes the pool
    with it, and its siblings are ended without cleaning up. The repositories being staged then,
    the ones with a `.part` file, are staged again each in a process of its own, so a crash is
    only that repository's; the ones not started go to a new pool.
    """
    expected = stage.key()
    failed: dict[str, str] = {}
    done = 0

    def report(repo: github.Repo, outcome: str) -> None:
        nonlocal done
        done += 1
        print(f"[{done}/{len(repos)}] {repo.name}: {outcome}", file=sys.stderr)

    pending = repos
    while pending:
        _empty(work)
        suspects: list[github.Repo] = []
        waiting: list[github.Repo] = []
        # The platform's start method: max_tasks_per_child alone would make it spawn.
        context = multiprocessing.get_context()
        with ProcessPoolExecutor(jobs, context, max_tasks_per_child=TASKS_PER_WORKER) as pool:
            futures = {
                pool.submit(stage.stage, repo, stage.path(staging, repo), expected, work): repo
                for repo in pending
            }
            for future in as_completed(futures):
                repo = futures[future]
                try:
                    report(repo, f"{future.result()} units")
                except BrokenProcessPool:
                    running = stage.part(stage.path(staging, repo)).exists()
                    (suspects if running else waiting).append(repo)
                except Exception as e:  # one broken repository must not lose the build
                    failed[repo.name] = f"{type(e).__name__}: {e}"
                    report(repo, failed[repo.name])
        _empty(work)
        if not suspects and len(waiting) == len(pending):
            # The pool died before staging anything: it would again.
            for repo in waiting:
                failed[repo.name] = "the worker pool broke before any repository started"
                report(repo, failed[repo.name])
            break
        with ThreadPoolExecutor(jobs) as threads:
            outcomes = threads.map(partial(_isolated, staging=staging, work=work), suspects)
            for repo, outcome in zip(suspects, outcomes, strict=True):
                if isinstance(outcome, int):
                    report(repo, f"{outcome} units")
                else:
                    failed[repo.name] = outcome
                    report(repo, outcome)
        pending = waiting
    _empty(work)
    return failed


def _empty(work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)


def _isolated(repo: github.Repo, staging: Path, work: Path) -> int | str:
    """Stage a repository in a process of its own: its units, or why it failed."""
    with ProcessPoolExecutor(1) as pool:
        try:
            return pool.submit(
                stage.stage, repo, stage.path(staging, repo), stage.key(), work
            ).result()
        except Exception as e:
            return f"{type(e).__name__}: {e}"
