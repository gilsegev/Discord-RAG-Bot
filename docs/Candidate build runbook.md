# Isolated candidate corpus build

This capability creates and evaluates a non-serving Qdrant collection. It cannot switch serving traffic, replace the canonical manifest, promote, or roll back a corpus.

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

Send the returned authorization ID, candidate ID, collection, corpus version, manifest digest, and cutoff together to the authenticated regression workflow. The workflow consumes that short-lived authorization before it calls intake; omitted, mismatched, expired, or previously consumed authorization fails closed. Ordinary serving regressions against `tpm_unite_history` do not require candidate authorization. This path does not add any promotion or cutover behavior.

## Test

- `python -m unittest ingestion.test_candidate_rebuild`
- `python -m py_compile ingestion/candidate_rebuild.py ingestion/candidate_build_cli.py`

## Pass when

- A caller cannot claim a cutoff other than the observed database maximum.
- Equivalent export/capture rows deduplicate after field normalization and semantic conflicts fail.
- Every admitted source message is owned exactly once and never crosses channel boundaries.
- Only a new physical Qdrant collection is written, and no serving state is changed.
- Candidate regression targeting requires a live authorization derived from persisted candidate evidence.
