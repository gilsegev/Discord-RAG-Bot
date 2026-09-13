-- Candidate rebuilds are isolated from the serving manifest and corpus state.
CREATE TABLE IF NOT EXISTS rag_candidate_rebuilds (
  candidate_id TEXT PRIMARY KEY,
  collection_name TEXT NOT NULL UNIQUE,
  frozen_capture_sequence BIGINT NOT NULL,
  manifest_digest TEXT NOT NULL,
  point_count INTEGER NOT NULL CHECK (point_count >= 0),
  source_message_count INTEGER NOT NULL CHECK (source_message_count >= 0),
  source_message_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('built','regression_passed','rejected','promoted','rolled_back')),
  evidence JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  regression_run_id TEXT,
  snapshot_name TEXT,
  promoted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS rag_candidate_chunk_manifest (
  candidate_id TEXT NOT NULL REFERENCES rag_candidate_rebuilds(candidate_id) ON DELETE CASCADE,
  point_id TEXT NOT NULL,
  logical_group_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  thread_id TEXT,
  root_message_id TEXT,
  message_ids TEXT[] NOT NULL CHECK (cardinality(message_ids) > 0),
  first_message_id TEXT NOT NULL,
  last_message_id TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  PRIMARY KEY (candidate_id, point_id)
);
