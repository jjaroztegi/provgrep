"""Build an index from a pinned corpus: every function of every repository, the license it is
under with the evidence for it, its notices, and where else the same code is found.

The build writes into one directory, in steps:

1. staging/<key>/: one file per repository (stage.py). A repository staged under the current key
   is not fetched again; files under another key are removed.
2. records.sqlite: repositories, files, snippets (one per distinct normalized code) and their
   locations, in corpus order. The location shown for a snippet is its first copy outside a
   vendored directory, in the most-starred repository: the same corpus gives the same index.
3. lexical/: the search index over the snippets, in shards searched at once (search.py).
4. manifest.json: what was built from what, with which versions, and what was left out.
"""

import gzip
import json
import shutil
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from provgrep import github, search, source_commit, stage
from provgrep.fetch import sha256_of
from provgrep.licenses import choices, permissive, shareable

SCHEMA = """
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
CREATE TABLE snippets (
    id INTEGER PRIMARY KEY,  -- order of first appearance in the corpus
    hash TEXT NOT NULL UNIQUE,
    language TEXT NOT NULL,
    code TEXT NOT NULL,  -- as found at the location shown
    doc TEXT NOT NULL,
    location INTEGER NOT NULL,  -- the one shown
    copies INTEGER NOT NULL DEFAULT 1,
    repos INTEGER NOT NULL DEFAULT 1,
    licenses INTEGER NOT NULL DEFAULT 1  -- distinct license expressions among the copies
);
CREATE TABLE locations (
    id INTEGER PRIMARY KEY,
    snippet INTEGER NOT NULL REFERENCES snippets,
    file INTEGER NOT NULL REFERENCES files,
    symbol TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL
);
CREATE INDEX locations_snippet ON locations (snippet);
"""


def build(corpus: Path, out: Path, jobs: int, limit: int | None = None) -> dict[str, Any]:
    """Build the index of a pinned corpus into out; return the manifest. With limit, only the
    first `limit` repositories of each language."""
    started = time.monotonic()
    commit = source_commit()  # the code that runs is the code at the start
    repos = github.load(corpus)
    if limit is not None:
        taken: Counter[str] = Counter()
        kept = []
        for repo in repos:
            taken[repo.language] += 1
            if taken[repo.language] <= limit:
                kept.append(repo)
        repos = kept
    key = stage.key()
    staging = out / "staging" / key
    staging.mkdir(parents=True, exist_ok=True)
    for old in staging.parent.iterdir():
        if old != staging:
            shutil.rmtree(old)
    todo = [r for r in repos if not stage.path(staging, r).exists()]
    failed = stage.stage_all(todo, staging, jobs, out / "work")

    records = out / "records.sqlite"
    counts = _records(repos, staging, records)
    search.write_lexical(records, out / "lexical", jobs)

    manifest = {
        "corpus": {
            "path": corpus.as_posix(),
            "sha256": sha256_of(corpus),
            "repos": len(repos),
            "limit_per_language": limit,
        },
        "built": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": commit,
        "stage_key": key,
        "versions": {name: version(name) for name in (*stage.TOOLS, "tantivy")},
        "counts": counts,
        "failed": failed,
        "seconds": round(time.monotonic() - started),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def _add_file(
    db: sqlite3.Connection,
    repo: int,
    f: dict[str, Any],
    snippets: dict[str, tuple[int, bool]],
    locations: int,
) -> int:
    """Insert a staged file and the locations of its units; a unit not seen before is a new
    snippet, one seen only in vendored files is shown from here if this file is not vendored.
    Return the number of locations so far."""
    expression = f["expression"]
    file_id = db.execute(
        "INSERT INTO files (repo, path, language, vendored, license, level, conflict, permissive,"
        " shareable, choices, evidence, notices) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            repo,
            f["path"],
            f["language"],
            f["vendored"],
            expression,
            f["level"],
            f["conflict"],
            permissive(expression),
            shareable(expression),
            json.dumps(choices(expression) if expression else []),
            json.dumps(f["evidence"]),
            json.dumps(f["notices"]),
        ),
    ).lastrowid
    for u in f["units"]:
        locations += 1
        seen = snippets.get(u["hash"])
        if seen is None:
            snippet = len(snippets) + 1
            snippets[u["hash"]] = (snippet, f["vendored"])
            db.execute(
                "INSERT INTO snippets (id, hash, language, code, doc, location)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (snippet, u["hash"], f["language"], u["code"], u["doc"], locations),
            )
        else:
            snippet = seen[0]
            if seen[1] and not f["vendored"]:
                db.execute(
                    "UPDATE snippets SET language = ?, code = ?, doc = ?, location = ?"
                    " WHERE id = ?",
                    (f["language"], u["code"], u["doc"], locations, snippet),
                )
                snippets[u["hash"]] = (snippet, False)
        db.execute(
            "INSERT INTO locations VALUES (?, ?, ?, ?, ?, ?)",
            (locations, snippet, file_id, u["symbol"], u["start"], u["end"]),
        )
    return locations


def _records(repos: list[github.Repo], staging: Path, path: Path) -> dict[str, Any]:
    """Write records.sqlite from the staged repositories, in corpus order; return the counts."""
    part = path.with_name(path.name + ".part")
    part.unlink(missing_ok=True)
    db = sqlite3.connect(part)
    db.executescript(SCHEMA)
    kinds: Counter[str] = Counter()
    skipped: dict[str, str] = {}
    snippets: dict[str, tuple[int, bool]] = {}  # hash: id, whether the location shown is vendored
    locations = 0
    for repo_id, repo in enumerate(repos, 1):
        staged_path = stage.path(staging, repo)
        if not staged_path.exists():
            continue  # failed; listed in the manifest
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
                staged = json.loads(line)
                if "skipped" in staged:
                    skipped[repo.name] = staged["skipped"]
                if "kinds" in staged:
                    kinds.update(staged["kinds"])
                else:
                    locations = _add_file(db, repo_id, staged, snippets, locations)
    db.execute(
        """UPDATE snippets SET (copies, repos, licenses) = (
            SELECT count(*), count(DISTINCT f.repo), count(DISTINCT coalesce(f.license, ''))
            FROM locations l JOIN files f ON f.id = l.file WHERE l.snippet = snippets.id)
        WHERE id IN (SELECT snippet FROM locations GROUP BY snippet HAVING count(*) > 1)"""
    )
    db.commit()
    counts = {
        "repos": db.execute("SELECT count(*) FROM repos").fetchone()[0],
        "skipped": skipped,
        "files": dict(kinds),
        "locations": locations,
        "snippets": len(snippets),
        "snippets_with_copies": db.execute(
            "SELECT count(*) FROM snippets WHERE copies > 1"
        ).fetchone()[0],
        "snippets_whose_copies_differ_in_license": db.execute(
            "SELECT count(*) FROM snippets WHERE licenses > 1"
        ).fetchone()[0],
        "by_language": dict(
            db.execute("SELECT language, count(*) FROM snippets GROUP BY language").fetchall()
        ),
        "by_level": dict(
            db.execute(
                "SELECT f.level, count(*) FROM snippets s JOIN locations l ON l.id = s.location"
                " JOIN files f ON f.id = l.file GROUP BY f.level"
            ).fetchall()
        ),
    }
    db.close()
    part.replace(path)
    return counts
