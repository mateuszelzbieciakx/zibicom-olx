# syntax=docker/dockerfile:1

# uv jako binarka z oficjalnego obrazu - bez apt-get w warstwie runtime.
FROM ghcr.io/astral-sh/uv:0.11.31 AS uv

FROM python:3.14-slim AS runtime

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Zaleznosci w osobnej warstwie - zmiana kodu nie uniewaznia cache.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

RUN useradd --system --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", \
         "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]

CMD ["python", "-m", "zibicom"]
