FROM ghcr.io/astral-sh/uv:0.11.29 AS uv
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
# The read-only runtime must not need network access or a writable home for tokenizers.
ENV TIKTOKEN_CACHE_DIR=/app/tiktoken-cache
RUN .venv/bin/python -c "import tiktoken; [tiktoken.get_encoding(name) for name in ('o200k_base', 'cl100k_base', 'o200k_harmony', 'p50k_base', 'p50k_edit', 'r50k_base', 'gpt2')]"
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY scripts/entrypoint.sh /entrypoint.sh
RUN groupadd --gid 10001 deadline \
    && useradd --uid 10001 --gid deadline --no-create-home deadline \
    && mkdir -p /app/data \
    && chown deadline:deadline /app/data \
    && chmod 700 /app/data \
    && chmod 755 /entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 PORT=8000
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"]
ENTRYPOINT ["/entrypoint.sh"]
