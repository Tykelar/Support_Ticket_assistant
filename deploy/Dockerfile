# syntax=docker/dockerfile:1
#
# Multi-stage (PACKAGING.md): the build stage resolves dependencies into a venv, the
# runtime stage copies only that venv and the source. Build tooling never ships.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
# The [dev] extra is installed too, on purpose: PACKAGING.md wants
# `docker compose run --rm api pytest` to run in the same image that serves traffic.
RUN pip install -e ".[dev]"


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# A non-root user owns the app and the venv. /app/data is created here so that a fresh
# named volume mounted over it inherits this ownership on first use and stays writable.
RUN useradd --create-home --uid 1000 app

COPY --from=builder --chown=app:app /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app pyproject.toml ./
COPY --chown=app:app src/ ./src/
COPY --chown=app:app tests/ ./tests/
RUN mkdir -p /app/data && chown -R app:app /app

USER app
EXPOSE 8000

# `python:3.12-slim` has no curl, so the probe is the stdlib. urlopen raises on the 503
# path, which is what makes an unreachable database report the container unhealthy.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3).read()"]

CMD ["uvicorn", "support_assistant.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
