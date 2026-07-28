#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 RAILWAY_POSTGRES_HOST RAILWAY_POSTGRES_PORT" >&2
  exit 2
fi

target_host="$1"
target_port="$2"
repo_root="${REPO_ROOT:-$HOME/Discord-RAG-Bot}"
compose_root="$repo_root/deploy/phase0"

cd "$compose_root"
set -a
# shellcheck disable=SC1091
source .env
set +a

run_target_sql() {
  local database="$1"
  docker compose exec -T \
    -e PGPASSWORD="$POSTGRES_PASSWORD" \
    postgres psql \
    -X \
    -v ON_ERROR_STOP=1 \
    -h "$target_host" \
    -p "$target_port" \
    -U ragbot_admin \
    -d "$database"
}

run_target_sql ragbot <<'SQL'
BEGIN;

CREATE EXTENSION IF NOT EXISTS postgres_fdw;
DROP SERVER IF EXISTS oracle_final_server CASCADE;
DROP SCHEMA IF EXISTS oracle_final CASCADE;
CREATE SERVER oracle_final_server
  FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (dbname 'oracle_final_ragbot');
CREATE USER MAPPING FOR CURRENT_USER
  SERVER oracle_final_server
  OPTIONS (user 'ragbot_admin');
CREATE SCHEMA oracle_final;
IMPORT FOREIGN SCHEMA public FROM SERVER oracle_final_server INTO oracle_final;

WITH inserted AS (
  INSERT INTO public.rag_transactions
  SELECT source.*
  FROM oracle_final.rag_transactions AS source
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.rag_transactions AS target
    WHERE target.transaction_id = source.transaction_id
  )
  RETURNING 1
)
SELECT 'rag_transactions_inserted' AS metric, count(*) AS value FROM inserted;

WITH inserted AS (
  INSERT INTO public.rag_regression_runs
  SELECT source.*
  FROM oracle_final.rag_regression_runs AS source
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.rag_regression_runs AS target
    WHERE target.run_id = source.run_id
  )
  RETURNING 1
)
SELECT 'rag_regression_runs_inserted' AS metric, count(*) AS value FROM inserted;

WITH inserted AS (
  INSERT INTO public.rag_trace_events (
    transaction_id,
    event_name,
    node_name,
    status,
    latency_ms,
    event_payload,
    created_at
  )
  SELECT
    source.transaction_id,
    source.event_name,
    source.node_name,
    source.status,
    source.latency_ms,
    source.event_payload,
    source.created_at
  FROM oracle_final.rag_trace_events AS source
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.rag_trace_events AS target
    WHERE target.transaction_id = source.transaction_id
      AND target.event_name IS NOT DISTINCT FROM source.event_name
      AND target.node_name IS NOT DISTINCT FROM source.node_name
      AND target.created_at IS NOT DISTINCT FROM source.created_at
  )
  RETURNING 1
)
SELECT 'rag_trace_events_inserted' AS metric, count(*) AS value FROM inserted;

WITH inserted AS (
  INSERT INTO public.rag_retrieval_results (
    transaction_id,
    qdrant_point_id,
    rank,
    retrieval_score,
    reranker_score,
    boosted_reranker_score,
    channel_id,
    channel_name,
    thread_name,
    first_message_id,
    message_ids,
    start_ts,
    end_ts,
    payload,
    created_at,
    dedupe_status,
    dedupe_reason,
    dedupe_matched_chunk_id,
    dedupe_overlap_ratio,
    dedupe_shared_message_count,
    rank_after_dedupe,
    selected_for_context
  )
  SELECT
    source.transaction_id,
    source.qdrant_point_id,
    source.rank,
    source.retrieval_score,
    source.reranker_score,
    source.boosted_reranker_score,
    source.channel_id,
    source.channel_name,
    source.thread_name,
    source.first_message_id,
    source.message_ids,
    source.start_ts,
    source.end_ts,
    source.payload,
    source.created_at,
    source.dedupe_status,
    source.dedupe_reason,
    source.dedupe_matched_chunk_id,
    source.dedupe_overlap_ratio,
    source.dedupe_shared_message_count,
    source.rank_after_dedupe,
    source.selected_for_context
  FROM oracle_final.rag_retrieval_results AS source
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.rag_retrieval_results AS target
    WHERE target.transaction_id = source.transaction_id
      AND target.qdrant_point_id IS NOT DISTINCT FROM source.qdrant_point_id
      AND target.rank IS NOT DISTINCT FROM source.rank
  )
  RETURNING 1
)
SELECT 'rag_retrieval_results_inserted' AS metric, count(*) AS value FROM inserted;

