"""Планировщик публикаций календаря и контентных слотов."""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.main import DEFAULT_BOT_TIMEZONE, DEFAULT_SLOT_TIMES, local_now, timezone_for, validate_runtime_config
from app.ai_generator import AIRateLimitError

_rate_limit_until = 0.0


def _set_rate_limit_cooldown(exc: AIRateLimitError) -> None:
    global _rate_limit_until
    configured = float(os.getenv("GEMINI_RATE_LIMIT_COOLDOWN_SECONDS", "300"))
    maximum = float(os.getenv("GEMINI_RATE_LIMIT_COOLDOWN_MAX_SECONDS", "3600"))
    duration = min(max(configured, exc.cooldown_seconds), maximum)
    _rate_limit_until = time.monotonic() + duration
    logger.error("Gemini rate limit; scheduler paused for %.0f seconds", duration)


def _cooldown_active() -> bool:
    return time.monotonic() < _rate_limit_until


def _cooldown_remaining() -> float:
    return max(0.0, _rate_limit_until - time.monotonic())


def _sleep_scheduler_interval() -> None:
    interval = max(1, int(os.getenv("SCHEDULE_POLL_SECONDS", "20")))
    time.sleep(min(interval, _cooldown_remaining()) if _cooldown_active() else interval)

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
        "gemini_max_retries": int(os.getenv("GEMINI_MAX_RETRIES", "3")),
        "gemini_rate_limit_retries": int(os.getenv("GEMINI_RATE_LIMIT_RETRIES", "2")),
        "gemini_transient_retries": int(os.getenv("GEMINI_TRANSIENT_RETRIES", "2")),
        "gemini_backoff_base": float(os.getenv("GEMINI_BACKOFF_BASE_SECONDS", "2")),
        "gemini_backoff_max": float(os.getenv("GEMINI_BACKOFF_MAX_SECONDS", "30")),
        "gemini_rate_limit_cooldown": float(os.getenv("GEMINI_RATE_LIMIT_COOLDOWN_SECONDS", "300")),
        "gemini_rate_limit_cooldown_max": float(os.getenv("GEMINI_RATE_LIMIT_COOLDOWN_MAX_SECONDS", "3600")),
        "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        "content_mode": os.getenv("CONTENT_MODE", "cards").strip().lower(),
        "daily_posts": int(os.getenv("DAILY_POSTS", "7")),
        "bot_timezone": os.getenv("BOT_TIMEZONE", DEFAULT_BOT_TIMEZONE),
        "slot_times": os.getenv("SLOT_TIMES", DEFAULT_SLOTS),
        "schedule_poll_seconds": int(os.getenv("SCHEDULE_POLL_SECONDS", "20")),
        "slot_grace_seconds": int(os.getenv("SLOT_GRACE_SECONDS", "300")),
        "allow_card_reuse": env_bool(os.getenv("ALLOW_CARD_REUSE")),
        "vk_token": os.getenv("VK_TOKEN", ""),
        "vk_group_id": int(os.getenv("VK_GROUP_ID", "0")),
        "max_holidays": int(os.getenv("MAX_HOLIDAYS", "3")),
        "with_photos": env_bool(os.getenv("WITH_PHOTOS")),
        "registry_path": os.getenv("SOURCE_REGISTRY_DB", "state/sources.sqlite3"),
        "registry_mode": os.getenv("SOURCE_REGISTRY_MODE", "auto").strip().lower(),
        "image_provider": os.getenv("IMAGE_PROVIDER", "none").strip().lower(),
        "image_base_url": os.getenv("IMAGE_BASE_URL", "https://image.pollinations.ai/prompt/"),
        "image_model": os.getenv("IMAGE_MODEL", "flux"),
        "image_timeout": int(os.getenv("IMAGE_TIMEOUT_SECONDS", "30")),
        "image_fallback_to_text": env_bool(os.getenv("IMAGE_FALLBACK_TO_TEXT"), default=True),
        "cloudflare_account_id": os.getenv("CLOUDFLARE_ACCOUNT_ID", ""),
        "cloudflare_api_token": os.getenv("CLOUDFLARE_API_TOKEN", ""),
        "cloudflare_model": os.getenv("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell"),
        "cloudflare_timeout": int(os.getenv("CLOUDFLARE_IMAGE_TIMEOUT_SECONDS") or "60"),
    }


