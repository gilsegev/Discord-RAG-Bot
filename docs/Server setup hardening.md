# Server Setup Hardening
**Status:** Draft for implementation planning
**Scope:** Make the Oracle-hosted RAG runtime repeatable, service-name based, and safer to operate.

## Goal
The current server setup works, but it still depends on manual container startup, manually connected Docker networks, and hardcoded container IPs in n8n workflow configuration.

The hardened setup should make these true:

- Services start from one repo-owned Compose stack.
- n8n reaches Postgres, Qdrant, Phoenix, and the embedder by stable Docker service names.
- Existing n8n can remain available during migration.
- Secrets stay out of Git.
- Startup, verification, backup, and rollback paths are documented.

## Current Issues
- n8n workflow values use container IPs such as `172.20.x.x`.
- The active n8n instance may live outside the repo-owned Compose stack.
- Container DNS and network membership have required manual repair.
- Service startup order is only partially guarded by healthchecks.
- Runtime secrets and workflow templates need clearer separation.

## Target Architecture
Preferred target:

```text
repo-owned ragbot-n8n
  -> postgres:5432
  -> qdrant:6333
  -> embedder:8000
  -> phoenix:6006
```

All services should share one named Docker network, for example:

```text
ragbot_backend
```

During migration, the existing external n8n container can remain on its current public port while repo-owned n8n runs on a separate test port.

## Migration Principles
- Do not delete existing n8n containers or volumes during the first migration pass.
- Run repo-owned n8n in parallel on a test port before cutover.
- Replace container IPs with service names only after internal DNS is verified.
- Keep `.env`, API keys, webhook URLs, and n8n runtime data out of Git.
- Treat token/webhook values in workflow JSON as placeholders only.

## Implementation Steps
1. Add a named Docker network to `deploy/phase0/docker-compose.yml`.
2. Add healthchecks for Qdrant, Phoenix, embedder, and n8n.
3. Update `verify_phase0.sh` to validate service DNS and HTTP/TCP connectivity from inside n8n.
4. Add an optional helper script for connecting an existing external n8n container to the backend network.
5. Update workflow templates to use service hostnames instead of IPs.
6. Add startup and rollback instructions for parallel repo-owned n8n migration.
7. Add lightweight Postgres and Qdrant backup scripts.
8. Document credential values for repo-owned n8n and external-n8n modes.

## Hostname Conventions
Repo-owned n8n mode:

| Service | Hostname |
|---|---|
| Postgres | `postgres` |
| Qdrant | `qdrant` |
| Embedder | `embedder` |
| Phoenix | `phoenix` |

External n8n compatibility mode:

| Service | Hostname |
|---|---|
| Postgres | `ragbot-postgres` |
| Qdrant | `ragbot-qdrant` |
| Embedder | `ragbot-embedder` |
| Phoenix | `ragbot-phoenix` |

## Secret Safety
The repo may contain:

- `docker-compose.yml`
- `.env.example`
- setup scripts
- schema scripts
- workflow templates with placeholders

The repo must not contain:

- `.env`
- Gemini API keys
- Discord webhook URLs
- Postgres passwords
- n8n encryption keys
- n8n credential databases or execution history

## Initial Acceptance Criteria
- `docker compose up -d` starts backend services reliably.
- Repo-owned n8n can resolve `postgres`, `qdrant`, `embedder`, and `phoenix`.
- External n8n compatibility mode has a documented, repeatable network attach path.
- Phase 3 workflow can run using service hostnames instead of container IPs.
- Verification script catches DNS, healthcheck, and connectivity failures before workflow testing.
