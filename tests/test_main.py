"""Тесты оркестратора main.py: сбор → генерация → публикация."""
import pytest

from app.holidays_parser import Holiday
from app.main import run_daily, today_str
from app.sources import SourceItem


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


class TestTodayStr:
    def test_today_str_format(self):
        """today_str должен вернуть дату в формате ГГГГ-ММ-ДД."""
        s = today_str()
        assert len(s) == 10
        parts = s.split("-")
        assert len(parts) == 3
        assert all(len(p) == 2 or (parts[0] and len(parts[0]) == 4) for p in parts)


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
