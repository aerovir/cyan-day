"""Генерация ироничных алкогольных постов через Mistral API.

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
from typing import Any, Mapping

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.mistral.ai/v1/chat/completions"
DEFAULT_MODEL = "mistral-small-latest"

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


@dataclass(frozen=True)
class GeneratedContent:
    label: str
    body: str
    claims_used: tuple[str, ...]
    unsupported_claims: tuple[str, ...]
    visual_brief: str = ""


_STATUS_LABELS = {
    "unverified": "МИФ",
    "disputed": "РАЗБОР",
    "refuted": "РАЗБОР",
    "rejected": "МИФ",
}


class AIGenerator:
    """Клиент Mistral для текстов и редакционного контента."""

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
            'Верни только JSON: {"label":"МИФ|РАЗБОР","body":"3-5 предложений",'
            '"claims_used":["claim_id"],"unsupported_claims":[],"visual_brief":"без текста"}'
        )
        raw = self.generate(prompt)
        try:
            data = json.loads(raw)
            actual = str(data["label"])
            body = str(data["body"]).strip()
            used = tuple(str(value) for value in data["claims_used"])
            unsupported = tuple(str(value) for value in data["unsupported_claims"])
            brief = str(data.get("visual_brief", "")).strip()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise AIError("Mistral returned invalid content JSON") from exc
        known = {str(claim.get("claim_id")) for claim in claims or []}
        if actual != label or not body or unsupported or any(value not in known for value in used):
            raise AIError("Mistral content failed editorial validation")
        return GeneratedContent(actual, body, used, unsupported, brief)

    def generate_for_content(self, card: Mapping[str, Any]) -> GeneratedContent:
        return self.generate_for_status(
            str(card.get("status", "unverified")),
            str(card.get("title", "")),
            str(card.get("summary", "")),
            list(card.get("claims", [])),
        )


    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_retries: int = 3,
        timeout: int = 60,
    ) -> None:
        """Инициализировать генератор.

        Args:
            api_key: API-ключ Mistral (или пустая строка).
            model: Имя модели Mistral.
            max_retries: Сколько раз повторить запрос при ошибках 429/5xx.
            timeout: Таймаут HTTP-запроса, секунд.
        """
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.timeout = timeout

    def generate(self, user_prompt: str) -> str:
        """Сгенерировать текст по пользовательскому промпту.

        Args:
            user_prompt: Текст запроса (с данными о празднике).

        Returns:
            Сгенерированный текст.

        Raises:
            AIError: При ошибке API или отсутствии ключа.
        """
        if not self.api_key:
            raise AIError("MISTRAL_API_KEY не задан")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 500,
        }

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    API_URL, headers=headers, json=payload, timeout=self.timeout
                )
                if resp.status_code == 200:
                    return self._extract_content(resp.json())
                # Повторяем при rate-limit и серверных ошибках
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "Mistral вернул %s, повторяю через %ss (%d/%d)",
                        resp.status_code, wait, attempt, self.max_retries,
                    )
                    time.sleep(wait)
                    continue
                raise AIError(f"Mistral API вернул {resp.status_code}: {resp.text[:200]}")
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    wait = 2 ** attempt
                    logger.warning("Сетевая ошибка Mistral: %s, повтор через %ss", exc, wait)
                    time.sleep(wait)
                    continue
                raise AIError(f"Сетевая ошибка Mistral: {exc}") from exc

        raise AIError(f"Не удалось получить ответ от Mistral: {last_exc}")

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
        """Достать текст из JSON-ответа Mistral."""
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(f"Неожиданный формат ответа Mistral: {data}") from exc


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
        api_key: API-ключ Mistral. Если None — берётся из env MISTRAL_API_KEY.
        model: Модель Mistral.

    Returns:
        Текст поста.

    Raises:
        AIError: При ошибке генерации.
    """
    key = api_key or os.getenv("MISTRAL_API_KEY", "")
    generator = AIGenerator(api_key=key, model=model)
    prompt = USER_PROMPT_TEMPLATE.format(
        title=holiday_title,
        description=holiday_description[:1500],
    )
    return generator.generate(prompt)


def _smoke_test() -> int:
    """Смоук-тест для GitHub Actions: проверить, что ключ работает."""
    api_key = os.getenv("MISTRAL_API_KEY", "")
    if not api_key:
        print("MISTRAL_API_KEY не задан", file=sys.stderr)
        return 1
    try:
        text = generate_alcohol_post(
            holiday_title="День гранёного стакана",
            holiday_description="День гранёного стакана отмечается 11 сентября. "
            "Этот стакан стал символом СССР.",
            api_key=api_key,
        )
        print("OK. Сгенерированный текст:\n", text)
        return 0
    except AIError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="Смоук-тест Mistral")
    args = parser.parse_args()
    if args.smoke:
        sys.exit(_smoke_test())
