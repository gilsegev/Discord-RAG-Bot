# n8n Workflow Sync
**Scope:** Push and pull Git-managed workflow JSON files directly between this repo and repo-owned n8n.

## Purpose
Use this utility when Codex or a developer updates workflow JSON under:

```text
workflows/n8n/
```

Instead of manually importing the JSON through the n8n UI, the workflow can be pushed to n8n through the n8n public API.

## Local Setup
Open an SSH tunnel to repo-owned n8n:

```powershell
ssh -i "$HOME\mykey.key" -L 5679:127.0.0.1:5679 ubuntu@discord-notifier.duckdns.org
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

`.env.local` is ignored by Git.

## Commands
List workflows currently in n8n:

```bash
npm run n8n:list
```

Push all local workflow JSON files:

```bash
npm run n8n:push
```

Push one workflow file:

```bash
npm run n8n:push -- workflows/n8n/rag-active-call-phase-4-stage-1-retrieval-gate.json
```

Push one workflow by n8n workflow name:

```bash
npm run n8n:push -- "RAG Active Call - Phase 4 Stage 1 Retrieval Gate"
```

Pull all remote workflows into `workflows/n8n/`:

```bash
npm run n8n:pull
```

Pull one remote workflow by name or ID:

```bash
npm run n8n:pull -- "RAG Active Call - Phase 4 Stage 1 Retrieval Gate"
```

## Matching Behavior
Push matches existing n8n workflows by:

1. Workflow `id`, if the local JSON has one.
2. Workflow `name`, if no matching ID is present.

If neither matches, the script creates a new workflow and writes the generated n8n `id` back into the local JSON file.

## Sanitization
Pull writes only stable workflow fields:

- `id`
- `name`
- `active`
- `nodes`
- `connections`
- `settings`
- `staticData`
- `meta`

This avoids noisy Git diffs from n8n database timestamps and other volatile API fields.

## Safety Notes
- Do not commit `.env.local`.
- Do not commit Gemini API keys or Discord webhook URLs inside workflow JSON.
- Prefer pushing one workflow at a time while actively developing.
- If n8n reports credential warnings after a push, verify the workflow JSON uses the correct credential ID for the target n8n instance.
