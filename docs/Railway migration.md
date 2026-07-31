# Railway Migration

## Status

**Cutover status:** Complete as of July 2026. Railway is the active production
runtime. The Oracle listener and database writers must remain stopped and are
not part of routine operation, development, regression, or review.

This document preserves the deployment, cutover, restore, and rollback history
for the move from Oracle Cloud to Railway. Any retained Oracle VM, boot volume,
or migration backup is an owner-controlled recovery/archive asset only. It must
not be started while Railway writers or the Railway Discord listener are
active.

The Railway project is `Discord-RAG-Bot` in `us-west2`. Production data
services are private. The only public application endpoints are:

- n8n: `https://n8n-production-951e.up.railway.app`
- Phoenix: `https://phoenix-production-1386.up.railway.app`

Do not deploy the Railway `discord-listener` while the Oracle listener is
running. Two connected listeners can produce duplicate responses.

## Service Inventory

| Service | Version/source | Persistent path | Public |
|---|---|---|---|
| `postgres` | `postgres:16.14` | `/var/lib/postgresql/data` | No |
| `n8n` | `docker.n8n.io/n8nio/n8n:2.23.4` | State is in Postgres | Yes |
| `qdrant` | `qdrant/qdrant:v1.18.2` | `/qdrant/storage` | No |
| `phoenix` | `arizephoenix/phoenix:17.2.0` | State is in Postgres | Yes |
| `embedder` | `deploy/phase0/embedder` | `/models` | No |
| `reranker` | `deploy/phase0/reranker` | `/models` | No |
| `trace-emitter` | `deploy/phase0/trace-emitter` | None | No |
| `discord-listener` | `deploy/phase0/discord-listener` | None | No |

The detached n8n volume is intentionally unused. n8n workflow, credential,
user, and execution state is stored in the restored `n8n` Postgres database.

## Required Variable Names

Values are stored only in Railway. Never copy their values into Git.

- `postgres`: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `PGDATA`
- `n8n`: database variables, `N8N_ENCRYPTION_KEY`, `GEMINI_API_KEY`,
  `DISCORD_BOT_TOKEN`, n8n host/editor/webhook variables,
  `N8N_WEBHOOK_SHARED_SECRET`, and the four internal runtime service URLs
- `phoenix`: `PHOENIX_SQL_DATABASE_URL`, authentication secrets, admin
  bootstrap password, secure-cookie and CSRF settings
- `trace-emitter`: `PHOENIX_OTLP_HTTP_ENDPOINT`, `PHOENIX_AUTH_TOKEN`
- `embedder` and `reranker`: model name/cache variables and
  `MODEL_MAX_CONCURRENCY=1`; native math/tokenizer thread counts are also `1`
- `discord-listener`: Discord token/guild/hash settings,
  `N8N_INTAKE_URL`, `N8N_FEEDBACK_URL`, and
  `N8N_WEBHOOK_SHARED_SECRET`

All internal URLs use `*.railway.internal`; do not expose Postgres, Qdrant,
the model services, or the trace emitter publicly.

## Initial Backup

Run on Oracle:

```bash
cd ~/Discord-RAG-Bot
bash scripts/migration/backup-oracle-for-railway.sh
```

The backup contains custom-format dumps for `n8n`, `phoenix`, and `ragbot`, a
Qdrant collection snapshot, repository/worktree recovery material, service
inventory, and Docker volume archives. `SHA256SUMS` uses relative paths and
must pass after the archive is copied off the server.

The backup contains production secrets and must not be committed or placed in
shared storage without encryption.

## Restore

Postgres restore is destructive to the three target databases. Keep Railway
`n8n`, `phoenix`, and `trace-emitter` stopped and the Railway listener
undeployed. Create a temporary Railway Postgres TCP proxy, then run on Oracle:

```bash
cd ~/Discord-RAG-Bot
bash scripts/migration/restore-postgres-to-railway.sh \
  /home/ubuntu/railway-migration-YYYYMMDDTHHMMSSZ \
  RAILWAY_PROXY_HOST \
  RAILWAY_PROXY_PORT
```

The script verifies all checksums and dump catalogs before dropping a target
database. Delete the temporary TCP proxy immediately afterward.

For Qdrant, create a temporary authenticated public endpoint only for the
restore window:

