"""Debian source packages as license ground truth.

A package's upstream tarball (`.orig.tar.*`) is the tree; its `debian/copyright`, in the
machine-readable format (DEP-5), is a maintainer's account of the license of every file in it.
Both sides describe the same tree, so no versions have to be matched across sources. The set is
pinned to a snapshot.debian.org timestamp, which keeps every file downloadable after unstable
moves on.
"""

import hashlib
import json
import lzma
import re
import tarfile
import urllib.request
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path

from provgrep.fetch import USER_AGENT, cache_dir, download, sha256_of, unpack
from provgrep.licenses import licensing

MIRROR = "https://deb.debian.org/debian"
SNAPSHOT = "https://snapshot.debian.org/archive/debian/{snapshot}"


@dataclass(frozen=True)
class SourceFile:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    directory: str
    homepage: str
    orig: SourceFile
    debian: SourceFile


@dataclass(frozen=True)
class Stanza:
    patterns: tuple[str, ...]
    license: str  # as Debian writes it: `GPL-2+ or Artistic-2.0`


def paragraphs(text: str) -> list[dict[str, str]]:
    """deb822 paragraphs (Sources, DEP-5), field names lower-cased; a field's continuation lines
    are stripped and joined to it with newlines."""
    found: list[dict[str, str]] = []
    fields: dict[str, str] = {}
    key = ""
    for line in text.splitlines():
        if not line.strip():
            if fields:
                found.append(fields)
            fields, key = {}, ""
        elif line[0] in " \t":
            if key:
                fields[key] += "\n" + line.strip()
        elif not line.startswith("#"):
            name, _, value = line.partition(":")
            key = name.strip().lower()
            fields[key] = value.strip()
    if fields:
        found.append(fields)
    return found


def candidates(sources: str) -> list[Package]:
    """Source packages that build a python3-* module from a GitHub upstream out of one upstream
    tarball, in a fixed pseudo-random order: by the sha256 of their name."""
    found = []
    for p in paragraphs(sources):
        binaries = [b.strip() for b in p.get("binary", "").split(",")]
        if (
            p.get("format") != "3.0 (quilt)"
            or "github.com" not in p.get("homepage", "")
            or not any(b.startswith("python3-") for b in binaries)
        ):
            continue
        lines = [line.split() for line in p.get("checksums-sha256", "").split("\n") if line]
        files = [
            SourceFile(name, sha256, int(size))
            for sha256, size, name in lines
            if not name.endswith(".asc")
        ]
        origs = [f for f in files if ".orig" in f.name]
        debs = [f for f in files if ".debian.tar." in f.name]
        # Component tarballs (.orig-NAME.tar.*) unpack into subdirectories; left out.
        if len(origs) == 1 and ".orig.tar." in origs[0].name and len(debs) == 1:
            found.append(
                Package(p["package"], p["version"], p["directory"], p["homepage"], *origs, *debs)
            )
    return sorted(found, key=lambda pkg: hashlib.sha256(pkg.name.encode()).hexdigest())


def fetch(pkg: Package, file: SourceFile, snapshot: str) -> Path:
    urls = [
        f"{MIRROR}/{pkg.directory}/{file.name}",
        f"{SNAPSHOT.format(snapshot=snapshot)}/{pkg.directory}/{file.name}",
    ]
    return download(urls, file.sha256, cache_dir() / "debian" / file.name)


def copyright_text(debian_tarball: Path) -> str | None:
    with tarfile.open(debian_tarball) as tar:
        try:
            member = tar.extractfile("debian/copyright")
        except KeyError:
            return None
        return member.read().decode("utf-8", "replace") if member else None


def dep5(text: str) -> list[Stanza] | None:
    """The Files stanzas of a machine-readable debian/copyright; None for any other format."""
    paras = paragraphs(text)
    if not paras or "copyright-format/1.0" not in paras[0].get("format", ""):
        return None
    return [
        Stanza(tuple(p["files"].split()), p.get("license", "").split("\n", 1)[0].strip())
        for p in paras[1:]
        if "files" in p
    ]


def stanza_for(stanzas: list[Stanza], path: str) -> Stanza | None:
    """The last stanza with a pattern that matches path, as DEP-5 specifies."""
    return next(
        (s for s in reversed(stanzas) if any(_glob(p).fullmatch(path) for p in s.patterns)), None
    )


