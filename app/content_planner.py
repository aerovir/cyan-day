"""Deterministic seven-slot content planner."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from .content_store import ContentCard, SLOT_DEFINITIONS

DEFAULT_TIMEZONE = "Europe/Moscow"

@dataclass(frozen=True)
class PlannedSlot:
    local_date: str
    slot_key: str
    local_time: str
    card_id: str | None
    score: float
    reason: str
    state: str = "planned"


def _tags(card: ContentCard) -> set[str]:
    return set(card.tags)


def _score(card: ContentCard, requirements: Sequence[str], selected: Sequence[ContentCard], recent: Iterable[str]) -> float:
    tags = _tags(card)
    score = float(card.priority)
    score += 25 if any(tag in tags for tag in requirements) else 0
    score += 10 if card.status == "verified" else 0
    selected_tags = set().union(*(_tags(item) for item in selected)) if selected else set()
    score += 12 if not tags.intersection(selected_tags) else 0
    score -= 35 if card.card_id in set(recent) else 0
    score -= 20 * sum(1 for item in selected if tags.intersection(_tags(item)))
    tie = int(hashlib.sha256(card.card_id.encode()).hexdigest()[:8], 16) / 2**32
    return score + tie


def build_plan(local_date: str, cards: Sequence[ContentCard], recent_card_ids: Iterable[str] = ()) -> list[PlannedSlot]:
    """Select at most one card per semantic slot, deterministically."""
    available = [card for card in cards if card.active and card.status in {"verified", "unverified", "disputed", "refuted", "rejected"}]
    selected: list[ContentCard] = []
    plan: list[PlannedSlot] = []
    recent = tuple(recent_card_ids)
    for slot_key, local_time, requirements, strict in SLOT_DEFINITIONS:
        pool = [card for card in available if card not in selected and any(tag in _tags(card) for tag in requirements)]
        if not pool and not strict:
            pool = [card for card in available if card not in selected]
        if pool:
            card = max(pool, key=lambda candidate: _score(candidate, requirements, selected, recent))
            selected.append(card)
            plan.append(PlannedSlot(local_date, slot_key, local_time, card.card_id, _score(card, requirements, selected[:-1], recent), "semantic match and diversity"))
        else:
            plan.append(PlannedSlot(local_date, slot_key, local_time, None, 0, "no eligible card", "skipped"))
    return plan


def due_at(local_date: str, local_time: str, timezone: str = DEFAULT_TIMEZONE) -> datetime:
    """Return a timezone-aware UTC due time for a local slot."""
    local = datetime.combine(date.fromisoformat(local_date), time.fromisoformat(local_time), ZoneInfo(timezone))
    return local.astimezone(ZoneInfo("UTC"))


def parse_slot_times(value: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in value.split(",") if part.strip())
    if len(values) != 7:
        raise ValueError("SLOT_TIMES must contain exactly seven times")
    for value in values:
        parsed = time.fromisoformat(value)
        if parsed.second or parsed.microsecond:
            raise ValueError("slot times must use HH:MM")
    return values
