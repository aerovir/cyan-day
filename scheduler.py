"""Планировщик публикаций календаря и контентных слотов."""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.main import DEFAULT_BOT_TIMEZONE, DEFAULT_SLOT_TIMES, local_now, timezone_for, validate_runtime_config

from dotenv import load_dotenv

from app.content_store import SLOT_DEFINITIONS

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
logger = logging.getLogger("scheduler")
DEFAULT_SLOTS = DEFAULT_SLOT_TIMES
RECENT_WINDOW_DAYS = 7
SLOT_KEYS = tuple(key for key, _time, _requirements, _strict in SLOT_DEFINITIONS)


def _seconds_until(hour: int, minute: int, timezone_name: str = DEFAULT_BOT_TIMEZONE) -> float:
    now = local_now(timezone_name)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _daily_options() -> dict[str, object]:
    from app.main import env_bool
    return {
        "content_db": os.getenv("CONTENT_DB", "state/content.sqlite3"),
        "content_mode": os.getenv("CONTENT_MODE", "legacy").strip().lower(),
        "daily_posts": int(os.getenv("DAILY_POSTS", "7")),
        "bot_timezone": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
        "slot_times": os.getenv("SLOT_TIMES", DEFAULT_SLOTS),
        "max_catchup_slots": int(os.getenv("MAX_CATCHUP_SLOTS", "7")),
        "schedule_poll_seconds": int(os.getenv("SCHEDULE_POLL_SECONDS", "20")),
        "allow_card_reuse": env_bool(os.getenv("ALLOW_CARD_REUSE")),
        "vk_token": os.getenv("VK_TOKEN", ""),
        "vk_group_id": int(os.getenv("VK_GROUP_ID", "0")),
        "mistral_api_key": os.getenv("MISTRAL_API_KEY", ""),
        "max_holidays": int(os.getenv("MAX_HOLIDAYS", "3")),
        "mistral_model": os.getenv("MISTRAL_MODEL", "mistral-small-latest"),
        "with_photos": env_bool(os.getenv("WITH_PHOTOS")),
        "registry_path": os.getenv("SOURCE_REGISTRY_DB", "state/sources.sqlite3"),
        "registry_mode": os.getenv("SOURCE_REGISTRY_MODE", "auto").strip().lower(),
        "image_provider": os.getenv("IMAGE_PROVIDER", "none").strip().lower(),
        "image_base_url": os.getenv("IMAGE_BASE_URL", "https://image.pollinations.ai/prompt/"),
        "image_model": os.getenv("IMAGE_MODEL", "flux"),
        "image_timeout": int(os.getenv("IMAGE_TIMEOUT_SECONDS", "30")),
        "image_fallback_to_text": env_bool(os.getenv("IMAGE_FALLBACK_TO_TEXT"), default=True),
    }


def _slot_times() -> tuple[str, ...]:
    values = tuple(value.strip() for value in os.getenv("SLOT_TIMES", DEFAULT_SLOTS).split(",") if value.strip())
    if len(values) != 7:
        raise ValueError("SLOT_TIMES must contain exactly seven times")
    for value in values:
        datetime.strptime(value, "%H:%M")
    return values


def _recent_card_ids(content_db: str, local_date: str, days: int = RECENT_WINDOW_DAYS) -> tuple[str, ...]:
    """Карточки, опубликованные за последние дни — чтобы не повторять их."""
    from app.content_store import ContentStore
    since = (date.fromisoformat(local_date) - timedelta(days=days)).isoformat()
    with ContentStore(content_db) as store:
        return tuple(store.recent_published_card_ids(since))


def _run_cards_once() -> None:
    from app.main import env_bool, plan_content_day, run_content_slot
    timezone_name = os.getenv("BOT_TIMEZONE", DEFAULT_BOT_TIMEZONE)
    timezone_for(timezone_name)
    tz = ZoneInfo(timezone_name)
    now = datetime.now(timezone.utc)
    local_date = now.astimezone(tz).date().isoformat()
    content_db = os.getenv("CONTENT_DB", "state/content.sqlite3")
    recent = () if env_bool(os.getenv("ALLOW_CARD_REUSE")) else _recent_card_ids(content_db, local_date)
    plan_content_day(local_date, content_db=content_db, recent_card_ids=recent)
    # Догонка: публикуем просроченные слоты, но не больше MAX_CATCHUP_SLOTS за цикл;
    # опубликованные в прошлых циклах слоты повторно не считаются (claim_slot вернёт False).
    published = 0
    max_catchup = int(os.getenv("MAX_CATCHUP_SLOTS", "7"))
    for index, local_time in enumerate(_slot_times()):
        due = datetime.combine(datetime.fromisoformat(local_date).date(), datetime.strptime(local_time, "%H:%M").time(), tz).astimezone(timezone.utc)
        if now < due:
            continue
        if published >= max_catchup:
            logger.info("Лимит догонки %d достигнут, остальные слоты отложены до следующего цикла", max_catchup)
            break
        post_id = run_content_slot(
            vk_token=os.getenv("VK_TOKEN", ""),
            vk_group_id=int(os.getenv("VK_GROUP_ID", "0")),
            mistral_api_key=os.getenv("MISTRAL_API_KEY", ""),
            local_date=local_date,
            slot_key=SLOT_KEYS[index],
            content_db=content_db,
            mistral_model=os.getenv("MISTRAL_MODEL", "mistral-small-latest"),
            with_photos=env_bool(os.getenv("WITH_PHOTOS")),
            image_provider=str(os.getenv("IMAGE_PROVIDER", "none")),
            image_base_url=str(os.getenv("IMAGE_BASE_URL", "https://image.pollinations.ai/prompt/")),
            image_model=str(os.getenv("IMAGE_MODEL", "flux")),
            image_timeout=int(os.getenv("IMAGE_TIMEOUT_SECONDS", "30")),
            image_fallback_to_text=env_bool(os.getenv("IMAGE_FALLBACK_TO_TEXT"), default=True),
            recent_card_ids=recent,
        )
        if post_id is not None:
            published += 1


def main() -> int:
    from app.main import run_daily
    try:
        validate_runtime_config()
    except ValueError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return 1
    if os.getenv("CONTENT_MODE", "legacy").strip().lower() == "cards":
        logger.info("Планировщик карточек запущен: 7 слотов, timezone=%s", os.getenv("BOT_TIMEZONE", "Europe/Moscow"))
        while True:
            try:
                _run_cards_once()
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка планировщика карточек: %s", exc)
            time.sleep(max(1, int(os.getenv("SCHEDULE_POLL_SECONDS", "20"))))
    hour = int(os.getenv("POST_HOUR", "9")); minute = int(os.getenv("POST_MINUTE", "0"))
    logger.info("Планировщик legacy запущен. Постинг в %02d:%02d", hour, minute)
    while True:
        timezone_name = os.getenv("BOT_TIMEZONE", DEFAULT_BOT_TIMEZONE)
        wait = _seconds_until(hour, minute, timezone_name)
        logger.info("До следующего запуска: %.0f секунд", wait)
        time.sleep(min(wait, 3600))
        now = local_now(timezone_name)
        if now.hour == hour and now.minute == minute:
            logger.info("Время постить!")
            try:
                options = _daily_options()
                run_daily(**{key: options[key] for key in ("vk_token", "vk_group_id", "mistral_api_key", "max_holidays", "mistral_model", "with_photos", "registry_path", "registry_mode", "image_provider", "image_base_url", "image_model", "image_timeout", "image_fallback_to_text")}, timezone_name=str(options["bot_timezone"]))
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка при выполнении: %s", exc)
            time.sleep(60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
