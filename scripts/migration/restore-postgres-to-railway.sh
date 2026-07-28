#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 BACKUP_ROOT RAILWAY_POSTGRES_HOST RAILWAY_POSTGRES_PORT" >&2
  exit 2
fi

backup_root="$1"
railway_host="$2"
railway_port="$3"
repo_root="${REPO_ROOT:-$HOME/Discord-RAG-Bot}"
compose_root="$repo_root/deploy/phase0"

if [[ ! -d "$backup_root" || ! -f "$backup_root/SHA256SUMS" ]]; then
  echo "Backup root is missing or incomplete: $backup_root" >&2
  exit 2
fi
if [[ -z "$railway_host" || ! "$railway_port" =~ ^[0-9]+$ ]]; then
  echo "Railway PostgreSQL host/port are invalid." >&2
  exit 2
fi

cd "$compose_root"
set -a
# shellcheck disable=SC1091
source .env
set +a
export PGPASSWORD="$POSTGRES_PASSWORD"

echo "Verifying backup checksums and PostgreSQL archives before changing Railway"
(
  cd "$backup_root"
  sha256sum --check --strict SHA256SUMS
)

for database in n8n phoenix ragbot; do
  dump_path="$backup_root/postgres/$database.dump"
  if [[ ! -s "$dump_path" ]]; then
    echo "Required dump is missing or empty: $dump_path" >&2
    exit 2
  fi
  docker compose exec -T postgres pg_restore --list < "$dump_path" >/dev/null
done

docker compose exec -T -e PGPASSWORD postgres \
  psql \
    -h "$railway_host" \
    -p "$railway_port" \
    -U ragbot_admin \
    -d postgres \
    -v ON_ERROR_STOP=1 \
    -Atc "SELECT 'railway-postgres-ready';"

for database in n8n phoenix ragbot; do
  echo "Restoring $database to Railway"
  docker compose exec -T -e PGPASSWORD postgres \
    dropdb \
      -h "$railway_host" \
      -p "$railway_port" \
      -U ragbot_admin \
      --if-exists \
      --force \
      "$database"
  docker compose exec -T -e PGPASSWORD postgres \
    createdb \
      -h "$railway_host" \
      -p "$railway_port" \
      -U ragbot_admin \
      "$database"
  docker compose exec -T -e PGPASSWORD postgres \
    pg_restore \
      -h "$railway_host" \
      -p "$railway_port" \
      -U ragbot_admin \
      -d "$database" \
      --no-owner \
      --no-privileges \
      --exit-on-error \
    < "$backup_root/postgres/$database.dump"
done

for database in n8n phoenix ragbot; do
  docker compose exec -T -e PGPASSWORD postgres \
    psql \
      -h "$railway_host" \
      -p "$railway_port" \
      -U ragbot_admin \
      -d "$database" \
      -Atc \
      "SELECT current_database() || ':' || count(*) FROM information_schema.tables WHERE table_schema = 'public';"
done
