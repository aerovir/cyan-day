"""SSH/Docker CLI for managing the approved holiday source registry."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Sequence

from .source_registry import DEFAULT_DB_PATH, SourceRegistry
from .sources import SourceError, adapter_types


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="source", description="Manage holiday sources")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list", help="list sources")
    list_cmd.add_argument("--all", action="store_true", dest="include_removed")
    list_cmd.add_argument("--enabled", action="store_true", dest="enabled_only")

    show_cmd = sub.add_parser("show", help="show a source and its audit history")
    show_cmd.add_argument("identifier")

    add_cmd = sub.add_parser("add", help="add a disabled source")
    add_cmd.add_argument("--name", required=True)
    add_cmd.add_argument("--type", required=True, choices=adapter_types())
    add_cmd.add_argument("--url", required=True)
    add_cmd.add_argument("--timeout", type=int, default=30)

    test_cmd = sub.add_parser("test", help="fetch and parse a source without publishing")
    test_cmd.add_argument("identifier")
    test_cmd.add_argument("--date", default=None, dest="day")

    for name in ("enable", "disable"):
        command = sub.add_parser(name, help=f"{name} a source")
        command.add_argument("identifier")

    remove_cmd = sub.add_parser("remove", help="logically remove a source")
    remove_cmd.add_argument("identifier")
    remove_cmd.add_argument("--confirm", action="store_true", help="confirm logical removal")
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def run(argv: Sequence[str] | None = None) -> int:
    # Docker's admin service may expose ``source`` as its entrypoint while
    # direct invocation uses ``python -m app.source_admin list``.
    normalized_argv = list(argv) if argv is not None else sys.argv[1:]
    if argv is None and os.environ.get("SOURCE_ADMIN_DB") and "--db" not in normalized_argv:
        normalized_argv = ["--db", os.environ["SOURCE_ADMIN_DB"], *normalized_argv]
    if normalized_argv and normalized_argv[0] == "source":
        normalized_argv = normalized_argv[1:]
    args = _parser().parse_args(normalized_argv)
    return _run_parsed(args)


def _run_parsed(args: argparse.Namespace) -> int:
    """Execute a parsed command (kept separate for straightforward testing)."""
    try:
        with SourceRegistry(args.db) as registry:
            if args.command == "list":
                _print([record.to_dict() for record in registry.list(include_removed=args.include_removed, enabled_only=args.enabled_only)])
            elif args.command == "show":
                record = registry.show(args.identifier)
                _print({"source": record.to_dict(), "audit": registry.audit(record.id)})
            elif args.command == "add":
                record = registry.add(args.name, adapter_type=args.type, url=args.url, params={"timeout": args.timeout})
                _print(record.to_dict())
            elif args.command == "test":
                events = registry.test(args.identifier, day=args.day or date.today().isoformat())
                _print({"count": len(events), "events": [event.to_dict() for event in events]})
            elif args.command == "enable":
                _print(registry.enable(args.identifier).to_dict())
            elif args.command == "disable":
                _print(registry.disable(args.identifier).to_dict())
            elif args.command == "remove":
                _print(registry.remove(args.identifier, confirm=args.confirm).to_dict())
            else:  # argparse guarantees this is unreachable.
                raise SourceError(f"unknown command: {args.command}")
        return 0
    except (SourceError, OSError, ValueError) as exc:
        print(f"source: {type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:500]}", file=sys.stderr)
        return 2


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
