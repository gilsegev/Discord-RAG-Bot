# Candidate corpus promotion

This capability replaces a full in-place rebuild with a non-serving candidate and reuses the Phase 9C maintenance and lease boundary. It does not create a second serving architecture.

## Invariants

- `rag_active` is the only normal retrieval target; physical collections are immutable corpus versions.
- `rag_active_corpus` is the single durable pointer and serializes promotion with a row lock and revision.
- Candidate manifests are namespaced by corpus version, so identical point IDs in two physical collections cannot collide.
- Export and captured Postgres messages are normalized to the same semantic fields before duplicate comparison.
- A candidate cannot promote unless its 48-case regression record names the exact physical collection, corpus version, manifest digest, and capture cutoff.
- The previous physical collection is retained; its Qdrant snapshot is additional recovery evidence, not the fast rollback mechanism.
- A failed or ambiguous alias/database transition stays in maintenance until the alias is inspected and the pointer is reconciled.

## Build and catch up

1. Record the current maximum `capture_sequence` as cutoff `S`.
2. Call the incremental worker `POST /candidate/build` with a new physical collection name and `S`.
3. The worker unions all eligible exports with captured rows through `S`, rejects normalized duplicate conflicts, chunks once with the existing chunker, audits complete one-to-one ownership and channel boundaries, embeds, and writes only to the new collection.
4. Before final regression, enter the existing Phase 9C maintenance boundary and wait for execution leases to drain.
5. Record the new maximum sequence `T`. If `T > S`, discard the unapproved candidate and build a new candidate through `T`; this is intentionally a full deterministic refresh, not a parallel incremental architecture.
6. Verify the candidate manifest and Qdrant count, then run regression with `qdrant_collection`, `target_corpus_version_id`, `target_manifest_digest`, and `target_capture_cutoff_sequence` from the candidate evidence.
7. Record the passing regression with `rag_record_candidate_regression`. Any later candidate mutation invalidates this evidence by contract.

Messages captured after `T` remain in `rag_pending_chunk_work`. After promotion, Phase 9C processes them against `rag_active`; it must use the active pointer revision so a plan made against the former corpus cannot mutate the new one.

## Promote

1. Confirm `rag_active_corpus.state = 'maintenance'`, no live lease remains, and the approved candidate cutoff is not behind the active cutoff.
2. Call `POST /candidate/promote` with the candidate ID and `logical_alias: rag_active`.
3. The worker durably records switching intent, snapshots the current physical collection, moves the alias in one Qdrant aliases request, then commits the matching active pointer.
4. Reopen serving only after the pointer and observed alias agree. Do not delete the prior collection.

Because Qdrant and Postgres do not share a transaction, an interruption can leave the promotion in `switching`. Inspect the Qdrant alias: if it names the recorded target, finish the database commit; if it names the recorded previous collection, restore the candidate to `regression_passed`; any other target is corruption and must remain fail-closed in maintenance.

## Roll back

Enter maintenance, drain leases, then call `POST /candidate/rollback` with the promotion ID. The worker atomically moves `rag_active` back to the exact retained previous collection and commits its exact corpus version, manifest digest, and cutoff as the active pointer. Any database error after the alias request is treated as outcome-ambiguous and leaves maintenance in place for reconciliation.

## Test

- Run `python -m unittest ingestion.test_candidate_rebuild`.
- Run `python -m py_compile ingestion/candidate_rebuild.py deploy/phase0/incremental-worker/app.py`.
- Run all Phase 9C workflow and Postgres contract tests before deployment.
- In a disposable Qdrant/Postgres environment, inject failures after snapshot, alias switch, and pointer commit; verify readers see only the old or new corpus and ambiguous outcomes remain in maintenance.

## Pass when

- Duplicate comparison is order-independent and accepts representation-only differences while rejecting semantic differences.
- Candidate construction never changes `rag_active`.
- Regression evidence cannot be attached to a different collection, version, digest, or cutoff.
- Concurrent promotion attempts admit one transition only.
- Promotion and rollback each change the alias atomically and the durable pointer names the same exact corpus pair.
- Captures through the final cutoff are represented exactly once and later captures remain pending for Phase 9C.
