# Discord RAG Corpus

This context names the durable concepts used to keep the Discord knowledge
corpus current without rebuilding it in full.

## Language

**Catch-up run**:
A one-time incremental run that moves a proven, continuously captured backlog
into the current corpus through a fixed cutoff.
_Avoid_: Migration ingestion, backlog dump

**Capture coverage**:
Evidence that every eligible Discord message in a defined interval is present
in the durable capture store, including explicit recovery of any gap.
_Avoid_: Assuming all messages were stored

**Corpus boundary**:
The final Discord message represented by a specific healthy corpus version.
_Avoid_: Latest Qdrant update time

**Known-gap checklist**:
A minimum set of real Discord message IDs observed in other durable evidence
but absent from the current corpus; it supports reconciliation but does not
prove capture coverage.
_Avoid_: Complete gap inventory
