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

  UPDATE rag_candidate_regression_authorizations AS auth
  SET consumed_at = COALESCE(auth.consumed_at, now()),
      consumed_by_regression_run_id = p_regression_run_id
  FROM rag_candidate_rebuilds AS candidate
  WHERE auth.authorization_id = p_authorization_id
    AND auth.candidate_id = p_candidate_id
    AND auth.collection_name = p_collection_name
    AND auth.corpus_version_id = p_corpus_version_id
    AND auth.manifest_digest = p_manifest_digest
    AND auth.frozen_capture_sequence = p_frozen_capture_sequence
    AND auth.expires_at > now()
    AND auth.consumed_at IS NULL
    AND candidate.candidate_id = auth.candidate_id
    AND candidate.status = 'regression_authorized'
    AND candidate.collection_name = auth.collection_name
    AND candidate.corpus_version_id = auth.corpus_version_id
    AND candidate.manifest_digest = auth.manifest_digest
    AND candidate.frozen_capture_sequence = auth.frozen_capture_sequence
  RETURNING auth.authorization_id INTO admitted;

  IF admitted IS NULL THEN
    RAISE EXCEPTION 'candidate regression authorization is missing, expired, consumed, or does not match the requested target'
      USING ERRCODE = '28000';
  END IF;
  RETURN TRUE;
END $$;
