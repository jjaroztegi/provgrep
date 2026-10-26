"""provgrep command line."""

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="provgrep",
        description="Search open-source code by what it does, with the license of every "
        "snippet and the evidence for it.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser(
        "license", help="the license of every code file in a directory, and its evidence"
    )
    p.add_argument("path", type=Path)
    p.add_argument("--json", action="store_true", help="every resolution with its evidence")

    p = commands.add_parser("truth", help="pin a ground-truth set of Debian packages")
    p.add_argument(
        "--snapshot", required=True, help="snapshot.debian.org timestamp, e.g. 20261024T000000Z"
    )
    p.add_argument("--count", type=int, default=300, help="packages to pin (default: 300)")
    p.add_argument("--out", type=Path, default=Path("truth/debian-python.json"))

    evaluate = commands.add_parser("evaluate", help="measure licenses or search").add_subparsers(
        dest="target", required=True
    )
    p = evaluate.add_parser(
        "licenses", help="the resolved licenses against the pinned Debian ground-truth set"
    )
    p.add_argument("--set", type=Path, default=Path("truth/debian-python.json"))
    p.add_argument("--audit", type=Path, default=Path("truth/debian-python-audit.json"))
    p.add_argument("--limit", type=int, help="only the first N packages of the set")
    p.add_argument("--jobs", type=int, default=4, help="packages resolved at once (default: 4)")
    p.add_argument("--out", type=Path, default=Path("results"))
    p = evaluate.add_parser("search", help="search against suites of intents")
    p.add_argument("suites", type=Path, nargs="*", default=sorted(Path("truth/intents").glob("*")))
    p.add_argument("--index", type=Path, default=Path("index/github"))
    p.add_argument("--out", type=Path, default=Path("results"))

    p = commands.add_parser(
        "corpus", help="pin the most-starred GitHub repositories of each language to a commit"
    )
    p.add_argument(
        "--languages",
        default="c,c++,go,java,javascript,python,rust,typescript",
        help="GitHub language names, comma-separated",
    )
    p.add_argument("--count", type=int, default=500, help="repositories per language")
    p.add_argument("--out", type=Path, default=Path("corpus/github.json"))

    p = commands.add_parser("build", help="build the search index of a pinned corpus")
    p.add_argument("--corpus", type=Path, default=Path("corpus/github.json"))
    p.add_argument("--out", type=Path, default=Path("index/github"))
    p.add_argument("--jobs", type=int, default=4, help="repositories staged at once (default: 4)")
    p.add_argument("--limit", type=int, help="only the first N repositories of each language")

    p = commands.add_parser("search", help="search the index")
    p.add_argument("query")
    p.add_argument("--index", type=Path, default=Path("index/github"))
    p.add_argument("--language", action="append", default=[], help="repeat for several")
    p.add_argument(
        "--license",
        help='"permissive" (default without --project), "any", or SPDX ids, comma-separated',
    )
    p.add_argument(
        "--project",
        metavar="SPDX",
        help="your project's license: only code OSADL's matrix says may go into it",
    )
    p.add_argument("--level", action="append", default=[], help="A, B, C or X; repeat")
    p.add_argument(
        "--require", action="append", default=[], help="an identifier the code must contain"
    )
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")

    p = commands.add_parser(
        "copies", help="functions in several repositories, and their copies' licenses"
    )
    p.add_argument("--index", type=Path, default=Path("index/github"))
    p.add_argument("--out", type=Path, default=Path("results"))

    p = commands.add_parser("mcp", help="serve the search as MCP tools over stdio")
    p.add_argument("--index", type=Path, default=Path("index/github"))

    p = commands.add_parser("serve", help="serve the website, its API and MCP over an index")
    p.add_argument("--index", type=Path, default=Path("index/github"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--public", metavar="URL", help="the URL a proxy in front of it is reached at")

    args = parser.parse_args(argv)
    if args.command == "license":
        from provgrep.licenses import resolve_tree

        resolved = resolve_tree(args.path)
        if args.json:
            print(json.dumps({path: asdict(r) for path, r in resolved.items()}, indent=1))
        else:
            for path, r in resolved.items():
                note = "  (evidence disagrees)" if r.conflict else ""
                print(f"{r.level}  {r.expression or 'NOASSERTION'}  {path}{note}")
    elif args.command == "corpus":
        from provgrep import github

        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            parser.error("corpus needs a GitHub token in GITHUB_TOKEN (search is API-only)")
        count = github.pin(args.languages.split(","), args.count, args.out, token)
        print(f"pinned {count} repositories in {args.out}")
    elif args.command == "build":
        from provgrep.build import build

        manifest = build(args.corpus, args.out, args.jobs, args.limit)
        print(json.dumps(manifest["counts"], indent=1))
        if manifest["failed"]:
            print(f"failed: {len(manifest['failed'])} (listed in the manifest)")
    elif args.command == "search":
        from provgrep.search import Index, licenses_of

        page = Index(args.index).search(
            args.query,
            languages=args.language,
            licenses=licenses_of(args.license, args.project),
            project=args.project,
            levels=args.level,
            require=args.require,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps(asdict(page), indent=1))
        else:
            _print_page(page)
    elif args.command == "copies":
        from provgrep.copies import study

        out = study(args.index, args.out)
        print((out / "counts.json").read_text(encoding="utf-8"))
        print(f"details in {out}")
    elif args.command == "mcp":
        from provgrep.mcp_server import server
        from provgrep.search import Index

        server(Index(args.index)).run("stdio")
    elif args.command == "serve":
        from provgrep.server import serve

        serve(args.index, args.host, args.port, args.public)
    elif args.command == "truth":
        from provgrep.debian import pin

        print(f"pinned {pin(args.snapshot, args.count, args.out)} packages in {args.out}")
    elif args.target == "licenses":
        from provgrep.evaluate import evaluate as evaluate_licenses

        out = evaluate_licenses(args.set, args.audit, args.out, args.limit, args.jobs)
        print((out / "summary.md").read_text(encoding="utf-8").split("\n")[2])
        print(f"details in {out}")
    else:
        from provgrep.intents import evaluate as evaluate_search

        out = evaluate_search(args.suites, args.index, args.out)
        print((out / "summary.md").read_text(encoding="utf-8"))
        print(f"details in {out}")
    return 0


def _print_page(page: Any) -> None:
    print(f"{page.total} snippets")
    for r in page.results:
        loc = r.location
        lic = loc.license
        print(f"\n{r.score:6.2f}  {loc.repo}  {loc.path}:{loc.start}-{loc.end}  {loc.symbol}")
        copies = f", {r.copies} copies in {r.repos} repositories" if r.copies > 1 else ""
        print(f"        {lic.expression or 'no license found'} (level {lic.level}){copies}")
        if r.compatibility:
            fits = r.compatibility
            print(f"        {fits.answer} into {fits.project}: {' '.join(fits.reasons)}")
        for notice in lic.notices[:3]:
            print(f"        {notice}")
        print(f"        {loc.url}")
        if r.code is None:
            print("        (link only: no license that lets the code be shared)")
        else:
            print("\n".join("        | " + line for line in r.code.split("\n")[:12]))
