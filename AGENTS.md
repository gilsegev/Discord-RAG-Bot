# AGENTS.md

## Purpose
This file gives coding agents and project contributors the minimum context needed to work safely in this repo.

Use the deeper docs under `docs/` for full design details. This file is the quick orientation layer.

## 1. High-Level Design
Discord-RAG-Bot is a Discord community knowledge bot. It ingests Discord exports, chunks and embeds historical messages, stores them in Qdrant, and uses n8n to orchestrate retrieval, reranking, dedupe, context assembly, Gemini generation, Discord responses, regression runs, and observability.

Core runtime:

| Component | Role | Access Pattern |
|---|---|---|
| Discord | User interface and event source. | External Discord API/webhooks. |
| n8n | Main orchestrator and state machine. | Repo-owned Docker service, local tunnel on `5679`. |
| Qdrant | Vector database for embedded message chunks. | Docker service `qdrant`, REST port `6333`. |
| Embedder | Local Nomic embedding service. | Docker service `embedder`, HTTP port `8000`. |
| Reranker | Local CrossEncoder reranking service. | Docker service `reranker`, HTTP port `8002`. |
| Gemini | LLM generation and grounding. | External Google API. API keys must not be committed. |
| Postgres | Durable app state, n8n DB, Phoenix DB, RAG tables. | Docker service `postgres`, port `5432` inside Docker network. |
| Phoenix | Trace UI and observability surface. | Repo-owned Docker service, local tunnel on `6006`. |
| Trace Emitter | Small local service that sends n8n trace spans to Phoenix. | Docker service `trace-emitter`, HTTP port `8001`. |

Current workflow shape:

```text
Discord / Manual / Regression / CI
        |
        v
n8n Intake + Routing
        |
        v
Shared RAG Core
  normalize -> embed -> Qdrant -> rerank -> dedupe -> assemble context
        |
        +--> retrieval-only regression output
        |
        +--> optional Gemini generation
                  |
                  v
             Discord / regression / CI result
```

Important design rule: avoid duplicating the RAG path. Active calls, passive calls, regression runs, and future CI runs should route through the shared intake/core workflow contract with mode flags such as `trigger_source`, `run_mode`, `response_mode`, `allow_gemini`, and `allow_discord_post`.

Key design docs:

- `docs/Arch overview.md`
- `docs/n8n execution plan.md`
- `docs/retrieval-context-prompt-contracts.md`
- `docs/Observability design.md`
- `docs/Regression README.md`

## 2. Oracle Server And Component Access
The project runs on an Oracle Cloud Ubuntu VM. The repo-owned services are managed from:

```bash
~/Discord-RAG-Bot/deploy/phase0
```

### Admin SSH
Trusted maintainers use the shared `ubuntu` account:

```bash
ssh -i /path/to/private/key ubuntu@discord-notifier.duckdns.org
```

Admin access is documented in:

```text
docs/Admin access.md
```

Limited evaluator access is documented separately:

```text
docs/AltCtrlDeliver regression access.md
docs/Haragonda regression access.md
```

### Open Local Tunnels
Use one tunnel for n8n and Phoenix:

```bash
ssh -i /path/to/private/key \
  -L 5679:127.0.0.1:5679 \
  -L 6006:127.0.0.1:6006 \
  ubuntu@discord-notifier.duckdns.org
```

Then open:

```text
n8n:     http://127.0.0.1:5679
Phoenix: http://127.0.0.1:6006
```

Qdrant can be tunneled when needed:

```bash
ssh -i /path/to/private/key \
  -L 6333:127.0.0.1:6333 \
  ubuntu@discord-notifier.duckdns.org
```

Then open:

```text
Qdrant dashboard: http://127.0.0.1:6333/dashboard
```

### Server Service Commands
On the Oracle server:

```bash
cd ~/Discord-RAG-Bot/deploy/phase0
docker compose ps
docker compose up -d
docker compose restart
```

View logs:

