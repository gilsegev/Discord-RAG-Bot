#!/usr/bin/env bash
set -euo pipefail

echo "== Containers =="
docker compose ps

echo
echo "== Postgres app schema =="
docker compose exec -T postgres psql -U ragbot_admin -d ragbot \
  -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name;"

echo
echo "== Insert test transaction =="
docker compose exec -T postgres psql -U ragbot_admin -d ragbot \
  -c "INSERT INTO rag_transactions(route_type, status, retrieval_status, response_status, user_query) VALUES ('active_call', 'created', 'not_started', 'not_started', 'phase 0 smoke test') RETURNING transaction_id, created_at;"

echo
echo "== Qdrant health =="
curl -fsS http://127.0.0.1:${QDRANT_REST_PORT:-6333}/healthz || curl -fsS http://127.0.0.1:${QDRANT_REST_PORT:-6333}/
echo

echo
echo "== Phoenix UI =="
curl -fsSI http://127.0.0.1:${PHOENIX_UI_PORT:-6006}/ | head -n 5

echo
echo "== Trace emitter =="
curl -fsS http://127.0.0.1:${TRACE_EMITTER_PORT:-8001}/health
echo

echo
echo "== Reranker =="
curl -fsS http://127.0.0.1:${RERANKER_PORT:-8002}/health
echo

echo
echo "== n8n UI =="
curl -fsSI http://127.0.0.1:${N8N_PORT:-5678}/ | head -n 5

echo
echo "Verification complete."

