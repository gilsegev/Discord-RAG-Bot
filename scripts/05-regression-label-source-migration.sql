-- Allow Phase 8 regression runs to write automated labels without
-- pretending they were human or LLM-judge labels.

ALTER TABLE rag_eval_labels
    DROP CONSTRAINT IF EXISTS rag_eval_labels_source_check;

ALTER TABLE rag_eval_labels
    ADD CONSTRAINT rag_eval_labels_source_check
    CHECK (source IN ('human', 'judge', 'regression'));
