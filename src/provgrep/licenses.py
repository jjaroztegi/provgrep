"""The license of each code file in a source tree, and the evidence it was resolved from.

Evidence, most specific first: the file's own header (a notice or SPDX tag in its leading comments
or module docstring), the nearest license file in an enclosing directory, and the nearest package
manifest. The level says how much of it agrees:

- A: the file's own header.
- B: the nearest license file and the nearest manifest name the same licenses.
- C: one source only, or the two disagree; the deeper one wins, and at equal depth the license
  file, because it holds the text rather than a name.
- X: nothing. All rights reserved until shown otherwise.

Licenses are as detected, never concluded: a label is what the evidence says, not legal advice.

Notices go with the license: the copyright statements in the file's header, or else in the license
files it falls under, without email addresses.
"""

import contextlib
import email.parser
import re
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from functools import cache, lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from cluecode.copyrights import detect_copyrights_from_lines
from license_expression import LicenseWithExceptionSymbol, Licensing, get_spdx_licensing
from licensedcode.cache import get_index, get_licenses_db
from scancode.api import get_licenses, get_package_data

from provgrep import read_text, without_emails

_HASH = ("#",)
_SLASH = ("//",)
_C_BLOCK = (("/*", "*/"),)
_DOCSTRING = (('"""', '"""'), ("'''", "'''"))

