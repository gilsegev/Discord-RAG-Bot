-- Isolated, non-serving candidate corpus evidence. Safe to rerun.
CREATE TABLE IF NOT EXISTS rag_candidate_rebuilds (
 candidate_id TEXT PRIMARY KEY, corpus_version_id TEXT NOT NULL UNIQUE,
 collection_name TEXT NOT NULL UNIQUE CHECK(collection_name<>'tpm_unite_history'),
 frozen_capture_sequence BIGINT NOT NULL, frozen_at TIMESTAMPTZ,
 chunker_version TEXT NOT NULL, embedding_version TEXT NOT NULL,
 vector_size INTEGER NOT NULL CHECK(vector_size>0), vector_distance TEXT NOT NULL,
 manifest_digest TEXT NOT NULL, candidate_payload_digest TEXT NOT NULL,
 point_count INTEGER NOT NULL CHECK(point_count>=0), source_message_count INTEGER NOT NULL CHECK(source_message_count>=0),
 source_message_digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('built','regression_authorized','regression_passed','rejected')),
 evidence JSONB NOT NULL CHECK(jsonb_typeof(evidence)='object'), created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS rag_candidate_chunk_manifest (
 candidate_id TEXT NOT NULL REFERENCES rag_candidate_rebuilds(candidate_id) ON DELETE RESTRICT,
 corpus_version_id TEXT NOT NULL, collection_name TEXT NOT NULL, point_id TEXT NOT NULL,
 logical_group_id TEXT NOT NULL, channel_id TEXT NOT NULL, thread_id TEXT, root_message_id TEXT,
 message_ids TEXT[] NOT NULL CHECK(cardinality(message_ids)>0), first_message_id TEXT NOT NULL,
 last_message_id TEXT NOT NULL, payload_digest TEXT NOT NULL, PRIMARY KEY(corpus_version_id,point_id), UNIQUE(candidate_id,point_id));
CREATE TABLE IF NOT EXISTS rag_candidate_regression_authorizations (
 authorization_id UUID PRIMARY KEY DEFAULT gen_random_uuid(), candidate_id TEXT NOT NULL REFERENCES rag_candidate_rebuilds(candidate_id),
 collection_name TEXT NOT NULL, corpus_version_id TEXT NOT NULL, manifest_digest TEXT NOT NULL,
 frozen_capture_sequence BIGINT NOT NULL, expires_at TIMESTAMPTZ NOT NULL DEFAULT now()+interval '2 hours',
 consumed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE OR REPLACE FUNCTION rag_authorize_candidate_regression(p_candidate_id TEXT)
RETURNS SETOF rag_candidate_regression_authorizations LANGUAGE plpgsql AS $$ DECLARE c rag_candidate_rebuilds%ROWTYPE; BEGIN
 SELECT * INTO c FROM rag_candidate_rebuilds WHERE candidate_id=p_candidate_id FOR UPDATE;
 IF c.status NOT IN('built','regression_authorized') THEN RAISE EXCEPTION 'candidate is not regression eligible' USING ERRCODE='55000'; END IF;
 UPDATE rag_candidate_rebuilds SET status='regression_authorized' WHERE candidate_id=p_candidate_id;
 RETURN QUERY INSERT INTO rag_candidate_regression_authorizations(candidate_id,collection_name,corpus_version_id,manifest_digest,frozen_capture_sequence)
 VALUES(c.candidate_id,c.collection_name,c.corpus_version_id,c.manifest_digest,c.frozen_capture_sequence) RETURNING *; END $$;
