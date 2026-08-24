"""Планировщик ежедневного запуска бота.

Заменяет системный cron внутри контейнера: ждёт наступления заданного
времени (POST_HOUR:POST_MINUTE) и запускает main.py. Держит контейнер живым.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("scheduler")


def _seconds_until(hour: int, minute: int) -> float:
    """Сколько секунд до ближайшего наступления hour:minute."""
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        # Если время уже прошло — берём следующий день
        from datetime import timedelta
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main() -> int:
    """Бесконечный цикл планировщика."""
    from app.main import run_daily

    hour = int(os.getenv("POST_HOUR", "9"))
    minute = int(os.getenv("POST_MINUTE", "0"))

    logger.info("Планировщик запущен. Бот будет работать ежедневно в %02d:%02d", hour, minute)

    while True:
        wait = _seconds_until(hour, minute)
        logger.info("До следующего запуска: %.0f секунд", wait)
        # Держим контейнер живым, не жжём CPU
        time.sleep(min(wait, 3600))

        # Если дошли до времени — запускаем
        now = datetime.now()
        if now.hour == hour and now.minute == minute:
            logger.info("Время постить!")
            try:
                run_daily(
                    vk_token=os.getenv("VK_TOKEN", ""),
                    vk_group_id=int(os.getenv("VK_GROUP_ID", "0")),
                    mistral_api_key=os.getenv("MISTRAL_API_KEY", ""),
                    max_holidays=int(os.getenv("MAX_HOLIDAYS", "3")),
                    mistral_model=os.getenv("MISTRAL_MODEL", "mistral-small-latest"),
                )
            except Exception as exc:  # noqa: BLE001 — планировщик не должен падать
                logger.error("Ошибка при выполнении: %s", exc)
            # Небольшая пауза, чтобы не сработать дважды
            time.sleep(60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
