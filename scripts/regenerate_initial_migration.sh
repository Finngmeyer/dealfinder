#!/usr/bin/env bash
# Rebuild migration 0001 from store/tables.py.
#
# Only for use before the first release: once a database exists somewhere that matters, add a
# new migration instead (uv run alembic revision --autogenerate -m "...").
set -euo pipefail
cd "$(dirname "$0")/.."
versions=src/dealfinder/store/migrations/versions
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
rm -f "$versions"/0001_*.py
DEALFINDER_DB_URL="sqlite:///$scratch/empty.db" \
  uv run alembic revision --autogenerate -m "initial schema" --rev-id 0001 >/dev/null
# Alembic writes the app's own column type by its import path; the plain type is what it stores.
sed -i 's/dealfinder\.store\.tables\.UtcDateTime()/sa.DateTime()/g' "$versions"/0001_*.py
uv run ruff format -q "$versions"
uv run ruff check --fix -q "$versions"
uv run ruff format -q "$versions"
echo "Rebuilt $(ls "$versions"/0001_*.py)"
