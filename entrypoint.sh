#!/bin/sh
# Точка входа: запускает cron с ежедневным заданием для бота.
# Час и минута берутся из POST_HOUR / POST_MINUTE (по умолчанию 9:00).

set -e

POST_HOUR="${POST_HOUR:-9}"
POST_MINUTE="${POST_MINUTE:-0}"

# Формируем cron-запись: каждые день в POST_HOUR:POST_MINUTE
echo "${POST_MINUTE} ${POST_HOUR} * * * cd /app && /usr/local/bin/python main.py >> /var/log/bot.log 2>&1" > /etc/cron.d/bot
chmod 0644 /etc/cron.d/bot

# Убедимся, что cron.d обрабатывается
crontab /etc/cron.d/bot
service cron start 2>/dev/null || true

echo "Бот настроен. Ежедневный запуск в ${POST_HOUR}:${POST_MINUTE}."
echo "Логи: docker logs ryabov_bot (и /var/log/bot.log в контейнере)."

# Держим контейнер живым
tail -f /dev/null