```bash
QDRANT_API_KEY='temporary-restore-key' \
bash scripts/migration/restore-qdrant-snapshot.sh \
  /path/to/railway-migration-YYYYMMDDTHHMMSSZ \
  https://temporary-qdrant-domain \
  tpm_unite_history
```

Remove the temporary domain and API key after the collection reports green.
The source and target must have matching point and indexed-vector counts.

Apply the Phase 9c.1 capture schema after every final Postgres restore:

```bash
psql "$RAGBOT_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f deploy/phase0/sql/07-phase9c1-incremental-capture-migration.sql
```

Phase 9c.1 currently provides durable message capture and pending-work
creation. It does **not** yet provide the nightly chunk-building worker.

## Workflow Deployment

The active Git workflow JSON must be reconciled from the Oracle production
workflows before cutover. This preserves the Stage 0 PII/safety gate and the
latest regression cases. Then apply the Railway URL and webhook-auth
transformations:

```bash
node scripts/migration/configure-workflow-service-urls.js
node scripts/migration/configure-webhook-auth.js
npm run n8n:push -- workflows/n8n/rag-core-execution-phase-8.json
npm run n8n:push -- workflows/n8n/rag-intake-routing-phase-9.json
npm run n8n:push -- workflows/n8n/rag-regression-batch-runner-phase-8.json
npm run n8n:push -- workflows/n8n/rag-feedback-correlation-phase-10.json
```

The three public webhooks validate `X-RAG-Webhook-Secret` when
`N8N_WEBHOOK_SHARED_SECRET` is configured. The listener and regression client
send the same header. An unauthenticated request must stop at the first code
node and must not start retrieval, capture, or feedback work.

## Pre-Cutover Validation

1. Confirm every Railway dependency is running and the listener is not
   deployed.
2. Check n8n and Phoenix public `/healthz` endpoints.
3. Confirm Qdrant is green and source/target counts match.
4. Confirm restored n8n workflows and credentials load.
5. Run the targeted PII/safety and active/refusal smoke cases.
6. Run a full retrieval-only Oracle baseline and a full Railway regression
   from the same workflow definition and compare outcomes.
7. Review model logs for thread-creation errors, OOMs, and non-200 requests.

The Railway public edge can close a synchronous full-regression request after
60 seconds while n8n continues it. In that case, retrieve the completed
execution summary through the authenticated n8n API; do not treat the HTTP
timeout as a failed regression.

## Final Cutover

Use a short Discord maintenance window because the listener queue is
in-memory and the project does not yet have a Discord-history backfill tool.

1. Confirm every recent Oracle `Queued` message has a matching `Delivered`
   log entry.
2. Stop the Oracle listener first.
3. Wait for Oracle n8n running executions to reach zero.
4. Stop Oracle n8n, Phoenix, and trace-emitter to freeze database writers.
5. Create a new final backup, copy it off Oracle, and verify checksums.
6. Stop Railway n8n, Phoenix, and trace-emitter.
7. Restore the final Postgres dumps; reapply migration `07`.
8. Reconcile or restore Qdrant if final counts changed.
9. Start Railway dependencies, Phoenix, trace-emitter, and n8n.
10. Repeat health, count, workflow, and targeted regression checks.
11. Reconfirm the Oracle listener is stopped, then deploy exactly one Railway
    listener replica.
12. Require a Discord Gateway-ready log, send one direct mention, and confirm
    exactly one transaction and one response for its message ID.
13. Test one passive message and one supported reaction.

Keep the Oracle VM stopped but recoverable for 24–48 hours. Do not terminate
the VM or delete its boot volume until the Railway observation window passes.

## Rollback

1. Stop the Railway listener first and confirm it disconnected.
2. Back up Railway Postgres and record Discord message IDs handled since
   cutover.
3. Start Oracle dependencies and n8n.
4. Start the Oracle listener only after Railway remains stopped.
5. Reconcile messages processed after cutover; restoring the old Oracle
   database alone would lose that activity.

## Routine Operations

```bash
railway status
railway service list
railway logs --service n8n
railway logs --service discord-listener
railway logs --service embedder
railway logs --service reranker
railway logs --service trace-emitter
```

Railway health checks are deployment-readiness checks, not continuous
monitoring. Keep external uptime checks for n8n and Phoenix, create scheduled
Postgres/Qdrant exports, and monitor reranker memory and Railway spending.
