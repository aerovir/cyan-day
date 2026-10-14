"""Генерация ироничных алкогольных постов через Gemini API.

Принимает реальные данные о празднике (название + описание с calend.ru)
и перерабатывает их в юмористический текст в алкогольной тематике.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import math
from typing import Any, Mapping

import requests

logger = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
PROVIDER_NAME = "Gemini"
API_KEY_ENV = "GEMINI_API_KEY"
MODEL_ENV = "GEMINI_MODEL"

# Системный промпт задаёт стиль: ирония, сарказм, алкогольная тематика.
SYSTEM_PROMPT = (
    "Ты — автор юмористического паблика про алкоголь в ВК. "
    "Пиши коротко, иронично и с лёгким сарказмом, по-русски. "
    "Каждый пост обыгрывает праздник в алкогольном ключе. "
    "Формат: 2–4 предложения. Не используй заголовков и списков. "
    "Не выдумывай факты: используй только сведения из переданного описания "
    "и не добавляй неподтверждённые даты, имена, цифры, места или события. "
    "Перед ответом проверь, что каждое фактическое утверждение опирается на описание; "
    "если данных недостаточно, шути только на подтверждённых деталях и не заполняй пробелы догадками."
)

USER_PROMPT_TEMPLATE = (
    "Сегодня праздник: «{title}».\n"
    "Описание: {description}\n\n"
    "Перепиши описание в своём фирменном иронично-алкогольном стиле."
)


class AIError(Exception):
    """Ошибка взаимодействия с AI-сервисом."""


class AIRateLimitError(AIError):
    """Gemini rejected the request because the rate limit was exceeded."""

    def __init__(self, message: str, *, cooldown_seconds: float, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.cooldown_seconds = float(cooldown_seconds)
        self.retry_after = retry_after


def _parse_retry_after(value: object, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = int(text)
        return float(seconds) if seconds >= 0 else None
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        reference = now or datetime.now(timezone.utc)
        return max(0.0, (parsed - reference).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class GeneratedContent:
    label: str
    body: str
    claims_used: tuple[str, ...]
    unsupported_claims: tuple[str, ...]
    visual_brief: str = ""


def _recipe_ingredient_rows(value: object, field: str, problems: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        problems.append(f"{field} must contain 1-12 ingredients")
        return []
    rows = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            problems.append(f"{field}[{index}] must be an object")
            continue
        name = row.get("name")
        unit = row.get("unit")
        raw_amount = row.get("amount")
        try:
            amount = Decimal(str(raw_amount))
        except (InvalidOperation, TypeError, ValueError):
            amount = Decimal("0")
        if isinstance(raw_amount, bool) or not amount.is_finite() or amount <= 0:
            problems.append(f"{field}[{index}].amount must be positive")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{field}[{index}].name must not be empty")
        if not isinstance(unit, str) or not unit.strip():
            problems.append(f"{field}[{index}].unit must not be empty")
        rows.append({
            "name": name.strip() if isinstance(name, str) else "",
            "amount": amount,
            "unit": unit.strip() if isinstance(unit, str) else "",
        })
    return rows


def _recipe_problems(data: object, recipe_type: str) -> list[str]:
    if not isinstance(data, dict):
        return ["response must be a JSON object"]
    problems: list[str] = []
    if data.get("recipe_type") != recipe_type:
        problems.append("recipe_type does not match the requested type")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        problems.append("title must not be empty")
    if not isinstance(data.get("caption"), str) or not data["caption"].strip():
        problems.append("caption must not be empty")
    instructions = data.get("instructions")
    if not isinstance(instructions, list) or not 1 <= len(instructions) <= 8 or any(not isinstance(step, str) or not step.strip() for step in instructions):
        problems.append("instructions must contain 1-8 non-empty steps")
    original = _recipe_ingredient_rows(data.get("original_ingredients"), "original_ingredients", problems)
    ingredients = _recipe_ingredient_rows(data.get("ingredients"), "ingredients", problems)
    if original and ingredients:
        if len(original) != len(ingredients):
            problems.append("ingredient count must not change")
        changed: list[tuple[str, str]] = []
        for before, after in zip(original, ingredients):
            if before["amount"] != after["amount"] or before["unit"].casefold() != after["unit"].casefold():
                problems.append("ingredient amounts and units must remain unchanged")
            if before["name"].casefold() != after["name"].casefold():
                changed.append((before["name"], after["name"]))
        substitutions = data.get("substitutions")
        if not isinstance(substitutions, list):
            problems.append("substitutions must be an array")
            substitutions = []
        pairs = []
        for item in substitutions:
            if not isinstance(item, Mapping) or not isinstance(item.get("from"), str) or not isinstance(item.get("to"), str):
                problems.append("each substitution needs from/to names")
                continue
            pairs.append((item["from"].strip(), item["to"].strip()))
        if recipe_type == "joke":
            if not 1 <= len(changed) <= 2:
                problems.append("joke recipe must change exactly 1-2 ingredient names")
            if len(pairs) != len(changed) or set(pairs) != set(changed):
                problems.append("substitutions must match the changed ingredient names")
        else:
            if changed or pairs:
                problems.append("classic recipe must not change ingredients")
    return problems


def _format_recipe_amount(amount: Decimal) -> str:
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _render_recipe(data: Mapping[str, Any]) -> str:
    lines = [str(data["title"]).strip(), str(data["caption"]).strip(), "", "Ингредиенты:"]
    for item in data["ingredients"]:
        amount = Decimal(str(item["amount"]))
        lines.append(f"• {_format_recipe_amount(amount)} {str(item['unit']).strip()} {str(item['name']).strip()}")
    lines.extend(("", "Приготовление:"))
    lines.extend(f"{index}. {step.strip()}" for index, step in enumerate(data["instructions"], 1))
    return "\n".join(lines)


_STATUS_LABELS = {
    "unverified": "МИФ",
    "disputed": "РАЗБОР",
    "refuted": "РАЗБОР",
    "rejected": "МИФ",
}


class AIGenerator:
    """Клиент Gemini для текстов и редакционного контента."""

    def generate_for_status(
        self,
        status: str,
        title: str,
        description: str,
        claims: list[Mapping[str, Any]] | None = None,
    ) -> GeneratedContent:
        """Generate explicitly labelled copy for non-verified material."""
        try:
            label = _STATUS_LABELS[status]
        except KeyError as exc:
            raise AIError(f"unsupported editorial status: {status}") from exc
        claim_text = "\\n".join(f"- {claim.get('claim_id')}: {claim.get('text', '')}" for claim in claims or [])
        prompt = (
            "Паблик «Синий день календаря». Сохрани ироничный стиль и лёгкий алкогольный юмор.\\n"
            f"Статус материала: {status}. Обязательная маркировка: {label}.\\n"
            "Исходное утверждение является неподтверждённым, спорным или отвергнутым; не выдавай его за факт.\\n"
            "Используй только данные между SOURCE_FACTS и END_SOURCE_FACTS. Не выдумывай даты, имена, цифры, места или события.\\n"
            f"SOURCE_FACTS\\nЗаголовок: {title}\\nОписание: {description[:1500]}\\n{claim_text}\\nEND_SOURCE_FACTS\\n"
            f'Верни только JSON без пояснений и ограждений. Схема: {{"label":"{label}","body":"3-5 предложений",'
            '"claims_used":["сюда — только идентификаторы claim_id из списка SOURCE_FACTS, использованные в тексте, например \\"c1\\" — без текста утверждений"],'
            '"unsupported_claims":["сюда — только идентификаторы claim_id, которые не подтверждаются фактами; обычно пустой массив"],'
            '"visual_brief":"без текста"}'
        )
        raw = self.generate(prompt, response_format="json_object")
        parsed = self._parse_status_response(raw)
        known = {str(claim.get("claim_id")) for claim in claims or []}
        problems = self._status_problems(parsed, label, known)
        if problems:
            # Одна повторная попытка с явным указанием формата
            correction = (
                '\n\nИсправь формат ответа: верни только JSON; "label" должен быть ровно "' + label + '"; '
                '"claims_used" и "unsupported_claims" — только идентификаторы claim_id из списка SOURCE_FACTS '
                '(например, "c1"), без текста утверждений.'
            )
            raw = self.generate(prompt + correction, response_format="json_object")
            parsed = self._parse_status_response(raw)
            problems = self._status_problems(parsed, label, known)
        if parsed is None:
            raise AIError("Gemini returned invalid content JSON")
        if problems:
            raise AIError("Gemini content failed editorial validation: " + "; ".join(problems) + f" | raw: {raw[:300]}")
        actual, body, used, unsupported, brief = parsed
        # Метаданные claims не попадают в пост — только в лог: модель может честно
        # помечать миф-claims как неподтверждённые или изредка придумать id.
        if unsupported:
            logger.warning("Gemini пометил неподтверждёнными claims: %s", list(unsupported))
        unknown_used = [value for value in used if value not in known]
        if unknown_used:
            logger.warning("Gemini указал неизвестные claims_used: %s", unknown_used)
        return GeneratedContent(actual, body, used, unsupported, brief)

    @staticmethod
    def _parse_status_response(raw: str) -> tuple[str, str, tuple[str, ...], tuple[str, ...], str] | None:
        """Распарсить JSON-ответ статусной генерации; None при невалидной структуре."""
        try:
            data = _parse_json_block(raw)
            actual = str(data["label"])
            body = str(data["body"]).strip()
            used = tuple(str(value) for value in data["claims_used"])
            unsupported = tuple(str(value) for value in data["unsupported_claims"])
            brief = str(data.get("visual_brief", "")).strip()
            return actual, body, used, unsupported, brief
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _status_problems(parsed, label: str, known: set[str]) -> list[str]:
        """Блокирующие редакционные несоответствия ответа модели.

        Только маркировка и пустой текст: метаданные claims в пост не
        попадают и проверяются отдельно (см. generate_for_status).
        """
        if parsed is None:
            return ["invalid JSON structure"]
        actual, body, _used, _unsupported, _brief = parsed
        problems = []
        if actual != label:
            problems.append(f"label {actual!r} != {label!r}")
        if not body:
            problems.append("empty body")
        return problems

    def generate_for_content(self, card: Mapping[str, Any]) -> GeneratedContent:
        return self.generate_for_status(
            str(card.get("status", "unverified")),
            str(card.get("title", "")),
            str(card.get("summary", "")),
            list(card.get("claims", [])),
        )

    def generate_recipe(self, recipe_type: str) -> str:
        """Generate a structured cocktail recipe with mode-specific constraints."""
        if recipe_type not in {"joke", "classic"}:
            raise AIError("unsupported recipe type")
        if recipe_type == "joke":
            system_instruction = (
                "Ты пишешь шуточные рецепты коктейлей на русском языке. "
                "Сохраняй все меры и пропорции буквально; юмор — в названиях, подаче и шагах."
            )
            requirements = (
                "Выбери известный базовый коктейль. Верни его исходный состав в original_ingredients. "
                "В ingredients замени ровно 1 или 2 названия ингредиентов на близкие комические аналоги. "
                "Для каждой позиции строго сохрани amount и unit исходной позиции; не добавляй, не удаляй и не переставляй позиции. "
                "substitutions должен перечислять пары from/to для каждой замены и только для них. "
                "caption и instructions пиши иронично; инструкции не повторяют дозировки и не вводят новые ингредиенты."
            )
        else:
            system_instruction = (
                "Ты редактор классических рецептов коктейлей. Пиши по-русски нейтрально и точно, без шуток и без замен."
            )
            requirements = (
                "Выбери известный классический коктейль и его стандартные пропорции. "
                "Скопируй один и тот же состав в original_ingredients и ingredients, без добавления, удаления или замены ингредиентов. "
                "substitutions должен быть пустым массивом. "
                "caption и instructions нейтральные; инструкции не повторяют дозировки и не вводят новые ингредиенты."
            )
        prompt = (
            f"Тип рецепта: {recipe_type}. {requirements}\n"
            "Количество каждого ингредиента должно быть положительным числом, unit — непустой единицей измерения.\n"
            'Верни только JSON: {"recipe_type":"joke|classic","title":"название коктейля",'
            '"caption":"короткое описание","original_ingredients":[{"name":"...","amount":45,"unit":"мл"}],'
            '"ingredients":[{"name":"...","amount":45,"unit":"мл"}],'
            '"substitutions":[{"from":"...","to":"..."}],"instructions":["шаг приготовления"]}. '
            "Для classic substitutions=[] и составы полностью совпадают. Для joke original_ingredients — база, "
            "ingredients — результат с 1–2 заменами; меры и порядок совпадают."
        )
        correction = ""
        last_problems: list[str] = []
        for _attempt in range(2):
            raw = self.generate(
                prompt + correction,
                response_format="json_object",
                system_instruction=system_instruction,
                max_output_tokens=1200,
            )
            try:
                data = _parse_json_block(raw)
            except (json.JSONDecodeError, TypeError):
                data = None
            last_problems = _recipe_problems(data, recipe_type)
            if not last_problems:
                return _render_recipe(data)
            correction = "\n\nИсправь JSON и соблюди ограничения: " + "; ".join(last_problems) + ". Верни только JSON."
        raise AIError("Gemini returned invalid recipe: " + "; ".join(last_problems))


    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_retries: int = 3,
        timeout: int = 60,
        rate_limit_retries: int = 2,
        transient_retries: int = 2,
        backoff_base: float = 2.0,
        backoff_max: float = 30.0,
        rate_limit_cooldown: float = 300.0,
        rate_limit_cooldown_max: float = 3600.0,
        retry_sleep=None,
    ) -> None:
        """Инициализировать генератор.

        Args:
            api_key: API-ключ Gemini (или пустая строка).
            model: Имя модели Gemini.
            max_retries: Сколько раз повторить запрос при ошибках 429/5xx.
            timeout: Таймаут HTTP-запроса, секунд.
        """
        if max_retries < 1 or rate_limit_retries < 0 or transient_retries < 0:
            raise ValueError("retry limits are invalid")
        if backoff_base <= 0 or backoff_max < backoff_base or rate_limit_cooldown <= 0 or rate_limit_cooldown_max < rate_limit_cooldown:
            raise ValueError("retry delays are invalid")
        normalized_model = str(model).strip().removeprefix("models/")
        if not normalized_model or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in normalized_model):
            raise ValueError("GEMINI_MODEL must be a model ID")
        self.api_key = api_key
        self.model = normalized_model
        self.max_retries = max_retries
        self.timeout = timeout
        self.rate_limit_retries = rate_limit_retries
        self.transient_retries = transient_retries
        self.backoff_base = float(backoff_base)
        self.backoff_max = float(backoff_max)
        self.rate_limit_cooldown = float(rate_limit_cooldown)
        self.rate_limit_cooldown_max = float(rate_limit_cooldown_max)
        self._sleep = retry_sleep or time.sleep

    def generate(
        self,
        user_prompt: str,
        *,
        response_format: str | None = None,
        system_instruction: str | None = None,
        max_output_tokens: int = 500,
    ) -> str:
        """Сгенерировать текст по пользовательскому промпту.

        Args:
            user_prompt: Текст запроса (с данными о празднике).
            response_format: Передать в API как response_format.type
                (например, "json_object" для структурного ответа).

        Returns:
            Сгенерированный текст.

        Raises:
            AIError: При ошибке API или отсутствии ключа.
        """
        if not self.api_key:
            raise AIError("GEMINI_API_KEY не задан")

        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        payload = {
            "systemInstruction": {"parts": [{"text": system_instruction or SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output_tokens},
        }
        if response_format == "json_object":
            payload["generationConfig"]["responseMimeType"] = "application/json"

        last_exc: Exception | None = None
        rate_limits = 0
        transient = 0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(API_URL.format(model=self.model), headers=headers, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    return self._extract_content(resp.json())
                if resp.status_code == 429:
                    retry_value = getattr(resp, "headers", {}).get("Retry-After")
                    server_wait = _parse_retry_after(retry_value)
                    retry_index = rate_limits
                    wait = min(self.backoff_base * (2 ** retry_index), self.backoff_max)
                    if server_wait is not None:
                        wait = min(server_wait, self.backoff_max)
                    rate_limits += 1
                    if attempt >= self.max_retries or rate_limits > self.rate_limit_retries:
                        cooldown = min(max(self.rate_limit_cooldown, wait), self.rate_limit_cooldown_max)
                        raise AIRateLimitError(f"Gemini API вернул 429: {resp.text[:200]}", cooldown_seconds=cooldown, retry_after=str(retry_value) if retry_value is not None else None)
                    logger.warning("Gemini вернул 429, повторяю через %.1fs (%d/%d)", wait, attempt, self.max_retries)
                    self._sleep(wait)
                    continue
                if resp.status_code in (500, 502, 503, 504) and attempt < self.max_retries and transient < self.transient_retries:
                    wait = min(self.backoff_base * (2 ** transient), self.backoff_max)
                    transient += 1
                    logger.warning("Gemini вернул %s, повторяю через %.1fs (%d/%d)", resp.status_code, wait, attempt, self.max_retries)
                    self._sleep(wait)
                    continue
                raise AIError(f"Gemini API вернул {resp.status_code}: {resp.text[:200]}")
            except AIRateLimitError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries and transient < self.transient_retries:
                    wait = min(self.backoff_base * (2 ** transient), self.backoff_max)
                    transient += 1
                    logger.warning("Сетевая ошибка Gemini: %s, повтор через %.1fs", exc, wait)
                    self._sleep(wait)
                    continue
                raise AIError(f"Сетевая ошибка Gemini: {exc}") from exc

        raise AIError(f"Не удалось получить ответ от Gemini: {last_exc}")

    def generate_for_holiday(self, holiday) -> str:
        """Сгенерировать пост по объекту праздника.

        Args:
            holiday: Объект Holiday (title, description, image_url).

        Returns:
            Текст поста.
        """
        prompt = USER_PROMPT_TEMPLATE.format(
            title=holiday.title,
            description=holiday.description[:1500],
        )
        return self.generate(prompt)

    @staticmethod
    def _extract_content(data: dict) -> str:
        """Достать текст из JSON-ответа Gemini."""
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(str(part["text"]) for part in parts if isinstance(part, dict) and "text" in part).strip()
            if not text:
                raise ValueError("empty candidate text")
            return text
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AIError(f"Неожиданный формат ответа Gemini: {data}") from exc


def _parse_json_block(raw: str) -> dict:
    """Извлечь JSON-объект из ответа модели, включая ```json```-ограждения."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(raw[start : end + 1])


