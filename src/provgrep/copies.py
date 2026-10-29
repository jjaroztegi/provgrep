"""Functions found in several repositories, and how often their copies disagree on the license.

A snippet is one normalized function wherever it is found. Its copies can be under different
licenses: vendored code under its own, a fork that relicensed, code moved between projects. The
dangerous kind for someone filtering for permissive code is a function that is permissive in one
place and copyleft in another, since which copy came first is not decided here. Short helpers
are often written alike in many projects, so the counts are given by length too.
"""

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from provgrep import search, source_commit

LENGTHS = (3, 10, 20, 40)
EXAMPLES = 20


def study(index: Path, out_root: Path) -> Path:
    """Write results/<UTC time>-copies/: the counts, and examples of the longest disagreements."""
    started = time.monotonic()
    manifest = json.loads((index / "manifest.json").read_text(encoding="utf-8"))
    partitions = len(list((index / "partitions").iterdir()))
    dbs = [search.connect(index, n) for n in range(partitions)]

    def count(where: str) -> int:
        return sum(db.execute(f"SELECT count(*) FROM snippets {where}").fetchone()[0] for db in dbs)

    counts: dict[str, Any] = {
        "snippets": count(""),
        "in more than one place": count("WHERE copies > 1"),
        "in more than one repository": count("WHERE repos > 1"),
        "under more than one license": count("WHERE licenses > 1"),
    }
    # Each snippet whose copies disagree, across repositories: its lines, whether some copy is
    # permissive and some copyleft, and whether some copy is vendored.
    rows = [
        (n, *row)
        for n, db in enumerate(dbs)
        for row in db.execute(
            # The lines of the copy shown: its code's.
            """SELECT s.id, shown.end - shown.start + 1, s.repos, max(f.permissive),
                      max(f.shareable AND NOT f.permissive), max(f.vendored)
               FROM snippets s
               JOIN locations shown ON shown.snippet = s.id AND shown.id = s.location
               JOIN locations l ON l.snippet = s.id JOIN files f ON f.id = l.file
               WHERE s.licenses > 1 AND s.repos > 1 GROUP BY s.id"""
        )
    ]
    both = [r for r in rows if r[4] and r[5]]
    counts["permissive and copyleft, across repositories"] = {
        f"{n}+ lines": {
            "functions": sum(r[2] >= n for r in both),
            "with a vendored copy": sum(r[2] >= n and bool(r[6]) for r in both),
        }
        for n in LENGTHS
    }
    longest = sorted((r for r in both if r[2] >= LENGTHS[-1]), key=lambda r: (-r[3], -r[2]))
    examples = [_copies(dbs[r[0]], r[1]) for r in longest[:EXAMPLES]]
    run = {
        "index_built": manifest.get("built"),
        "index_commit": manifest.get("commit"),
        "corpus_sha256": manifest.get("corpus", {}).get("sha256"),
        "commit": source_commit(),
        "seconds": round(time.monotonic() - started),
    }
    out = out_root / (datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-copies")
    out.mkdir(parents=True)
    (out / "run.json").write_text(json.dumps(run, indent=1) + "\n", encoding="utf-8")
    (out / "counts.json").write_text(json.dumps(counts, indent=1) + "\n", encoding="utf-8")
    with (out / "examples.jsonl").open("w", encoding="utf-8") as f:
        f.writelines(json.dumps(e) + "\n" for e in examples)
    return out


def _copies(db: sqlite3.Connection, snippet: int) -> list[dict[str, Any]]:
    return [
        {
            "repo": repo,
            "license": license,
            "vendored": bool(vendored),
            "symbol": symbol,
            "url": f"https://github.com/{repo}/blob/{commit}/{quote(path)}#L{start}-L{end}",
        }
        for repo, commit, path, start, end, symbol, license, vendored in db.execute(
            """SELECT r.name, r.commit_id, f.path, l.start, l.end, l.symbol, f.license, f.vendored
               FROM locations l JOIN files f ON f.id = l.file JOIN repos r ON r.id = f.repo
               WHERE l.snippet = ? ORDER BY r.id""",
            (snippet,),
        )
    ]
