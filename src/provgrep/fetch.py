"""Downloads checked against a known hash, and archives unpacked without leaving their directory."""

import hashlib
import os
import shutil
import tarfile
import urllib.request
from pathlib import Path

USER_AGENT = "provgrep (+https://github.com/jjaroztegi/provgrep)"


def cache_dir() -> Path:
    if env := os.environ.get("PROVGREP_CACHE"):
        return Path(env)
    base = os.environ.get("LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME")
    return Path(base) / "provgrep" if base else Path.home() / ".cache" / "provgrep"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(urls: list[str], sha256: str, dest: Path) -> Path:
    """Fetch the first URL that serves a file with this hash, unless dest already is that file."""
    if dest.exists() and sha256_of(dest) == sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    errors = []
    for url in urls:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as f:
                shutil.copyfileobj(response, f)
        except OSError as e:
            errors.append(f"{url}: {e}")
            continue
        if (got := sha256_of(part)) != sha256:
            errors.append(f"{url}: sha256 {got}, expected {sha256}")
            continue
        part.replace(dest)
        return dest
    part.unlink(missing_ok=True)
    raise OSError(f"could not download {dest.name}:\n  " + "\n  ".join(errors))


def unpack(archive: Path, dest: Path) -> Path:
    """Unpack a tarball into dest once and return its root: its only top-level directory, if any.

    The "data" filter refuses members that would land outside dest (absolute paths, `..`, links
    pointing out) and device files, so an upstream tarball cannot write anywhere else.
    """
    if not dest.exists():
        part = dest.with_name(dest.name + ".part")
        shutil.rmtree(part, ignore_errors=True)
        with tarfile.open(archive) as tar:
            tar.extractall(part, filter="data")
        part.replace(dest)
    entries = list(dest.iterdir())
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else dest
