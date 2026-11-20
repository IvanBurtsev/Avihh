# Образ для API и воркера. Сборка не требует Node и не тянет dev-зависимости.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# uv ставится отдельным слоем: меняется редко, кэшируется хорошо.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Сначала только манифест — слой с зависимостями кэшируется отдельно от кода.
COPY pyproject.toml README.md ./
COPY src/hirebot/__init__.py src/hirebot/__init__.py

RUN uv pip install --system --no-cache -e ".[dev]" 2>/dev/null || uv pip install --system --no-cache .

COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN uv pip install --system --no-cache -e .

# Непривилегированный пользователь: контейнер не должен работать под root.
RUN useradd --create-home --uid 10001 hirebot && chown -R hirebot:hirebot /app
USER hirebot

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "hirebot.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
