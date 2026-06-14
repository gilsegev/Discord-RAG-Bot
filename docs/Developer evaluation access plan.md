# Developer Evaluation Access Plan

**Status:** In implementation
**Scope:** Give trusted developers access to a retrieval-only n8n workflow without granting a server shell or exposing production credentials.
**Initial user:** Shilpi

## 1. Goal

Allow Shilpi to use the n8n UI to run `docs/regression_questions.jsonl` through the current offline retrieval pipeline:

```text
Question
-> query normalization
-> local embedding
-> Qdrant Stage 1 retrieval
-> CrossEncoder reranking
-> dedupe status
-> structured evaluation result
```

The workflow must skip Gemini, Discord posting, production transaction writes, and personal API credentials.

## 2. Access Model

Shilpi receives two independent identities:

| Identity | Purpose | Permission boundary |
|---|---|---|
| Restricted Oracle SSH user | Opens a tunnel from her laptop to n8n | Port forwarding to host `127.0.0.1:5679` only; no shell, PTY, agent forwarding, or other destination |
| n8n member account | Opens and runs workflows in the n8n UI | Own personal workspace; no access to workflows or credentials owned by other n8n users |

The n8n Community Edition does not support shared projects or workflow sharing. The evaluation workflow will therefore remain Git-managed in this repository and Shilpi will import a copy into her n8n personal workspace.

## 3. Security Boundary

The restricted SSH account protects the Oracle host from interactive shell access. It does not limit what an editable n8n workflow can call from inside the n8n container.

Because Shilpi is a trusted project developer, she may edit and execute her evaluation workflow. The workflow must still follow these controls:

- No Gemini API key or Gemini node.
- No Discord webhook or Discord credential.
- No Postgres credential or production database write.
- No Phoenix write required for initial offline evaluation.
- No direct public Qdrant endpoint.
- No credentials embedded in workflow JSON.
- No chat-log export or bulk corpus download.
- Returned chunk text is limited to what is necessary to evaluate retrieval quality.

Access can be revoked independently by removing either her SSH public key or her n8n account.

## 4. Evaluation Workflow

Create this Git-managed artifact:

```text
workflows/n8n/rag-offline-regression-evaluation.json
```

Workflow name:

```text
RAG Offline Regression Evaluation
```

Implementation status:

- Workflow artifact created.
- No n8n credentials are referenced.
- Gemini, Discord, Postgres, and Phoenix are excluded.
- Dynamic Qdrant collection health and point counts are included in every valid result.
- Stage 1 failures skip the reranker and retain diagnostic retrieval results.
- Phase 6 dedupe remains explicitly marked as not implemented.
- Oracle deployment, n8n member creation, and tunnel provisioning remain pending.

### Nodes

| Node | Responsibility | Interface |
|---|---|---|
| Manual Trigger | Starts an interactive UI test | n8n |
| Set Evaluation Input | Accepts regression ID, question, expected action, and optional notes | Regression dataset |
| Validate Input | Rejects missing IDs or questions | n8n Code node |
| Normalize Query | Applies the production query prefix and normalization behavior | Retrieval contract |
| Embed Query | Creates the 768-dimensional query vector | `http://embedder:8000/embed` |
| Qdrant Search | Retrieves the top 20 candidates | `http://qdrant:6333`, collection `tpm_unite_history` |
| Stage 1 Gate | Applies `retrieval_score >= 0.55` | Retrieval contract |
| Prepare Reranker Batch | Creates one request containing all Stage 1 candidates | n8n Code node |
| CrossEncoder Rerank | Reranks candidates in one batch | `http://reranker:8002/rerank` |
| Reranker Gate | Applies `reranker_score > 0` and minimum-candidate rules | Retrieval contract |
| Dedupe | Applies the implemented dedupe contract when Phase 6 is available | n8n Code node |
| Build Evaluation Result | Produces a stable, reviewable output object | n8n |

Until Phase 6 is implemented, the result must state:

```json
{
  "dedupe_status": "not_implemented_phase_6"
}
```

The workflow must not imply that dedupe was performed when it was not.

## 5. Output Contract

Each run should return one result containing:

