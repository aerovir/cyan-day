"""Тесты оркестратора main.py: сбор → генерация → публикация."""
import pytest

from app.ai_generator import GeneratedContent
from app.content_store import ContentStore
from app.holidays_parser import Holiday
from app.main import run_daily, today_str
from app.sources import SourceItem


def test_today_str_uses_requested_timezone(mocker):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    mocker.patch("app.main.local_now", return_value=datetime(2026, 9, 1, 1, tzinfo=ZoneInfo("Europe/Moscow")))
    assert today_str("Europe/Moscow") == "2026-09-01"


def test_validate_runtime_config_rejects_club_group_id():
    from app.main import validate_runtime_config
    with pytest.raises(ValueError, match="without the 'club' prefix"):
        validate_runtime_config({"VK_TOKEN": "t", "VK_GROUP_ID": "club123", "MISTRAL_API_KEY": "k"})


def test_validate_runtime_config_requires_seven_slot_times():
    from app.main import validate_runtime_config
    with pytest.raises(ValueError, match="exactly seven"):
        validate_runtime_config({"VK_TOKEN": "t", "VK_GROUP_ID": "123", "MISTRAL_API_KEY": "k", "SLOT_TIMES": "09:00"})


def test_validate_runtime_config_accepts_cards_config():
    from app.main import validate_runtime_config
    config = validate_runtime_config({"VK_TOKEN": "t", "VK_GROUP_ID": "123", "MISTRAL_API_KEY": "k", "CONTENT_MODE": "cards"})
    assert config["vk_group_id"] == 123
    assert config["content_mode"] == "cards"


class TestTodayStr:
    def test_today_str_format(self):
        """today_str должен вернуть дату в формате ГГГГ-ММ-ДД."""
        s = today_str()
        assert len(s) == 10
        parts = s.split("-")
        assert len(parts) == 3
        assert all(len(p) == 2 or (parts[0] and len(parts[0]) == 4) for p in parts)


def make_item(title="День странной музыки", source_id="test", external_id="1", url=""):
    return SourceItem(
        source_id=source_id,
        external_id=external_id,
        event_date=today_str(),
        title=title,
        description="Описание",
        url=url,
    )


def make_holiday(title="День странной музыки"):
    return Holiday(
        title=title,
        url="https://www.calend.ru/holidays/0/0/3731/",
        description="Праздник, описание",
        image_url="https://www.calend.ru/img/content/i3/3731.jpg",
        category="Международные праздники",
    )


