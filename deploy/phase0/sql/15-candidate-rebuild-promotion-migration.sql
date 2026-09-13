-- Candidate corpus build and serialized alias promotion. Safe to rerun.
CREATE TABLE IF NOT EXISTS rag_candidate_rebuilds (
 candidate_id TEXT PRIMARY KEY, corpus_version_id TEXT NOT NULL UNIQUE,
 collection_name TEXT NOT NULL UNIQUE CHECK(collection_name<>'rag_active'),
 frozen_capture_sequence BIGINT NOT NULL, chunker_version TEXT NOT NULL,
 embedding_version TEXT NOT NULL, vector_size INTEGER NOT NULL DEFAULT 768 CHECK(vector_size>0),
 vector_distance TEXT NOT NULL DEFAULT 'Cosine', manifest_digest TEXT NOT NULL, candidate_payload_digest TEXT NOT NULL,
 point_count INTEGER NOT NULL CHECK(point_count>=0), source_message_count INTEGER NOT NULL CHECK(source_message_count>=0),
 source_message_digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('built','regression_passed','rejected','promoting','promoted','rolled_back')),
 evidence JSONB NOT NULL CHECK(jsonb_typeof(evidence)='object'), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 regression_run_id TEXT, regression_target JSONB, promoted_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS rag_candidate_chunk_manifest (
 candidate_id TEXT NOT NULL REFERENCES rag_candidate_rebuilds(candidate_id) ON DELETE RESTRICT,
 corpus_version_id TEXT NOT NULL, collection_name TEXT NOT NULL, point_id TEXT NOT NULL,
 logical_group_id TEXT NOT NULL, channel_id TEXT NOT NULL, thread_id TEXT, root_message_id TEXT,
 message_ids TEXT[] NOT NULL CHECK(cardinality(message_ids)>0), first_message_id TEXT NOT NULL,
 last_message_id TEXT NOT NULL, payload_digest TEXT NOT NULL, PRIMARY KEY(corpus_version_id,point_id), UNIQUE(candidate_id,point_id));
CREATE TABLE IF NOT EXISTS rag_active_corpus (
 logical_name TEXT PRIMARY KEY, control_collection_name TEXT NOT NULL REFERENCES rag_runtime_state(collection_name),
 collection_name TEXT NOT NULL, corpus_version_id TEXT NOT NULL,
 manifest_digest TEXT NOT NULL, capture_cutoff_sequence BIGINT NOT NULL, previous_collection_name TEXT,
 previous_corpus_version_id TEXT, previous_manifest_digest TEXT, previous_capture_cutoff_sequence BIGINT,
 promotion_id TEXT, state TEXT NOT NULL CHECK(state IN ('serving','maintenance','switching','rollback_switching')),
 revision BIGINT NOT NULL DEFAULT 1, changed_at TIMESTAMPTZ NOT NULL DEFAULT now(), CHECK(logical_name<>collection_name));