```json
{
  "regression_id": "RQ-001",
  "question": "How should I prepare for the Amazon TPM interview loop?",
  "expected_action": "answer",
  "corpus": {
    "collection": "tpm_unite_history",
    "corpus_version": "to-be-recorded",
    "points_count": 9519
  },
  "decision": "context_found",
  "refusal_reason": null,
  "stage_1": {
    "raw_count": 20,
    "passed_count": 10,
    "threshold": 0.55
  },
  "reranker": {
    "passed_count": 5,
    "threshold": 0,
    "weak_signal": false
  },
  "dedupe_status": "not_implemented_phase_6",
  "top_results": [],
  "latency_ms": {
    "embedding": 0,
    "retrieval": 0,
    "reranking": 0,
    "total": 0
  }
}
```

Each item in `top_results` should include:

- Qdrant point or chunk ID.
- Retrieval rank and score.
- Reranker rank and score.
- Channel name.
- Message IDs and Discord-link metadata when available.
- A bounded text preview suitable for evaluator review.
- Gate status and exclusion reason, when applicable.

## 6. Corpus Readiness

Before granting access:

1. Ingest the two local logs currently absent from the Oracle corpus.
2. Verify the final Qdrant point count and collection health.
3. Record a reproducible `corpus_version`, ingestion commit, log count, and ingestion timestamp.
4. Expose those values in every evaluation result.

The current Oracle collection is `tpm_unite_history` with 9,519 searchable points. It currently contains 21 of the 23 local JSON exports, so regression findings should not be treated as final until the remaining exports are ingested.

## 7. Implementation Sequence

1. Create a dedicated branch and PR for developer evaluation access.
2. Bring the Oracle corpus to the intended coverage and record its version.
3. Derive the evaluation workflow from Phase 5 without Gemini, Discord, Postgres, or Phoenix nodes.
4. Add the stable output contract and explicit Phase 6 dedupe placeholder.
5. Validate known-answer, weak-match, and malformed-input cases as the owner.
6. Create Shilpi's Linux tunnel-only account and install her public key with forwarding restrictions.
7. Invite Shilpi as an n8n member.
8. Have Shilpi import the Git-managed workflow into her personal workspace.
9. Run a single regression question together and verify UI visibility and output.
10. Run all regression questions sequentially and export the results for scoring.
11. Write a separate user guide with exact tunnel, login, import, run, export, and troubleshooting instructions.

## 8. Restricted Tunnel Setup

This section must be performed by the Oracle server administrator because it requires `sudo`. Shilpi should never receive the `ubuntu` account, its private key, or general SSH access.

### 8.1 Shilpi Generates A Dedicated Key

On Shilpi's laptop:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/tpm-rag-eval -C "shilpi-tpm-rag-eval"
```

She should set a passphrase and send only this public-key file to the server administrator:

```text
~/.ssh/tpm-rag-eval.pub
```

She must not send the private file `~/.ssh/tpm-rag-eval`.

### 8.2 Create The Tunnel-Only Linux Account

On the Oracle server, logged in as the existing `ubuntu` administrator:

```bash
sudo adduser --disabled-password --gecos "Shilpi n8n evaluation tunnel" n8n_eval_shilpi
sudo install -d -m 700 -o n8n_eval_shilpi -g n8n_eval_shilpi /home/n8n_eval_shilpi/.ssh
sudo touch /home/n8n_eval_shilpi/.ssh/authorized_keys
sudo chown n8n_eval_shilpi:n8n_eval_shilpi /home/n8n_eval_shilpi/.ssh/authorized_keys
sudo chmod 600 /home/n8n_eval_shilpi/.ssh/authorized_keys
```

Keep the account's normal shell. The SSH daemon restrictions below prevent interactive shell access while allowing the required local port forwarding.

### 8.3 Restrict The Account In SSHD

Create a dedicated SSH configuration fragment:

```bash
sudo tee /etc/ssh/sshd_config.d/90-n8n-eval-shilpi.conf > /dev/null <<'EOF'
Match User n8n_eval_shilpi
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    AllowTcpForwarding local
    GatewayPorts no
    PermitOpen 127.0.0.1:5679
    ForceCommand /usr/sbin/nologin
