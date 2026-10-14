"""Тесты планировщика сетки публикаций: факты утром, мифы вечером."""

from app.content_planner import build_plan, due_at, parse_slot_times
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


def test_slot_definitions_have_six_slots_in_order():
    keys = [key for key, _time, _req, _strict in SLOT_DEFINITIONS]
    times = [local_time for _key, local_time, _req, _strict in SLOT_DEFINITIONS]
    assert keys == ["fact", "drink", "context", "custom_law", "myth", "analysis"]
    assert times == ["12:00", "14:00", "16:00", "18:00", "20:00", "22:00"]


def test_slot_times_validate_six_ordered_unique_values():
    assert parse_slot_times("12:00,14:00,16:00,18:00,20:00,22:00") == tuple(
        local_time for _key, local_time, _req, _strict in SLOT_DEFINITIONS
    )
    for invalid in (
        "12:00,14:00,16:00,18:00,20:00",
        "12:00,14:00,16:00,18:00,20:00,20:00",
        "14:00,12:00,16:00,18:00,20:00,22:00",
    ):
        try:
            parse_slot_times(invalid)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid SLOT_TIMES: {invalid}")


def test_due_at_uses_fixed_utc_plus_two():
    assert due_at("2026-10-14", "12:00").isoformat() == "2026-10-14T10:00:00+00:00"


def test_full_grid_fills_status_slots():
    """Полный день: fact берёт verified, myth — unverified, analysis — disputed."""
    plan = _plan_for([
        make_card("fact-1", "verified", ("topic.context",), priority=90),
        make_card("drink-1", "verified", ("drink.cocktail",)),
        make_card("context-1", "verified", ("topic.context",)),
        make_card("law-1", "verified", ("topic.customs",)),
        make_card("trade-1", "verified", ("topic.trade",)),
        make_card("myth-1", "unverified", ()),
        make_card("analysis-1", "disputed", ()),
    ])
    assert _slot(plan, "fact").card_id == "fact-1"
    assert _slot(plan, "drink").card_id == "drink-1"
    assert _slot(plan, "context").card_id in {"context-1", "trade-1"}
    assert _slot(plan, "myth").card_id == "myth-1"
    assert _slot(plan, "analysis").card_id == "analysis-1"


def test_fact_slot_skips_without_verified_card():
    plan = _plan_for([make_card("unverified-1", "unverified", ())])
    fact = _slot(plan, "fact")
    assert fact.card_id is None
    assert fact.state == "skipped"


def test_non_strict_slots_fall_back_to_available_card_when_unused_cards_remain():
    plan = _plan_for([make_card(f"verified-{index}", "verified", ("topic.context",)) for index in range(1, 7)])
    assert _slot(plan, "myth").card_id is not None


def test_drink_slot_can_be_a_dynamic_recipe_action():
    card = ContentStore(":memory:")
    try:
        imported = [card.import_card(make_card("fact-1"))]
        plan = build_plan("2026-08-31", imported, recipe_type="joke")
    finally:
        card.close()

    drink = _slot(plan, "drink")
    assert drink.content_type == "recipe"
    assert drink.recipe_type == "joke"
    assert drink.card_id is None


def test_person_place_cards_match_context_slot():
    plan = _plan_for([
        make_card("fact-1", "verified", ("topic.context",), priority=90),
        make_card("drink-1", "verified", ("drink.cocktail",)),
        make_card("people-1", "verified", ("topic.people",)),
        make_card("law-1", "verified", ("topic.customs",)),
        make_card("trade-1", "verified", ("topic.trade",)),
    ])
    assert _slot(plan, "context").card_id in {"people-1", "trade-1"}


def test_empty_card_pool_skips_all_slots():
    plan = _plan_for([])
    assert [slot.slot_key for slot in plan] == [key for key, _t, _r, _s in SLOT_DEFINITIONS]
    assert all(slot.card_id is None for slot in plan)
