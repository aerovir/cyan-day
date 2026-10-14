"""CLI for importing and planning editorial content."""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Sequence

from .content_planner import build_plan
from .content_store import RECIPE_TYPES, ContentError, ContentStore, DEFAULT_CONTENT_DB


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
    draft = sub.add_parser("draft")
    draft_sub = draft.add_subparsers(dest="draft_action", required=True)
    draft_create = draft_sub.add_parser("create")
    draft_create.add_argument("--text", required=True)
    draft_create.add_argument("--notification-state", choices=("pending", "sent", "failed"), default="pending")
    draft_create.add_argument("--notification-error", default="")
    draft_sub.add_parser("show")
    sub.add_parser("slots")
    pubs = sub.add_parser("publications")
    pubs.add_argument("action", choices=("list", "show", "attempts", "events", "stale", "reconcile"))
    pubs.add_argument("target_key", nargs="?")
    pubs.add_argument("--date", dest="local_date")
    pubs.add_argument("--state")
    pubs.add_argument("--mode")
    pubs.add_argument("--older-than", type=float, default=3600)
    pubs.add_argument("--reconcile-state", choices=("published", "failed"))
    pubs.add_argument("--post-id", type=int)
    pubs.add_argument("--error", default="")
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
                if args.dry_run:
                    cards = store.list_cards(args.local_date[5:])
                    planned = build_plan(args.local_date, cards, recipe_type=random.choice(sorted(RECIPE_TYPES)))
                    result = [slot.__dict__ for slot in planned]
                else:
                    from .main import plan_content_day
                    result = plan_content_day(args.local_date, content_db=args.db)
            elif args.command == "draft":
                if args.draft_action == "create":
                    result = store.create_or_get_test_draft(
                        args.text,
                        notification_state=args.notification_state,
                        notification_error=args.notification_error,
                    )
                else:
                    result = store.get_test_draft()
            elif args.command == "publications":
                targets = store.publication_targets(local_date=args.local_date, state=args.state, mode=args.mode)
                if args.action == "list":
                    result = targets
                elif args.action == "stale":
                    result = store.mark_publications_stale(args.older_than)
                elif not args.target_key:
                    raise ContentError("target_key is required")
                elif args.action == "show":
                    result = next((row for row in targets if row["target_key"] == args.target_key), None)
                    if result is None:
                        raise ContentError("publication target not found")
                elif args.action == "attempts":
                    result = store.publication_attempts(args.target_key)
                elif args.action == "events":
                    result = store.publication_events(args.target_key)
                elif args.action == "stale":
                    result = store.mark_publications_stale(args.older_than)
                elif args.action == "reconcile":
                    target = next((row for row in store.publication_targets() if row["target_key"] == args.target_key), None)
                    if target is None or target["state"] != "unknown":
                        raise ContentError("only unknown targets can be reconciled")
                    if args.reconcile_state is None:
                        raise ContentError("--reconcile-state is required")
                    if args.reconcile_state == "published" and (args.post_id is None or args.post_id <= 0):
                        raise ContentError("--post-id must be positive for published reconciliation")
                    if not store.reconcile_publication(args.target_key, args.reconcile_state, post_id=args.post_id, error=args.error):
                        raise ContentError("reconciliation failed")
                    result = next(row for row in store.publication_targets() if row["target_key"] == args.target_key)
            else:
                raise ContentError("unknown command")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (ContentError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"content: {type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:500]}")
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