class TestRunDaily:
    def test_run_daily_posts_each_holiday(
        self, mocker, monkeypatch
    ):
        """run_daily должен опубликовать пост по каждому празднику."""
        holidays = [make_holiday(), make_holiday("Другой праздник")]

        # Мокаем парсер
        parser_mock = mocker.Mock()
        parser_mock.fetch_day_page.return_value = "<html>"
        parser_mock.parse_day_page.return_value = holidays

        # Мокаем генератор
        ai_mock = mocker.Mock()
        ai_mock.generate_for_holiday.return_value = "Ироничный текст про праздник"

        # Мокаем публикатор
        pub_mock = mocker.Mock()
        pub_mock.post.return_value = 1

        mocker.patch("app.main.HolidaysParser", return_value=parser_mock)
        mocker.patch("app.main.AIGenerator", return_value=ai_mock)
        mocker.patch("app.main.VKPublisher", return_value=pub_mock)

        run_daily(
            vk_token="tok",
            vk_group_id=123,
            mistral_api_key="key",
        )

        assert pub_mock.post.call_count == 2
        # Проверяем, что сообщения передаются
        messages = [c.kwargs.get("message") for c in pub_mock.post.call_args_list]
        assert all(m and "Ироничный текст" in m for m in messages)

    def test_run_daily_no_holidays_does_nothing(self, mocker):
        """Если праздников нет, не должно быть публикаций."""
        parser_mock = mocker.Mock()
        parser_mock.fetch_day_page.return_value = "<html>"
        parser_mock.parse_day_page.return_value = []

        ai_mock = mocker.Mock()
        pub_mock = mocker.Mock()

        mocker.patch("app.main.HolidaysParser", return_value=parser_mock)
        mocker.patch("app.main.AIGenerator", return_value=ai_mock)
        mocker.patch("app.main.VKPublisher", return_value=pub_mock)

        run_daily(
            vk_token="tok",
            vk_group_id=123,
            mistral_api_key="key",
        )

        assert pub_mock.post.call_count == 0

    def test_run_daily_limits_holidays(self, mocker):
        """run_daily должен ограничивать количество праздников (MAX_HOLIDAYS)."""
        many = [make_holiday(f"Праздник {i}") for i in range(10)]
        parser_mock = mocker.Mock()
        parser_mock.fetch_day_page.return_value = "<html>"
        parser_mock.parse_day_page.return_value = many

        ai_mock = mocker.Mock()
        ai_mock.generate_for_holiday.return_value = "текст"
        pub_mock = mocker.Mock()

        mocker.patch("app.main.HolidaysParser", return_value=parser_mock)
        mocker.patch("app.main.AIGenerator", return_value=ai_mock)
        mocker.patch("app.main.VKPublisher", return_value=pub_mock)

        run_daily(
            vk_token="tok",
            vk_group_id=123,
            mistral_api_key="key",
            max_holidays=3,
        )

        assert pub_mock.post.call_count == 3

    def test_registry_collects_sources_and_deduplicates_before_limit(self, mocker):
        source_one = mocker.Mock()
        source_two = mocker.Mock()
        duplicate = make_item(source_id="two", external_id="other", url="https://calend.ru/holidays/1/")
        source_one.fetch.return_value = [make_item(title="First", url="https://calend.ru/holidays/1/")]
        source_two.fetch.return_value = [duplicate, make_item(title="Second", external_id="2")]
        record_one = mocker.Mock(name="one")
        record_one.name = "one"
        record_two = mocker.Mock(name="two")
        record_two.name = "two"
        registry = mocker.Mock()
        registry.enabled_adapters.return_value = [(record_one, source_one), (record_two, source_two)]
        registry.__enter__ = mocker.Mock(return_value=registry)
        registry.__exit__ = mocker.Mock(return_value=None)
        mocker.patch("app.main.SourceRegistry", return_value=registry)
        mocker.patch("app.main.os.path.exists", return_value=True)
        ai_mock = mocker.Mock()
        ai_mock.generate_for_holiday.return_value = "текст"
        pub_mock = mocker.Mock()
        pub_mock.post.side_effect = [1, 2]
        mocker.patch("app.main.AIGenerator", return_value=ai_mock)
        mocker.patch("app.main.VKPublisher", return_value=pub_mock)

        run_daily("tok", 123, "key", max_holidays=2, registry_path="db", registry_mode="registry")

        assert pub_mock.post.call_count == 2
        assert [call.kwargs["message"] for call in pub_mock.post.call_args_list] == ["текст", "текст"]
        assert source_one.fetch.called and source_two.fetch.called

    def test_registry_source_error_does_not_stop_other_sources(self, mocker):
        broken = mocker.Mock()
        broken.fetch.side_effect = RuntimeError("down")
        working = mocker.Mock()
        working.fetch.return_value = [make_item()]
        records = []
        for name in ("broken", "working"):
            record = mocker.Mock()
            record.name = name
            records.append(record)
        registry = mocker.Mock()
        registry.enabled_adapters.return_value = list(zip(records, [broken, working]))
        registry.__enter__ = mocker.Mock(return_value=registry)
        registry.__exit__ = mocker.Mock(return_value=None)
        mocker.patch("app.main.SourceRegistry", return_value=registry)
        mocker.patch("app.main.os.path.exists", return_value=True)
        ai_mock = mocker.Mock()
        ai_mock.generate_for_holiday.return_value = "текст"
        pub_mock = mocker.Mock()
        pub_mock.post.return_value = 1
        mocker.patch("app.main.AIGenerator", return_value=ai_mock)
        mocker.patch("app.main.VKPublisher", return_value=pub_mock)

        run_daily("tok", 123, "key", registry_path="db", registry_mode="registry")
        assert pub_mock.post.call_count == 1

    def test_legacy_fallback_when_registry_missing(self, mocker):
        parser = mocker.Mock()
        parser.fetch_day_page.return_value = "html"
        parser.parse_day_page.return_value = [make_holiday()]
        mocker.patch("app.main.HolidaysParser", return_value=parser)
        mocker.patch("app.main.os.path.exists", return_value=False)
        ai_mock = mocker.Mock()
        ai_mock.generate_for_holiday.return_value = "текст"
        pub_mock = mocker.Mock()
        pub_mock.post.return_value = 1
        mocker.patch("app.main.AIGenerator", return_value=ai_mock)
        mocker.patch("app.main.VKPublisher", return_value=pub_mock)

        run_daily("tok", 123, "key", registry_path="missing.sqlite")
        assert pub_mock.post.call_count == 1
        parser.fetch_day_page.assert_called_once()

    def test_registry_mode_with_no_sources_publishes_nothing(self, mocker):
        registry = mocker.Mock()
        registry.enabled_adapters.return_value = []
        registry.__enter__ = mocker.Mock(return_value=registry)
        registry.__exit__ = mocker.Mock(return_value=None)
        mocker.patch("app.main.SourceRegistry", return_value=registry)
        mocker.patch("app.main.os.path.exists", return_value=True)
        pub_mock = mocker.Mock()
        mocker.patch("app.main.AIGenerator")
        mocker.patch("app.main.VKPublisher", return_value=pub_mock)

        run_daily("tok", 123, "key", registry_path="db", registry_mode="registry")
        pub_mock.post.assert_not_called()


