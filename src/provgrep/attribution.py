"""The code of a result with its attribution, to paste into another file.

REUSE 3.3 snippet tags (https://reuse.software/spec-3.3/): the copyright and license of a part
of a file, in comments around it. Each copyright statement found becomes a
SPDX-SnippetCopyrightText line; with none found, the holders are named as the spec's examples
do, "Contributors to <project> <url>", since code without a notice still has authors.
"""

import re

from provgrep.search import Result

_COMMENT = {"python": "#"}  # every other indexed language takes //
_PREFIX = re.compile(r"^\s*(copyright\b\s*)?((\(c\)|©)\s*)?", re.IGNORECASE)


def reuse_snippet(result: Result) -> str | None:
    """The code between REUSE snippet tags; None when the code is not shown (link only)."""
    if result.code is None:
        return None
    loc = result.location
    mark = _COMMENT.get(result.language, "//")
    holders = [h for n in loc.license.notices if (h := _PREFIX.sub("", n).strip())]
    if not holders:
        holders = [f"Contributors to {loc.repo} <https://github.com/{loc.repo}>"]
    lines = [
        "SPDX-SnippetBegin",
        *(f"SPDX-SnippetCopyrightText: {h}" for h in holders),
        f"SPDX-License-Identifier: {loc.license.expression}",
        f"Source: {loc.url}",
    ]
    head = "\n".join(f"{mark} {line}" for line in lines)
    return f"{head}\n{result.code.rstrip()}\n{mark} SPDX-SnippetEnd\n"
