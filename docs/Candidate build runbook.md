# Isolated candidate corpus build

This capability creates and evaluates a non-serving Qdrant collection. Promotion is a separate, explicit operation after regression passes.

## Build

Apply migration 15 in a non-production validation environment, then run:

```text
python -m ingestion.candidate_build_cli --collection candidate-YYYYMMDD
```

The database-derived maximum capture sequence is the admitted cutoff. An optional `--requested-cutoff` is an assertion and must equal that observed maximum; future or stale values are rejected. Export messages newer than the cutoff timestamp are also rejected. The builder normalizes export and Postgres field shapes, rejects conflicting duplicate IDs, audits complete single ownership and channel boundaries, creates a previously absent physical collection, embeds through the existing embedder service, verifies the point count, and commits an isolated version-namespaced manifest.

If the build fails after collection creation, the new non-serving collection is deleted and database evidence is rolled back.

## Candidate regression

Request a short-lived database-backed target authorization:

```text
python -m ingestion.candidate_build_cli --authorize-regression candidate-ID
```

Send the returned authorization ID, candidate ID, collection, corpus version, manifest digest, and cutoff together to the authenticated regression workflow. The workflow consumes that short-lived authorization before it calls intake; omitted, mismatched, expired, or previously consumed authorization fails closed. Ordinary serving regressions do not require candidate authorization.

## Promote

Disable the current incremental schedule, wait for active retrieval and incremental
runs to drain, apply migration 17, and run the authorized candidate regression.
Then call `rag_promote_candidate_manifest(candidate_id, regression_run_id,
previous_collection_name)` with the exact 48-case candidate regression run. The function
fails closed unless the run consumed an authorization bound to the candidate and
the capture cutoff is still current.

The promotion transaction snapshots the previous active manifest, installs the
candidate manifest and ownership rows, records the candidate as the healthy corpus,
marks messages through the frozen cutoff complete, disables the old schedule, and
enables the incremental schedule for the promoted collection. Keep the previous
Qdrant collection until production retrieval and incremental validation pass. After
the transaction commits, push all serving, regression, and incremental workflows
with the promoted collection as their default, verify those defaults, and run the
production smoke and regression checks. If workflow deployment fails, restore the
previous workflow defaults and leave the promoted schedule disabled for review.

## Test

- `python -m unittest ingestion.test_candidate_rebuild`
- `python -m py_compile ingestion/candidate_rebuild.py ingestion/candidate_build_cli.py`
- Execute the promotion function inside `BEGIN ... ROLLBACK` and verify the
  candidate manifest count, healthy corpus, and schedule target before committing.

## Pass when

- A caller cannot claim a cutoff other than the observed database maximum.
- Equivalent export/capture rows deduplicate after field normalization and semantic conflicts fail.
- Every admitted source message is owned exactly once and never crosses channel boundaries.
- Only a new physical Qdrant collection is written, and no serving state is changed.
- Candidate regression targeting requires a live authorization derived from persisted candidate evidence.
- Promotion preserves the previous manifest snapshot and makes exactly one
  collection the active manifest and incremental schedule target.
