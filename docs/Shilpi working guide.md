# Shilpi Working Guide

## What You Own

You can independently design, implement, deploy, and verify changes to the
Discord-RAG-Bot production system. Production runs in Railway; do not use the
retired Oracle environment.

Use your own Railway and GitHub accounts. Store the separately shared n8n API
key only in your local `.env.local`; never commit it or paste it into workflow
JSON.

## Normal Change Flow

```text
Git branch -> implement + test -> merge -> Railway deploy -> n8n workflow push -> live verification
```

1. Create a focused Git branch and implement the change, including any database
   migration and test coverage.
2. Commit and push the branch, then merge it according to the repository's
   normal Git workflow.
3. Confirm the affected Railway service deploys successfully and inspect its
   logs for the first live request.
4. If the change affects orchestration, update and push only the relevant n8n
   workflow JSON file.
5. Run one representative success case and one intentional no-result/failure
   case before declaring the change complete.

## Railway

Railway project: `Discord-RAG-Bot`, environment: `production`.

- Use the Railway dashboard to view services, deployments, variables, and logs.
- Services connected to GitHub deploy from the configured production branch
  after merge; use the service's deployment view to verify the release.
- Use the Railway CLI only after linking the production project:

```powershell
railway login
railway link -p Discord-RAG-Bot -e production
railway status
railway service list
railway logs --service <service-name>
```

- Postgres, Qdrant, embedder, reranker, trace emitter, and the incremental
  worker are private services. Do not make them public for routine work.

## Postgres

Treat schema changes as versioned application migrations, committed alongside
the code that uses them.

- Review the existing schema and migrations before designing a new table.
- Make the migration safe to apply once and safe for existing production data.
- Apply it through the application’s normal deployment/migration path.
- Use Railway private-service access (`railway run` or `railway ssh`) only when
  direct production inspection is necessary.
- Before any destructive or data-rewriting operation, take a backup and verify
  the exact target and row count.

For the referral-request user search, verify both: a known user is found via
the dedicated table, and an unknown user does not return an unrelated match.

## n8n

n8n production URL: `https://n8n-production-951e.up.railway.app`

Your n8n Member account is for personal workflow work. Production workflows
are managed from Git and deployed through the API key shared with you.

Create a local `.env.local` in the repository root:

```env
N8N_API_URL=https://n8n-production-951e.up.railway.app/api/v1
N8N_API_KEY=<shared privately; do not commit>
```

Use the repository commands:

```powershell
npm run n8n:list
npm run n8n:pull -- "<workflow name>"
npm run n8n:push -- workflows/n8n/<changed-workflow>.json
```

- Pull before editing if n8n may have changed since your last update.
- Push one workflow at a time while developing.
- Preserve workflow credential references; do not place API keys, webhook
  secrets, or database passwords in workflow JSON.
- After pushing, run the affected workflow through its real entry point and
  confirm the n8n execution and downstream service logs are healthy.

## Escalate Immediately If

- A deployment fails, a migration cannot be reversed safely, or live data is
  inconsistent.
- A change would expose a private Railway service publicly.
- A required credential or workflow reference is missing or unclear.

Phoenix is intentionally out of scope for now.
