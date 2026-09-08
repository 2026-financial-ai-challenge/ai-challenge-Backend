#!/bin/sh
set -eu

attempt=1
max_attempts=10

until alembic upgrade head; do
  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "Database migration failed after $max_attempts attempts." >&2
    exit 1
  fi

  echo "Database is not ready; retrying migration ($attempt/$max_attempts)..." >&2
  attempt=$((attempt + 1))
  sleep 2
done

if [ -n "${UVICORN_RELOAD:-}" ]; then
  set -- --reload
else
  set --
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" "$@"
