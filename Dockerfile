# syntax=docker/dockerfile:1.7

# ---------- builder ----------
# Installs the app and its dependencies into an isolated virtualenv so the
# runtime image doesn't need pip, setuptools, or build tooling.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
 && pip install .

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATABASE_URL="sqlite:////app/data/signalweek.db"

WORKDIR /app

# Non-root user owns the code and the data volume mount point.
RUN groupadd --system --gid 1000 signalweek \
 && useradd  --system --uid 1000 --gid 1000 --home-dir /app --shell /usr/sbin/nologin signalweek \
 && mkdir -p /app/data \
 && chown -R signalweek:signalweek /app

COPY --from=builder --chown=signalweek:signalweek /opt/venv /opt/venv
COPY --chown=signalweek:signalweek alembic.ini ./alembic.ini
COPY --chown=signalweek:signalweek alembic ./alembic
COPY --chown=signalweek:signalweek src ./src
COPY --chown=signalweek:signalweek docker/entrypoint.sh /usr/local/bin/signalweek-entrypoint

RUN chmod +x /usr/local/bin/signalweek-entrypoint

USER signalweek

EXPOSE 8000

VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["signalweek-entrypoint"]
CMD ["uvicorn", "signalweek.main:app", "--host", "0.0.0.0", "--port", "8000"]
