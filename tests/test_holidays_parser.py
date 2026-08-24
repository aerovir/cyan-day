"""Тесты для парсера праздников calend.ru.

Покрывают: извлечение праздников со страницы дня, полных описаний,
категорий, картинок и работу с HTTP.
"""
import pytest

from app.holidays_parser import Holiday, HolidaysParser


# --- Фикстуры ---------------------------------------------------------------

@pytest.fixture
def day_html():
    """Реальный HTML страницы дня calend.ru (зафиксирован)."""
    with open("tests/fixtures_day.html", encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def holiday_html():
    """Реальный HTML страницы праздника (с обёрткой maintext)."""
    return """<html>
    <head>
      <meta name="description" content="24 августа отмечается Международный день странной музыки."/>
    </head>
    <body>
    <h1>День странной музыки</h1>
    <p>Сегодня до 18:45 двенадцатый день лунного цикла.</p>
    <div class="maintext" itemprop="articleBody">
      <p>24 августа отмечается Международный день странной музыки.</p>
      <p>Произведения этого композитора представляют собой синтез различных
      жанров — от попсы до пост-панка. Он основал свой лейбл Strange Music.</p>
      <p>Главная цель Дня — познакомить слушателей с незнакомой музыкой.</p>
      <p>Сам праздник можно отметить, слушая непривычную для себя музыку.</p>
    </div>
    <p>Именины Памфил, Илья, Яков.</p>
    </body></html>"""


# --- Тесты модели Holiday ---------------------------------------------------

def test_holiday_model_fields():
    """Holiday должен хранить title, url, description, image, category."""
    h = Holiday(
        title="День странной музыки",
        url="https://www.calend.ru/holidays/0/0/3731/",
        description="Описание",
        image_url="https://www.calend.ru/img/content/i3/3731.jpg",
        category="Международные праздники",
    )
    assert h.title == "День странной музыки"
    assert h.url.endswith("/3731/")
    assert h.description == "Описание"
    assert h.image_url.startswith("https://")
    assert h.category == "Международные праздники"


def test_holiday_defaults():
    """Поля image_url и category должны быть пустыми по умолчанию."""
    h = Holiday(title="X", url="http://x")
    assert h.image_url == ""
    assert h.category == ""


# --- Тесты парсера страницы дня ---------------------------------------------

def test_parse_day_page_extracts_holidays(day_html):
    """Парсер должен извлечь все праздники со страницы дня."""
    parser = HolidaysParser()
    holidays = parser.parse_day_page(day_html)

    assert isinstance(holidays, list)
    assert len(holidays) >= 3
    # Первый праздник на 24 августа 2026 — День странной музыки
    assert any("странной музыки" in h.title.lower() for h in holidays)


def test_parse_day_page_holiday_fields(day_html):
    """Каждый праздник должен иметь непустые title, url, description."""
    parser = HolidaysParser()
    holidays = parser.parse_day_page(day_html)

    for h in holidays:
        assert h.title.strip(), "title не должен быть пустым"
        assert h.url.startswith("http"), f"url должен быть абсолютным: {h.url}"
        assert h.description.strip(), "description не должен быть пустым"


def test_parse_day_page_skips_noise(day_html):
    """Парсер не должен включать «праздники» без ссылок (виджет сегодня/завтра)."""
    parser = HolidaysParser()
    holidays = parser.parse_day_page(day_html)

    for h in holidays:
        # Реальные праздники всегда имеют URL вида /holidays/0/0/ID/
        assert "/holidays/" in h.url, f"нет ссылки на праздник: {h.url}"


def test_parse_day_page_image_url(day_html):
    """Картинка должна быть абсолютной (https://)."""
    parser = HolidaysParser()
    holidays = parser.parse_day_page(day_html)

    for h in holidays:
        if h.image_url:
            assert h.image_url.startswith("https://"), h.image_url


# --- Тесты парсера страницы праздника ---------------------------------------

def test_parse_holiday_page_full_description(holiday_html):
    """Полное описание должно объединять все абзацы со страницы праздника."""
    parser = HolidaysParser()
    desc = parser.parse_holiday_page(holiday_html)

    assert len(desc) > 200, "Полное описание должно быть больше 200 символов"
    assert "странной музыки" in desc
    assert "пост-панка" in desc  # из второго абзаца
    assert "незнакомой музыкой" in desc  # из третьего абзаца


def test_parse_holiday_page_removes_nav_noise(holiday_html):
    """Парсер не должен включать «Сегодня/Завтра лунный цикл» и «Именины»."""
    parser = HolidaysParser()
    desc = parser.parse_holiday_page(holiday_html)

    assert "лунного цикла" not in desc
    assert "Именины" not in desc


# --- Тесты HTTP-функций ------------------------------------------------------

def test_fetch_day_page_uses_correct_url(requests_mock):
    """fetch_day_page должен обращаться к calend.ru/day/ГГГГ-ММ-ДД/."""
    mock_url = "https://calend.ru/day/2026-08-24/"
    requests_mock.get(mock_url, body="<html>ok</html>")

    parser = HolidaysParser()
    result = parser.fetch_day_page("2026-08-24")

    assert result == "<html>ok</html>"
    # Проверяем, что запрос был именно на ожидаемый URL
    assert requests_mock.calls[0].request.url == mock_url


def test_fetch_day_page_handles_http_error(requests_mock):
    """При ошибке HTTP должен подниматься исключительный случай."""
    import requests
    requests_mock.get("https://calend.ru/day/2026-08-24/", status=500)

    parser = HolidaysParser()
    with pytest.raises(requests.RequestException):
        parser.fetch_day_page("2026-08-24")
