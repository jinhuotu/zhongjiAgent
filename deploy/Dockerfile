# 通用运行镜像：API / chat-consumer-worker 等可复用
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/packages:/app/apps:/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml poetry.lock* README.md ./
COPY packages ./packages
COPY apps ./apps
COPY workers ./workers
COPY scripts ./scripts
COPY alembic.ini ./

RUN pip install --no-cache-dir poetry \
    && poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --no-root \
    && poetry install --no-interaction --no-ansi

CMD ["python", "workers/stream_consumer_main.py"]
