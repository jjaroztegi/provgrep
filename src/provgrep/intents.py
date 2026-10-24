"""How well search answers what people ask, scored against suites of intents.

An intent is a question in plain words and the identifiers a right answer's code contains
(`A|B` for either). A result is a hit when its code contains all of them, so a suite is scored
without anyone grading results by hand; a hit is not proof the snippet is the best answer, so
the hits are listed for reading.

Each intent is searched twice: with any license (can the code be found?) and with the intent's
own filter, permissive unless it says otherwise (can code you may copy be found?). For both,
coverage says how many snippets in the index contain the identifiers at all, so a miss can be
told apart from a corpus without the answer.
"""

import json
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from provgrep import source_commit
from provgrep.fetch import sha256_of
from provgrep.search import Index

DEPTH = 10  # results read per search


def evaluate(suites: list[Path], index_path: Path, out_root: Path) -> Path:
    """Score every intent of the suites against the index; write results/<UTC time>-search/."""
    started = time.monotonic()
    index = Index(index_path)
    db = sqlite3.connect(f"{(index_path / 'records.sqlite').resolve().as_uri()}?mode=ro", uri=True)
    rows = []
    for suite_path in suites:
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
        for intent in suite["intents"]:
            for mode in ("any", intent.get("licenses", "permissive")):
                rows.append({"suite": suite["suite"], **score(index, db, intent, mode)})
    run = {
        "suites": {p.as_posix(): sha256_of(p) for p in suites},
        "index": index_path.as_posix(),
        "index_built": index.manifest.get("built"),
        "corpus_sha256": index.manifest.get("corpus", {}).get("sha256"),
        "commit": source_commit(),
        "depth": DEPTH,
        "seconds": round(time.monotonic() - started),
    }
    out = out_root / (datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-search")
    out.mkdir(parents=True)
    (out / "run.json").write_text(json.dumps(run, indent=1) + "\n", encoding="utf-8")
    with (out / "intents.jsonl").open("w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    (out / "summary.md").write_text(summary(rows, run), encoding="utf-8")
    return out


def score(
    index: Index, db: sqlite3.Connection, intent: dict[str, Any], mode: Any
) -> dict[str, Any]:
    """The rank of the first hit in the top DEPTH results (None if there is none), the hits, and
    how many snippets in the index contain what is expected."""
    languages = intent.get("languages", ())
    started = time.perf_counter()
    page = index.search(intent["query"], languages=languages, licenses=mode, limit=DEPTH)
    ms = (time.perf_counter() - started) * 1000
    hits = []
    for rank, result in enumerate(page.results, 1):
        # Read from the records, not the result: code without a license is linked to, not
        # shown, and still counts as found.
        code = db.execute(
            "SELECT code FROM snippets WHERE hash >= ? AND hash < ?", (result.id, result.id + "g")
        ).fetchone()[0]
        if contains(code, intent["expect"]):
            loc = result.location
            hits.append(
                {
                    "rank": rank,
                    "url": loc.url,
                    "symbol": loc.symbol,
                    "license": loc.license.expression,
                    "level": loc.license.level,
                }
            )
    covered = index.search(
        "", languages=languages, licenses=mode, require=intent["expect"], limit=1
    ).total
    return {
        "id": intent["id"],
        "licenses": mode,
        "rank": hits[0]["rank"] if hits else None,
        "hits": hits,
        "coverage": covered,
        "results": page.total,
        "ms": round(ms, 1),
    }


def contains(code: str, expect: list[str]) -> bool:
    """Whether code has each expected identifier, whole: one of the alternatives of each."""
    return all(
        any(re.search(rf"(?<!\w){re.escape(alt)}(?!\w)", code) for alt in item.split("|"))
        for item in expect
    )


def summary(rows: list[dict[str, Any]], run: dict[str, Any]) -> str:
    lines = [
        "# Search against intent suites",
        "",
        f"Top {run['depth']} results per intent; a hit has every expected identifier in its code."
        " Coverage: snippets in the index that have them, whatever their rank.",
        "",
        f"- Index built {run['index_built']}, corpus sha256 {str(run['corpus_sha256'])[:12]};"
        f" provgrep {run['commit']}; {run['seconds']} s",
        f"- Search time, top {run['depth']} with their records: p50 {_percentile(rows, 50)} ms,"
        f" p95 {_percentile(rows, 95)} ms ({len(rows)} searches)",
        "",
        "| suite | licenses | intents | hit@1 | hit@10 | MRR@10 | covered |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        mode = r["licenses"] if isinstance(r["licenses"], str) else "allowed list"
        groups.setdefault((r["suite"], mode), []).append(r)
    for (suite, mode), rs in groups.items():
        ranks = [r["rank"] for r in rs]
        cells = [
            suite,
            mode,
            len(rs),
            sum(r == 1 for r in ranks),
            sum(r is not None for r in ranks),
            f"{sum(1 / r for r in ranks if r) / len(rs):.2f}",
            sum(r["coverage"] > 0 for r in rs),
        ]
        lines.append("| " + " | ".join(map(str, cells)) + " |")
    lines += [
        "",
        "## Every intent",
        "",
        "| suite | intent | licenses | rank | covered | first hit |",
        "|---|---|---|---:|---:|---|",
    ]
    for r in rows:
        mode = r["licenses"] if isinstance(r["licenses"], str) else ", ".join(r["licenses"])
        first = r["hits"][0] if r["hits"] else None
        hit = f"[{first['symbol']}]({first['url']}) {first['license']}" if first else "-"
        lines.append(
            f"| {r['suite']} | {r['id']} | {mode} | {r['rank'] or '-'} | {r['coverage']} | {hit} |"
        )
    return "\n".join(lines) + "\n"


def _percentile(rows: list[dict[str, Any]], p: int) -> float:
    times = sorted(r["ms"] for r in rows)
    return times[min(len(times) - 1, len(times) * p // 100)] if times else 0.0
