"""Оркестратор бота: собрать праздники → сгенерировать посты → опубликовать."""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from datetime import datetime
from typing import Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv

from .ai_generator import AIGenerator, AIError
from .content_planner import build_plan
from .content_store import PLAN_VERSION, ContentCard, ContentError, ContentStore, DEFAULT_CONTENT_DB
from .holidays_parser import HolidaysParser, Holiday
from .source_registry import DEFAULT_DB_PATH, SourceRegistry
from .sources import SourceError, SourceItem, deduplicate_source_items, validate_url
from .vk_publisher import VKError, VKPhotoUploadError, VKPublisher, VKUnknownError

logger = logging.getLogger(__name__)

CONTENT_LABELS = {"verified": "ФАКТ", "unverified": "МИФ", "disputed": "РАЗБОР", "refuted": "РАЗБОР", "rejected": "МИФ"}
SOURCE_REGISTRY_MODES = {"auto", "legacy", "registry"}
IMAGE_HOSTS = ("calend.ru", "www.calend.ru")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_BOT_TIMEZONE = "Europe/Moscow"
IMAGE_PROVIDERS = {"none", "pollinations"}
DEFAULT_IMAGE_BASE_URL = "https://image.pollinations.ai/prompt/"
DEFAULT_IMAGE_MODEL = "flux"
DEFAULT_IMAGE_TIMEOUT = 30
MAX_PROMPT_LENGTH = 1000
DEFAULT_SLOT_TIMES = "09:00,10:30,12:00,13:30,15:00,17:00,19:00"


def timezone_for(name: str = DEFAULT_BOT_TIMEZONE) -> ZoneInfo:
    """Return a validated IANA timezone for all scheduler date calculations."""
    try:
        return ZoneInfo(name.strip())
    except (AttributeError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"invalid BOT_TIMEZONE: {name!r}") from exc