WITH inserted AS (
  INSERT INTO public.rag_regression_results (
    run_id,
    case_id,
    category,
    question,
    expected_action,
    expected_caveat,
    expected_flags,
    expected_behavior,
    transaction_id,
    trace_id,
    actual_status,
    retrieval_status,
    refusal_reason,
    selected_context_count,
    selected_channels,
    selected_chunk_ids,
    retrieval_scores,
    reranker_scores,
    context_token_estimate,
    answer_length,
    citation_status,
    latency_ms,
    outcome,
    failure_type,
    review_notes,
    result_payload,
    created_at
  )
  SELECT
    source.run_id,
    source.case_id,
    source.category,
    source.question,
    source.expected_action,
    source.expected_caveat,
    source.expected_flags,
    source.expected_behavior,
    source.transaction_id,
    source.trace_id,
    source.actual_status,
    source.retrieval_status,
    source.refusal_reason,
    source.selected_context_count,
    source.selected_channels,
    source.selected_chunk_ids,
    source.retrieval_scores,
    source.reranker_scores,
    source.context_token_estimate,
    source.answer_length,
    source.citation_status,
    source.latency_ms,
    source.outcome,
    source.failure_type,
    source.review_notes,
    source.result_payload,
    source.created_at
  FROM oracle_final.rag_regression_results AS source
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.rag_regression_results AS target
    WHERE target.run_id = source.run_id
      AND target.case_id = source.case_id
  )
  RETURNING 1
)
SELECT 'rag_regression_results_inserted' AS metric, count(*) AS value FROM inserted;

SELECT setval(
  pg_get_serial_sequence('public.rag_trace_events', 'event_id'),
  coalesce((SELECT max(event_id) FROM public.rag_trace_events), 1),
  EXISTS (SELECT 1 FROM public.rag_trace_events)
);
SELECT setval(
  pg_get_serial_sequence('public.rag_retrieval_results', 'result_id'),
  coalesce((SELECT max(result_id) FROM public.rag_retrieval_results), 1),
  EXISTS (SELECT 1 FROM public.rag_retrieval_results)
);
SELECT setval(
  pg_get_serial_sequence('public.rag_regression_results', 'result_id'),
  coalesce((SELECT max(result_id) FROM public.rag_regression_results), 1),
  EXISTS (SELECT 1 FROM public.rag_regression_results)
);

DROP SERVER oracle_final_server CASCADE;
DROP SCHEMA oracle_final;
COMMIT;
SQL

run_target_sql phoenix <<'SQL'
BEGIN;

CREATE EXTENSION IF NOT EXISTS postgres_fdw;
DROP SERVER IF EXISTS oracle_final_server CASCADE;
DROP SCHEMA IF EXISTS oracle_final CASCADE;
CREATE SERVER oracle_final_server
  FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (dbname 'oracle_final_phoenix');
CREATE USER MAPPING FOR CURRENT_USER
  SERVER oracle_final_server
  OPTIONS (user 'ragbot_admin');
CREATE SCHEMA oracle_final;
IMPORT FOREIGN SCHEMA public FROM SERVER oracle_final_server INTO oracle_final;

WITH inserted AS (
  INSERT INTO public.traces (
    project_rowid,
    trace_id,
    start_time,
    end_time,
    project_session_rowid
  )
  SELECT
    source.project_rowid,
    source.trace_id,
    source.start_time,
    source.end_time,
    source.project_session_rowid
  FROM oracle_final.traces AS source
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.traces AS target
    WHERE target.trace_id = source.trace_id
  )
  RETURNING 1
)
SELECT 'phoenix_traces_inserted' AS metric, count(*) AS value FROM inserted;

UPDATE public.traces AS target
SET
  start_time = least(target.start_time, source.start_time),
  end_time = greatest(target.end_time, source.end_time),
  project_session_rowid = coalesce(
    target.project_session_rowid,
    source.project_session_rowid
  )
FROM oracle_final.traces AS source
WHERE target.trace_id = source.trace_id;

WITH inserted AS (
  INSERT INTO public.spans (
    trace_rowid,
    span_id,
    parent_id,
    name,
    span_kind,
    start_time,
    end_time,
    attributes,
    events,
    status_code,
    status_message,
    cumulative_error_count,
    cumulative_llm_token_count_prompt,
    cumulative_llm_token_count_completion,
    llm_token_count_prompt,
    llm_token_count_completion
  )
  SELECT
    target_trace.id,
    source_span.span_id,
    source_span.parent_id,
    source_span.name,
    source_span.span_kind,
    source_span.start_time,
    source_span.end_time,
    source_span.attributes,
    source_span.events,
    source_span.status_code,
    source_span.status_message,
    source_span.cumulative_error_count,
    source_span.cumulative_llm_token_count_prompt,
    source_span.cumulative_llm_token_count_completion,
    source_span.llm_token_count_prompt,
    source_span.llm_token_count_completion
  FROM oracle_final.spans AS source_span
  JOIN oracle_final.traces AS source_trace
    ON source_trace.id = source_span.trace_rowid
  JOIN public.traces AS target_trace
    ON target_trace.trace_id = source_trace.trace_id
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.spans AS target_span
    WHERE target_span.span_id = source_span.span_id
  )
  RETURNING 1
)
SELECT 'phoenix_spans_inserted' AS metric, count(*) AS value FROM inserted;

