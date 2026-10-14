"""Оркестратор бота: собрать праздники → сгенерировать посты → опубликовать."""

from __future__ import annotations

import logging
import os
import random
import re
import sys
import tempfile
from datetime import datetime
from typing import Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv

from .ai_generator import AIGenerator, AIError, AIRateLimitError
from .content_planner import PlannedSlot, build_plan
from .content_store import PLAN_VERSION, RECIPE_LEDGER_CARD_ID, RECIPE_TYPES, SLOT_DEFINITIONS, ContentCard, ContentError, ContentStore, DEFAULT_CONTENT_DB
from .holidays_parser import HolidaysParser, Holiday
from .source_registry import DEFAULT_DB_PATH, SourceRegistry
from .sources import SourceError, SourceItem, deduplicate_source_items, validate_url
from .vk_publisher import VKError, VKPhotoUploadError, VKPublisher, VKUnknownError

logger = logging.getLogger(__name__)

CONTENT_LABELS = {"verified": "ФАКТ", "unverified": "МИФ", "disputed": "РАЗБОР", "refuted": "РАЗБОР", "rejected": "МИФ"}
SOURCE_REGISTRY_MODES = {"auto", "legacy", "registry"}
IMAGE_HOSTS = ("calend.ru", "www.calend.ru")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_BOT_TIMEZONE = "Etc/GMT-2"
IMAGE_PROVIDERS = {"none", "pollinations", "cloudflare"}
DEFAULT_IMAGE_BASE_URL = "https://image.pollinations.ai/prompt/"
DEFAULT_IMAGE_MODEL = "flux"
DEFAULT_IMAGE_TIMEOUT = 30
MAX_PROMPT_LENGTH = 1000
DEFAULT_SLOT_TIMES = ",".join(local_time for _key, local_time, _requirements, _strict in SLOT_DEFINITIONS)


def timezone_for(name: str = DEFAULT_BOT_TIMEZONE) -> ZoneInfo:
    """Return a validated IANA timezone for all scheduler date calculations."""
    try:
        return ZoneInfo(name.strip())
    except (AttributeError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"invalid BOT_TIMEZONE: {name!r}") from exc



