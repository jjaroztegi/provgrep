"""provgrep command line."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path


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

    p = commands.add_parser(
        "evaluate", help="measure the resolved licenses against the pinned ground-truth set"
    )
    p.add_argument("--set", type=Path, default=Path("truth/debian-python.json"))
    p.add_argument("--audit", type=Path, default=Path("truth/debian-python-audit.json"))
    p.add_argument("--limit", type=int, help="only the first N packages of the set")
    p.add_argument("--jobs", type=int, default=4, help="packages resolved at once (default: 4)")
    p.add_argument("--out", type=Path, default=Path("results"))

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
    elif args.command == "truth":
        from provgrep.debian import pin

        print(f"pinned {pin(args.snapshot, args.count, args.out)} packages in {args.out}")
    else:
        from provgrep.evaluate import evaluate

        out = evaluate(args.set, args.audit, args.out, args.limit, args.jobs)
        print((out / "summary.md").read_text(encoding="utf-8").split("\n")[2])
        print(f"details in {out}")
    return 0
