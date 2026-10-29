"""A corpus of GitHub repositories, each pinned to a commit.

The corpus is the most-starred repositories whose main language is the one asked for, forks left
out, as GitHub's search ranked them when they were pinned: a number of them per language, or all
with at least a number of stars. The pinned file is the corpus: building
from it needs no API and no token, only each commit's tarball, and the commit id that `git archive`
writes into the tarball's pax header shows the tarball is that commit.
"""

import json
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from provgrep.fetch import USER_AGENT, download, unpack

GRAPHQL = "https://api.github.com/graphql"
# Larger repositories are mostly assets or data; they are left out of a build and listed.
MAX_TARBALL = 400 << 20
_SEARCH = """
query($q: String!, $after: String) {
  search(query: $q, type: REPOSITORY, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on Repository {
      nameWithOwner stargazerCount
      primaryLanguage { name }
      licenseInfo { spdxId }
      defaultBranchRef { target { ... on Commit { oid committedDate } } }
    } }
  }
}
"""


@dataclass(frozen=True)
class Repo:
    name: str  # owner/repo
    commit: str
    committed: str  # the commit's date as git records it
    stars: int  # when pinned
    language: str  # GitHub's main language for it, the one it was chosen for
    license: str | None  # what GitHub detects at the root, kept as outside evidence

    @property
    def purl(self) -> str:
        return f"pkg:github/{self.name.lower()}@{self.commit}"

    def url(self, path: str = "", lines: tuple[int, int] | None = None) -> str:
        """The repository at its pinned commit, or one of its files, or lines of a file."""
        if not path:
            return f"https://github.com/{self.name}/tree/{self.commit}"
        anchor = f"#L{lines[0]}-L{lines[1]}" if lines else ""
        return f"https://github.com/{self.name}/blob/{self.commit}/{path}{anchor}"


def pin(languages: list[str], out: Path, token: str, count: int | None, min_stars: int = 0) -> int:
    """Write the `count` most-starred repositories of each language (all of them with None)
    that have at least min_stars stars, each at its default branch's head, most-starred
    first."""
    repos = [r for language in languages for r in _top(language, count, min_stars, token)]
    repos.sort(key=lambda r: (-r.stars, r.name.lower()))
    pinned = {
        "pinned": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "languages": languages,
        "per_language": count,
        "min_stars": min_stars,
        "repos": [asdict(r) for r in repos],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pinned, indent=1) + "\n", encoding="utf-8")
    return len(repos)


def _top(language: str, count: int | None, min_stars: int, token: str) -> list[Repo]:
    """Search returns at most 1000 results per query, so it is asked again at or below the fewest
    stars seen, until enough repositories are found or there are no more."""
    found: dict[str, Repo] = {}
    stars = f"stars:>={min_stars}"
    while count is None or len(found) < count:
        before = len(found)
        query = f"language:{language} fork:false {stars} sort:stars-desc"
        for node in _search(query, token):
            target = (node.get("defaultBranchRef") or {}).get("target")
            if not target or node["nameWithOwner"] in found:
                continue  # an empty repository has no commit to pin
            found[node["nameWithOwner"]] = Repo(
                node["nameWithOwner"],
                target["oid"],
                target["committedDate"],
                node["stargazerCount"],
                (node.get("primaryLanguage") or {}).get("name") or language,
                (node.get("licenseInfo") or {}).get("spdxId") or None,
            )
        if len(found) == before:
            break
        stars = f"stars:{min_stars}..{min(r.stars for r in found.values())}"
    return sorted(found.values(), key=lambda r: (-r.stars, r.name.lower()))[:count]


def load(path: Path) -> list[Repo]:
    return [Repo(**r) for r in json.loads(path.read_text(encoding="utf-8"))["repos"]]


def tree(repo: Repo, work: Path, keep: Callable[[PurePosixPath], bool]) -> Path:
    """Download the pinned commit into work and unpack the files `keep` wants; return the root.
    work is the caller's to remove: the tarball is not kept, what is built from it is."""
    archive = download(
        [f"https://codeload.github.com/{repo.name}/tar.gz/{repo.commit}"],
        None,
        work / "archive.tar.gz",
        MAX_TARBALL,
    )
    with tarfile.open(archive) as tar:
        if (commit := tar.pax_headers.get("comment")) != repo.commit:
            raise ValueError(f"{repo.name}: the tarball is commit {commit}, not {repo.commit}")
    # Member names start with the archive's top directory, `repo-<sha>/`.
    return unpack(
        archive, work / "tree", lambda name: keep(PurePosixPath(*PurePosixPath(name).parts[1:]))
    )


def _search(query: str, token: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    after = None
    while True:
        data = _graphql(_SEARCH, {"q": query, "after": after}, token)["search"]
        nodes += data["nodes"]
        if not data["pageInfo"]["hasNextPage"]:
            return nodes
        after = data["pageInfo"]["endCursor"]


def _graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {"Authorization": f"bearer {token}", "User-Agent": USER_AGENT}
    for attempt in range(4):
        request = urllib.request.Request(GRAPHQL, body, headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                reply = json.load(response)
        except urllib.error.HTTPError as e:
            # Search times out now and then with a 502; anything else is not worth retrying.
            if e.code < 500 or attempt == 3:
                raise
            time.sleep(2**attempt)
            continue
        except OSError:  # a dropped connection (URLError), as in a long pin
            if attempt == 3:
                raise
            time.sleep(2**attempt)
            continue
        if reply.get("errors"):
            raise RuntimeError(f"GitHub: {reply['errors']}")
        data: dict[str, Any] = reply["data"]
        return data
    raise AssertionError("unreachable")
