from datetime import datetime, timezone

import pytest


def _freeze_scheduler_time(mocker, utc_time):
    import scheduler

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return utc_time.astimezone(tz) if tz else utc_time.replace(tzinfo=None)

    mocker.patch.object(scheduler, "datetime", FrozenDatetime)
    return scheduler


def test_daily_options_forwards_photos_and_registry(monkeypatch):
    monkeypatch.setenv("VK_TOKEN", "token")
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("WITH_PHOTOS", "yes")
    monkeypatch.setenv("SOURCE_REGISTRY_DB", "/app/state/sources.sqlite3")
    monkeypatch.setenv("SOURCE_REGISTRY_MODE", "registry")

    from scheduler import _daily_options

    options = _daily_options()
    assert options["vk_token"] == "token"
    assert options["vk_group_id"] == 123
    assert options["gemini_api_key"] == "key"
    assert options["max_holidays"] == 3
    assert options["gemini_model"] == "gemini-3.5-flash-lite"
    assert options["with_photos"] is True
    assert options["registry_path"] == "/app/state/sources.sqlite3"
    assert options["registry_mode"] == "registry"
    assert options["daily_posts"] == 7
    assert options["bot_timezone"] == "Etc/GMT-2"
    assert options["slot_times"] == "12:00,14:00,16:00,18:00,20:00,22:00"
    assert options["slot_grace_seconds"] == 300


def test_daily_options_forwards_cloudflare_settings(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret")
    monkeypatch.setenv("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
    monkeypatch.setenv("CLOUDFLARE_IMAGE_TIMEOUT_SECONDS", "90")

    from scheduler import _daily_options

    options = _daily_options()
    assert options["cloudflare_account_id"] == "0123456789abcdef0123456789abcdef"
    assert options["cloudflare_api_token"] == "secret"
    assert options["cloudflare_model"] == "@cf/black-forest-labs/flux-1-schnell"
    assert options["cloudflare_timeout"] == 90


def _prepare_cards_scheduler(monkeypatch, mocker, tmp_path, utc_time):
    monkeypatch.setenv("CONTENT_DB", str(tmp_path / "content.sqlite3"))
    monkeypatch.setenv("BOT_TIMEZONE", "Etc/GMT-2")
    monkeypatch.setenv("SLOT_GRACE_SECONDS", "300")
    monkeypatch.setenv("WITH_PHOTOS", "yes")
    monkeypatch.setenv("ALLOW_CARD_REUSE", "false")
    monkeypatch.setenv("VK_TOKEN", "tok")
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cloudflare-test")
    scheduler = _freeze_scheduler_time(mocker, utc_time)
    mocker.patch("app.main.plan_content_day", return_value=[])
    mocker.patch("scheduler._recent_card_ids", return_value=("card-9",))
    slot_mock = mocker.patch("app.main.run_content_slot", return_value=101)
    return scheduler, slot_mock


def test_run_cards_once_processes_only_latest_due_slot(monkeypatch, mocker, tmp_path):
    utc_time = datetime(2026, 10, 14, 12, 2, tzinfo=timezone.utc)  # 14:02 UTC+2
    scheduler, slot_mock = _prepare_cards_scheduler(monkeypatch, mocker, tmp_path, utc_time)

    scheduler._run_cards_once()

    assert [call.kwargs["slot_key"] for call in slot_mock.call_args_list] == ["drink"]
    assert slot_mock.call_args.kwargs["with_photos"] is True
    assert slot_mock.call_args.kwargs["recent_card_ids"] == ("card-9",)
    assert slot_mock.call_args.kwargs["cloudflare_api_token"] == "cloudflare-test"


def test_run_cards_once_skips_slots_outside_grace_window(monkeypatch, mocker, tmp_path):
    utc_time = datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc)  # 15:00 UTC+2
    scheduler, slot_mock = _prepare_cards_scheduler(monkeypatch, mocker, tmp_path, utc_time)

    scheduler._run_cards_once()

    slot_mock.assert_not_called()


def test_run_cards_once_uses_six_slot_keys_from_definitions(monkeypatch, mocker, tmp_path):
    from app.content_store import SLOT_DEFINITIONS

    assert [key for key, _time, _req, _strict in SLOT_DEFINITIONS] == ["fact", "drink", "context", "custom_law", "myth", "analysis"]
    utc_time = datetime(2026, 10, 14, 14, 1, tzinfo=timezone.utc)  # 16:01 UTC+2
    scheduler, slot_mock = _prepare_cards_scheduler(monkeypatch, mocker, tmp_path, utc_time)

    scheduler._run_cards_once()

    assert [call.kwargs["slot_key"] for call in slot_mock.call_args_list] == ["context"]


def test_legacy_scheduler_calls_run_daily_with_supported_options(monkeypatch, mocker):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import scheduler

    monkeypatch.setenv("VK_TOKEN", "token")
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cloudflare-test")
    monkeypatch.setenv("POST_HOUR", "9")
    monkeypatch.setenv("POST_MINUTE", "0")
    monkeypatch.setenv("CONTENT_MODE", "legacy")
    mocker.patch.object(scheduler, "validate_runtime_config", return_value={})
    mocker.patch.object(scheduler, "_seconds_until", return_value=0)
    mocker.patch.object(scheduler, "local_now", return_value=datetime(2026, 10, 13, 9, 0, tzinfo=ZoneInfo("Europe/Moscow")))
    run_daily = mocker.patch("app.main.run_daily")

    class StopLoop(Exception):
        pass

    sleeps = 0

    def stop_after_run(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise StopLoop

    mocker.patch.object(scheduler.time, "sleep", side_effect=stop_after_run)
    with pytest.raises(StopLoop):
        scheduler.main()

    assert run_daily.call_count == 1
    assert "content_db" not in run_daily.call_args.kwargs
    assert run_daily.call_args.kwargs["gemini_model"] == "gemini-3.5-flash-lite"
    assert run_daily.call_args.kwargs["cloudflare_api_token"] == "cloudflare-test"
