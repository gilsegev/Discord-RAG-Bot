# Contributor Access And Permissions Design

**Status:** Proposed for Phase 10 implementation

**Audience:** Gil, Haragonda, AltCtrlDeliver, and future project contributors

**Scope:** GitHub, Railway, n8n, Postgres, Qdrant, Phoenix, credentials, and
production operations

## Purpose

Haragonda and AltCtrlDeliver need enough access to contribute code, develop and
run n8n workflows, inspect transactions and reports, and review pending issues.
They should not need Gil's SSH private key, a shared production login, database
administrator credentials, or plaintext Gemini and Discord secrets.

The model uses two boundaries:

1. Contributors can build, change, and run freely in development.
2. Contributors can run approved workflows and inspect approved data in
   production, but production definitions and credentials change only through
   review.

Running a production workflow is intentionally allowed. Editing a production
workflow is a separate permission because an n8n editor can add Code, HTTP, or
SQL nodes that use production data and attached credentials.

## Access At A Glance

| Capability | Haragonda and AltCtrlDeliver |
|---|---|
| Clone the repository and push feature branches | Yes |
| Open and review pull requests | Yes |
| Triage, label, and comment on issues | Yes |
| Push or merge directly to `main` | No |
| Create, edit, and run development n8n workflows | Yes |
| Run approved production n8n workflows | Yes |
| View production execution results and Phoenix traces | Yes |
| Retry an approved production workflow | Yes |
| Edit or activate production workflow definitions directly | No |
| Read approved production transactions and reports | Yes |
| Write production application tables directly | No |
| View or administer production credentials | No |
| Use Gil's SSH private key or a shared server account | No |

## Everyday Contributor Workflow

### Change code or an n8n workflow

1. Create a feature branch.
2. Develop and run the change locally or in the Railway development project.
3. For n8n changes, export or pull the workflow JSON into `workflows/n8n/`.
4. Run the relevant repository checks.
5. Open a pull request describing the change and test evidence.
6. Address review comments.
7. After approval and merge, the production deployment identity promotes the
   reviewed version.

Contributors do not paste production credentials into development. Development
uses separate test credentials and a test Discord destination.

### Run an approved production workflow

Use one of the approved operator surfaces:

- an authenticated, purpose-specific n8n webhook;
- a production n8n run control when the installed n8n edition can enforce
  protected production definitions; or
- a GitHub Actions `workflow_dispatch` button that calls n8n with a protected
  service credential.

Every run records the contributor's identity, workflow or operation, input
parameters, start time, result, and related transaction or regression run ID.
The server derives `requested_by` from authentication; it does not trust a name
supplied in the request body.

### Review production results

- Use Phoenix for traces and execution diagnosis.
- Use the approved reporting views for transactions, regression results,
  weekly metrics, failures, and pending reviews.
- Use n8n execution history when the contributor's n8n role permits it.
- Open or update a GitHub issue when a result needs code or workflow work.

Raw community message content and personally identifying fields are available
only when the review task requires them. Default reports should use the minimum
necessary fields.

## Permission Model

### GitHub

Give Haragonda and AltCtrlDeliver the repository `Write` role. Keep `Admin` with
Gil.

Protect `main` with a repository ruleset:

- require pull requests and passing checks;
- require at least one approval;
- dismiss stale approvals after new changes;
- block force pushes and branch deletion;
- do not grant contributors ruleset bypass;
- require Gil or a production-owner CODEOWNER for:
  - `deploy/**`;
  - `.github/workflows/**`;
  - `workflows/n8n/**`;
  - database migrations; and
  - permission, secret, or production configuration changes.

Repository access uses each contributor's GitHub identity. Do not share a
GitHub account, personal access token, or writable deploy key.

### Railway

Use separate Railway projects rather than relying only on environment names.

#### Development project

Give both contributors Railway project `Editor`.

The development project contains:

- development n8n;
- development Postgres and Qdrant;
- development Phoenix, embedder, reranker, and trace emitter;
- synthetic or approved sanitized test data;
- a test Discord bot or test-only channel;
- separate, quota-limited model credentials; and
- no references to production secrets.

#### Production project

Keep Gil as `Owner`. Give contributors `Viewer` only if its visible logs and
metadata have been reviewed as appropriate; otherwise give no direct Railway
project membership and provide the operator and reporting surfaces described in
this document.

Do not give contributors production Railway CLI tokens. Automated deployment
uses a production-scoped service or project token stored in a protected CI
environment.

Railway sealed variables should be used for production Gemini, Discord, n8n,
database-administrator, and deployment secrets. Sealing prevents normal
dashboard, API, and CLI retrieval, but it does not make arbitrary production
code safe: code running beside a secret can use or transmit it. Reviewed
production deployment is therefore the primary boundary.

If Railway Enterprise environment RBAC is adopted later, production may be a
restricted environment in a shared project. Until then, separate projects are
the clearer least-privilege boundary.

### n8n

Run separate development and production n8n instances.

Development n8n gives contributors full workflow editing and execution access.
Production n8n is an execution target for reviewed workflow JSON.

Production contributors may:

