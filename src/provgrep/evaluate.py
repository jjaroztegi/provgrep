"""How often a resolved license is wrong, against the licenses Debian maintainers record in DEP-5.

The number that matters to a user is the false-permissive rate: of the files a permissive filter
lets through, how many Debian records under a license that is not permissive. It is reported per
level, for every file and for the files whose license differs from their package's main one
(vendored or borrowed code, where a repository-level label goes wrong).

DEP-5 is a maintainer's conclusion, not an oracle: a disagreement is a case to audit, not
automatically a mistake of ours.
"""

import fnmatch
import json
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from provgrep import debian, source_commit
from provgrep.fetch import sha256_of
from provgrep.licenses import licensing, permissive, resolve_tree

Row = dict[str, Any]
LEVELS = ("A", "B", "C", "X")


def package_rows(pkg: debian.Package, snapshot: str) -> list[Row]:
    """One row per code file of the package: our resolution beside Debian's."""
    got = debian.tree(pkg, snapshot)
    if got is None:
        return []
    root, stanzas = got
    main = next((s for s in stanzas if "*" in s.patterns), None)
    rows = []
    for path, res in resolve_tree(root).items():
        if path.startswith("debian/"):
            continue
        stanza = debian.stanza_for(stanzas, path)
        truth = debian.spdx_expression(stanza.license) if stanza else None
        rows.append(
            {
                "package": pkg.name,
                "path": path,
                "debian": stanza.license if stanza else None,
                "main": stanza is not None and main is not None and stanza.license == main.license,
                "truth": truth,
                "expression": res.expression,
                "level": res.level,
                "conflict": res.conflict,
                "permissive": permissive(res.expression),
                "truth_permissive": permissive(truth) if truth else None,
                "exact": bool(
                    truth and res.expression and licensing().is_equivalent(truth, res.expression)
                ),
                "evidence": [asdict(e) for e in res.evidence],
            }
        )
    return rows


def evaluate(
    set_path: Path, audit_path: Path, out_root: Path, limit: int | None, jobs: int
) -> Path:
    """Resolve every package of the pinned set and write results/<UTC time>/. Files shown as
    permissive against Debian's record take their verdict from the audit, where one covers them."""
    snapshot, packages = debian.load(set_path)
    packages = packages[:limit]
    started = time.monotonic()
    rows: list[Row] = []
    failed: dict[str, str] = {}
    with ProcessPoolExecutor(jobs) as pool:
        futures = {pool.submit(package_rows, pkg, snapshot): pkg for pkg in packages}
        for done, future in enumerate(as_completed(futures), 1):
            pkg = futures[future]
            try:
                got = future.result()
            except Exception as e:  # one broken package must not lose the run; it is listed
                failed[pkg.name] = f"{type(e).__name__}: {e}"
                got = []
            rows += got
            print(f"[{done}/{len(packages)}] {pkg.name}: {len(got)} files", file=sys.stderr)
    rows.sort(key=lambda r: (r["package"], r["path"]))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))["entries"]
    for r in rows:
        if r["truth"] and r["permissive"] and not r["truth_permissive"]:
            r["verdict"] = next(
                (
                    e["verdict"]
                    for e in audit
                    if e["package"] == r["package"]
                    and any(fnmatch.fnmatchcase(r["path"], p) for p in e["paths"])
                ),
                None,
            )

    run = {
        "set": set_path.as_posix(),
        "set_sha256": sha256_of(set_path),
        "audit": audit_path.as_posix(),
        "audit_sha256": sha256_of(audit_path),
        "snapshot": snapshot,
        "limit": limit,
        "commit": source_commit(),
        "scancode-toolkit": version("scancode-toolkit"),
        "license-expression": version("license-expression"),
        "packages": len(packages),
        "packages_with_code": len({r["package"] for r in rows}),
        "failed": failed,
        "seconds": round(time.monotonic() - started),
    }
    out = out_root / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    (out / "run.json").write_text(json.dumps(run, indent=1) + "\n", encoding="utf-8")
    with (out / "files.jsonl").open("w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    (out / "summary.md").write_text(summary(rows, run), encoding="utf-8")
    return out


def summary(rows: list[Row], run: dict[str, Any]) -> str:
    judged = [r for r in rows if r["truth"] is not None]
    shown = [r for r in judged if r["permissive"]]
    wrong = [r for r in shown if not r["truth_permissive"]]
    verdicts = Counter(r["verdict"] for r in wrong)
    groups = Counter(
        (r["package"], r["expression"], r["level"], r["debian"], r["verdict"]) for r in wrong
    )
    unmapped = Counter(r["debian"] for r in rows if r["truth"] is None)
    lines = [
        "# License resolution against Debian DEP-5",
        "",
        f"Of {len(shown)} files a permissive filter would show, {len(wrong)} "
        f"({_pct(len(wrong), len(shown))}) are under a license Debian does not record as "
        f"permissive. Read by hand: Debian's record is stale for {verdicts['debian-stale']} "
        f"(upstream relicensed) and coarse for {verdicts['debian-coarse']} (the files carry their "
        f"own notices); provgrep is wrong for {verdicts['provgrep-wrong']}; not read yet: "
        f"{verdicts[None]}.",
        "",
        f"- Packages: {run['packages']} from the set ({run['packages_with_code']} with code files),"
        f" Debian snapshot {run['snapshot']}; failed: {len(run['failed'])}",
        f"- Code files: {len(rows)}; judged: {len(judged)}; left out because Debian's license "
        f"name has no certain SPDX id: {len(rows) - len(judged)}",
        f"- provgrep {run['commit']}, scancode-toolkit {run['scancode-toolkit']}, "
        f"license-expression {run['license-expression']}; {run['seconds']} s",
        "",
        "## Every judged file",
        "",
        *_table(judged),
        "",
        "## Files under another license than their package's main one",
        "",
        *_table([r for r in judged if not r["main"]]),
        "",
        "Same category: both permissive (or public domain), or both not.",
        "",
        "## Shown as permissive, not permissive in Debian",
        "",
        "| package | files | ours | level | Debian | verdict |",
        "|---|---:|---|---|---|---|",
        *(
            f"| {package} | {count} | {ours} | {level} | {theirs} | {verdict or 'not read yet'} |"
            for (package, ours, level, theirs, verdict), count in groups.most_common()
        ),
        "",
        "## Debian license names left out",
        "",
        *(f"- {count} files: `{name}`" for name, count in unmapped.most_common(15)),
        "",
    ]
    return "\n".join(lines)


def _table(rows: list[Row]) -> list[str]:
    out = [
        "| level | files | same expression | same category | shown as permissive "
        "| of those, not permissive in Debian | permissive in Debian, not shown |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for level in (*LEVELS, "all"):
        rs = [r for r in rows if level in ("all", r["level"])]
        if not rs:
            continue
        shown = [r for r in rs if r["permissive"]]
        wrong = sum(not r["truth_permissive"] for r in shown)
        missed = sum(r["truth_permissive"] and not r["permissive"] for r in rs)
        same_category = sum(r["permissive"] == r["truth_permissive"] for r in rs)
        cells = [
            level,
            len(rs),
            _pct(sum(r["exact"] for r in rs), len(rs)),
            _pct(same_category, len(rs)),
            len(shown),
            f"{wrong} ({_pct(wrong, len(shown))})",
            missed,
        ]
        out.append("| " + " | ".join(map(str, cells)) + " |")
    return out


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"