SELECT setval(
  pg_get_serial_sequence('public.traces', 'id'),
  coalesce((SELECT max(id) FROM public.traces), 1),
  EXISTS (SELECT 1 FROM public.traces)
);
SELECT setval(
  pg_get_serial_sequence('public.spans', 'id'),
  coalesce((SELECT max(id) FROM public.spans), 1),
  EXISTS (SELECT 1 FROM public.spans)
);

DROP SERVER oracle_final_server CASCADE;
DROP SCHEMA oracle_final;
COMMIT;
SQL

run_target_sql n8n <<'SQL'
BEGIN;

CREATE EXTENSION IF NOT EXISTS postgres_fdw;
DROP SERVER IF EXISTS oracle_final_server CASCADE;
DROP SCHEMA IF EXISTS oracle_final CASCADE;
CREATE SERVER oracle_final_server
  FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (dbname 'oracle_final_n8n');
CREATE USER MAPPING FOR CURRENT_USER
  SERVER oracle_final_server
  OPTIONS (user 'ragbot_admin');
CREATE SCHEMA oracle_final;
IMPORT FOREIGN SCHEMA public FROM SERVER oracle_final_server INTO oracle_final;

CREATE TEMP TABLE oracle_execution_id_map (
  old_id integer PRIMARY KEY,
  new_id integer UNIQUE NOT NULL
) ON COMMIT DROP;

INSERT INTO oracle_execution_id_map (old_id, new_id)
SELECT
  source.id,
  nextval(pg_get_serial_sequence('public.execution_entity', 'id'))::integer
FROM oracle_final.execution_entity AS source
JOIN public.execution_entity AS target
  ON target.id = source.id
WHERE (to_jsonb(target) - 'id') IS DISTINCT FROM (to_jsonb(source) - 'id')
ORDER BY source.id;

WITH inserted AS (
  INSERT INTO public.execution_entity (
    id,
    finished,
    mode,
    "retryOf",
    "retrySuccessId",
    "startedAt",
    "stoppedAt",
    "waitTill",
    status,
    "workflowId",
    "deletedAt",
    "createdAt",
    "storedAt",
    "tracingContext",
    "deduplicationKey"
  )
  SELECT
    mapping.new_id,
    source.finished,
    source.mode,
    coalesce(
      (
        SELECT retry_mapping.new_id::text
        FROM oracle_execution_id_map AS retry_mapping
        WHERE retry_mapping.old_id::text = source."retryOf"
      ),
      source."retryOf"
    ),
    coalesce(
      (
        SELECT success_mapping.new_id::text
        FROM oracle_execution_id_map AS success_mapping
        WHERE success_mapping.old_id::text = source."retrySuccessId"
      ),
      source."retrySuccessId"
    ),
    source."startedAt",
    source."stoppedAt",
    source."waitTill",
    source.status,
    source."workflowId",
    source."deletedAt",
    source."createdAt",
    source."storedAt",
    source."tracingContext",
    source."deduplicationKey"
  FROM oracle_final.execution_entity AS source
  JOIN oracle_execution_id_map AS mapping
    ON mapping.old_id = source.id
  RETURNING 1
)
SELECT 'n8n_executions_inserted' AS metric, count(*) AS value FROM inserted;

WITH inserted AS (
  INSERT INTO public.execution_data (
    "executionId",
    "workflowData",
    data,
    "workflowVersionId"
  )
  SELECT
    mapping.new_id,
    source."workflowData",
    source.data,
    source."workflowVersionId"
  FROM oracle_final.execution_data AS source
  JOIN oracle_execution_id_map AS mapping
    ON mapping.old_id = source."executionId"
  RETURNING 1
)
SELECT 'n8n_execution_data_inserted' AS metric, count(*) AS value FROM inserted;

DO $$
DECLARE
  mapped_count integer;
  entity_count integer;
  data_count integer;
BEGIN
  SELECT count(*) INTO mapped_count FROM oracle_execution_id_map;
  SELECT count(*) INTO entity_count
  FROM public.execution_entity AS target
  JOIN oracle_execution_id_map AS mapping ON mapping.new_id = target.id;
  SELECT count(*) INTO data_count
  FROM public.execution_data AS target
  JOIN oracle_execution_id_map AS mapping
    ON mapping.new_id = target."executionId";

  IF mapped_count <> entity_count OR mapped_count <> data_count THEN
    RAISE EXCEPTION
      'n8n execution merge mismatch: mapped %, entities %, data %',
      mapped_count,
      entity_count,
      data_count;
  END IF;
END
$$;

SELECT setval(
  pg_get_serial_sequence('public.execution_entity', 'id'),
  coalesce((SELECT max(id) FROM public.execution_entity), 1),
  EXISTS (SELECT 1 FROM public.execution_entity)
);

DROP SERVER oracle_final_server CASCADE;
DROP SCHEMA oracle_final;
COMMIT;
SQL

echo "Oracle final delta merge completed"