def _parse_positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def gemini_runtime_options(values: Mapping[str, str]) -> dict[str, object]:
    result = {
        "max_retries": _parse_positive_int(values.get("GEMINI_MAX_RETRIES", "3"), "GEMINI_MAX_RETRIES"),
        "rate_limit_retries": _parse_nonnegative_int(values.get("GEMINI_RATE_LIMIT_RETRIES", "2"), "GEMINI_RATE_LIMIT_RETRIES"),
        "transient_retries": _parse_nonnegative_int(values.get("GEMINI_TRANSIENT_RETRIES", "2"), "GEMINI_TRANSIENT_RETRIES"),
        "backoff_base": _parse_positive_float(values.get("GEMINI_BACKOFF_BASE_SECONDS", "2"), "GEMINI_BACKOFF_BASE_SECONDS"),
        "backoff_max": _parse_positive_float(values.get("GEMINI_BACKOFF_MAX_SECONDS", "30"), "GEMINI_BACKOFF_MAX_SECONDS"),
        "rate_limit_cooldown": _parse_positive_float(values.get("GEMINI_RATE_LIMIT_COOLDOWN_SECONDS", "300"), "GEMINI_RATE_LIMIT_COOLDOWN_SECONDS"),
        "rate_limit_cooldown_max": _parse_positive_float(values.get("GEMINI_RATE_LIMIT_COOLDOWN_MAX_SECONDS", "3600"), "GEMINI_RATE_LIMIT_COOLDOWN_MAX_SECONDS"),
    }
    if result["backoff_max"] < result["backoff_base"]:
        raise ValueError("GEMINI_BACKOFF_MAX_SECONDS must be at least base")
    if result["rate_limit_cooldown_max"] < result["rate_limit_cooldown"]:
        raise ValueError("GEMINI_RATE_LIMIT_COOLDOWN_MAX_SECONDS must be at least cooldown")
    return result

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
    required = ("VK_TOKEN", "VK_GROUP_ID", "GEMINI_API_KEY")
    missing = [name for name in required if not values.get(name, "").strip()]
    if missing:
        raise ValueError("required environment variables are missing: " + ", ".join(missing))

    group_id = values["VK_GROUP_ID"].strip()
    if group_id.lower().startswith("club"):
        raise ValueError("VK_GROUP_ID must be numeric without the 'club' prefix")
    parsed_group_id = _parse_positive_int(group_id, "VK_GROUP_ID")
    mode = values.get("CONTENT_MODE", "cards").strip().lower()
    if mode not in {"legacy", "cards"}:
        raise ValueError("CONTENT_MODE must be either 'legacy' or 'cards'")
    timezone_name = values.get("BOT_TIMEZONE", DEFAULT_BOT_TIMEZONE).strip()
    timezone_for(timezone_name)

    slot_times = tuple(item.strip() for item in values.get("SLOT_TIMES", DEFAULT_SLOT_TIMES).split(",") if item.strip())
    expected_slot_times = tuple(local_time for _key, local_time, _requirements, _strict in SLOT_DEFINITIONS)
    if len(slot_times) != len(SLOT_DEFINITIONS):
        raise ValueError(f"SLOT_TIMES must contain exactly {len(SLOT_DEFINITIONS)} times")
    if len(set(slot_times)) != len(slot_times):
        raise ValueError("SLOT_TIMES must not contain duplicates")
    for item in slot_times:
        try:
            datetime.strptime(item, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"invalid slot time: {item!r}") from exc
    if slot_times != expected_slot_times:
        raise ValueError("SLOT_TIMES must match the ordered slot definitions")

    poll_seconds = _parse_positive_int(values.get("SCHEDULE_POLL_SECONDS", "20"), "SCHEDULE_POLL_SECONDS")
    slot_grace_seconds = _parse_positive_int(values.get("SLOT_GRACE_SECONDS", "300"), "SLOT_GRACE_SECONDS")
    max_holidays = _parse_nonnegative_int(values.get("MAX_HOLIDAYS", "3"), "MAX_HOLIDAYS")
    post_hour = _parse_nonnegative_int(values.get("POST_HOUR", "9"), "POST_HOUR")
    post_minute = _parse_nonnegative_int(values.get("POST_MINUTE", "0"), "POST_MINUTE")
    if post_hour > 23 or post_minute > 59:
        raise ValueError("POST_HOUR must be 0..23 and POST_MINUTE must be 0..59")
    image_provider = values.get("IMAGE_PROVIDER", "none").strip().lower()
    if image_provider not in IMAGE_PROVIDERS:
        raise ValueError("IMAGE_PROVIDER must be 'none', 'pollinations', or 'cloudflare'")
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
    cloudflare_account_id = values.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    cloudflare_api_token = values.get("CLOUDFLARE_API_TOKEN", "").strip()
    cloudflare_model = values.get("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell").strip()
    cloudflare_timeout = _parse_positive_int(values.get("CLOUDFLARE_IMAGE_TIMEOUT_SECONDS") or "60", "CLOUDFLARE_IMAGE_TIMEOUT_SECONDS")
    if cloudflare_timeout > 120:
        raise ValueError("CLOUDFLARE_IMAGE_TIMEOUT_SECONDS must be at most 120")
    if image_provider == "cloudflare":
        if not cloudflare_account_id:
            raise ValueError("CLOUDFLARE_ACCOUNT_ID is required when IMAGE_PROVIDER=cloudflare")
        if not cloudflare_api_token:
            raise ValueError("CLOUDFLARE_API_TOKEN is required when IMAGE_PROVIDER=cloudflare")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", cloudflare_account_id):
            raise ValueError("CLOUDFLARE_ACCOUNT_ID must be a 32-character hexadecimal ID")
        if not cloudflare_model or not re.fullmatch(r"@cf/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+", cloudflare_model):
            raise ValueError("CLOUDFLARE_IMAGE_MODEL must be a Workers AI model ID")
    return {
        "image_provider": image_provider,
        "image_base_url": image_base_url,
        "image_model": image_model,
        "image_timeout": image_timeout,
        "image_fallback_to_text": env_bool(values.get("IMAGE_FALLBACK_TO_TEXT"), default=True),
        "cloudflare_account_id": cloudflare_account_id,
        "cloudflare_api_token": cloudflare_api_token,
        "cloudflare_model": cloudflare_model,
        "cloudflare_timeout": cloudflare_timeout,
        "vk_group_id": parsed_group_id,
        "content_mode": mode,
        "bot_timezone": timezone_name,
        "slot_times": slot_times,
        "slot_grace_seconds": slot_grace_seconds,
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
    cloudflare_account_id: str = "",
    cloudflare_api_token: str = "",
    cloudflare_model: str = "@cf/black-forest-labs/flux-1-schnell",
    cloudflare_timeout: int = 60,
) -> Optional[str]:
    if image_provider == "none":
        return None
    try:
        if image_provider == "pollinations":
            from .image_provider import PollinationsImageProvider
            provider = PollinationsImageProvider(model=image_model, timeout=image_timeout, base_url=image_base_url)
            path = provider.generate(prompt, downloader=download_image)
        elif image_provider == "cloudflare":
            from .image_provider import CloudflareImageProvider
            provider = CloudflareImageProvider(
                account_id=cloudflare_account_id,
                api_token=cloudflare_api_token,
                model=cloudflare_model,
                timeout=cloudflare_timeout,
            )
            path = provider.generate(prompt)
        else:
            raise ValueError(f"unsupported image provider: {image_provider}")
        if path is None:
            logger.error("Image provider %s returned no image; continuing with text-only post", image_provider)
        return path
    except Exception as exc:  # noqa: BLE001 — image is optional
        logger.error("Image generation failed for %s; continuing with text-only post: %s", image_provider, exc)
        return None