CREATE TABLE IF NOT EXISTS rag_candidate_promotions (
 promotion_id TEXT PRIMARY KEY, logical_name TEXT NOT NULL REFERENCES rag_active_corpus(logical_name),
 candidate_id TEXT NOT NULL REFERENCES rag_candidate_rebuilds(candidate_id), previous_collection_name TEXT NOT NULL,
 previous_corpus_version_id TEXT NOT NULL, previous_manifest_digest TEXT NOT NULL, previous_capture_cutoff_sequence BIGINT NOT NULL,
 target_collection_name TEXT NOT NULL, target_corpus_version_id TEXT NOT NULL, target_manifest_digest TEXT NOT NULL,
 target_capture_cutoff_sequence BIGINT NOT NULL, expected_revision BIGINT NOT NULL, snapshot_name TEXT,
 status TEXT NOT NULL CHECK(status IN ('switching','promoted','rollback_switching','rolled_back','aborted','reconciliation_required')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_candidate_one_transition ON rag_candidate_promotions(logical_name)
 WHERE status IN ('switching','rollback_switching','reconciliation_required');
INSERT INTO rag_active_corpus(logical_name,control_collection_name,collection_name,corpus_version_id,
 manifest_digest,capture_cutoff_sequence,state)
SELECT 'rag_active',v.collection_name,v.collection_name,v.corpus_version_id,v.manifest_digest,
 coalesce((SELECT max(capture_sequence) FROM rag_discord_messages),0),'serving'
FROM rag_corpus_versions v JOIN rag_runtime_state s ON s.collection_name=v.collection_name
WHERE v.status='healthy' ORDER BY v.activated_at DESC NULLS LAST LIMIT 1
ON CONFLICT(logical_name) DO NOTHING;
ALTER TABLE rag_regression_runs ADD COLUMN IF NOT EXISTS target_collection_name TEXT,
 ADD COLUMN IF NOT EXISTS target_corpus_version_id TEXT, ADD COLUMN IF NOT EXISTS target_manifest_digest TEXT,
 ADD COLUMN IF NOT EXISTS target_capture_cutoff_sequence BIGINT;

CREATE OR REPLACE FUNCTION rag_record_candidate_regression(p_candidate_id TEXT,p_run_id TEXT) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE c rag_candidate_rebuilds%ROWTYPE; r rag_regression_runs%ROWTYPE; BEGIN
 SELECT * INTO c FROM rag_candidate_rebuilds WHERE candidate_id=p_candidate_id FOR UPDATE;
 SELECT * INTO r FROM rag_regression_runs WHERE run_id::text=p_run_id;
 IF c.status<>'built' OR r.status<>'completed' OR r.case_count<>48 OR r.target_collection_name IS DISTINCT FROM c.collection_name
  OR r.target_corpus_version_id IS DISTINCT FROM c.corpus_version_id OR r.target_manifest_digest IS DISTINCT FROM c.manifest_digest
  OR r.target_capture_cutoff_sequence IS DISTINCT FROM c.frozen_capture_sequence OR r.fail_count<>1 OR r.pass_count<>43 OR r.review_count<>4
 THEN RAISE EXCEPTION 'regression evidence does not match candidate' USING ERRCODE='55000'; END IF;
 UPDATE rag_candidate_rebuilds SET status='regression_passed',regression_run_id=p_run_id,
 regression_target=jsonb_build_object('collection',collection_name,'corpus_version',corpus_version_id,'manifest_digest',manifest_digest,'capture_cutoff',frozen_capture_sequence)
 WHERE candidate_id=p_candidate_id; END $$;

CREATE OR REPLACE FUNCTION rag_begin_candidate_promotion(p_candidate_id TEXT,p_logical_name TEXT)
RETURNS TABLE(previous_collection TEXT,target_collection TEXT,promotion_id TEXT) LANGUAGE plpgsql AS $$
DECLARE a rag_active_corpus%ROWTYPE; c rag_candidate_rebuilds%ROWTYPE; p TEXT; BEGIN
 SELECT * INTO a FROM rag_active_corpus WHERE logical_name=p_logical_name FOR UPDATE;
 SELECT * INTO c FROM rag_candidate_rebuilds WHERE candidate_id=p_candidate_id FOR UPDATE;
 IF a.state<>'serving' OR NOT EXISTS(SELECT 1 FROM rag_runtime_state WHERE collection_name=a.control_collection_name AND runtime_state='maintenance')
 THEN RAISE EXCEPTION 'promotion requires Phase 9C maintenance' USING ERRCODE='55000'; END IF;
 IF EXISTS(SELECT 1 FROM rag_active_execution_leases WHERE collection_name=a.control_collection_name AND released_at IS NULL AND expires_at>clock_timestamp())
 THEN RAISE EXCEPTION 'active execution leases have not drained' USING ERRCODE='55000'; END IF;
 IF c.status<>'regression_passed' OR c.frozen_capture_sequence<a.capture_cutoff_sequence
 THEN RAISE EXCEPTION 'candidate is unapproved or stale' USING ERRCODE='55000'; END IF;
 p:='promotion-'||substr(encode(digest(p_candidate_id||':'||a.revision::text,'sha256'),'hex'),1,24);
 INSERT INTO rag_candidate_promotions VALUES(p,p_logical_name,p_candidate_id,a.collection_name,a.corpus_version_id,a.manifest_digest,
 a.capture_cutoff_sequence,c.collection_name,c.corpus_version_id,c.manifest_digest,c.frozen_capture_sequence,a.revision,NULL,'switching',now(),NULL);
 UPDATE rag_active_corpus SET state='switching',promotion_id=p,revision=revision+1,changed_at=now() WHERE logical_name=p_logical_name;
 UPDATE rag_candidate_rebuilds SET status='promoting' WHERE candidate_id=p_candidate_id;
 RETURN QUERY SELECT a.collection_name,c.collection_name,p; END $$;
CREATE OR REPLACE FUNCTION rag_record_candidate_snapshot(p_promotion_id TEXT,p_snapshot_name TEXT) RETURNS VOID LANGUAGE plpgsql AS $$ BEGIN
 IF btrim(coalesce(p_snapshot_name,''))='' THEN RAISE EXCEPTION 'snapshot name required'; END IF;
 UPDATE rag_candidate_promotions SET snapshot_name=p_snapshot_name WHERE promotion_id=p_promotion_id AND status='switching';
 IF NOT FOUND THEN RAISE EXCEPTION 'promotion is not switching' USING ERRCODE='55000'; END IF; END $$;
CREATE OR REPLACE FUNCTION rag_commit_candidate_promotion(p_promotion_id TEXT) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE p rag_candidate_promotions%ROWTYPE; BEGIN SELECT * INTO p FROM rag_candidate_promotions WHERE promotion_id=p_promotion_id FOR UPDATE;
 IF p.status<>'switching' OR p.snapshot_name IS NULL THEN RAISE EXCEPTION 'promotion not committable' USING ERRCODE='55000'; END IF;
 UPDATE rag_active_corpus SET previous_collection_name=p.previous_collection_name,previous_corpus_version_id=p.previous_corpus_version_id,
 previous_manifest_digest=p.previous_manifest_digest,previous_capture_cutoff_sequence=p.previous_capture_cutoff_sequence,
 collection_name=p.target_collection_name,corpus_version_id=p.target_corpus_version_id,manifest_digest=p.target_manifest_digest,
 capture_cutoff_sequence=p.target_capture_cutoff_sequence,state='serving',revision=revision+1,changed_at=now()
 WHERE logical_name=p.logical_name AND state='switching' AND promotion_id=p_promotion_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'active pointer changed' USING ERRCODE='40001'; END IF;
 UPDATE rag_candidate_promotions SET status='promoted',completed_at=now() WHERE promotion_id=p_promotion_id;
 UPDATE rag_candidate_rebuilds SET status='promoted',promoted_at=now() WHERE candidate_id=p.candidate_id; END $$;
CREATE OR REPLACE FUNCTION rag_begin_candidate_rollback(p_promotion_id TEXT)
RETURNS TABLE(current_collection TEXT,previous_collection TEXT,logical_alias TEXT) LANGUAGE plpgsql AS $$
DECLARE p rag_candidate_promotions%ROWTYPE; a rag_active_corpus%ROWTYPE; BEGIN
 SELECT * INTO p FROM rag_candidate_promotions WHERE promotion_id=p_promotion_id FOR UPDATE;
 SELECT * INTO a FROM rag_active_corpus WHERE logical_name=p.logical_name FOR UPDATE;
 IF p.status<>'promoted' OR a.state<>'serving' OR a.collection_name<>p.target_collection_name
  OR NOT EXISTS(SELECT 1 FROM rag_runtime_state WHERE collection_name=a.control_collection_name AND runtime_state='maintenance')
 THEN RAISE EXCEPTION 'rollback requires matching promoted corpus in maintenance' USING ERRCODE='55000'; END IF;
 IF EXISTS(SELECT 1 FROM rag_active_execution_leases WHERE collection_name=a.control_collection_name AND released_at IS NULL AND expires_at>clock_timestamp())
 THEN RAISE EXCEPTION 'active execution leases have not drained' USING ERRCODE='55000'; END IF;
 UPDATE rag_candidate_promotions SET status='rollback_switching' WHERE promotion_id=p_promotion_id;
 UPDATE rag_active_corpus SET state='rollback_switching',revision=revision+1 WHERE logical_name=p.logical_name;
 RETURN QUERY SELECT p.target_collection_name,p.previous_collection_name,p.logical_name; END $$;
CREATE OR REPLACE FUNCTION rag_commit_candidate_rollback(p_promotion_id TEXT) RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE p rag_candidate_promotions%ROWTYPE; BEGIN SELECT * INTO p FROM rag_candidate_promotions WHERE promotion_id=p_promotion_id FOR UPDATE;
 IF p.status<>'rollback_switching' THEN RAISE EXCEPTION 'rollback not committable' USING ERRCODE='55000'; END IF;
 UPDATE rag_active_corpus SET collection_name=p.previous_collection_name,corpus_version_id=p.previous_corpus_version_id,
 manifest_digest=p.previous_manifest_digest,capture_cutoff_sequence=p.previous_capture_cutoff_sequence,state='serving',revision=revision+1,changed_at=now()
 WHERE logical_name=p.logical_name AND state='rollback_switching';
 IF NOT FOUND THEN RAISE EXCEPTION 'active pointer changed' USING ERRCODE='40001'; END IF;
 UPDATE rag_candidate_promotions SET status='rolled_back',completed_at=now() WHERE promotion_id=p_promotion_id;
 UPDATE rag_candidate_rebuilds SET status='rolled_back' WHERE candidate_id=p.candidate_id; END $$;

CREATE OR REPLACE FUNCTION rag_reconcile_candidate_switch(p_promotion_id TEXT,p_observed_collection TEXT)
RETURNS TEXT LANGUAGE plpgsql AS $$ DECLARE p rag_candidate_promotions%ROWTYPE; BEGIN
 SELECT * INTO p FROM rag_candidate_promotions WHERE promotion_id=p_promotion_id FOR UPDATE;
 IF p.status='switching' AND p_observed_collection=p.target_collection_name THEN
   PERFORM rag_commit_candidate_promotion(p_promotion_id); RETURN 'promoted';
 ELSIF p.status='switching' AND p_observed_collection=p.previous_collection_name THEN
   UPDATE rag_active_corpus SET state='serving',promotion_id=NULL,revision=revision+1 WHERE logical_name=p.logical_name;
   UPDATE rag_candidate_promotions SET status='aborted',completed_at=now() WHERE promotion_id=p_promotion_id;
   UPDATE rag_candidate_rebuilds SET status='regression_passed' WHERE candidate_id=p.candidate_id; RETURN 'previous_serving';
 ELSIF p.status='rollback_switching' AND p_observed_collection=p.previous_collection_name THEN
   PERFORM rag_commit_candidate_rollback(p_promotion_id); RETURN 'rolled_back';
 ELSIF p.status='rollback_switching' AND p_observed_collection=p.target_collection_name THEN
   UPDATE rag_active_corpus SET state='serving',revision=revision+1 WHERE logical_name=p.logical_name;
   UPDATE rag_candidate_promotions SET status='promoted' WHERE promotion_id=p_promotion_id; RETURN 'target_serving';
 ELSE
   RETURN 'unknown_target';
 END IF; END $$;
