"""Search open-source code by what it does, with the license of every snippet and its evidence."""

import re
import subprocess
from pathlib import Path

# An address, in angle brackets or not, also written `name [at] host (dot) org`. Spaces before it
# go with it, newlines do not: line numbers must not move. "name at host dot org" in plain words
# is left: it is also how "look at the dot product" reads.
_EMAIL = re.compile(
    r"[ \t]*[<(\[]?(mailto:)?[\w.+-]+(@|[ \t]*[\[(]at[\])][ \t]*)[\w-]+"
    r"((\.|[ \t]*[\[(]dot[\])][ \t]*)[\w-]+)+[>)\]]?",
    re.IGNORECASE,
)


def without_emails(text: str) -> str:
    """The text with its email addresses left out: provgrep never stores or shows one."""
    return _EMAIL.sub("", text)


def read_text(path: Path) -> str:
    """A source, license or manifest file as provgrep reads it: UTF-8 without the byte-order mark
    some editors start a file with (left in, it hid the file's header and so its license), bytes
    that are not UTF-8 replaced."""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def source_commit() -> str | None:
    """The source commit, with `-dirty` if tracked files have changed since."""
    here = Path(__file__).parent

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=here, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        dirty = git("status", "--porcelain", "--untracked-files=no")
        return git("rev-parse", "--short", "HEAD") + ("-dirty" if dirty else "")
    except OSError, subprocess.CalledProcessError:
        return None