def test_env_bool_accepts_common_values():
    from app.main import env_bool

    assert all(env_bool(value) for value in ("1", "true", "YES", "on"))
    assert not env_bool("false")
    assert env_bool(None, default=True)


def make_content_card(card_id="card-1", status="verified", image_url=""):
    return {
        "card_id": card_id,
        "calendar_day": "08-31",
        "title": "День гранёного стакана",
        "summary": "Стакан как символ эпохи.",
        "status": status,
        "tags": ["topic.context"],
        "claims": [{"claim_id": "c1", "text": "Стакан стал символом СССР.", "provenance_id": "src-1"}],
        "provenance": [{"provenance_id": "src-1", "source_type": "academic", "title": "Энциклопедия стаканов"}],
        "image_url": image_url,
    }


class TestRunContentSlot:
    def test_run_content_slot_publishes_verified_card_once(self, mocker, tmp_path):
        from app.main import run_content_slot

        content_db = str(tmp_path / "content.sqlite3")
        with ContentStore(content_db) as store:
            store.import_card(make_content_card())
        pub_mock = mocker.Mock()
        pub_mock.post.return_value = 123
        mocker.patch("app.main.VKPublisher", return_value=pub_mock)

        post_id = run_content_slot("tok", 123, "key", "2026-08-31", "fact", content_db=content_db)

        assert post_id == 123
        message = pub_mock.post.call_args.kwargs["message"]
        assert message.startswith("ФАКТ:") and "Стакан стал символом СССР." in message
        # Повторный запуск того же слота не публикует повторно
        assert run_content_slot("tok", 123, "key", "2026-08-31", "fact", content_db=content_db) is None
        assert pub_mock.post.call_count == 1

    def test_run_content_slot_generates_labelled_copy_for_unverified(self, mocker, tmp_path):
        from app.main import run_content_slot

        content_db = str(tmp_path / "content.sqlite3")
        with ContentStore(content_db) as store:
            store.import_card(make_content_card(status="unverified"))
        gen_mock = mocker.Mock()
        gen_mock.generate_for_content.return_value = GeneratedContent(
            label="МИФ", body="ироничный разбор мифа", claims_used=("c1",), unsupported_claims=()
        )
        mocker.patch("app.main.AIGenerator", return_value=gen_mock)
        pub_mock = mocker.Mock()
        pub_mock.post.return_value = 5
        mocker.patch("app.main.VKPublisher", return_value=pub_mock)

        # unverified-карточка не попадает в strict fact-слот; первый дневной
        # слот с фолбэком (drink) заберёт её
        post_id = run_content_slot("tok", 123, "key", "2026-08-31", "drink", content_db=content_db)

        assert post_id == 5
        assert pub_mock.post.call_args.kwargs["message"] == "МИФ: ироничный разбор мифа"

    def test_run_content_slot_attaches_image_when_enabled(self, mocker, tmp_path):
        from app.main import run_content_slot

        content_db = str(tmp_path / "content.sqlite3")
        with ContentStore(content_db) as store:
            store.import_card(make_content_card(image_url="https://example.org/stakan.jpg"))
        photo_file = tmp_path / "photo.jpg"
        photo_file.write_bytes(b"jpeg-bytes")
        download_mock = mocker.patch("app.main.download_image", return_value=str(photo_file))
        pub_mock = mocker.Mock()
        pub_mock.post.return_value = 7
        mocker.patch("app.main.VKPublisher", return_value=pub_mock)

        post_id = run_content_slot(
            "tok", 123, "key", "2026-08-31", "fact",
            content_db=content_db, with_photos=True,
        )

        assert post_id == 7
        assert pub_mock.post.call_args.kwargs["photo_path"] == str(photo_file)
        download_mock.assert_called_once_with("https://example.org/stakan.jpg", allowed_hosts=())

    def test_run_content_slot_forwards_recent_cards_to_planner(self, mocker, tmp_path):
        from app.main import run_content_slot

        content_db = str(tmp_path / "content.sqlite3")
        planner_mock = mocker.patch("app.main.build_plan", return_value=[])

        result = run_content_slot(
            "tok", 123, "key", "2026-08-31", "context",
            content_db=content_db, recent_card_ids=("card-9",),
        )

        assert result is None
        planner_mock.assert_called_once()
        assert planner_mock.call_args.args[2] == ("card-9",)


