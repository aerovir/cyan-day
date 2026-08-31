"""Тесты планировщика сетки публикаций: факты утром, мифы вечером."""

from app.content_planner import build_plan
from app.content_store import ContentStore, SLOT_DEFINITIONS


def make_card(card_id="card-1", status="verified", tags=("topic.context",), priority=50):
    return {
        "card_id": card_id,
        "calendar_day": "00-00",
        "title": f"Карточка {card_id}",
        "summary": "Описание.",
        "status": status,
        "tags": list(tags),
        "claims": [{"claim_id": f"{card_id}-c1", "text": "Факт.", "provenance_id": "src-1"}],
        "provenance": [{"provenance_id": "src-1", "source_type": "academic", "title": "Статья"}],
        "priority": priority,
    }


def _plan_for(cards):
    with ContentStore(":memory:") as store:
        imported = [store.import_card(card) for card in cards]
        return build_plan("2026-08-31", imported)


def _slot(plan, key):
    return next(slot for slot in plan if slot.slot_key == key)


def test_slot_definitions_have_seven_slots_in_new_order():
    keys = [key for key, _time, _req, _strict in SLOT_DEFINITIONS]
    assert keys == ["fact", "drink", "context", "custom_law", "trade", "myth", "analysis"]


def test_full_grid_fills_status_slots():
    """Полный день: fact берёт verified, myth — unverified, analysis — disputed."""
    plan = _plan_for([
        make_card("fact-1", "verified", ("topic.context",), priority=90),
        make_card("drink-1", "verified", ("drink.beer",)),
        make_card("context-1", "verified", ("topic.context",)),
        make_card("law-1", "verified", ("topic.customs",)),
        make_card("trade-1", "verified", ("topic.trade",)),
        make_card("myth-1", "unverified", ()),
        make_card("analysis-1", "disputed", ()),
    ])
    assert _slot(plan, "fact").card_id == "fact-1"
    assert _slot(plan, "drink").card_id == "drink-1"
    assert _slot(plan, "context").card_id == "context-1"
    assert _slot(plan, "myth").card_id == "myth-1"
    assert _slot(plan, "analysis").card_id == "analysis-1"


def test_fact_slot_skips_without_verified_card():
    plan = _plan_for([make_card("unverified-1", "unverified", ())])
    fact = _slot(plan, "fact")
    assert fact.card_id is None
    assert fact.state == "skipped"


def test_myth_slot_skips_when_only_verified_available():
    plan = _plan_for([make_card("verified-1", "verified", ("topic.context",))])
    assert _slot(plan, "myth").card_id is None


def test_person_place_cards_match_context_slot():
    plan = _plan_for([
        make_card("fact-1", "verified", ("topic.context",), priority=90),
        make_card("drink-1", "verified", ("drink.beer",)),
        make_card("people-1", "verified", ("topic.people",)),
        make_card("law-1", "verified", ("topic.customs",)),
        make_card("trade-1", "verified", ("topic.trade",)),
    ])
    assert _slot(plan, "context").card_id == "people-1"


def test_empty_card_pool_skips_all_slots():
    plan = _plan_for([])
    assert [slot.slot_key for slot in plan] == [key for key, _t, _r, _s in SLOT_DEFINITIONS]
    assert all(slot.card_id is None for slot in plan)
