FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /bin/uv
WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN SECRET_KEY=build-only DEBUG=false python manage.py collectstatic --noinput

RUN useradd --system --no-create-home app
USER app

EXPOSE 8000
# Threads, and a timeout above the longest Claude call (Ask my data, weekly review).
CMD ["gunicorn", "config.wsgi", "--bind", "0.0.0.0:8000", "--worker-class", "gthread", "--workers", "2", "--threads", "4", "--timeout", "120", "--access-logfile", "-"]
