import json
import re
from pathlib import Path
from typing import Any

import pytest

from provgrep import github


def test_a_star_threshold_pins_past_searchs_1000_results(monkeypatch: pytest.MonkeyPatch) -> None:
    stars = [5000 - n for n in range(2500)]  # more than one search returns, 2,501 to 5,000 stars
    asked = []

    def search(query: str, token: str) -> list[dict[str, Any]]:
        asked.append(query)
        window = re.search(r"stars:(?:>=(\d+)|(\d+)\.\.(\d+))", query)
        assert window is not None
        at_least, low, high = window.groups()
        lo, hi = (int(at_least), 10**9) if at_least else (int(low), int(high))
        shown = [s for s in stars if lo <= s <= hi][:1000]  # as GitHub, at most 1000
        return [
            {
                "nameWithOwner": f"o/r{s}",
                "stargazerCount": s,
                "defaultBranchRef": {"target": {"oid": "a" * 40, "committedDate": "2026"}},
            }
            for s in shown
        ]

    monkeypatch.setattr(github, "_search", search)
    found = github._top("c", None, 3000, "token")
    assert len(found) == 2001  # every repository with 3,000 stars or more, and no other
    assert asked[0] == "language:c fork:false stars:>=3000 sort:stars-desc"
    assert asked[1] == "language:c fork:false stars:3000..4001 sort:stars-desc"
    assert len(github._top("c", 10, 0, "token")) == 10


def test_a_tier_added_keeps_the_commits_the_corpus_has(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def top(commit: str) -> Any:
        def found(language: str, count: int | None, min_stars: int, token: str) -> Any:
            names = {"c": ["o/old", "o/new"], "zig": ["o/zag"]}[language]
            return [github.Repo(n, commit, "2026", 100, language, None) for n in names]

        return found

    out = tmp_path / "github.json"
    monkeypatch.setattr(github, "_top", top("a" * 40))
    github.pin(["zig"], out, "token", 500)
    monkeypatch.setattr(github, "_top", top("b" * 40))
    assert github.pin(["c"], out, "token", None, 1000, add=True) == 2
    commits = {r.name: r.commit for r in github.load(out)}
    assert commits == {"o/zag": "a" * 40, "o/old": "b" * 40, "o/new": "b" * 40}
    tiers = json.loads(out.read_text(encoding="utf-8"))["tiers"]
    assert [(t["languages"], t["repos"]) for t in tiers] == [(["zig"], 1), (["c"], 2)]
    assert github.pin(["zig"], out, "token", 500, add=True) == 0  # o/zag stays at its commit
    assert {r.name: r.commit for r in github.load(out)}["o/zag"] == "a" * 40
