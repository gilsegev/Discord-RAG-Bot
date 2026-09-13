# Candidate corpus promotion implementation review

## Scope reviewed

The review covered the candidate build adapter, worker endpoints, migration 15 state machine, serving and regression workflows, canonical manifest handoff, active-pointer revision checks, and the operator runbook against the production-safe rebuild brief.

## Deviations and fixes

- **Fixed — migration collision:** PR #65 used migration 11 although main already owned 11–14; the implementation uses migration 15.
- **Fixed — direct serving bypass:** intake and shared core now default to the stable `rag_active` alias.
- **Fixed — ambiguous commits:** switching intent is durable before Qdrant mutation and ambiguous commit outcomes remain fail-closed for reconciliation.
- **Fixed — stale or mutated candidates:** promotion rescans Qdrant and verifies both the ownership manifest and full payload digest.
- **Fixed — incomplete handoff:** promotion installs the candidate into the canonical serving manifest and archives manifests by corpus version; rollback restores the exact previous version.
- **Fixed — stale incremental work:** Phase 9C plans record logical pointer revision and physical target, recheck them before mutation, and advance the pointer plus versioned manifest on commit.
- **Fixed — split maintenance admission:** reads and Phase 9C use the stable `rag_active` runtime key; candidate transitions reject any other non-serving corpus runtime and require a maintenance owner at the safe pre-replacement state.
- **Accepted narrow duplication:** candidate construction still has a small adapter for full-corpus payload creation and embedding, but it reuses the existing chunker, ownership planner, Qdrant payload contract, embedder service, Phase 9C maintenance boundary, and canonical manifest. Extracting a universal ingestion engine would widen this production-safety change.

## Verification

- Candidate, planner, and promotion integration tests pass.
- Python compilation and workflow contract tests pass.
- Phase 9C.3.5–9C.6 workflow tests pass.
- PostgreSQL integration tests could not run because no local PostgreSQL/Docker service is available; production was intentionally not used.
- Two reply-root tests and one citation-link test fail unchanged on clean `origin/main` and are not caused by this branch.

## As built

```text
Discord -> n8n -> lease(rag_active) -> Qdrant alias
                    |                     |
                    v                     v
               Phase 9C --------> active physical corpus
                    |
                    v
              Postgres pointer + versioned manifests
```

```text
candidate adapter -> normalize -> chunk -> audit -> embed
                                      |             |
                                      v             v
                              candidate manifest  Qdrant
```

```text
plan -> resolve pointer -> bind target/version/revision
apply -> recheck -> mutate target -> CAS pointer + archive
```

```text
build at S -> catch up/rebuild at T -> regress exact T
 -> maintenance/drain -> snapshot -> alias swap -> handoff
 -> Phase 9C processes captures after T
rollback -> drain -> alias back -> exact manifest restore
```
