import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from provgrep.fetch import TooLarge, download, unpack


def _tarball(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_unpack_returns_the_single_top_directory(tmp_path: Path) -> None:
    _tarball(tmp_path / "a.tar.gz", {"pkg-1.0/setup.py": b"", "pkg-1.0/pkg/__init__.py": b""})
    assert unpack(tmp_path / "a.tar.gz", tmp_path / "out") == tmp_path / "out" / "pkg-1.0"


def test_unpack_refuses_a_member_outside_its_directory(tmp_path: Path) -> None:
    _tarball(tmp_path / "evil.tar.gz", {"../escaped.txt": b"x"})
    with pytest.raises(tarfile.OutsideDestinationError):
        unpack(tmp_path / "evil.tar.gz", tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


def test_download_checks_the_hash(tmp_path: Path) -> None:
    source = tmp_path / "file"
    source.write_bytes(b"content")
    url = source.as_uri()
    good = hashlib.sha256(b"content").hexdigest()
    assert download([url], good, tmp_path / "cache" / "file").read_bytes() == b"content"
    with pytest.raises(OSError, match="sha256"):
        download([url], "0" * 64, tmp_path / "cache" / "other")
    assert not (tmp_path / "cache" / "other").exists()


def test_download_refuses_a_file_over_its_limit(tmp_path: Path) -> None:
    source = tmp_path / "big"
    source.write_bytes(b"x" * (3 << 20))
    dest = tmp_path / "cache" / "big"
    with pytest.raises(TooLarge):
        download([source.as_uri()], None, dest, max_bytes=1 << 20)
    assert list(dest.parent.iterdir()) == []
