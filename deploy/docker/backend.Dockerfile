# 后端镜像供 API、Worker、Scheduler 和迁移任务复用。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先复制依赖元数据，利用 Docker 层缓存减少重复安装。
COPY pyproject.toml ./
COPY deploy/docker/package-readme.md ./README.md
COPY src/campus_reserve/__init__.py ./src/campus_reserve/__init__.py
RUN pip install --no-cache-dir ".[dev]"

# 源码和迁移脚本在安装依赖后复制；运行时使用非 root 用户。
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY apps ./apps
COPY tests ./tests

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

ENV PYTHONPATH=/app/src:/app
