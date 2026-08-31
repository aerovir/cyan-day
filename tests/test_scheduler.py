def test_daily_options_forwards_photos_and_registry(monkeypatch):
    monkeypatch.setenv("VK_TOKEN", "token")
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("MISTRAL_API_KEY", "key")
    monkeypatch.setenv("WITH_PHOTOS", "yes")
    monkeypatch.setenv("SOURCE_REGISTRY_DB", "/app/state/sources.sqlite3")
    monkeypatch.setenv("SOURCE_REGISTRY_MODE", "registry")

    from scheduler import _daily_options

    options = _daily_options()
    assert options["vk_token"] == "token"
    assert options["vk_group_id"] == 123
    assert options["mistral_api_key"] == "key"
    assert options["max_holidays"] == 3
    assert options["mistral_model"] == "mistral-small-latest"
    assert options["with_photos"] is True
    assert options["registry_path"] == "/app/state/sources.sqlite3"
    assert options["registry_mode"] == "registry"
    assert options["daily_posts"] == 7
    assert options["bot_timezone"] == "Europe/Moscow"
    assert len(options["slot_times"].split(",")) == 7


def test_run_cards_once_respects_max_catchup_slots(monkeypatch, mocker, tmp_path):
    monkeypatch.setenv("CONTENT_DB", str(tmp_path / "content.sqlite3"))
    monkeypatch.setenv("MAX_CATCHUP_SLOTS", "2")
    # Все семь слотов уже просрочены
    monkeypatch.setenv("SLOT_TIMES", "00:00,00:00,00:00,00:00,00:00,00:00,00:00")
    monkeypatch.setenv("VK_TOKEN", "tok")
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("MISTRAL_API_KEY", "key")

    mocker.patch("app.main.plan_content_day", return_value=[])
    slot_mock = mocker.patch("app.main.run_content_slot", return_value=101)

    from scheduler import _run_cards_once

    _run_cards_once()

    assert slot_mock.call_count == 2


def test_run_cards_once_uses_slot_keys_from_definitions(monkeypatch, mocker, tmp_path):
    monkeypatch.setenv("CONTENT_DB", str(tmp_path / "content.sqlite3"))
    monkeypatch.setenv("MAX_CATCHUP_SLOTS", "7")
    monkeypatch.setenv("SLOT_TIMES", "00:00,00:00,00:00,00:00,00:00,00:00,00:00")
    monkeypatch.setenv("VK_TOKEN", "tok")
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("MISTRAL_API_KEY", "key")

    mocker.patch("app.main.plan_content_day", return_value=[])
    slot_mock = mocker.patch("app.main.run_content_slot", return_value=None)

    from app.content_store import SLOT_DEFINITIONS
    from scheduler import _run_cards_once

    _run_cards_once()

    keys = [call.kwargs["slot_key"] for call in slot_mock.call_args_list]
    assert keys == [key for key, _time, _req, _strict in SLOT_DEFINITIONS]


def test_run_cards_once_forwards_photos_and_recent_cards(monkeypatch, mocker, tmp_path):
    monkeypatch.setenv("CONTENT_DB", str(tmp_path / "content.sqlite3"))
    monkeypatch.setenv("MAX_CATCHUP_SLOTS", "7")
    monkeypatch.setenv("SLOT_TIMES", "00:00,00:00,00:00,00:00,00:00,00:00,00:00")
    monkeypatch.setenv("WITH_PHOTOS", "yes")
    monkeypatch.setenv("ALLOW_CARD_REUSE", "false")
    monkeypatch.setenv("VK_TOKEN", "tok")
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("MISTRAL_API_KEY", "key")

    mocker.patch("app.main.plan_content_day", return_value=[])
    slot_mock = mocker.patch("app.main.run_content_slot", return_value=None)
    mocker.patch("scheduler._recent_card_ids", return_value=("card-9",))

    from scheduler import _run_cards_once

    _run_cards_once()

    assert slot_mock.call_count == 7
    for call in slot_mock.call_args_list:
        assert call.kwargs["with_photos"] is True
        assert call.kwargs["recent_card_ids"] == ("card-9",)
