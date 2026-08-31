FROM python:3.12-slim

# Локаль для корректной работы с русским текстом
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# Сначала зависимости — кэшируются слоем Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом код
COPY app/ ./app/
COPY data/ ./data/
COPY main.py .
COPY scheduler.py .

CMD ["python", "scheduler.py"]
