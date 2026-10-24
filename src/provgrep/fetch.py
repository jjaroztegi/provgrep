"""Downloads checked against a known hash, and archives unpacked without leaving their directory."""

import hashlib
import os
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

USER_AGENT = "provgrep (+https://github.com/jjaroztegi/provgrep)"


class TooLarge(OSError):
    """A download over its size limit: no other source of the same file would be smaller."""


_ATTEMPTS = 4


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


def download(urls: list[str], sha256: str | None, dest: Path, max_bytes: int | None = None) -> Path:
    """Fetch the first URL that serves a file with this hash, unless dest already is that file.
    Without a hash, any file the URL serves, and dest as it is if it exists: the caller checks it.
    A file larger than max_bytes is not kept.
    """
    if dest.exists() and (sha256 is None or sha256_of(dest) == sha256):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    errors = []
    for url in urls:
        try:
            _fetch(url, part, max_bytes)
        except TooLarge:
            part.unlink(missing_ok=True)
            raise
        except OSError as e:
            errors.append(f"{url}: {e}")
            continue
        if sha256 is not None and (got := sha256_of(part)) != sha256:
            errors.append(f"{url}: sha256 {got}, expected {sha256}")
            continue
        part.replace(dest)
        return dest
    part.unlink(missing_ok=True)
    raise OSError(f"could not download {dest.name}:\n  " + "\n  ".join(errors))


def _fetch(url: str, dest: Path, max_bytes: int | None) -> None:
    """Write what url serves to dest. Asked to slow down (429) or failing on its side (5xx), a
    server is asked again after the wait it gives, or a growing one; so is one the connection to
    failed."""
    for attempt in range(_ATTEMPTS):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as f:
                while block := response.read(1 << 20):
                    f.write(block)
                    if max_bytes is not None and f.tell() > max_bytes:
                        raise TooLarge(f"larger than {max_bytes >> 20} MB")
            return
        except urllib.error.HTTPError as e:
            if (e.code != 429 and e.code < 500) or attempt == _ATTEMPTS - 1:
                raise
            after = e.headers.get("Retry-After", "")
            time.sleep(min(int(after), 600) if after.isdigit() else 30 * 2**attempt)
        except TooLarge:
            raise
        except OSError:  # a reset connection, a name not resolved, a read timed out
            if attempt == _ATTEMPTS - 1:
                raise
            time.sleep(5 * 2**attempt)


def unpack(archive: Path, dest: Path, keep: Callable[[str], bool] | None = None) -> Path:
    """Unpack a tarball into dest once and return its root: its only top-level directory, if any.
    With `keep`, only the regular files whose member name it accepts.

    The "data" filter refuses members that would land outside dest (absolute paths, `..`, links
    pointing out) and device files, so an upstream tarball cannot write anywhere else.
    """

    def wanted(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        if keep is not None and not (member.isfile() and keep(member.name)):
            return None
        return tarfile.data_filter(member, path)

    if not dest.exists():
        part = dest.with_name(dest.name + ".part")
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir(parents=True)  # kept even when `keep` wants none of the files
        with tarfile.open(archive) as tar:
            tar.extractall(part, filter=wanted)
        part.replace(dest)
    entries = list(dest.iterdir())
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else dest