def test_plan_content_day_forwards_recent_cards_to_planner(mocker, tmp_path):
    from app.main import plan_content_day

    content_db = str(tmp_path / "content.sqlite3")
    planner_mock = mocker.patch("app.main.build_plan", return_value=[])

    plan_content_day("2026-08-31", content_db=content_db, recent_card_ids=("card-9",))

    planner_mock.assert_called_once()
    assert planner_mock.call_args.args[2] == ("card-9",)


def test_plan_content_day_regenerates_stale_plan_version(tmp_path):
    from app.content_planner import build_plan
    from app.main import plan_content_day

    content_db = str(tmp_path / "content.sqlite3")
    with ContentStore(content_db) as store:
        store.import_card(make_content_card())
    with ContentStore(content_db) as store:
        store.save_plan("2026-08-31", build_plan("2026-08-31", store.list_cards()), plan_version=1)

    result = plan_content_day("2026-08-31", content_db=content_db)

    assert result[0]["plan_version"] == 2


def test_fact_slot_publishes_verified_card(mocker, tmp_path):
    from app.main import run_content_slot

    content_db = str(tmp_path / "content.sqlite3")
    with ContentStore(content_db) as store:
        store.import_card(make_content_card())
    pub_mock = mocker.Mock()
    pub_mock.post.return_value = 42
    mocker.patch("app.main.VKPublisher", return_value=pub_mock)

    post_id = run_content_slot("tok", 123, "key", "2026-08-31", "fact", content_db=content_db)

    assert post_id == 42
    assert pub_mock.post.call_args.kwargs["message"].startswith("ФАКТ:")
