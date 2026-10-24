"""Stage a repository: fetch its pinned commit, classify its files, resolve their licenses and
cut them into units, and write the result as one file the build reads back.

A staged file depends only on the repository's commit and on the code and tools that staged it.
The build keeps staged files under a key that hashes those (key()), so a stopped build continues
where it was, and a change to any of them stages everything again rather than mixing outputs.
"""

import gzip
import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict
from functools import cache, partial
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import TextIO

from provgrep import chunks, github
from provgrep.fetch import TooLarge
from provgrep.licenses import evidence_file, resolve_tree

SKIPPED = ("test", "generated")
# What a staged repository is the output of.
_STAGED_BY = ("stage.py", "chunks.py", "fetch.py", "github.py", "licenses.py")
TOOLS = (
    "scancode-toolkit",
    "license-expression",
    "tree-sitter",
    *(f"tree-sitter-{g}" for g in ("c", "cpp", "go", "java", "javascript", "python", "rust")),
    "tree-sitter-typescript",
)


def stage(repo: github.Repo, dest: Path, expected: str, work: Path) -> int:
    """Write a repository's indexed files, with their license resolution and units, to dest as
    JSON lines: one per file, then the count of files of each kind. Return the number of units.
    A repository whose tarball is over the size limit is staged as skipped, with why: it would be
    over it every time. expected is the key the build stages under; a worker started after the
    code changed has another and refuses, rather than mix outputs under one key."""
    if key() != expected:
        raise RuntimeError(f"the staging code changed since the build started ({key()})")
    part = _part(dest)
    with (
        tempfile.TemporaryDirectory(prefix="provgrep-", dir=work) as tmp,
        gzip.open(part, "wt", encoding="utf-8") as out,
    ):
        try:
            root = github.tree(repo, Path(tmp), _wanted)
        except TooLarge as e:
            out.write(json.dumps({"skipped": str(e), "kinds": {}}) + "\n")
            count = 0
        else:
            count = _stage_tree(root, out)
    part.replace(dest)
    return count


def _wanted(rel: PurePosixPath) -> bool:
    return rel.suffix.lower() in chunks.SUFFIXES or evidence_file(rel)


def _stage_tree(root: Path, out: TextIO) -> int:
    """Classify, resolve and cut the files under root, writing each one with units as a line.
    One file's units at a time are in memory: a repository can have hundreds of thousands."""
    paths = sorted(
        PurePosixPath(p.relative_to(root).as_posix()) for p in root.rglob("*") if p.is_file()
    )
    cpp = any(p.suffix.lower() in chunks.CPP_SUFFIXES for p in paths)
    kinds: Counter[str] = Counter()
    code: dict[str, tuple[str, str]] = {}
    for rel in paths:
        language = chunks.language_of(rel, cpp)
        if language is None:
            continue
        path = root / rel
        text = path.read_text(encoding="utf-8", errors="replace")
        kind = chunks.kind(rel, text, language)
        kinds[kind] += 1
        if kind in SKIPPED:
            path.unlink()  # not indexed, so not worth resolving
        else:
            code[rel.as_posix()] = (language, kind)
    count = 0
    for name, resolution in resolve_tree(root).items():
        if name not in code:
            continue
        language, kind = code[name]
        units = chunks.units((root / name).read_text(encoding="utf-8", errors="replace"), language)
        if units:
            staged = {
                "path": name,
                "language": chunks.LANGUAGES[language].name,
                "vendored": kind == "vendored",
                **asdict(resolution),
                "units": [asdict(u) for u in units],
            }
            out.write(json.dumps(staged) + "\n")
            count += len(units)
    out.write(json.dumps({"kinds": dict(kinds)}) + "\n")
    return count


@cache
def key() -> str:
    h = hashlib.sha256()
    for name in _STAGED_BY:
        h.update((Path(__file__).parent / name).read_bytes())
    for tool in TOOLS:
        h.update(f"{tool}=={version(tool)}\n".encode())
    return h.hexdigest()[:16]


def path(staging: Path, repo: github.Repo) -> Path:
    return staging / f"{repo.name.replace('/', '__')}-{repo.commit}.jsonl.gz"


def stage_all(repos: list[github.Repo], staging: Path, jobs: int, work: Path) -> dict[str, str]:
    """Stage repositories in parallel; return the ones that failed, with why. Workers unpack
    under work, which is emptied whenever none is running.

    A worker that dies (a crash in native code, the kernel ending it for memory) takes the pool
    with it, and its siblings are ended without cleaning up. The repositories being staged then,
    the ones with a `.part` file, are staged again each in a process of its own, so a crash is
    only that repository's; the ones not started go to a new pool.
    """
    expected = key()
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
        with ProcessPoolExecutor(jobs) as pool:
            futures = {
                pool.submit(stage, repo, path(staging, repo), expected, work): repo
                for repo in pending
            }
            for future in as_completed(futures):
                repo = futures[future]
                try:
                    report(repo, f"{future.result()} units")
                except BrokenProcessPool:
                    running = _part(path(staging, repo)).exists()
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


def _part(dest: Path) -> Path:
    """Where a repository is staged until it is done: its existence means staging started."""
    return dest.with_name(dest.name + ".part")


def _isolated(repo: github.Repo, staging: Path, work: Path) -> int | str:
    """Stage a repository in a process of its own: its units, or why it failed."""
    with ProcessPoolExecutor(1) as pool:
        try:
            return pool.submit(stage, repo, path(staging, repo), key(), work).result()
        except Exception as e:
            return f"{type(e).__name__}: {e}"
