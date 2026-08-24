"""Тесты оркестратора main.py: сбор → генерация → публикация."""
import pytest

from app.holidays_parser import Holiday
from app.main import run_daily, today_str


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