def generate_alcohol_post(
    holiday_title: str,
    holiday_description: str,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    """Удобная функция: сгенерировать ироничный алкогольный пост о празднике.

    Args:
        holiday_title: Название праздника.
        holiday_description: Описание праздника (с calend.ru).
        api_key: API-ключ Gemini. Если None — берётся из env GEMINI_API_KEY.
        model: Модель Gemini.

    Returns:
        Текст поста.

    Raises:
        AIError: При ошибке генерации.
    """
    key = api_key or os.getenv("GEMINI_API_KEY", "")
    generator = AIGenerator(api_key=key, model=model)
    prompt = USER_PROMPT_TEMPLATE.format(
        title=holiday_title,
        description=holiday_description[:1500],
    )
    return generator.generate(prompt)


def _smoke_test() -> int:
    """Смоук-тест для GitHub Actions: проверить, что ключ работает."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        print("GEMINI_API_KEY не задан", file=sys.stderr)
        return 1
    try:
        text = generate_alcohol_post(
            holiday_title="День гранёного стакана",
            holiday_description="День гранёного стакана отмечается 11 сентября. "
            "Этот стакан стал символом СССР.",
            api_key=api_key,
            model=os.getenv(MODEL_ENV, DEFAULT_MODEL),
        )
        print("OK. Сгенерированный текст:\n", text)
        return 0
    except AIError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="Смоук-тест Gemini")
    args = parser.parse_args()
    if args.smoke:
        sys.exit(_smoke_test())