def _slot_times() -> tuple[str, ...]:
    values = tuple(value.strip() for value in os.getenv("SLOT_TIMES", DEFAULT_SLOTS).split(",") if value.strip())
    expected = tuple(local_time for _key, local_time, _requirements, _strict in SLOT_DEFINITIONS)
    if len(values) != len(SLOT_DEFINITIONS) or len(set(values)) != len(values):
        raise ValueError(f"SLOT_TIMES must contain exactly {len(SLOT_DEFINITIONS)} unique times")
    for value in values:
        datetime.strptime(value, "%H:%M")
    if values != expected:
        raise ValueError("SLOT_TIMES must match the ordered slot definitions")
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
    options = _daily_options()
    recent = () if env_bool(os.getenv("ALLOW_CARD_REUSE")) else _recent_card_ids(content_db, local_date)
    plan_content_day(local_date, content_db=content_db, recent_card_ids=recent)
    grace_seconds = int(os.getenv("SLOT_GRACE_SECONDS", "300"))
    due_slots = []
    for index, local_time in enumerate(_slot_times()):
        due = datetime.combine(datetime.fromisoformat(local_date).date(), datetime.strptime(local_time, "%H:%M").time(), tz).astimezone(timezone.utc)
        lateness = (now - due).total_seconds()
        if 0 <= lateness <= grace_seconds:
            due_slots.append((index, due))
    if not due_slots:
        return
    # Process only the latest slot still within its grace window; never burst missed slots.
    index, _due = due_slots[-1]
    if len(due_slots) > 1:
        logger.warning("Пропускаю просроченные слоты; обрабатываю только %s", SLOT_KEYS[index])
    run_content_slot(
        vk_token=os.getenv("VK_TOKEN", ""),
        vk_group_id=int(os.getenv("VK_GROUP_ID", "0")),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        local_date=local_date,
        slot_key=SLOT_KEYS[index],
        content_db=content_db,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        with_photos=env_bool(os.getenv("WITH_PHOTOS")),
        image_provider=str(os.getenv("IMAGE_PROVIDER", "none")),
        image_base_url=str(os.getenv("IMAGE_BASE_URL", "https://image.pollinations.ai/prompt/")),
        image_model=str(os.getenv("IMAGE_MODEL", "flux")),
        image_timeout=int(os.getenv("IMAGE_TIMEOUT_SECONDS", "30")),
        image_fallback_to_text=env_bool(os.getenv("IMAGE_FALLBACK_TO_TEXT"), default=True),
        cloudflare_account_id=str(options["cloudflare_account_id"]),
        cloudflare_api_token=str(options["cloudflare_api_token"]),
        cloudflare_model=str(options["cloudflare_model"]),
        cloudflare_timeout=int(options["cloudflare_timeout"]),
        recent_card_ids=recent,
        **{key: options[key] for key in ("gemini_max_retries", "gemini_rate_limit_retries", "gemini_transient_retries", "gemini_backoff_base", "gemini_backoff_max", "gemini_rate_limit_cooldown", "gemini_rate_limit_cooldown_max")},
    )


def main() -> int:
    from app.main import run_daily
    try:
        validate_runtime_config()
    except ValueError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return 1
    if os.getenv("CONTENT_MODE", "cards").strip().lower() == "cards":
        logger.info("Планировщик карточек запущен: %d слотов, timezone=%s", len(SLOT_KEYS), os.getenv("BOT_TIMEZONE", DEFAULT_BOT_TIMEZONE))
        while True:
            if _cooldown_active():
                logger.info("Gemini cooldown: %.0f seconds remaining", _cooldown_remaining())
                _sleep_scheduler_interval()
                continue
            try:
                _run_cards_once()
            except AIRateLimitError as exc:
                _set_rate_limit_cooldown(exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка планировщика карточек: %s", exc)
            time.sleep(max(1, int(os.getenv("SCHEDULE_POLL_SECONDS", "20"))))
    hour = int(os.getenv("POST_HOUR", "9")); minute = int(os.getenv("POST_MINUTE", "0"))
    logger.info("Планировщик legacy запущен. Постинг в %02d:%02d", hour, minute)
    while True:
        timezone_name = os.getenv("BOT_TIMEZONE", DEFAULT_BOT_TIMEZONE)
        if _cooldown_active():
            logger.info("Gemini cooldown: %.0f seconds remaining", _cooldown_remaining())
            _sleep_scheduler_interval()
            continue
        wait = _seconds_until(hour, minute, timezone_name)
        logger.info("До следующего запуска: %.0f секунд", wait)
        time.sleep(min(wait, 3600))
        now = local_now(timezone_name)
        if now.hour == hour and now.minute == minute:
            logger.info("Время постить!")
            try:
                options = _daily_options()
                run_daily(**{key: options[key] for key in ("vk_token", "vk_group_id", "gemini_api_key", "max_holidays", "gemini_model", "with_photos", "registry_path", "registry_mode", "image_provider", "image_base_url", "image_model", "image_timeout", "image_fallback_to_text", "cloudflare_account_id", "cloudflare_api_token", "cloudflare_model", "cloudflare_timeout")}, timezone_name=str(options["bot_timezone"]))
            except AIRateLimitError as exc:
                _set_rate_limit_cooldown(exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка при выполнении: %s", exc)
            time.sleep(60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
