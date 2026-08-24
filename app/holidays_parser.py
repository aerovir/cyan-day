"""Парсер праздников с calend.ru.

Извлекает список праздников на конкретную дату со страницы
https://calend.ru/day/ГГГГ-ММ-ДД/ и полные описания со страниц праздников.

Модуль не зависит от ИИ и VK — только от сети и HTML-структуры calend.ru.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

import requests

BASE_URL = "https://calend.ru"
DAY_URL = "https://calend.ru/day/{date}/"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# Хостим картинки с www (calend.ru/img/... отдаёт редирект)
IMAGE_HOST = "https://www.calend.ru"

# Разделители описания — чтобы не смешивать абзацы
_WS_RE = re.compile(r"\s+")

# URL настоящего праздника вида /holidays/0/0/ID/
_HOLIDAY_URL_RE = re.compile(r"/holidays/0/\d+/")


@dataclass
class Holiday:
    """Один праздник.

    Attributes:
        title: Название праздника.
        url: Абсолютная ссылка на страницу праздника.
        description: Краткое описание (со страницы дня).
        image_url: Абсолютная ссылка на иллюстрацию (может быть пустой).
        category: Категория праздника (например, «Международные праздники»).
    """

    title: str
    url: str
    description: str = ""
    image_url: str = ""
    category: str = ""


class HolidaysParser:
    """Парсер праздников calend.ru."""

    def fetch_day_page(self, date: str) -> str:
        """Загрузить HTML страницы дня.

        Args:
            date: Дата в формате ГГГГ-ММ-ДД.

        Returns:
            HTML страницы дня как строку.

        Raises:
            requests.RequestException: При сетевой ошибке или HTTP-ошибке.
        """
        resp = requests.get(
            DAY_URL.format(date=date),
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text

    def parse_day_page(self, html: str) -> List[Holiday]:
        """Извлечь список праздников из HTML страницы дня.

        Обрабатывает карточки ``<li class="three-three">``. Отфильтровывает
        «шум» (народный календарь, ссылки без /holidays/0/0/).

        Args:
            html: HTML страницы дня.

        Returns:
            Список праздников дня.
        """
        holidays: List[Holiday] = []
        for block in re.findall(r'<li class="three-three">(.*?)</li>', html, re.S):
            holiday = self._parse_holiday_card(block)
            if holiday is None:
                continue
            holidays.append(holiday)
        return holidays

    def _parse_holiday_card(self, block: str) -> Holiday | None:
        """Разобрать одну карточку праздника из HTML страницы дня.

        Returns:
            Holiday или None, если карточка не является праздником
            (например, народный календарь).
        """
        # URL: первая ссылка в блоке
        url_match = re.search(r'<a[^>]+href="([^"]+)"', block)
        if not url_match:
            return None
        url = url_match.group(1)
        if not _HOLIDAY_URL_RE.search(url):
            return None  # народный календарь и прочий шум
        if not url.startswith("http"):
            url = BASE_URL + url

        # Название: содержимое <span class="title">…</span>
        title = ""
        title_match = re.search(r'<span class="title">(.*?)</span>', block, re.S)
        if title_match:
            title = self._clean_html(title_match.group(1))

        # Категория: текст в <div class="link">…</div>
        category = ""
        cat_match = re.search(r'<div class="link">(.*?)</div>', block, re.S)
        if cat_match:
            category = self._clean_html(cat_match.group(1))

        # Описание: <p class="descr…">…</p>
        description = ""
        desc_match = re.search(
            r'<p class="descr[^"]*"[^>]*>(.*?)</p>', block, re.S
        )
        if desc_match:
            description = self._clean_html(desc_match.group(1))

        # Картинка: первый <img src="…">
        image_url = ""
        img_match = re.search(r'<img[^>]+src="([^"]+)"', block)
        if img_match:
            image_url = img_match.group(1)
            if image_url.startswith("/"):
                image_url = IMAGE_HOST + image_url

        return Holiday(
            title=title,
            url=url,
            description=description,
            image_url=image_url,
            category=category,
        )

    def parse_holiday_page(self, html: str) -> str:
        """Извлечь полное описание праздника из HTML его страницы.

        Берёт все абзацы из контейнера ``<div class="maintext"…>``.
        Исключает навигационный шум (лунный цикл, именины и пр.), который
        находится вне этого контейнера.

        Args:
            html: HTML страницы праздника.

        Returns:
            Полное описание как одна строка.
        """
        container = re.search(
            r'<div class="maintext"[^>]*>(.*?)</div>', html, re.S
        )
        if not container:
            return ""

        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", container.group(1), re.S)
        cleaned = [
            self._clean_html(p) for p in paragraphs if self._clean_html(p)
        ]
        return "\n\n".join(cleaned)

    @staticmethod
    def _clean_html(fragment: str) -> str:
        """Убрать HTML-теги и лишние пробелы из фрагмента."""
        text = re.sub(r"<[^>]+>", " ", fragment)
        # Нормализуем неразрывные пробелы
        text = text.replace("\xa0", " ").replace("&nbsp;", " ")
        return _WS_RE.sub(" ", text).strip()
