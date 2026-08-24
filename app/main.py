"""Оркестратор бота: собрать праздники → сгенерировать посты → опубликовать.

Ежедневный запуск:
    python main.py
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from datetime import date
from typing import List, Optional

import requests
from dotenv import load_dotenv

from .ai_generator import AIGenerator, AIError
from .holidays_parser import HolidaysParser, Holiday
from .vk_publisher import VKError, VKPublisher

logger = logging.getLogger(__name__)


def today_str() -> str:
    """Вернуть сегодняшнюю дату в формате ГГГГ-ММ-ДД."""
    return date.today().strftime("%Y-%m-%d")


def setup_logging(level: str = "INFO") -> None:
    """Настроить логирование в stdout."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def download_image(url: str, timeout: int = 30) -> Optional[str]:
    """Скачать картинку по URL во временный файл.

    Args:
        url: Абсолютная ссылка на картинку.
        timeout: Таймаут запроса.

    Returns:
        Путь к временному файлу, или None при ошибке.
    """
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        # Определяем расширение по content-type
        ctype = resp.headers.get("Content-Type", "")
        ext = ".jpg"
        if "png" in ctype:
            ext = ".png"
        fd, path = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as f:
            f.write(resp.content)
        return path
    except (requests.RequestException, OSError) as exc:
        logger.warning("Не удалось скачать картинку %s: %s", url, exc)
        return None


def run_daily(
    vk_token: str,
    vk_group_id: int,
    mistral_api_key: str,
    max_holidays: int = 3,
    mistral_model: str = "mistral-small-latest",
) -> List[int]:
    """Основной цикл: собрать праздники, сгенерировать и опубликовать посты.

    Args:
        vk_token: Токен VK-сообщества.
        vk_group_id: Положительный ID группы VK.
        mistral_api_key: API-ключ Mistral.
        max_holidays: Максимум праздников для публикации.
        mistral_model: Модель Mistral для генерации.

    Returns:
        Список ID опубликованных постов.

    Raises:
        AIError: При ошибке генерации текста.
        VKError: При ошибке публикации.
    """
    parser = HolidaysParser()
    generator = AIGenerator(api_key=mistral_api_key, model=mistral_model)
    publisher = VKPublisher(token=vk_token, group_id=vk_group_id)

    today = today_str()
    logger.info("Собираю праздники на %s", today)
    html = parser.fetch_day_page(today)
    holidays = parser.parse_day_page(html)

    if not holidays:
        logger.warning("Праздников на %s не найдено", today)
        return []

    # Ограничиваем количество постов
    selected = holidays[:max_holidays]
    logger.info("Найдено праздников: %d, публикуем: %d", len(holidays), len(selected))

    post_ids: List[int] = []
    for holiday in selected:
        photo_path = None
        try:
            logger.info("Обрабатываю праздник: %s", holiday.title)
            text = generator.generate_for_holiday(holiday)
            # Скачиваем картинку, если есть
            if holiday.image_url:
                photo_path = download_image(holiday.image_url)
            post_id = publisher.post(
                message=text,
                photo_path=photo_path,
            )
            post_ids.append(post_id)
        except (AIError, VKError) as exc:
            logger.error("Не удалось опубликовать «%s»: %s", holiday.title, exc)
            continue
        finally:
            # Удаляем временный файл
            if photo_path:
                try:
                    os.remove(photo_path)
                except OSError:
                    pass

    logger.info("Готово. Опубликовано постов: %d", len(post_ids))
    return post_ids


def main() -> int:
    """Точка входа: читает .env и запускает run_daily."""
    load_dotenv()
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    vk_token = os.getenv("VK_TOKEN", "")
    vk_group_id = os.getenv("VK_GROUP_ID", "")
    mistral_api_key = os.getenv("MISTRAL_API_KEY", "")

    if not vk_token:
        logger.error("VK_TOKEN не задан в .env")
        return 1
    if not vk_group_id:
        logger.error("VK_GROUP_ID не задан в .env")
        return 1
    if not mistral_api_key:
        logger.error("MISTRAL_API_KEY не задан в .env")
        return 1

    max_holidays = int(os.getenv("MAX_HOLIDAYS", "3"))
    mistral_model = os.getenv("MISTRAL_MODEL", "mistral-small-latest")

    try:
        run_daily(
            vk_token=vk_token,
            vk_group_id=int(vk_group_id),
            mistral_api_key=mistral_api_key,
            max_holidays=max_holidays,
            mistral_model=mistral_model,
        )
    except (AIError, VKError) as exc:
        logger.error("Ошибка выполнения: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
