#!/usr/bin/env bash
set -Eeuo pipefail

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_root="${1:-$HOME/railway-migration-$timestamp}"
repo_root="${REPO_ROOT:-$HOME/Discord-RAG-Bot}"
compose_root="$repo_root/deploy/phase0"

umask 077
mkdir -p "$backup_root"/{host,postgres,qdrant,repo,volumes}

echo "Writing migration backup to $backup_root"

cd "$repo_root"
git rev-parse HEAD > "$backup_root/repo/git-commit.txt"
git status --short --branch > "$backup_root/repo/git-status.txt"
git diff --binary > "$backup_root/repo/uncommitted.patch"
git bundle create "$backup_root/repo/repository.bundle" --all
tar \
  --exclude=.git \
  --exclude=.venv \
  --exclude=.model_cache \
  -czf "$backup_root/repo/worktree.tar.gz" \
  .

find "$compose_root" -maxdepth 1 -type f -name '.env*' \
  -exec cp --preserve=mode,timestamps '{}' "$backup_root/host/" ';'
cp --preserve=mode,timestamps \
  "$compose_root/docker-compose.yml" \
  "$backup_root/host/docker-compose.yml"

cp --preserve=mode,timestamps \
  "$HOME/.ssh/authorized_keys" \
  "$backup_root/host/authorized_keys"
chmod 600 "$backup_root/host/authorized_keys"

hostnamectl > "$backup_root/host/hostnamectl.txt"
uname -a > "$backup_root/host/uname.txt"
free -h > "$backup_root/host/memory.txt"
df -h > "$backup_root/host/filesystems.txt"
docker version > "$backup_root/host/docker-version.txt"
docker compose version > "$backup_root/host/docker-compose-version.txt"
docker ps -a --no-trunc > "$backup_root/host/docker-containers.txt"
docker image ls --digests --no-trunc > "$backup_root/host/docker-images.txt"
docker volume ls > "$backup_root/host/docker-volumes.txt"
docker network ls > "$backup_root/host/docker-networks.txt"
docker inspect \
  ragbot-postgres ragbot-n8n ragbot-phoenix ragbot-qdrant \
  ragbot-embedder ragbot-reranker ragbot-trace-emitter \
  ragbot-discord-listener \
  > "$backup_root/host/docker-inspect.json"
crontab -l > "$backup_root/host/ubuntu-crontab.txt" 2>&1 || true
sudo systemctl list-unit-files --state=enabled \
  > "$backup_root/host/enabled-system-services.txt"
sudo ufw status verbose > "$backup_root/host/ufw-status.txt" 2>&1 || true

cd "$compose_root"
docker compose exec -T postgres \
  pg_dumpall -U ragbot_admin --globals-only \
  > "$backup_root/postgres/globals.sql"

for database in n8n phoenix ragbot; do
  echo "Backing up PostgreSQL database: $database"
  docker compose exec -T postgres \
    pg_dump -U ragbot_admin -Fc "$database" \
    > "$backup_root/postgres/$database.dump"
  docker compose exec -T postgres \
    pg_restore --list \
    < "$backup_root/postgres/$database.dump" \
    > "$backup_root/postgres/$database.contents.txt"
done

collection_json="$(curl -fsS http://127.0.0.1:6333/collections)"
printf '%s\n' "$collection_json" > "$backup_root/qdrant/collections.json"

mapfile -t collections < <(
  python3 -c \
    'import json,sys; print("\n".join(x["name"] for x in json.load(sys.stdin)["result"]["collections"]))' \
    <<< "$collection_json"
)

for collection in "${collections[@]}"; do
  echo "Creating Qdrant snapshot: $collection"
  curl -fsS "http://127.0.0.1:6333/collections/$collection" \
    > "$backup_root/qdrant/$collection.metadata.json"
  snapshot_json="$(
    curl -fsS -X POST \
      "http://127.0.0.1:6333/collections/$collection/snapshots"
  )"
  printf '%s\n' "$snapshot_json" \
    > "$backup_root/qdrant/$collection.snapshot-response.json"
  snapshot_name="$(
    python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])' \
      <<< "$snapshot_json"
  )"
  curl -fsSL \
    "http://127.0.0.1:6333/collections/$collection/snapshots/$snapshot_name" \
    -o "$backup_root/qdrant/$collection.snapshot"
  test -s "$backup_root/qdrant/$collection.snapshot"
  curl -fsS -X DELETE \
    "http://127.0.0.1:6333/collections/$collection/snapshots/$snapshot_name" \
    > "$backup_root/qdrant/$collection.snapshot-delete-response.json"
done

backup_volume() {
  local volume_name="$1"
  local output_name="$2"
  if docker volume inspect "$volume_name" >/dev/null 2>&1; then
    echo "Backing up Docker volume: $volume_name"
    docker run --rm \
      -v "$volume_name:/source:ro" \
      -v "$backup_root/volumes:/backup" \
      alpine:3.22 \
      sh -c "cd /source && tar -czf /backup/$output_name ."
  fi
}

active_n8n_volume="$(
  docker inspect ragbot-n8n \
    --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Name}}{{end}}{{end}}'
)"
if [[ -n "$active_n8n_volume" ]]; then
  backup_volume "$active_n8n_volume" "active-n8n-data.tar.gz"
  printf '%s\n' "$active_n8n_volume" \
    > "$backup_root/volumes/active-n8n-volume-name.txt"
fi

backup_volume "n8n_n8n_data" "legacy-n8n-data.tar.gz"

(
  cd "$backup_root"
  find . -type f ! -name SHA256SUMS \
    -print0 \
    | sort -z \
    | xargs -0 sha256sum \
    > SHA256SUMS
)

du -sh "$backup_root"
echo "$backup_root"
