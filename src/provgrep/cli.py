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
    return 0
