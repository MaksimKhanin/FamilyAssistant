# Сервер: веб-панель, агентское ядро, Telegram-бот и планировщик — один образ,
# три разных команды запуска (см. docker-compose.yml).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# libglib/libgl — то, чего не хватает opencv-python-headless в slim-образе
# (превью для архива декодирует jpeg и первый кадр mp4).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libglib2.0-0 libgl1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
# Миграции — чтобы `alembic upgrade head` работал прямо из контейнера.
COPY alembic.ini .
COPY migrations ./migrations

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
