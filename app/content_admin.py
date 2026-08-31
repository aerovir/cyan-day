"""CLI for importing and planning editorial content."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .content_planner import build_plan
from .content_store import ContentError, ContentStore, DEFAULT_CONTENT_DB


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="content")
    parser.add_argument("--db", default=DEFAULT_CONTENT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("--file", required=True)
    imp.add_argument("--dry-run", action="store_true")
    listed = sub.add_parser("list")
    listed.add_argument("--date", dest="calendar_day")
    listed.add_argument("--status", action="append", dest="statuses")
    show = sub.add_parser("show")
    show.add_argument("card_id")
    status = sub.add_parser("status")
    status.add_argument("card_id")
    status.add_argument("--value", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--date", required=True, dest="local_date")
    plan.add_argument("--dry-run", action="store_true")
    sub.add_parser("slots")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with ContentStore(args.db) as store:
            if args.command == "import":
                path = Path(args.file)
                files = sorted(path.glob("*.json")) if path.is_dir() else [path]
                if not files:
                    raise ContentError(f"no JSON files found in: {path}")
                result = []
                for file_path in files:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    cards = data if isinstance(data, list) else [data]
                    result.extend(store.import_card(card, dry_run=args.dry_run).__dict__ for card in cards)
            elif args.command == "list":
                result = [card.packet() for card in store.list_cards(args.calendar_day, args.statuses or ())] if args.statuses else [card.packet() for card in store.list_cards(args.calendar_day)]
            elif args.command == "show":
                result = store.get_card(args.card_id).packet()
            elif args.command == "status":
                result = store.set_status(args.card_id, args.value).packet()
            elif args.command == "slots":
                result = store.slots()
            elif args.command == "plan":
                cards = store.list_cards()
                planned = build_plan(args.local_date, cards)
                if not args.dry_run:
                    store.save_plan(args.local_date, planned)
                result = [slot.__dict__ for slot in planned]
            else:
                raise ContentError("unknown command")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (ContentError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"content: {type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:500]}")
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
