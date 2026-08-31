"""Тесты хранилища контентных карточек."""

import pytest

from app.content_planner import build_plan
from app.content_store import ContentError, ContentStore


def make_card(card_id="card-1", status="verified", tags=("topic.context",), image_url="", source_type="academic", calendar_day="08-31"):
    return {
        "card_id": card_id,
        "calendar_day": calendar_day,
        "title": "День гранёного стакана",
        "summary": "Стакан как символ эпохи.",
        "status": status,
        "tags": list(tags),
        "claims": [{"claim_id": "c1", "text": "Стакан стал символом СССР.", "provenance_id": "src-1"}],
        "provenance": [{"provenance_id": "src-1", "source_type": source_type, "title": "Энциклопедия стаканов"}],
        "image_url": image_url,
    }


def test_planned_card_is_none_without_plan():
    with ContentStore(":memory:") as store:
        assert store.planned_card("2026-08-31", "context") is None


def test_planned_card_returns_card_from_saved_plan():
    with ContentStore(":memory:") as store:
        card = store.import_card(make_card())
        store.save_plan("2026-08-31", build_plan("2026-08-31", [card]))

        # verified-карточка с автотегом status.verified попадает в утренний слот fact
        result = store.planned_card("2026-08-31", "fact")
        assert result is not None
        assert result.card_id == "card-1"
        # Пустой слот остаётся пустым
        assert store.planned_card("2026-08-31", "drink") is None


def test_claims_are_restored_with_text_key():
    with ContentStore(":memory:") as store:
        store.import_card(make_card())
        card = store.get_card("card-1")
        assert card.claims
        assert card.claims[0]["text"] == "Стакан стал символом СССР."


def test_card_stores_image_url():
    with ContentStore(":memory:") as store:
        store.import_card(make_card(image_url="https://example.org/stakan.jpg"))
        card = store.get_card("card-1")
        assert card.image_url == "https://example.org/stakan.jpg"


def test_import_adds_status_tag_automatically():
    with ContentStore(":memory:") as store:
        card = store.import_card(make_card(status="unverified", tags=("topic.context",)))
        assert "status.unverified" in card.tags
        assert "status.unverified" in store.get_card("card-1").tags


def test_import_dedupes_matching_status_tag():
    with ContentStore(":memory:") as store:
        card = store.import_card(make_card(tags=("topic.context", "status.verified")))
        assert card.tags.count("status.verified") == 1


def test_import_rejects_conflicting_status_tag():
    with ContentStore(":memory:") as store:
        with pytest.raises(ContentError):
            store.import_card(make_card(status="unverified", tags=("status.verified",)))


def test_verified_requires_trusted_source():
    with ContentStore(":memory:") as store:
        with pytest.raises(ContentError):
            store.import_card(make_card(source_type="popular"))


def test_unverified_allows_untrusted_sources():
    with ContentStore(":memory:") as store:
        card = store.import_card(make_card(status="unverified", source_type="popular"))
        assert card.status == "unverified"


def test_unknown_source_type_rejected():
    with ContentStore(":memory:") as store:
        with pytest.raises(ContentError):
            store.import_card(make_card(source_type="book"))


def test_set_status_checks_trust():
    with ContentStore(":memory:") as store:
        store.import_card(make_card(status="unverified", source_type="popular"))
        with pytest.raises(ContentError):
            store.set_status("card-1", "verified")
        assert store.get_card("card-1").status == "unverified"


def test_set_status_updates_status_tag():
    with ContentStore(":memory:") as store:
        store.import_card(make_card(card_id="card-2", status="unverified"))
        card = store.set_status("card-2", "verified")
        assert "status.verified" in card.tags
        assert "status.unverified" not in card.tags


def test_any_day_cards_eligible_any_date():
    with ContentStore(":memory:") as store:
        store.import_card(make_card(card_id="topic-1", calendar_day="00-00"))
        store.import_card(make_card(card_id="dated-1", calendar_day="08-31"))
        cards = store.list_cards("08-31")
        assert {card.card_id for card in cards} == {"topic-1", "dated-1"}


def test_slot_definitions_reseeded(tmp_path):
    path = str(tmp_path / "content.sqlite3")
    with ContentStore(path) as store:
        # Симулируем устаревшую строку из старой сетки
        store.conn.execute(
            "INSERT OR REPLACE INTO slot_definitions(slot_key,ordinal,local_time,requirements_json,enabled) "
            "VALUES('person_place',2,'12:00','[]',1)"
        )
    with ContentStore(path) as store:
        keys = [row["slot_key"] for row in store.slots()]
        assert keys == ["fact", "drink", "context", "custom_law", "trade", "myth", "analysis"]


def test_claim_slot_retries_after_failure():
    """Упавший слот можно забрать повторно; опубликованный — нельзя."""
    with ContentStore(":memory:") as store:
        card = store.import_card(make_card())
        assert store.claim_slot("2026-08-31", "context", card) is True
        store.mark_failed("2026-08-31", "context", "ошибка")
        assert store.claim_slot("2026-08-31", "context", card) is True
        store.mark_published("2026-08-31", "context", 1, "текст")
        assert store.claim_slot("2026-08-31", "context", card) is False


def test_recent_published_card_ids_returns_newest_first():
    with ContentStore(":memory:") as store:
        for card_id, local_date in (("card-1", "2026-08-30"), ("card-2", "2026-08-31")):
            store.import_card(make_card(card_id))
            card = store.get_card(card_id)
            store.save_plan(local_date, build_plan(local_date, [card]))
            assert store.claim_slot(local_date, "context", card)
            store.mark_published(local_date, "context", 1, "текст")

        assert store.recent_published_card_ids("2026-08-30") == ["card-2", "card-1"]
        assert store.recent_published_card_ids("2026-08-31") == ["card-2"]