EOF
```

Validate the complete SSH configuration before reloading it:

```bash
sudo sshd -t
```

If that command produces no output, reload SSH without terminating existing sessions:

```bash
sudo systemctl reload ssh
sudo systemctl status ssh --no-pager
```

Keep the current administrator SSH session open until the restricted account has been tested successfully.

### 8.4 Install Shilpi's Public Key

Open the authorized-key file:

```bash
sudo nano /home/n8n_eval_shilpi/.ssh/authorized_keys
```

Paste the single line from `tpm-rag-eval.pub`. Prefix that line with the following restrictions:

```text
restrict,port-forwarding,permitopen="127.0.0.1:5679" ssh-ed25519 PUBLIC_KEY_CONTENT shilpi-tpm-rag-eval
```

Do not copy the placeholder literally. Preserve Shilpi's actual `ssh-ed25519` public key and comment after the restriction prefix.

Reapply ownership and permissions:

```bash
sudo chown n8n_eval_shilpi:n8n_eval_shilpi /home/n8n_eval_shilpi/.ssh/authorized_keys
sudo chmod 700 /home/n8n_eval_shilpi/.ssh
sudo chmod 600 /home/n8n_eval_shilpi/.ssh/authorized_keys
```

The account is constrained in both `sshd_config` and `authorized_keys`. The duplicate boundary is intentional defense in depth.

### 8.5 Shilpi Opens The Tunnel

From Shilpi's laptop, replace `ORACLE_PUBLIC_IP` with the server's public IP or DNS name:

```bash
ssh -i ~/.ssh/tpm-rag-eval \
  -N \
  -L 5679:127.0.0.1:5679 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  n8n_eval_shilpi@ORACLE_PUBLIC_IP
```

While that command remains running, Shilpi opens:

```text
http://127.0.0.1:5679
```

She then signs in using her own n8n member account. The SSH key and n8n password are separate credentials.

### 8.6 Verify The Restrictions

The permitted tunnel should work:

```bash
curl -I http://127.0.0.1:5679
```

An interactive server shell should be refused:

```bash
ssh -i ~/.ssh/tpm-rag-eval n8n_eval_shilpi@ORACLE_PUBLIC_IP
```

Forwarding to another destination should be refused:

```bash
ssh -i ~/.ssh/tpm-rag-eval \
  -N \
  -L 6333:127.0.0.1:6333 \
  -o ExitOnForwardFailure=yes \
  n8n_eval_shilpi@ORACLE_PUBLIC_IP
```

The final command must fail because only `127.0.0.1:5679` is permitted.

### 8.7 Revoke Access

To revoke only the SSH tunnel while retaining the n8n account:

```bash
sudo truncate -s 0 /home/n8n_eval_shilpi/.ssh/authorized_keys
```

To disable the Linux account completely:

```bash
sudo usermod --lock n8n_eval_shilpi
sudo pkill -u n8n_eval_shilpi || true
```

Also remove or disable Shilpi's n8n member account to revoke UI access. Both access layers should be revoked when she no longer needs the environment.

## 9. Validation Cases

| Case | Expected result |
|---|---|
| Known answer | Stage 1 and reranker pass; relevant top results are returned |
| Weak or absent context | Retrieval-only refusal with an exact gate reason; Gemini is never called |
| Missing question | Validation error before any service call |
| Embedder unavailable | Explicit `embedding_failed` result |
| Qdrant unavailable | Explicit `qdrant_failed` result |
| Reranker unavailable | Explicit `reranker_failed` result |
| Unauthorized SSH use | Shell and unrestricted forwarding are denied |
| Other user's n8n assets | Shilpi cannot see another member's workflows or credentials |

## 10. Pass Criteria

This access design passes when:

- Shilpi can open n8n only while her restricted SSH tunnel is active.
- She can sign in with her own n8n account.
- She can import, inspect, edit, and run only her copy of the evaluation workflow.
- The workflow reaches embedder, Qdrant, and reranker by Docker service name.
- No personal API key, Discord webhook, or production credential is visible or required.
- Every result identifies the corpus version and all retrieval/reranking decisions.
- The known-answer and weak-match tests produce explainable outputs.
- Removing her SSH key immediately removes tunnel access.

## 11. Deferred Work

- Add contract-compliant dedupe after Phase 6 is implemented.
- Add a batch runner after the interactive single-question workflow is validated.
- Decide whether offline evaluation results should later be persisted in a dedicated evaluation table or artifact store.
- Revisit n8n project-level RBAC if the deployment moves to an edition that supports shared projects and Viewer/Editor roles.