def _parse_positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _parse_nonnegative_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def validate_runtime_config(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Validate boundary configuration before starting a long-lived process."""
    values = os.environ if environ is None else environ
    required = ("VK_TOKEN", "VK_GROUP_ID", "MISTRAL_API_KEY")
    missing = [name for name in required if not values.get(name, "").strip()]
    if missing:
        raise ValueError("required environment variables are missing: " + ", ".join(missing))

    group_id = values["VK_GROUP_ID"].strip()
    if group_id.lower().startswith("club"):
        raise ValueError("VK_GROUP_ID must be numeric without the 'club' prefix")
    parsed_group_id = _parse_positive_int(group_id, "VK_GROUP_ID")
    mode = values.get("CONTENT_MODE", "legacy").strip().lower()
    if mode not in {"legacy", "cards"}:
        raise ValueError("CONTENT_MODE must be either 'legacy' or 'cards'")
    timezone_name = values.get("BOT_TIMEZONE", DEFAULT_BOT_TIMEZONE).strip()
    timezone_for(timezone_name)

    slot_times = tuple(item.strip() for item in values.get("SLOT_TIMES", DEFAULT_SLOT_TIMES).split(",") if item.strip())
    if len(slot_times) != 7:
        raise ValueError("SLOT_TIMES must contain exactly seven times")
    for item in slot_times:
        try:
            datetime.strptime(item, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"invalid slot time: {item!r}") from exc

    max_catchup = _parse_positive_int(values.get("MAX_CATCHUP_SLOTS", "7"), "MAX_CATCHUP_SLOTS")
    poll_seconds = _parse_positive_int(values.get("SCHEDULE_POLL_SECONDS", "20"), "SCHEDULE_POLL_SECONDS")
    max_holidays = _parse_nonnegative_int(values.get("MAX_HOLIDAYS", "3"), "MAX_HOLIDAYS")
    post_hour = _parse_nonnegative_int(values.get("POST_HOUR", "9"), "POST_HOUR")
    post_minute = _parse_nonnegative_int(values.get("POST_MINUTE", "0"), "POST_MINUTE")
    if post_hour > 23 or post_minute > 59:
        raise ValueError("POST_HOUR must be 0..23 and POST_MINUTE must be 0..59")
    image_provider = values.get("IMAGE_PROVIDER", "none").strip().lower()
    if image_provider not in IMAGE_PROVIDERS:
        raise ValueError("IMAGE_PROVIDER must be either 'none' or 'pollinations'")
    image_timeout = _parse_positive_int(values.get("IMAGE_TIMEOUT_SECONDS", str(DEFAULT_IMAGE_TIMEOUT)), "IMAGE_TIMEOUT_SECONDS")
    if image_timeout > 120:
        raise ValueError("IMAGE_TIMEOUT_SECONDS must be at most 120")
    image_model = values.get("IMAGE_MODEL", DEFAULT_IMAGE_MODEL).strip()
    if image_provider == "pollinations" and not image_model:
        raise ValueError("IMAGE_MODEL must not be empty when Pollinations is enabled")
    image_base_url = values.get("IMAGE_BASE_URL", DEFAULT_IMAGE_BASE_URL).strip()
    if image_provider == "pollinations":
        validate_url(image_base_url, allowed_hosts=("image.pollinations.ai",), resolve=False)
        if not image_base_url.rstrip("/").endswith("/prompt"):
            raise ValueError("IMAGE_BASE_URL must point to the Pollinations /prompt endpoint")
    return {
        "image_provider": image_provider,
        "image_base_url": image_base_url,
        "image_model": image_model,
        "image_timeout": image_timeout,
        "image_fallback_to_text": env_bool(values.get("IMAGE_FALLBACK_TO_TEXT"), default=True),
        "vk_group_id": parsed_group_id,
        "content_mode": mode,
        "bot_timezone": timezone_name,
        "slot_times": slot_times,
        "max_catchup_slots": max_catchup,
        "schedule_poll_seconds": poll_seconds,
        "max_holidays": max_holidays,
    }


def _image_prompt(title: str, description: str, *, visual_brief: str = "", tags: Iterable[str] = ()) -> str:
    """Build a bounded editorial prompt without facts or credentials."""
    parts = [visual_brief.strip() or "archival satirical editorial illustration", title.strip(), description.strip()]
    tag_text = ", ".join(tag for tag in tags if isinstance(tag, str) and tag.strip())
    if tag_text:
        parts.append(f"Themes: {tag_text}")
    parts.append("No readable text, logos, modern brands, real persons, graphic intoxication, medical or political claims.")
    return " ".join(part for part in parts if part)[:MAX_PROMPT_LENGTH]


def _generated_image(
    prompt: str,
    *,
    image_provider: str,
    image_model: str,
    image_timeout: int,
    image_base_url: str,
) -> Optional[str]:
    if image_provider != "pollinations":
        return None
    try:
        from .image_provider import PollinationsImageProvider
        provider = PollinationsImageProvider(model=image_model, timeout=image_timeout, base_url=image_base_url)
        return provider.generate(prompt, downloader=download_image)
    except Exception as exc:  # noqa: BLE001 — image is optional
        logger.warning("Не удалось сгенерировать картинку: %s", exc)
        return None


def _post_text_with_photo_fallback(publisher: VKPublisher, text: str, photo_path: Optional[str]) -> int:
    """Retry once without photo only when upload failed before wall.post."""
    try:
        return publisher.post(message=text, photo_path=photo_path)
    except VKPhotoUploadError:
        if not photo_path:
            raise
        logger.warning("Загрузка фото не удалась; повторяю пост без картинки")
        return publisher.post(message=text, photo_path=None)


def _verified_text(card: ContentCard) -> str:
    """Render verified claims without allowing a model to alter facts."""
    claims = " ".join(claim["text"] for claim in card.claims)
    return f"{CONTENT_LABELS[card.status]}: {claims or card.summary}\nИсточник: {card.provenance[0].get('title', '') if card.provenance else 'редакционная карточка'}"


def run_content_slot(
    vk_token: str,
    vk_group_id: int,
    mistral_api_key: str,
    local_date: str,
    slot_key: str,
    *,
    content_db: str = DEFAULT_CONTENT_DB,
    mistral_model: str = "mistral-small-latest",
    with_photos: bool = False,
    recent_card_ids: Iterable[str] = (),
    image_provider: str = "none",
    image_base_url: str = DEFAULT_IMAGE_BASE_URL,
    image_model: str = DEFAULT_IMAGE_MODEL,
    image_timeout: int = DEFAULT_IMAGE_TIMEOUT,
    image_fallback_to_text: bool = True,
) -> int | None:
    """Generate and publish one planned editorial slot exactly once."""
    ledger_key = None
    with ContentStore(content_db) as store:
        card = store.planned_card(local_date, slot_key)
        if card is None:
            cards = store.list_cards(local_date[5:])
            planned = build_plan(local_date, cards, recent_card_ids)
            store.save_plan(local_date, planned)
            card = store.planned_card(local_date, slot_key)
        if card is None or not store.claim_slot(local_date, slot_key, card):
            return None
        ledger = store.get_publication(local_date, slot_key)
        ledger_key = ledger["idempotency_key"] if ledger else None
    photo_path = None
    try:
        generator = AIGenerator(api_key=mistral_api_key, model=mistral_model)
        if card.status == "verified":
            text = _verified_text(card)
        else:
            generated = generator.generate_for_content(card.packet())
            text = f"{generated.label}: {generated.body}"
        if with_photos:
            if card.image_url:
                # Картинки карточек задаёт редактор, поэтому без allowlist хостов;
                # остальные SSRF-проверки сохраняются.
                photo_path = download_image(card.image_url, allowed_hosts=())
            else:
                photo_path = _generated_image(
                    _image_prompt(card.title, card.summary, tags=card.tags),
                    image_provider=image_provider, image_model=image_model,
                    image_timeout=image_timeout, image_base_url=image_base_url,
                )
        publisher = VKPublisher(token=vk_token, group_id=vk_group_id)
        post_id = _post_text_with_photo_fallback(publisher, text, photo_path)
        with ContentStore(content_db) as store:
            store.mark_published(local_date, slot_key, post_id, text, expected_idempotency_key=ledger_key)
        return post_id
    except VKUnknownError as exc:
        with ContentStore(content_db) as store:
            store.mark_unknown(local_date, slot_key, str(exc), expected_idempotency_key=ledger_key)
        logger.error("Не удалось определить результат публикации слота %s: %s", slot_key, exc)
        return None
    except (AIError, VKError, OSError, ContentError) as exc:
        with ContentStore(content_db) as store:
            store.mark_failed(local_date, slot_key, str(exc), expected_idempotency_key=ledger_key)
        logger.error("Не удалось опубликовать слот %s: %s", slot_key, exc)
        return None

    finally:
        if photo_path:
            try:
                os.remove(photo_path)
            except OSError:
                pass


def plan_content_day(
    local_date: str,
    *,
    content_db: str = DEFAULT_CONTENT_DB,
    recent_card_ids: Iterable[str] = (),
) -> list[dict[str, object]]:
    """Create or refresh the seven-slot plan for a local calendar date."""
    with ContentStore(content_db) as store:
        existing = store.get_plan(local_date)
        if existing and existing[0]["plan_version"] == PLAN_VERSION:
            return existing
        # Устаревшая версия плана (старая сетка слотов) — перегенерируем
        planned = build_plan(local_date, store.list_cards(local_date[5:]), recent_card_ids)
        store.save_plan(local_date, planned)
        return store.get_plan(local_date)


def env_bool(value: str | None, default: bool = False) -> bool:
    """Parse common boolean environment values consistently."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def today_str(timezone_name: str = DEFAULT_BOT_TIMEZONE) -> str:
    """Вернуть текущую дату в заданном часовом поясе."""
    return local_now(timezone_name).date().isoformat()


def local_now(timezone_name: str = DEFAULT_BOT_TIMEZONE) -> datetime:
    """Return timezone-aware current time for scheduler and legacy flows."""
    return datetime.now(timezone_for(timezone_name))


def local_date_from_now(timezone_name: str = DEFAULT_BOT_TIMEZONE) -> str:
    """Return the current local date in ISO format."""
    return local_now(timezone_name).date().isoformat()


def validate_config() -> dict[str, object]:
    """Validate process environment and return parsed runtime values."""
    return validate_runtime_config()


def setup_logging(level: str = "INFO") -> None:
    """Настроить логирование в stdout."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _holiday_to_item(holiday: Holiday, day: str) -> SourceItem:
    """Adapt the legacy parser's result to the normalized source model."""
    return SourceItem(
        source_id="calendru_legacy",
        external_id=holiday.url or holiday.title,
        event_date=day,
        title=holiday.title,
        description=holiday.description,
        url=holiday.url,
        image_url=holiday.image_url,
        category=holiday.category,
    )


def _legacy_items(parser: HolidaysParser, day: str) -> list[SourceItem]:
    html = parser.fetch_day_page(day)
    return [_holiday_to_item(item, day) for item in parser.parse_day_page(html)]


def _collect_items(
    day: str,
    *,
    registry_path: str | None = None,
    registry_mode: str = "auto",
) -> list[SourceItem]:
    """Collect normalized events from the registry or the legacy source."""
    if registry_mode not in SOURCE_REGISTRY_MODES:
        raise SourceError(f"unsupported source registry mode: {registry_mode}")
    parser = HolidaysParser()
    if registry_mode == "legacy":
        return _legacy_items(parser, day)

    path = registry_path or DEFAULT_DB_PATH
    if registry_mode == "auto" and path != ":memory:" and not os.path.exists(path):
        return _legacy_items(parser, day)

    try:
        with SourceRegistry(path) as registry:
            adapters = registry.enabled_adapters()
            if not adapters:
                if registry_mode == "registry":
                    logger.info("В реестре нет включённых источников")
                    return []
                return _legacy_items(parser, day)
            items: list[SourceItem] = []
            for record, adapter in adapters:
                try:
                    logger.info("Собираю источник %s", record.name)
                    fetched = adapter.fetch(day)
                    valid: list[SourceItem] = []
                    for item in fetched:
                        if not isinstance(item, SourceItem):
                            logger.warning("Источник %s вернул неизвестный тип события", record.name)
                            continue
                        if item.event_date != day or not item.title.strip():
                            logger.warning("Источник %s вернул событие с неверными данными", record.name)
                            continue
                        valid.append(item)
                    logger.info("Источник %s вернул %d событий", record.name, len(valid))
                    items.extend(valid)
                except Exception as exc:  # noqa: BLE001 — isolate source failures
                    logger.error(
                        "Ошибка источника %s (%s): %s",
                        record.name,
                        type(exc).__name__,
                        exc,
                    )
            return deduplicate_source_items(items)
    except (OSError, SourceError):
        if registry_mode == "auto" and path != ":memory:" and not os.path.exists(path):
            return _legacy_items(parser, day)
        raise


def _download_image_safe(
    url: str,
    timeout: int = 30,
    allowed_hosts: tuple[str, ...] = IMAGE_HOSTS,
) -> Optional[str]:
    """Download a bounded image without redirects, proxies, or private IPs."""
    try:
        normalized = validate_url(url, allowed_hosts=allowed_hosts, resolve=True)
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                normalized,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
            if 300 <= response.status_code < 400:
                raise SourceError("image URL redirects are not allowed")
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                raise SourceError("image response has an unsupported content type")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_IMAGE_BYTES:
                raise SourceError("image response is too large")
            ext = ".png" if content_type == "image/png" else ".jpg"
            fd, path = tempfile.mkstemp(suffix=ext)
            total = 0
            try:
                with os.fdopen(fd, "wb") as output:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise SourceError("image response is too large")
                        output.write(chunk)
                return path
            except Exception:
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise
        finally:
            session.close()
    except (requests.RequestException, OSError, SourceError, ValueError) as exc:
        logger.warning("Не удалось скачать безопасную картинку %s: %s", url, exc)
        return None


def download_image(
    url: str,
    timeout: int = 30,
    allowed_hosts: tuple[str, ...] | None = None,
) -> Optional[str]:
    """Backward-compatible safe image download helper.

    ``allowed_hosts=None`` применяет allowlist по умолчанию (calend.ru);
    пустой кортеж означает «без ограничения хостов» (остальные
    SSRF-проверки сохраняются).
    """
    if allowed_hosts is None:
        allowed_hosts = IMAGE_HOSTS
    return _download_image_safe(url, timeout=timeout, allowed_hosts=allowed_hosts)


def run_daily(
    vk_token: str,
    vk_group_id: int,
    mistral_api_key: str,
    max_holidays: int = 3,
    mistral_model: str = "mistral-small-latest",
    with_photos: bool = False,
    registry_path: str | None = None,
    registry_mode: str = "auto",
    timezone_name: str = DEFAULT_BOT_TIMEZONE,
    image_provider: str = "none",
    image_base_url: str = DEFAULT_IMAGE_BASE_URL,
    image_model: str = DEFAULT_IMAGE_MODEL,
    image_timeout: int = DEFAULT_IMAGE_TIMEOUT,
    image_fallback_to_text: bool = True,
) -> List[int]:
    """Основной цикл: собрать праздники, сгенерировать и опубликовать посты."""
    generator = AIGenerator(api_key=mistral_api_key, model=mistral_model)
    publisher = VKPublisher(token=vk_token, group_id=vk_group_id)

    today = today_str(timezone_name)
    logger.info("Собираю праздники на %s", today)
    items = _collect_items(today, registry_path=registry_path, registry_mode=registry_mode)

    if not items:
        logger.warning("Праздников на %s не найдено", today)
        return []

    selected = items[:max_holidays]
    logger.info("Найдено праздников: %d, публикуем: %d", len(items), len(selected))

    post_ids: List[int] = []
    for item in selected:
        photo_path = None
        try:
            logger.info("Обрабатываю праздник: %s", item.title)
            text = generator.generate_for_holiday(item)
            if with_photos:
                if item.image_url:
                    photo_path = download_image(item.image_url)
                else:
                    photo_path = _generated_image(
                        _image_prompt(item.title, item.description),
                        image_provider=image_provider, image_model=image_model,
                        image_timeout=image_timeout, image_base_url=image_base_url,
                    )
            post_id = _post_text_with_photo_fallback(publisher, text, photo_path)
            post_ids.append(post_id)
        except (AIError, VKError) as exc:
            logger.error("Не удалось опубликовать «%s»: %s", item.title, exc)
            continue
        finally:
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
    mistral_api_key = os.getenv("MISTRAL_API_KEY", "")

    try:
        runtime = validate_runtime_config()
        max_holidays = int(runtime["max_holidays"])
        timezone_name = str(runtime["bot_timezone"])
        parsed_group_id = int(runtime["vk_group_id"])
    except ValueError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return 1

    mistral_model = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
    with_photos = env_bool(os.getenv("WITH_PHOTOS"))
    registry_path = os.getenv("SOURCE_REGISTRY_DB", DEFAULT_DB_PATH)
    registry_mode = os.getenv("SOURCE_REGISTRY_MODE", "auto").strip().lower()

    try:
        run_daily(
            vk_token=vk_token,
            vk_group_id=parsed_group_id,
            mistral_api_key=mistral_api_key,
            max_holidays=max_holidays,
            mistral_model=mistral_model,
            with_photos=with_photos,
            registry_path=registry_path,
            registry_mode=registry_mode,
            timezone_name=timezone_name,
            image_provider=str(runtime["image_provider"]),
            image_base_url=str(runtime["image_base_url"]),
            image_model=str(runtime["image_model"]),
            image_timeout=int(runtime["image_timeout"]),
            image_fallback_to_text=bool(runtime["image_fallback_to_text"]),
        )
    except (AIError, VKError, SourceError, OSError) as exc:
        logger.error("Ошибка выполнения: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