@cache
def _glob(pattern: str) -> re.Pattern[str]:
    """DEP-5 wildcards: `*` matches anything, `/` included; `?` one character; `\\` escapes."""
    out = []
    escaped = False
    for ch in pattern.removeprefix("./"):
        if escaped or ch not in "*?\\":
            out.append(re.escape(ch))
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            out.append(".*" if ch == "*" else ".")
    return re.compile("".join(out), re.DOTALL)


# Debian short names that are not SPDX ids. Names without a version (`Apache`, `BSD`, `GPL`) are
# left out on purpose: they do not say which license.
_NAMES = {
    "expat": "MIT",
    "apache-2": "Apache-2.0",
    "apache2": "Apache-2.0",
    "bsd-2": "BSD-2-Clause",
    "bsd-3": "BSD-3-Clause",
    "bsd-3-clauses": "BSD-3-Clause",
    "psf-2": "PSF-2.0",
    "python": "PSF-2.0",
    "cc0": "CC0-1.0",
    "public-domain": "LicenseRef-scancode-public-domain",
    "zope-2.1": "ZPL-2.1",
    "boost-1.0": "BSL-1.0",
}
_GNU = re.compile(r"(a?gpl|lgpl|gfdl)-(\d)(?:\.(\d))?(\+)?", re.IGNORECASE)


def spdx_name(name: str) -> str | None:
    """The SPDX id of one Debian short name; None when the name does not say for certain."""
    key = name.strip().lower()
    if key in _NAMES:
        return _NAMES[key]
    if m := _GNU.fullmatch(key):
        family, major, minor, later = m.groups()
        return f"{family.upper()}-{major}.{minor or '0'}-{'or-later' if later else 'only'}"
    if not re.fullmatch(r"[\w.+-]+", key):
        return None  # empty, or with an exception (`GPL-2+ with OpenSSL exception`)
    valid = licensing().validate(name.strip())
    return None if valid.errors else str(valid.normalized_expression)


def spdx_expression(debian: str) -> str | None:
    """SPDX for a DEP-5 License line, or None if any name in it is uncertain. `and` binds tighter
    than `or`, as in SPDX; a comma joins groups with `and` at the lowest precedence."""
    groups = []
    for group in re.split(r",\s*(?:and\s+)?", debian.strip()):
        tokens = re.split(r"\s+(and|or)\s+", group.strip(), flags=re.IGNORECASE)
        parts = []
        for i, token in enumerate(tokens):
            spdx = token.upper() if i % 2 else spdx_name(token)
            if spdx is None:
                return None
            parts.append(spdx)
        groups.append(" ".join(parts))
    return groups[0] if len(groups) == 1 else " AND ".join(f"({g})" for g in groups)


def pin(snapshot: str, count: int, out: Path) -> int:
    """Write the first `count` candidates whose debian/copyright is machine-readable."""
    url = f"{SNAPSHOT.format(snapshot=snapshot)}/dists/unstable/main/source/Sources.xz"
    sources_xz = cache_dir() / "debian" / f"Sources-{snapshot}.xz"
    if not sources_xz.exists():
        sources_xz.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=300) as response:
            sources_xz.write_bytes(response.read())
    chosen = []
    for pkg in candidates(lzma.decompress(sources_xz.read_bytes()).decode()):
        text = copyright_text(fetch(pkg, pkg.debian, snapshot))
        if text is not None and dep5(text) is not None:
            chosen.append(pkg)
            if len(chosen) == count:
                break
    pinned = {
        "snapshot": snapshot,
        "sources": {"url": url, "sha256": sha256_of(sources_xz)},
        "packages": [asdict(p) for p in chosen],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pinned, indent=1) + "\n", encoding="utf-8")
    return len(chosen)


def load(path: Path) -> tuple[str, list[Package]]:
    pinned = json.loads(path.read_text(encoding="utf-8"))
    packages = [
        Package(**{**p, "orig": SourceFile(**p["orig"]), "debian": SourceFile(**p["debian"])})
        for p in pinned["packages"]
    ]
    return pinned["snapshot"], packages


def tree(pkg: Package, snapshot: str) -> tuple[Path, list[Stanza]] | None:
    """The unpacked upstream tree and its DEP-5 stanzas; None if the copyright is not DEP-5."""
    stanzas = dep5(copyright_text(fetch(pkg, pkg.debian, snapshot)) or "")
    if stanzas is None:
        return None
    # The tarball's name, not the version: versions can hold an epoch (`1:2.0-1`), and `:` is
    # not allowed in a Windows path.
    name = pkg.orig.name.partition(".orig.tar.")[0]
    return unpack(fetch(pkg, pkg.orig, snapshot), cache_dir() / "src" / name), stanzas
