"""Оркестратор бота: собрать праздники → сгенерировать посты → опубликовать."""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from datetime import date
from typing import Iterable, List, Optional

import requests
from dotenv import load_dotenv

from .ai_generator import AIGenerator, AIError
from .content_planner import build_plan
from .content_store import PLAN_VERSION, ContentCard, ContentError, ContentStore, DEFAULT_CONTENT_DB
from .holidays_parser import HolidaysParser, Holiday
from .source_registry import DEFAULT_DB_PATH, SourceRegistry
from .sources import SourceError, SourceItem, deduplicate_source_items, validate_url
from .vk_publisher import VKError, VKPublisher

logger = logging.getLogger(__name__)

CONTENT_LABELS = {"verified": "ФАКТ", "unverified": "МИФ", "disputed": "РАЗБОР", "refuted": "РАЗБОР", "rejected": "МИФ"}
SOURCE_REGISTRY_MODES = {"auto", "legacy", "registry"}
IMAGE_HOSTS = ("calend.ru", "www.calend.ru")
MAX_IMAGE_BYTES = 10 * 1024 * 1024


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
) -> int | None:
    """Generate and publish one planned editorial slot exactly once."""
    with ContentStore(content_db) as store:
        card = store.planned_card(local_date, slot_key)
        if card is None:
            cards = store.list_cards(local_date[5:])
            planned = build_plan(local_date, cards, recent_card_ids)
            store.save_plan(local_date, planned)
            card = store.planned_card(local_date, slot_key)
        if card is None or not store.claim_slot(local_date, slot_key, card):
            return None
    photo_path = None
    try:
        generator = AIGenerator(api_key=mistral_api_key, model=mistral_model)
        if card.status == "verified":
            text = _verified_text(card)
        else:
            generated = generator.generate_for_content(card.packet())
            text = f"{generated.label}: {generated.body}"
        if with_photos and card.image_url:
            # Картинки карточек задаёт редактор, поэтому без allowlist хостов;
            # остальные SSRF-проверки (https, без редиректов, без приватных IP) остаются.
            photo_path = download_image(card.image_url, allowed_hosts=())
        publisher = VKPublisher(token=vk_token, group_id=vk_group_id)
        post_id = publisher.post(message=text, photo_path=photo_path)
        with ContentStore(content_db) as store:
            store.mark_published(local_date, slot_key, post_id, text)
        return post_id
    except (AIError, VKError, OSError, ContentError) as exc:
        with ContentStore(content_db) as store:
            store.mark_failed(local_date, slot_key, str(exc))
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
) -> List[int]:
    """Основной цикл: собрать праздники, сгенерировать и опубликовать посты."""
    generator = AIGenerator(api_key=mistral_api_key, model=mistral_model)
    publisher = VKPublisher(token=vk_token, group_id=vk_group_id)

    today = today_str()
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
            if with_photos and item.image_url:
                photo_path = download_image(item.image_url)
            post_id = publisher.post(message=text, photo_path=photo_path)
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
    with_photos = env_bool(os.getenv("WITH_PHOTOS"))
    registry_path = os.getenv("SOURCE_REGISTRY_DB", DEFAULT_DB_PATH)
    registry_mode = os.getenv("SOURCE_REGISTRY_MODE", "auto").strip().lower()

    try:
        run_daily(
            vk_token=vk_token,
            vk_group_id=int(vk_group_id),
            mistral_api_key=mistral_api_key,
            max_holidays=max_holidays,
            mistral_model=mistral_model,
            with_photos=with_photos,
            registry_path=registry_path,
            registry_mode=registry_mode,
        )
    except (AIError, VKError, SourceError, OSError) as exc:
        logger.error("Ошибка выполнения: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
