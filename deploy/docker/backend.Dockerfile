FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY deploy/docker/package-readme.md ./README.md
COPY src/campus_reserve/__init__.py ./src/campus_reserve/__init__.py
RUN pip install --no-cache-dir ".[dev]"

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY apps ./apps
COPY tests ./tests

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

ENV PYTHONPATH=/app/src:/app
