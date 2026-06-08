# Phase 0 OCI Setup
**Status:** Draft
**Scope:** Set up the server foundation for n8n, Postgres, Phoenix, and Qdrant
**Related:** n8n Execution Plan, n8n Workflow Design, Observability Design

## Goal
Phase 0 proves that the runtime foundation works before we build RAG logic.

By the end of this setup:

- n8n is running.
- Postgres is running.
- Phoenix is running with Postgres storage.
- Qdrant is running.
- n8n can later connect to Postgres, Phoenix, and Qdrant on the same Docker network.
- You can access n8n and Phoenix through SSH tunnels from your laptop.

## Assumptions
This guide assumes:

- Oracle Cloud instance: `VM.Standard.E5.Flex`
- OCPU count: `1`
- Memory: `12 GB`
- OS: Ubuntu 22.04 or Ubuntu 24.04
- You can SSH into the server.
- You are using the repository branch that contains `deploy/phase0`.

If the image is Oracle Linux instead of Ubuntu, do not run the Ubuntu setup script directly. The Docker install commands are different.

## Security Model For Phase 0
The Phase 0 Docker Compose file binds service ports to `127.0.0.1` on the server.

That means:

- n8n is not publicly exposed.
- Phoenix is not publicly exposed.
- Qdrant is not publicly exposed.
- Postgres is not publicly exposed.

You access n8n and Phoenix with SSH tunnels.

This is intentional. Public ingress, TLS, reverse proxy, and Discord webhook exposure should be added later as a separate deployment step.

## Step 1: SSH Into The Oracle Instance
From your laptop:

```bash
ssh -i /path/to/private_key ubuntu@YOUR_SERVER_PUBLIC_IP
```

Some OCI images use `opc` instead of `ubuntu`:

```bash
ssh -i /path/to/private_key opc@YOUR_SERVER_PUBLIC_IP
```

## Step 2: Install Git And Clone The Repo
On the server:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/gilsegev/Discord-RAG-Bot.git
cd Discord-RAG-Bot
git checkout n8n-workflow-design
```

If the branch is not available locally:

```bash
git fetch origin n8n-workflow-design
git checkout n8n-workflow-design
```

## Step 3: Run The Phase 0 Setup Script
On the server:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
chmod +x setup_phase0_ubuntu.sh verify_phase0.sh
./setup_phase0_ubuntu.sh
```

The script installs Docker, creates a `.env` file with generated passwords if one does not exist, pulls the containers, and starts the services.

If Docker was installed for the first time, log out and back in:

```bash
exit
ssh -i /path/to/private_key ubuntu@YOUR_SERVER_PUBLIC_IP
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose ps
```

## Step 4: Verify Services On The Server
Run:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
./verify_phase0.sh
```

Expected results:

- Docker shows `ragbot-postgres`, `ragbot-n8n`, `ragbot-phoenix`, and `ragbot-qdrant`.
- Postgres lists the RAG tables.
- A test transaction inserts successfully.
- Qdrant health returns successfully.
- Phoenix UI returns HTTP headers.
- n8n UI returns HTTP headers.

## Step 5: Open SSH Tunnels From Your Laptop
Keep this terminal open on your laptop:

```bash
ssh -i /path/to/private_key \
  -L 5678:127.0.0.1:5678 \
  -L 6006:127.0.0.1:6006 \
  -L 6333:127.0.0.1:6333 \
  ubuntu@YOUR_SERVER_PUBLIC_IP
```

If your instance user is `opc`, use:

```bash
ssh -i /path/to/private_key \
  -L 5678:127.0.0.1:5678 \
  -L 6006:127.0.0.1:6006 \
  -L 6333:127.0.0.1:6333 \
  opc@YOUR_SERVER_PUBLIC_IP
```

Then open these in your local browser:

- n8n: `http://localhost:5678`
- Phoenix: `http://localhost:6006`
- Qdrant dashboard: `http://localhost:6333/dashboard`

## Step 6: Save The Generated Passwords
On the server:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
cat .env
```

Save these values somewhere secure:

- `POSTGRES_PASSWORD`
- `N8N_ENCRYPTION_KEY`
- `N8N_BASIC_AUTH_USER`
- `N8N_BASIC_AUTH_PASSWORD`
- `PHOENIX_ADMIN_PASSWORD`

Do not commit `.env`.

## Step 7: Useful Commands
Check service status:

```bash
docker compose ps
```

View logs:

```bash
docker compose logs -f n8n
docker compose logs -f phoenix
docker compose logs -f postgres
docker compose logs -f qdrant
```

Restart services:

```bash
docker compose restart
```

Stop services:

```bash
docker compose down
```

Start services:

```bash
docker compose up -d
```

## Postgres Databases
The setup creates three databases in one Postgres container:

| Database | Purpose |
|---|---|
| `n8n` | n8n workflows, credentials, and executions |
| `phoenix` | Phoenix trace/eval storage |
| `ragbot` | Application-owned RAG transaction, retrieval, feedback, eval, and weekly metrics data |

## RAG Tables
The `ragbot` database creates:

- `rag_transactions`
- `rag_trace_events`
- `rag_retrieval_results`
- `rag_feedback`
- `rag_eval_labels`
- `rag_weekly_metrics`
- `rag_recent_transactions`
- `rag_failed_transactions`

These are the Phase 0/Phase 1 foundation tables. They are intentionally enough to support the first active-call workflow, not the final full system.

## Phoenix Configuration
Phoenix uses:

```text
PHOENIX_SQL_DATABASE_URL=postgresql://ragbot_admin:<password>@postgres:5432/phoenix
```

Phoenix UI:

```text
http://localhost:6006
```

Phoenix OTLP gRPC collector:

```text
localhost:4317
```

## n8n Configuration
n8n uses Postgres instead of SQLite:

```text
DB_TYPE=postgresdb
DB_POSTGRESDB_HOST=postgres
DB_POSTGRESDB_DATABASE=n8n
```

n8n UI:

```text
http://localhost:5678
```

## Qdrant Configuration
Qdrant REST API:

```text
http://localhost:6333
```

Qdrant dashboard:

```text
http://localhost:6333/dashboard
```

The ingestion code already expects Qdrant on `localhost:6333` when run on the same host.

## OCI Console Notes
For Phase 0, you only need SSH access from your laptop to the instance.

Do not open these ports publicly yet:

- `5678` n8n
- `6006` Phoenix
- `6333` Qdrant
- `5432` Postgres

Later, if the Discord integration requires inbound webhooks, add a reverse proxy and TLS first, then expose only HTTPS `443`.

