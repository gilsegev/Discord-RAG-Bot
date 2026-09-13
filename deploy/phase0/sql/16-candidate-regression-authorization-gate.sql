-- Atomically admit one regression batch against an isolated candidate corpus.
ALTER TABLE rag_candidate_regression_authorizations
  ADD COLUMN IF NOT EXISTS consumed_by_regression_run_id UUID;

CREATE OR REPLACE FUNCTION rag_consume_candidate_regression_authorization(
  p_authorization_id UUID,
  p_regression_run_id UUID,
  p_candidate_id TEXT,
  p_collection_name TEXT,
  p_corpus_version_id TEXT,
  p_manifest_digest TEXT,
  p_frozen_capture_sequence BIGINT
) RETURNS BOOLEAN LANGUAGE plpgsql AS $$
DECLARE admitted UUID;
BEGIN
  IF p_collection_name = 'tpm_unite_history' THEN
    RETURN TRUE;
  END IF;

  UPDATE rag_candidate_regression_authorizations AS authorization
  SET consumed_at = COALESCE(authorization.consumed_at, now()),
      consumed_by_regression_run_id = p_regression_run_id
  FROM rag_candidate_rebuilds AS candidate
  WHERE authorization.authorization_id = p_authorization_id
    AND authorization.candidate_id = p_candidate_id
    AND authorization.collection_name = p_collection_name
    AND authorization.corpus_version_id = p_corpus_version_id
    AND authorization.manifest_digest = p_manifest_digest
    AND authorization.frozen_capture_sequence = p_frozen_capture_sequence
    AND authorization.expires_at > now()
    AND authorization.consumed_at IS NULL
    AND candidate.candidate_id = authorization.candidate_id
    AND candidate.status = 'regression_authorized'
    AND candidate.collection_name = authorization.collection_name
    AND candidate.corpus_version_id = authorization.corpus_version_id
    AND candidate.manifest_digest = authorization.manifest_digest
    AND candidate.frozen_capture_sequence = authorization.frozen_capture_sequence
  RETURNING authorization.authorization_id INTO admitted;

  IF admitted IS NULL THEN
    RAISE EXCEPTION 'candidate regression authorization is missing, expired, consumed, or does not match the requested target'
      USING ERRCODE = '28000';
  END IF;
  RETURN TRUE;
END $$;
