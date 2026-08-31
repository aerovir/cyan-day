"""Тесты стартового набора редакционных карточек (data/cards)."""

import json
from pathlib import Path

from app.content_store import ANY_DAY, ContentStore

CARDS_DIR = Path(__file__).resolve().parent.parent / "data" / "cards"


def _card_files() -> list[Path]:
    files = sorted(CARDS_DIR.glob("*.json"))
    assert files, "data/cards пуст — стартовый набор не создан"
    return files


def _import_all(store: ContentStore):
    return [store.import_card(json.loads(path.read_text(encoding="utf-8"))) for path in _card_files()]


def test_all_seed_cards_import_cleanly():
    with ContentStore(":memory:") as store:
        cards = _import_all(store)
        assert len(cards) >= 20


def test_seed_card_ids_unique():
    with ContentStore(":memory:") as store:
        ids = [card.card_id for card in _import_all(store)]
        assert len(ids) == len(set(ids))


def test_seed_status_distribution():
    with ContentStore(":memory:") as store:
        statuses = {card.status for card in _import_all(store)}
        assert {"verified", "unverified", "disputed", "refuted"} <= statuses


def test_seed_calendar_days_valid():
    with ContentStore(":memory:") as store:
        for card in _import_all(store):
            assert card.calendar_day == ANY_DAY or (
                len(card.calendar_day) == 5 and card.calendar_day[2] == "-"
            ), card.card_id


def test_seed_verified_cards_have_trusted_sources_with_quotes():
    with ContentStore(":memory:") as store:
        for card in _import_all(store):
            if card.status != "verified":
                continue
            trusted = [s for s in card.provenance if s["source_type"] in {"academic", "primary"}]
            assert trusted, f"{card.card_id}: нет доверенного источника"
            for source in card.provenance:
                assert source["url"].startswith("https://"), f"{card.card_id}: url не https"
                assert source["quote"], f"{card.card_id}: у источника {source['provenance_id']} нет цитаты"


def test_seed_refuted_cards_are_date_bound_or_thematic():
    """Мифы о Менделееве привязаны к 31 января — «дню рождения водки»."""
    with ContentStore(":memory:") as store:
        cards = {card.card_id: card for card in _import_all(store)}
        for card_id in ("mendeleev-vodka-refuted", "mendeleev-40-degrees-refuted"):
            assert cards[card_id].calendar_day == "01-31", card_id