# Line-comment markers and block delimiters by suffix: the files we resolve, and how their
# header ends. `#` is not a comment in C (`#include`), so a C header ends at the first directive.
_COMMENTS: dict[str, tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = {
    **dict.fromkeys((".py", ".pyi", ".pyx", ".pxd"), (_HASH, _DOCSTRING)),
    **dict.fromkeys((".sh", ".bash", ".rb", ".pl", ".r"), (_HASH, ())),
    **dict.fromkeys((".c", ".h", ".cc", ".cpp", ".cxx", ".c++"), (_SLASH, _C_BLOCK)),
    **dict.fromkeys((".hh", ".hpp", ".hxx", ".h++"), (_SLASH, _C_BLOCK)),
    **dict.fromkeys((".java", ".kt", ".scala", ".swift", ".cs", ".go", ".rs"), (_SLASH, _C_BLOCK)),
    **dict.fromkeys((".js", ".mjs", ".cjs", ".jsx"), (_SLASH, _C_BLOCK)),
    **dict.fromkeys((".ts", ".mts", ".cts", ".tsx"), (_SLASH, _C_BLOCK)),
    ".lua": (("--",), (("--[[", "]]"),)),
}
CODE_SUFFIXES = frozenset(_COMMENTS)

# LICENSE, LICENSE-MIT, COPYING.LESSER, ... A LICENSES/ directory holds texts that other files
# refer to (REUSE, bundled third-party texts), not a license for its directory.
_LEGAL_NAME = re.compile(r"(licen[cs]e|copying|copyright|unlicense)([._-].*)?", re.IGNORECASE)
MANIFESTS = frozenset(
    {"pyproject.toml", "setup.cfg", "setup.py", "PKG-INFO", "package.json", "Cargo.toml"}
)
# Only headers that mention a license are worth a ScanCode pass.
_HINT = re.compile(
    r"spdx-license-identifier|licen[cs]|copyright|\(c\)|permission is hereby granted"
    r"|redistribution and use|general public|public domain",
    re.IGNORECASE,
)
_GNU_VERSION = re.compile(r"-(only|or-later)$")
_MAX_SCAN = 1 << 20  # larger files are generated or minified, and slow to scan
PERMISSIVE_CATEGORIES = frozenset({"Permissive", "Public Domain"})
# Licenses that let code be copied and passed on, with conditions or without.
SHAREABLE_CATEGORIES = PERMISSIVE_CATEGORIES | {"Copyleft", "Copyleft Limited", "Free Restricted"}


@dataclass(frozen=True)
class Evidence:
    kind: str  # "header", "license_file" or "manifest"
    path: str  # where in the tree it was found
    expression: str


@dataclass(frozen=True)
class Resolution:
    expression: str | None
    level: str
    evidence: tuple[Evidence, ...]
    conflict: bool  # some evidence named other licenses
    notices: tuple[str, ...] = ()  # copyright statements


@dataclass(frozen=True)
class _Scope:
    depth: int
    expression: str
    evidence: tuple[Evidence, ...]
    notices: tuple[str, ...] = ()


@cache
def licensing() -> Licensing:
    return get_spdx_licensing()


def header_end(text: str, suffix: str) -> int:
    """Last line (1-based) of the leading comments and module docstring; 0 if there are none."""
    markers, blocks = _COMMENTS[suffix]
    end = 0
    closing: str | None = None
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if closing is not None:
            end = n
            if closing in s:
                closing = None
            continue
        if not s or (n == 1 and s.startswith("#!")):
            continue
        body = s[1:] if s[:1] in "rRuU" and s[1:4] in ('"""', "'''") else s
        block = next((b for b in blocks if body.startswith(b[0])), None)
        if block is not None:
            end = n
            if block[1] not in body[len(block[0]) :]:
                closing = block[1]
        elif s.startswith(markers):
            end = n
        else:
            break
    return end


def evidence_file(rel: PurePosixPath) -> bool:
    """A license file or a manifest: what resolve_tree reads besides code."""
    return rel.name in MANIFESTS or _license_file_name(rel)


def _license_file_name(rel: PurePosixPath) -> bool:
    return bool(_LEGAL_NAME.fullmatch(rel.name)) and rel.parent.name.lower() != "licenses"


def resolve_tree(root: Path) -> dict[str, Resolution]:
    """Resolve every code file under root, keyed by its path relative to root."""
    code: list[PurePosixPath] = []
    legal: list[PurePosixPath] = []
    manifests: list[PurePosixPath] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = PurePosixPath(path.relative_to(root).as_posix())
        if ".git" in rel.parts:
            continue
        if rel.name in MANIFESTS:
            manifests.append(rel)
        if rel.suffix.lower() in CODE_SUFFIXES:
            code.append(rel)
        elif _license_file_name(rel):
            legal.append(rel)

    legal_scopes = _scopes(root, legal, "license_file", _license_file)
    manifest_scopes = _scopes(root, manifests, "manifest", _declared)
    resolved = {}
    for rel in code:
        own, own_notices = _header(root / rel, rel)
        license_file = _nearest(rel, legal_scopes)
        resolution = _resolve(own, license_file, _nearest(rel, manifest_scopes))
        notices = own_notices or (license_file.notices if license_file else ())
        resolved[rel.as_posix()] = replace(resolution, notices=notices)
    return resolved


def permissive(expression: str | None) -> bool:
    """Whether code under this expression can be copied keeping only notices: every license an
    AND requires is permissive or public domain, or at least one choice of an OR is."""
    return bool(expression) and _permissive(licensing().parse(expression))


def shareable(expression: str | None) -> bool:
    """Whether an expression names a license that lets the code be shared. Code under none
    (proprietary, source-available, unknown, or no license found) is linked to, not shown. A
    shareable license beside others is enough: GPL headers also match commercial or proprietary
    rules on their wording, and the whole expression goes with the code."""
    return bool(expression) and any(
        _categories().get(key.lower()) in SHAREABLE_CATEGORIES
        for key in licensing().license_keys(expression)
    )


def choices(expression: str) -> list[tuple[str, ...]]:
    """The ways to comply with an expression, each the licenses it takes together:
    `MIT OR (Apache-2.0 AND BSD-3-Clause)` is [("MIT",), ("Apache-2.0", "BSD-3-Clause")]."""
    dnf = licensing().dnf(licensing().parse(expression))
    alternatives = dnf.args if isinstance(dnf, licensing().OR) else (dnf,)
    return [
        tuple(sorted(str(a) for a in (alt.args if isinstance(alt, licensing().AND) else (alt,))))
        for alt in alternatives
    ]


def _permissive(node: Any) -> bool:
    if isinstance(node, licensing().AND):
        return all(_permissive(arg) for arg in node.args)
    if isinstance(node, licensing().OR):
        return any(_permissive(arg) for arg in node.args)
    if isinstance(node, LicenseWithExceptionSymbol):
        node = node.license_symbol
    return _categories().get(node.key.lower()) in PERMISSIVE_CATEGORIES


@cache
def _categories() -> dict[str, str]:
    """ScanCode LicenseDB category by SPDX id (and its other ids), lower-cased."""
    categories = {}
    for lic in get_licenses_db().values():
        categories[f"licenseref-scancode-{lic.key}"] = lic.category
        for key in lic.spdx_keys():
            categories[key.lower()] = lic.category
    return categories


def _combine(expressions: Iterable[str]) -> str | None:
    """AND of the distinct terms of the expressions, flattened (`MIT AND (MIT AND Apache-2.0)` is
    `MIT AND Apache-2.0`), leaving out pointers and contributor agreements."""
    terms: dict[str, Any] = {}

    def add(node: Any) -> None:
        if isinstance(node, licensing().AND):
            for arg in node.args:
                add(arg)
        elif not _pointer_or_cla(node):
            terms.setdefault(str(node), node)

    for expression in expressions:
        if expression:
            add(licensing().parse(expression))
    if len(terms) < 2:
        return next(iter(terms), None)
    return str(licensing().AND(*terms.values()))


def _pointer_or_cla(node: Any) -> bool:
    """A pointer to another file ("see LICENSE"), or a contributor agreement ("by contributing
    you release your content..."): neither is the license of the code."""
    key = str(node).lower()
    return "unknown-license-reference" in key or _categories().get(key) == "CLA"


Lines = tuple[tuple[int, int], ...]


@lru_cache(maxsize=1 << 16)
def _detect(text: str, approximate: bool = True) -> tuple[str | None, Lines]:
    """What ScanCode detects in a text, and the lines it matched as the whole text of a license.
    Headers repeat across a project's files, and license texts across projects, so each distinct
    text is matched once per process. Without `approximate`, only exact matches of license
    texts, notices and SPDX ids."""
    # Below 50 are fragments of longer notices: 12% of an EUPL notice matched in an MIT header.
    found = get_licenses(None, query_string=text, min_score=50, approximate=approximate)
    detections = found["license_detections"]
    whole = tuple(
        (m["start_line"], m["end_line"])
        for d in detections
        for m in d["matches"]
        if m["rule_identifier"] in _license_texts()
    )
    return _combine(d["license_expression_spdx"] for d in detections), whole


@cache
def _license_texts() -> frozenset[str]:
    """ScanCode rules that are the whole text of a license: its canonical `*.LICENSE` and the
    variants a project's copy matches instead (`gpl-2.0_temple_variant.RULE`, the old address)."""
    return frozenset(r.identifier for r in get_index().rules_by_rid if r.is_license_text)


@lru_cache(maxsize=1 << 16)
def _copyrights(text: str, license_texts: Lines = ()) -> tuple[str, ...]:
    """The copyright statements in a text, without email addresses. Those inside the text of a
    license are the license's own, like the FSF's in a copy of the GPL, and are left out."""
    lines = list(enumerate(text.splitlines(), 1))
    found = detect_copyrights_from_lines(lines, include_holders=False, include_authors=False)
    return tuple(
        dict.fromkeys(
            " ".join(without_emails(d.copyright).split())
            for d in found
            if not any(a <= d.start_line and d.end_line <= b for a, b in license_texts)
        )
    )


def _header(path: Path, rel: PurePosixPath) -> tuple[Evidence | None, tuple[str, ...]]:
    """The license a file's header names, and its copyright statements."""
    if path.stat().st_size > _MAX_SCAN:
        return None, ()
    text = read_text(path)
    header = "\n".join(text.splitlines()[: header_end(text, rel.suffix.lower())])
    if not _HINT.search(header):
        return None, ()
    # Approximate matching costs ~0.5 s a header with prose beside its notice; it is kept for
    # notices that no exact match finds, edited ones.
    expression, texts = _detect(header, approximate=False)
    if not expression:
        expression, texts = _detect(header)
    own = Evidence("header", rel.as_posix(), expression) if expression else None
    return own, _copyrights(header, texts)


def _license_file(path: Path) -> tuple[str | None, tuple[str, ...]]:
    if path.stat().st_size > _MAX_SCAN:
        return None, ()
    text = read_text(path)
    expression, texts = _detect(text)
    return expression, _copyrights(text, texts)


def _declared(path: Path) -> tuple[str | None, tuple[str, ...]]:
    return _declared_expression(path), ()


def _declared_expression(path: Path) -> str | None:
    """The license a manifest declares. An SPDX expression in the field made for one (PEP 639
    `project.license`, `License-Expression`) is taken as written: ScanCode would AND it with the
    classifiers, turning `MIT OR Apache-2.0` into `(MIT OR Apache-2.0) AND MIT`."""
    explicit = None
    text = read_text(path)
    if path.name == "pyproject.toml":
        with contextlib.suppress(tomllib.TOMLDecodeError):
            explicit = tomllib.loads(text).get("project", {}).get("license")
    elif path.name == "PKG-INFO":
        explicit = email.parser.Parser().parsestr(text, headersonly=True).get("License-Expression")
    if isinstance(explicit, str):
        valid = licensing().validate(explicit)
        if not valid.errors:
            return str(valid.normalized_expression)
    try:
        packages = get_package_data(str(path))["package_data"]
    except Exception:  # ScanCode's manifest parsers raise anything on malformed input
        return None
    return _combine(p["declared_license_expression_spdx"] for p in packages)


def _scopes(
    root: Path,
    files: list[PurePosixPath],
    kind: str,
    detect: Callable[[Path], tuple[str | None, tuple[str, ...]]],
) -> dict[PurePosixPath, _Scope]:
    """One scope per directory: the AND of what its files name, with each file as evidence, and
    their copyright statements."""
    found: dict[PurePosixPath, list[Evidence]] = {}
    notices: dict[PurePosixPath, list[str]] = {}
    for rel in files:
        expression, statements = detect(root / rel)
        if expression:
            found.setdefault(rel.parent, []).append(Evidence(kind, rel.as_posix(), expression))
            notices.setdefault(rel.parent, []).extend(statements)
    scopes = {}
    for directory, evidence in found.items():
        expression = _combine(e.expression for e in evidence)
        if expression:
            depth = 0 if directory == PurePosixPath(".") else len(directory.parts)
            statements = tuple(dict.fromkeys(notices[directory]))
            scopes[directory] = _Scope(depth, expression, tuple(evidence), statements)
    return scopes


def _nearest(rel: PurePosixPath, scopes: dict[PurePosixPath, _Scope]) -> _Scope | None:
    return next((scopes[p] for p in rel.parents if p in scopes), None)


def _resolve(
    own: Evidence | None, license_file: _Scope | None, manifest: _Scope | None
) -> Resolution:
    scopes = [s for s in (license_file, manifest) if s is not None]
    evidence = tuple(e for s in scopes for e in s.evidence)
    if own is not None:
        conflict = any(not _same(own.expression, s.expression) for s in scopes)
        return Resolution(own.expression, "A", (own, *evidence), conflict)
    if license_file is not None and manifest is not None:
        if licensing().is_equivalent(license_file.expression, manifest.expression):
            return Resolution(license_file.expression, "B", evidence, False)
        if _same(license_file.expression, manifest.expression):
            # LICENSE-MIT and LICENSE-APACHE beside `MIT OR Apache-2.0`, or COPYING beside
            # `LGPL-2.1-or-later`: only the manifest can say how the licenses combine, or that a
            # later version may be chosen.
            return Resolution(manifest.expression, "B", evidence, False)
        winner = manifest if manifest.depth > license_file.depth else license_file
        return Resolution(winner.expression, "C", evidence, True)
    if scopes:
        return Resolution(scopes[0].expression, "C", evidence, False)
    return Resolution(None, "X", (), False)


def _same(a: str, b: str) -> bool:
    """Whether two expressions name the same licenses, however combined. `-only` and `-or-later`
    count as the same: a GNU license text never says "or later", only a notice or a manifest
    does."""

    def keys(expression: str) -> set[str]:
        return {_GNU_VERSION.sub("", key) for key in licensing().license_keys(expression)}

    return keys(a) == keys(b)
