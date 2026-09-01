FROM python:3.12-slim

# Локаль для корректной работы с русским текстом
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# Запуск от непривилегированного пользователя; state и logs остаются writable.
RUN addgroup --system bot \
    && adduser --system --ingroup bot bot \
    && mkdir -p /app/logs /app/state \
    && chown -R bot:bot /app

# Сначала зависимости — кэшируются слоем Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом код
COPY --chown=bot:bot app/ ./app/
COPY --chown=bot:bot data/ ./data/
COPY --chown=bot:bot main.py .
COPY --chown=bot:bot scheduler.py .

USER bot

# Проверяем, что планировщик жив (в Compose PID 1 может быть init-процессом).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import glob, sys; sys.exit(0 if any(b'scheduler.py' in open(path, 'rb').read() for path in glob.glob('/proc/[0-9]*/cmdline')) else 1)"]

CMD ["python", "scheduler.py"]

# The healthcheck is also declared in Compose for consistent local status reporting.
