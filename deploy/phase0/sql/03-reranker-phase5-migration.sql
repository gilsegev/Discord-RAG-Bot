ALTER TABLE rag_retrieval_results
    ADD COLUMN IF NOT EXISTS reranker_score DOUBLE PRECISION;

ALTER TABLE rag_retrieval_results
    ADD COLUMN IF NOT EXISTS boosted_reranker_score DOUBLE PRECISION;
