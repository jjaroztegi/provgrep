"""Whether code may go into a project under a given license, as the OSADL License Compatibility
Matrix says.

For each pair of 119 licenses the matrix answers whether code under one can be integrated into
software under the other and distributed compliantly: Yes, No, Check dependency (it depends on
how the code is combined; the explanation says how) or Unknown, each with OSADL's explanation.
Those answers are OSADL's, carried as they are. What is ours, and only this:

- A snippet's license is a choice of ways to comply (licenses.choices); each way is as good as
  the worst answer among its licenses, and the snippet as good as its best way.
- `X WITH exception`, when the matrix lacks it, takes X's answer, as ORT does (an exception adds
  permissions), except that a No becomes Unknown: the exception may be what lifts it.
- A license the matrix lacks (CC0-1.0, LicenseRef-*) is Unknown, never compatible.

osadl/matrixseqexpl.json is the matrix as published at
https://www.osadl.org/fileadmin/checklists/matrixseqexpl.json (CC-BY-4.0; see REUSE.toml),
unmodified; its timestamp goes with every verdict.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

MATRIX = Path(__file__).parent / "osadl" / "matrixseqexpl.json"
SOURCE = "https://www.osadl.org/Access-to-raw-data.oss-compliance-raw-data-access.0.html"
ANSWERS = ("yes", "conditional", "unknown", "no")  # best first
_ANSWER = {
    "Same": "yes",
    "Yes": "yes",
    "Check dependency": "conditional",
    "Unknown": "unknown",
    "No": "no",
}
# The project licenses offered to pick from: the licenses of the corpus's repositories, with -only
# and -or-later apart since their answers differ. Any license the matrix has can be asked for.
PROJECTS = (
    "MIT",
    "Apache-2.0",
    "BSD-3-Clause",
    "BSD-2-Clause",
    "ISC",
    "Zlib",
    "BSL-1.0",
    "Unlicense",
    "0BSD",
    "MPL-2.0",
    "LGPL-2.1-or-later",
    "LGPL-3.0-or-later",
    "GPL-2.0-only",
    "GPL-2.0-or-later",
    "GPL-3.0-only",
    "GPL-3.0-or-later",
    "AGPL-3.0-only",
    "AGPL-3.0-or-later",
    "EPL-2.0",
    "EUPL-1.2",
)


@dataclass(frozen=True)
class Verdict:
    project: str  # the project's license, as the matrix names it
    answer: str  # one of ANSWERS
    licenses: list[str]  # the way to comply with the code's license it is about
    reasons: list[str]  # for each of those licenses, OSADL's explanation or why there is none
    source: str  # the matrix and its timestamp


def project(spdx: str) -> str:
    """A project license as the matrix names it, case aside; ValueError if it has no row."""
    found = _matrix()[1].get(spdx.strip().lower())
    if found is None:
        raise ValueError(f"OSADL's compatibility matrix has no license {spdx!r}")
    return found[0]


def verdict(project_license: str, choices: Sequence[Sequence[str]]) -> Verdict:
    """Whether code under one of these ways to comply may go into a project under the license."""
    timestamp, rows = _matrix()
    name, cells = rows[project_license.lower()]
    source = f"OSADL License Compatibility Matrix, {timestamp}"
    best = Verdict(name, "unknown", [], ["no license found"], source)
    for choice in choices:
        judged = [_cell(cells, spdx) for spdx in choice]
        answer = max((a for a, _ in judged), key=ANSWERS.index, default="unknown")
        if ANSWERS.index(answer) < ANSWERS.index(best.answer) or not best.licenses:
            best = Verdict(name, answer, list(choice), [r for _, r in judged], source)
    return best


def _cell(cells: dict[str, tuple[str, str]], spdx: str) -> tuple[str, str]:
    if spdx.lower() in cells:
        return cells[spdx.lower()]
    base, _, exception = spdx.partition(" WITH ")
    if exception and base.lower() in cells:
        answer, reason = cells[base.lower()]
        note = f"{exception} is not in the matrix; this is the answer for {base}."
        return ("unknown" if answer == "no" else answer), f"{reason} {note}"
    return "unknown", f"OSADL's matrix does not have {spdx}."


@cache
def _matrix() -> tuple[str, dict[str, tuple[str, dict[str, tuple[str, str]]]]]:
    """The timestamp, and by lower-cased project license: its name, and the answer and
    explanation for each lower-cased license of the code."""
    data = json.loads(MATRIX.read_text(encoding="utf-8"))
    rows = {
        row["name"].lower(): (
            row["name"],
            {
                c["name"].lower(): (
                    _ANSWER[c["compatibility"]],
                    "The same license." if c["compatibility"] == "Same" else c["explanation"],
                )
                for c in row["compatibilities"]
            },
        )
        for row in data["licenses"]
    }
    return data["timestamp"], rows
