#!/bin/sh
# Container entrypoint: apply Alembic migrations, then exec the requested command.
#
# Running migrations here (instead of at build time) means the running image can
# be pointed at any SQLite/Postgres URL supplied via DATABASE_URL and will bring
# the schema up to head on first boot.
set -eu

echo "[signalweek] applying database migrations..."
alembic -c /app/alembic.ini upgrade head

echo "[signalweek] launching: $*"
exec "$@"
