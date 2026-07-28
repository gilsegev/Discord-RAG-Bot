#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 BACKUP_ROOT QDRANT_BASE_URL COLLECTION" >&2
  echo "Set QDRANT_API_KEY when the target requires authentication." >&2
  exit 2
fi

backup_root="$1"
qdrant_base_url="${2%/}"
collection="$3"
snapshot_path="$backup_root/qdrant/$collection.snapshot"

if [[ ! -d "$backup_root" || ! -f "$backup_root/SHA256SUMS" ]]; then
  echo "Backup root is missing or incomplete: $backup_root" >&2
  exit 2
fi
if [[ ! -s "$snapshot_path" ]]; then
  echo "Collection snapshot is missing or empty: $snapshot_path" >&2
  exit 2
fi
if [[ ! "$collection" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "Collection name contains unsupported characters." >&2
  exit 2
fi

(
  cd "$backup_root"
  sha256sum --check --strict SHA256SUMS
)

curl_args=(-fsS)
if [[ -n "${QDRANT_API_KEY:-}" ]]; then
  curl_args+=(-H "api-key: $QDRANT_API_KEY")
fi

echo "Restoring Qdrant collection '$collection' from verified snapshot"
restore_response="$(
  curl "${curl_args[@]}" \
    -X POST \
    "$qdrant_base_url/collections/$collection/snapshots/upload?priority=snapshot" \
    -F "snapshot=@$snapshot_path"
)"
python3 -c \
  'import json,sys; data=json.load(sys.stdin); assert data.get("status") == "ok", data' \
  <<< "$restore_response"

collection_response="$(
  curl "${curl_args[@]}" "$qdrant_base_url/collections/$collection"
)"
python3 -c \
  'import json,sys
data=json.load(sys.stdin)
result=data["result"]
print("status=%s points_count=%s indexed_vectors_count=%s" % (
    result["status"], result["points_count"], result["indexed_vectors_count"]
))
assert result["status"] == "green", result' \
  <<< "$collection_response"