def _post_text_with_photo_fallback(publisher: VKPublisher, text: str, photo_path: Optional[str]) -> int:
    """Retry once without photo only when upload failed before wall.post."""
    try:
        return publisher.post(message=text, photo_path=photo_path)
    except VKPhotoUploadError as exc:
        if not photo_path:
            raise
        logger.warning("VK photo upload failed (code=%s): %s; retrying text-only", exc.code, exc.reason)
        return publisher.post(message=text, photo_path=None)


def _verified_text(card: ContentCard) -> str:
    """Render verified claims without allowing a model to alter facts."""
    claims = " ".join(claim["text"] for claim in card.claims)
    return f"{CONTENT_LABELS[card.status]}: {claims or card.summary}\nИсточник: {card.provenance[0].get('title', '') if card.provenance else 'редакционная карточка'}"


def _with_hashtags(text: str, tags: Iterable[str]) -> str:
    hashtags = []
    seen = set()
    for tag in tags:
        if not isinstance(tag, str) or "." not in tag:
            continue
        normalized = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_]", "_", tag.replace(".", "_" )).strip("_")
        if normalized and normalized not in seen:
            seen.add(normalized)
            hashtags.append("#" + normalized)
    return text.rstrip() + ("\n\n" + " ".join(hashtags) if hashtags else "")