```bash
docker compose logs -f n8n
docker compose logs -f postgres
docker compose logs -f qdrant
docker compose logs -f embedder
docker compose logs -f reranker
docker compose logs -f phoenix
docker compose logs -f trace-emitter
```

Validate service reachability from n8n:

```bash
docker compose exec n8n node -e "fetch('http://qdrant:6333/collections').then(r=>console.log('qdrant', r.status))"
docker compose exec n8n node -e "fetch('http://embedder:8000/health').then(r=>console.log('embedder', r.status))"
docker compose exec n8n node -e "fetch('http://reranker:8002/health').then(r=>console.log('reranker', r.status))"
docker compose exec n8n node -e "fetch('http://trace-emitter:8001/health').then(r=>console.log('trace-emitter', r.status))"
```

### Security Rules
- Never commit private SSH keys.
- Never commit `.env`, `.env.local`, Gemini API keys, Discord webhooks, n8n API keys, or generated service passwords.
- Prefer Docker service names (`postgres`, `qdrant`, `embedder`, `reranker`, `phoenix`, `trace-emitter`) over container IPs.
- Keep n8n, Phoenix, Qdrant, and Postgres behind SSH tunnels unless deployment docs explicitly say otherwise.

## 3. n8n Workflow Sync
Workflows live in:

```text
workflows/n8n/
```

Use the repo scripts to push/pull workflow JSON directly between Git and repo-owned n8n. Full docs:

```text
docs/n8n workflow sync.md
```

### Local Setup
Open the n8n tunnel:

```bash
ssh -i /path/to/private/key -L 5679:127.0.0.1:5679 ubuntu@discord-notifier.duckdns.org
```

Open n8n:

```text
http://127.0.0.1:5679
```

Create an n8n API key:

```text
Settings -> n8n API -> Create an API key
```

Create `.env.local` in the repo root:

```env
N8N_API_URL=http://127.0.0.1:5679/api/v1
N8N_API_KEY=replace_with_n8n_personal_api_key
```

Do not commit `.env.local`.

### Commands
List workflows in n8n:

```bash
npm run n8n:list
```

Push all workflow JSON files:

```bash
npm run n8n:push
```

Push one workflow by file:

```bash
npm run n8n:push -- workflows/n8n/rag-intake-routing-phase-8.json
```

Push one workflow by n8n workflow name:

```bash
npm run n8n:push -- "RAG Intake + Routing - Phase 8"
```

Pull all remote workflows into Git:

```bash
npm run n8n:pull
```

Pull one remote workflow:

```bash
npm run n8n:pull -- "RAG Core Execution - Phase 8"
```

### Workflow Push Notes
- Push matches by workflow `id` first, then workflow `name`.
- If no match exists, the script creates a new workflow and writes the generated n8n `id` back into the JSON file.
- If n8n shows credential warnings after a push, open the node in n8n and verify its credential binding for that n8n instance.
- Prefer pushing one workflow at a time when actively debugging.

## 4. Common Development Checks
Before committing workflow or ingestion changes, run the narrow checks that fit the change.

Python syntax check:

```bash
python -m py_compile ingestion/chunker.py ingestion/run.py
```

n8n workflow JSON parse check:

```bash
node -e "for (const f of require('fs').readdirSync('workflows/n8n')) JSON.parse(require('fs').readFileSync('workflows/n8n/' + f, 'utf8')); console.log('workflow json ok')"
```

Git status:

```bash
git status --short --branch
```

## 5. Current Regression Path
Regression evaluation is Phase 8. The batch runner is:

```text
workflows/n8n/rag-regression-batch-runner-phase-8.json
```

It calls the shared intake/core path and defaults to retrieval-only mode for evaluator runs:

```json
{
  "cases": "RQ-001,RQ-036",
  "mode": "retrieval_only",
  "allow_gemini": false,
  "allow_discord_post": false,
  "write_eval_labels": false,
  "requested_by": "developer"
}
```

Regression docs and data:

```text
docs/Regression README.md
scripts/regression_questions.jsonl
docs/regression-reports/
```
