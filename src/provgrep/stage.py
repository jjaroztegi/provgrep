"""Stage a repository: fetch its pinned commit, classify its files, resolve their licenses and
cut them into units, and write the result as one file the build reads back.

A staged file depends only on the repository's commit and on the code and tools that staged it.
The build keeps staged files under a key that hashes those (key()), so a stopped build continues
where it was, and a change to any of them stages everything again rather than mixing outputs.
How many repositories are staged at once, and by which processes, is the build's (build.py): it
changes no staged file, so it is not part of the key.
"""

import gzip
import hashlib
import json
import tempfile
from collections import Counter
from dataclasses import asdict
from functools import cache
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import TextIO

from provgrep import chunks, github, read_text
from provgrep.fetch import TooLarge
from provgrep.licenses import evidence_file, resolve_tree

SKIPPED = ("test", "generated")
# What a staged repository is the output of.
_STAGED_BY = ("stage.py", "chunks.py", "fetch.py", "github.py", "licenses.py")
TOOLS = (
    "scancode-toolkit",
    "license-expression",
    "tree-sitter",
    "tree-sitter-language-pack",  # every grammar, by its version
)


def stage(repo: github.Repo, dest: Path, expected: str, work: Path) -> int:
    """Write a repository's indexed files, with their license resolution and units, to dest as
    JSON lines: one per file, then the count of files of each kind. Return the number of units.
    A repository whose tarball is over the size limit is staged as skipped, with why: it would be
    over it every time. expected is the key the build stages under; a worker started after the
    code changed has another and refuses, rather than mix outputs under one key."""
    if key() != expected:
        raise RuntimeError(f"the staging code changed since the build started ({key()})")
    unfinished = part(dest)
    with (
        tempfile.TemporaryDirectory(prefix="provgrep-", dir=work) as tmp,
        gzip.open(unfinished, "wt", encoding="utf-8") as out,
    ):
        try:
            root = github.tree(repo, Path(tmp), _wanted)
        except TooLarge as e:
            out.write(json.dumps({"skipped": str(e), "kinds": {}}) + "\n")
            count = 0
        else:
            count = _stage_tree(root, out)
    unfinished.replace(dest)
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
        text = read_text(path)
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
        units = chunks.units(read_text(root / name), language)
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


def part(dest: Path) -> Path:
    """Where a repository is staged until it is done: its existence means staging started."""
    return dest.with_name(dest.name + ".part")