def run_content_slot(
    vk_token: str,
    vk_group_id: int,
    gemini_api_key: str,
    local_date: str,
    slot_key: str,
    *,
    content_db: str = DEFAULT_CONTENT_DB,
    gemini_model: str = "gemini-3.5-flash-lite",
    with_photos: bool = False,
    recent_card_ids: Iterable[str] = (),
    image_provider: str = "none",
    image_base_url: str = DEFAULT_IMAGE_BASE_URL,
    image_model: str = DEFAULT_IMAGE_MODEL,
    image_timeout: int = DEFAULT_IMAGE_TIMEOUT,
    image_fallback_to_text: bool = True,
    cloudflare_account_id: str = "",
    cloudflare_api_token: str = "",
    cloudflare_model: str = "@cf/black-forest-labs/flux-1-schnell",
    cloudflare_timeout: int = 60,
    gemini_max_retries: int = 3,
    gemini_rate_limit_retries: int = 2,
    gemini_transient_retries: int = 2,
    gemini_backoff_base: float = 2.0,
    gemini_backoff_max: float = 30.0,
    gemini_rate_limit_cooldown: float = 300.0,
    gemini_rate_limit_cooldown_max: float = 3600.0,
) -> int | None:
    """Generate and publish one planned editorial slot exactly once."""
    plan_content_day(local_date, content_db=content_db, recent_card_ids=recent_card_ids)
    with ContentStore(content_db) as store:
        planned_slot = store.planned_slot(local_date, slot_key)
        if planned_slot is None or planned_slot["state"] == "skipped":
            return None
        content_type = planned_slot.get("content_type", "card")
        recipe_type = planned_slot.get("recipe_type")
        card = store.planned_card(local_date, slot_key) if content_type == "card" else None
        if content_type == "card" and card is None:
            return None
        if content_type == "recipe" and recipe_type not in RECIPE_TYPES:
            raise ContentError("planned recipe slot has no valid recipe_type")
        if content_type not in {"card", "recipe"}:
            raise ContentError(f"unsupported content type: {content_type}")
        if not store.claim_slot(local_date, slot_key, card, recipe_type=recipe_type if content_type == "recipe" else None):
            return None
        ledger = store.get_publication(local_date, slot_key)
        ledger_key = ledger["idempotency_key"] if ledger else None
    photo_path = None
    try:
        generator = AIGenerator(api_key=gemini_api_key, model=gemini_model, **gemini_runtime_options(os.environ))
        if content_type == "recipe":
            recipe_text = generator.generate_recipe(recipe_type)
            text = _with_hashtags(
                recipe_text,
                ("drink.cocktail", "format.recipe", "tone.ironic" if recipe_type == "joke" else "tone.classic"),
            )
        else:
            if card.status == "verified":
                card_text = _verified_text(card)
            else:
                generated = generator.generate_for_content(card.packet())
                card_text = f"{generated.label}: {generated.body}"
            text = _with_hashtags(card_text, card.tags)
        if with_photos:
            if card and card.image_url:
                # Картинки карточек задаёт редактор, поэтому без allowlist хостов;
                # остальные SSRF-проверки сохраняются.
                photo_path = download_image(card.image_url, allowed_hosts=())
            else:
                image_title = card.title if card else "Коктейльный рецепт"
                image_tags = card.tags if card else ("drink.cocktail", "format.recipe")
                photo_path = _generated_image(
                    _image_prompt(image_title, text, tags=image_tags),
                    image_provider=image_provider, image_model=image_model,
                    image_timeout=image_timeout, image_base_url=image_base_url,
                    cloudflare_account_id=cloudflare_account_id,
                    cloudflare_api_token=cloudflare_api_token,
                    cloudflare_model=cloudflare_model,
                    cloudflare_timeout=cloudflare_timeout,
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
    except AIRateLimitError as exc:
        with ContentStore(content_db) as store:
            store.mark_failed(local_date, slot_key, str(exc), expected_idempotency_key=ledger_key)
        raise
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
    """Create or refresh the six-slot plan, preserving completed slot targets."""
    with ContentStore(content_db) as store:
        existing = store.get_plan(local_date)
        expected_keys = {key for key, _time, _requirements, _strict in SLOT_DEFINITIONS}
        existing_keys = {str(row["slot_key"]) for row in existing}
        drink_row = next((row for row in existing if row["slot_key"] == "drink"), None)
        saved_recipe_type = drink_row.get("recipe_type") if drink_row and drink_row.get("content_type") == "recipe" else None
        current = (
            len(existing) == len(SLOT_DEFINITIONS)
            and existing_keys == expected_keys
            and all(row["plan_version"] == PLAN_VERSION for row in existing)
            and saved_recipe_type in RECIPE_TYPES
        )
        if current:
            return existing

        recipe_type = saved_recipe_type if saved_recipe_type in RECIPE_TYPES else random.choice(sorted(RECIPE_TYPES))
        planned = build_plan(
            local_date,
            store.list_cards(local_date[5:]),
            recent_card_ids,
            recipe_type=recipe_type,
        )
        restored: list[PlannedSlot] = []
        for slot in planned:
            ledger = store.get_publication(local_date, slot.slot_key)
            if ledger and ledger["state"] in {"published", "unknown", "publishing"}:
                if ledger["card_id"] == RECIPE_LEDGER_CARD_ID:
                    previous = next((row for row in existing if row["slot_key"] == slot.slot_key), {})
                    previous_type = previous.get("recipe_type")
                    restored.append(PlannedSlot(
                        slot.local_date, slot.slot_key, slot.local_time, None, slot.score,
                        slot.reason, slot.state, "recipe", previous_type if previous_type in RECIPE_TYPES else recipe_type,
                    ))
                    continue
                try:
                    card = store.get_card(ledger["card_id"], ledger["revision"])
                except ContentError:
                    card = None
                if card is not None:
                    restored.append(PlannedSlot(
                        slot.local_date, slot.slot_key, slot.local_time, card.card_id, slot.score,
                        "preserved completed publication", slot.state, "card", None,
                    ))
                    continue
            restored.append(slot)
        store.save_plan(local_date, restored)
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
    gemini_api_key: str,
    max_holidays: int = 3,
    gemini_model: str = "gemini-3.5-flash-lite",
    with_photos: bool = False,
    registry_path: str | None = None,
    registry_mode: str = "auto",
    timezone_name: str = DEFAULT_BOT_TIMEZONE,
    image_provider: str = "none",
    image_base_url: str = DEFAULT_IMAGE_BASE_URL,
    image_model: str = DEFAULT_IMAGE_MODEL,
    image_timeout: int = DEFAULT_IMAGE_TIMEOUT,
    image_fallback_to_text: bool = True,
    cloudflare_account_id: str = "",
    cloudflare_api_token: str = "",
    cloudflare_model: str = "@cf/black-forest-labs/flux-1-schnell",
    cloudflare_timeout: int = 60,
) -> List[int]:
    """Основной цикл: собрать праздники, сгенерировать и опубликовать посты."""
    generator = AIGenerator(api_key=gemini_api_key, model=gemini_model, **gemini_runtime_options(os.environ))
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
                        _image_prompt(item.title, text),
                        image_provider=image_provider, image_model=image_model,
                        image_timeout=image_timeout, image_base_url=image_base_url,
                        cloudflare_account_id=cloudflare_account_id,
                        cloudflare_api_token=cloudflare_api_token,
                        cloudflare_model=cloudflare_model,
                        cloudflare_timeout=cloudflare_timeout,
                    )
            post_id = _post_text_with_photo_fallback(publisher, text, photo_path)
            post_ids.append(post_id)
        except AIRateLimitError:
            raise
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
    gemini_api_key = os.getenv("GEMINI_API_KEY", "")

    try:
        runtime = validate_runtime_config()
        max_holidays = int(runtime["max_holidays"])
        timezone_name = str(runtime["bot_timezone"])
        parsed_group_id = int(runtime["vk_group_id"])
    except ValueError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return 1

    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    with_photos = env_bool(os.getenv("WITH_PHOTOS"))
    registry_path = os.getenv("SOURCE_REGISTRY_DB", DEFAULT_DB_PATH)
    registry_mode = os.getenv("SOURCE_REGISTRY_MODE", "auto").strip().lower()

    try:
        run_daily(
            vk_token=vk_token,
            vk_group_id=parsed_group_id,
            gemini_api_key=gemini_api_key,
            max_holidays=max_holidays,
            gemini_model=gemini_model,
            with_photos=with_photos,
            registry_path=registry_path,
            registry_mode=registry_mode,
            timezone_name=timezone_name,
            image_provider=str(runtime["image_provider"]),
            image_base_url=str(runtime["image_base_url"]),
            image_model=str(runtime["image_model"]),
            image_timeout=int(runtime["image_timeout"]),
            image_fallback_to_text=bool(runtime["image_fallback_to_text"]),
            cloudflare_account_id=str(runtime["cloudflare_account_id"]),
            cloudflare_api_token=str(runtime["cloudflare_api_token"]),
            cloudflare_model=str(runtime["cloudflare_model"]),
            cloudflare_timeout=int(runtime["cloudflare_timeout"]),
        )
    except (AIRateLimitError, AIError, VKError, SourceError, OSError) as exc:
        logger.error("Ошибка выполнения: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