- start approved workflows;
- supply inputs allowed by a fixed schema;
- view their runs and approved execution details;
- retry an approved failed run; and
- follow links to Postgres reporting and Phoenix evidence.

Production contributors may not:

- add, remove, or rewire nodes;
- change SQL, URLs, or Code-node contents;
- attach, replace, or create credentials;
- activate, deactivate, publish, import, or delete workflows; or
- run arbitrary workflow IDs with arbitrary input.

n8n editions that bundle `Run` with workflow `Editor` do not provide a safe
run-only boundary. In that case, use authenticated operator webhooks or GitHub
Actions for production runs. If an n8n Business or Enterprise protected
production instance is adopted, contributors may use its UI subject to the
verified role behavior.

Production operator endpoints must:

- authenticate individual callers;
- authorize a fixed operation, not arbitrary n8n API access;
- validate inputs against an allowlist or JSON schema;
- enforce server-side mode flags;
- rate-limit expensive runs;
- log caller identity and outcome; and
- avoid returning secrets or unnecessarily sensitive execution data.

For example, the regression operator may permit `cases`, `category`, and
`limit`, while enforcing `allow_discord_post = false`. A separately approved
operation may allow a full-answer run or a test-channel post.

### Postgres

Create one login per contributor. Never share `ragbot_admin` or n8n's database
owner credential.

Production contributor roles receive:

- `CONNECT` to the application database;
- `USAGE` on a dedicated `reporting` schema;
- `SELECT` on approved views; and
- `default_transaction_read_only = on`.

Initial reporting views should cover:

- transaction summaries;
- regression runs and results;
- weekly metrics;
- failure summaries; and
- pending human-review items.

Do not grant access to n8n internal tables, secret/configuration tables, raw
credential storage, or unrestricted application schemas. Mask author and
message fields unless the review purpose requires them.

Prefer an authenticated reporting UI or API on Railway's private network.
If direct external PostgreSQL access through a Railway TCP proxy is required,
use TLS, unique generated passwords, read-only roles, connection logging, and a
documented rotation and revocation process.

Development database roles may write development data and apply proposed
migrations. Production migrations run only through the reviewed deployment
path.

### Qdrant And Phoenix

Phoenix is a contributor review surface. Give individual access to the projects
needed for regression and production diagnosis, with sensitive attributes
redacted where possible.

Do not expose unrestricted production Qdrant mutation credentials. Contributors
normally inspect retrieval evidence through regression reports, Phoenix, and
read-only diagnostic tooling. Any direct Qdrant review endpoint must be
read-only and authenticated.

## Credential And Key Rules

- Gil's SSH private key never leaves Gil's machine.
- Every person uses an individual identity and, when SSH is unavoidable, their
  own key pair.
- No shared `ubuntu`, Railway, GitHub, n8n, or database human login.
- Development and production secrets are different.
- Production n8n has its own `N8N_ENCRYPTION_KEY`; it is not copied to
  development.
- Secrets are stored in Railway or protected CI secret storage, never Git.
- Service tokens are scoped to one environment and operation where supported.
- Access removal includes credential rotation when a shared or broadly readable
  secret may have been exposed.

The current Oracle admin access documented for Haragonda and AltCtrlDeliver is a
transition risk because the shared `ubuntu` account can inspect containers,
volumes, environment variables, and databases. Remove those public keys after
Railway development, production operator, and reporting access are verified.
Keep tunnel-only evaluator accounts only until their replacement path works.

## Audit And Review

Record:

- GitHub reviews, merges, and production deployment status;
- Railway deployment actor and result;
- production workflow run actor, inputs, and outcome;
- database login and failed-authentication events;
- changes to roles, credentials, and operator allowlists; and
- emergency or break-glass access.

Gil reviews the access list quarterly and whenever a contributor joins, changes
responsibility, or leaves. Remove access from GitHub, Railway, n8n, reporting,
Phoenix, CI environments, and any remaining SSH account as one offboarding
checklist.

## Implementation Order

1. Create the Railway development project and independent development secrets.
2. Create individual GitHub, Railway development, n8n development, and Phoenix
   access.
3. Add protected-branch rules and CODEOWNERS for production-sensitive paths.
4. Create production reporting views and individual read-only database roles.
5. Implement authenticated production workflow operator endpoints and audit
   rows.
6. Add GitHub Actions run buttons for common regression and reporting jobs.
7. Verify contributors can develop, run production operations, and review
   results without a shared secret.
8. Seal and rotate production secrets.
9. Remove contributor keys from the Oracle `ubuntu` account and retire obsolete
   evaluator tunnels.
10. Run an access-revocation drill and document the owner runbook.

## Acceptance Criteria

- Both contributors can complete the everyday workflows in this document.
- Neither contributor needs Gil's private key or a shared human credential.
- A contributor can run approved production n8n operations without editing the
  production workflow definition.
- Production runs identify the authenticated caller and persist audit evidence.
- Contributors can read the reports needed for transaction and issue review but
  cannot write production application data directly.
- A feature branch or development n8n edit cannot deploy to production without
  the required review.
- Removing one contributor's accounts and database role revokes that person
  without disrupting the other contributor or the application.
