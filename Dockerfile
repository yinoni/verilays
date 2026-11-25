# The generic Verilays worker: configure it with env vars only, no app code needed.
#   VERILAYS_DSN                the action store
#   VERILAYS_POSTGRES_<NAME>    each database that action.postgres(db="<name>") checks
#   LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL   optional: send each verdict to Langfuse
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY verilays ./verilays
COPY examples ./examples
RUN uv sync --frozen --no-dev --all-extras
ENV PATH="/app/.venv/bin:$PATH"

CMD ["verilays", "worker", "--every", "10"]
