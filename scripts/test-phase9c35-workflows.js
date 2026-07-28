const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const coordinatorPath =
  'workflows/n8n/rag-incremental-coordinator-phase-9c35.json';
const corePath = 'workflows/n8n/rag-core-execution-phase-8.json';
const coordinatorText = fs.readFileSync(coordinatorPath, 'utf8');
const coreText = fs.readFileSync(corePath, 'utf8');
const coordinator = JSON.parse(coordinatorText);
const core = JSON.parse(coreText);

function node(workflow, name) {
  const found = workflow.nodes.find((candidate) => candidate.name === name);
  assert(found, `Missing workflow node: ${name}`);
  return found;
}

function destinations(workflow, name, branch = 0) {
  return (workflow.connections[name]?.main?.[branch] || []).map(
    (connection) => connection.node,
  );
}

function runCodeNode(workflow, name, input, items = {}) {
  const code = node(workflow, name).parameters.jsCode;
  const context = {
    $env: {},
    $json: input,
    $execution: { id: 'execution-123' },
    btoa(value) {
      return Buffer.from(value, 'binary').toString('base64');
    },
    $items(requestedName) {
      if (!(requestedName in items)) {
        throw new Error(`Node did not execute: ${requestedName}`);
      }
      return [{ json: items[requestedName] }];
    },
  };
  return vm.runInNewContext(`(function () { ${code} })()`, context)[0].json;
}

assert.strictEqual(
  coordinator.active,
  false,
  'Phase 9C.3.5 coordinator must remain disabled',
);
assert.strictEqual(node(coordinator, 'Manual Trigger').type, 'n8n-nodes-base.manualTrigger');
assert(
  !coordinator.nodes.some((candidate) =>
    /schedule|cron|webhook/i.test(candidate.type),
  ),
  'Coordinator must be manual-only in Phase 9C.3.5',
);

assert.deepStrictEqual(
  destinations(coordinator, 'Manual Trigger'),
  ['Normalize Coordinator Command'],
);
assert.deepStrictEqual(
  destinations(coordinator, 'Normalize Coordinator Command'),
  ['Create Or Read Durable Run'],
);
assert.deepStrictEqual(
  destinations(coordinator, 'Create Or Read Durable Run'),
  ['Transition Requested?'],
);
assert.deepStrictEqual(
  destinations(coordinator, 'Transition Requested?', 0),
  ['Transition Durable Run'],
);
assert.deepStrictEqual(
  destinations(coordinator, 'Transition Requested?', 1),
  ['Build Authoritative Run State'],
);
assert.deepStrictEqual(
  destinations(coordinator, 'Send Correlated Phoenix Span'),
  ['Return Authoritative DB State'],
);

const createSql = node(coordinator, 'Create Or Read Durable Run').parameters.query;
const transitionSql = node(coordinator, 'Transition Durable Run').parameters.query;
assert.match(createSql, /\brag_create_incremental_run\s*\(/);
assert.match(transitionSql, /\brag_transition_incremental_run\s*\(/);
assert.match(transitionSql, /expected_runtime_revision/);
assert.strictEqual(
  node(coordinator, 'Send Correlated Phoenix Span').onError,
  'continueRegularOutput',
  'Phoenix delivery must be best effort after the DB transaction commits',
);

const coordinatorLower = coordinatorText.toLowerCase();
assert.doesNotMatch(coordinatorLower, /\/points\/(?:delete|upsert|set_payload)/);
assert.doesNotMatch(coordinatorLower, /\bdelete\s+from\s+rag_pending_chunk_work\b/);
assert.doesNotMatch(coordinatorLower, /\bupdate\s+rag_pending_chunk_work\b/);
assert.doesNotMatch(coordinatorLower, /\bfor\s+update\s+skip\s+locked\b/);

const normalized = runCodeNode(coordinator, 'Normalize Coordinator Command', {
  incremental_run_id: 'run-123',
  plan_id: 'plan-456',
  requested_by: "O'Brien",
});
assert.strictEqual(normalized.action, 'create');
assert.strictEqual(normalized.expected_run_state, 'created');
assert.strictEqual(normalized.new_run_state, 'created');
assert.strictEqual(normalized.expected_runtime_state, 'serving');
assert.strictEqual(normalized.new_runtime_state, 'serving');
assert.strictEqual(normalized.event_name, 'incremental.run_created');
assert.strictEqual(normalized.expected_runtime_revision, 0);
assert.match(normalized.event_payload_sql, /O''Brien/);
assert.match(normalized.event_payload_sql, /"simulation":true/);
assert.match(normalized.event_payload_sql, /"qdrant_mutations":0/);

assert.throws(
  () =>
    runCodeNode(coordinator, 'Normalize Coordinator Command', {
      action: 'transition',
      expected_runtime_state: 'serving',
      new_runtime_state: 'draining',
    }),
  /cannot leave serving state/,
);
assert.throws(
  () =>
    runCodeNode(coordinator, 'Normalize Coordinator Command', {
      action: 'transition',
      event_name: 'incremental.maintenance_entered',
    }),
  /permits only the incremental\.run_failed transition event/,
);

const authoritative = {
  incremental_run_id: 'run-123',
  event_id: 42,
  event_created: true,
  run_state: 'planned',
  runtime_state: 'serving',
  runtime_revision: 7,
  plan_id: 'plan-456',
  collection_name: 'tpm_unite_history',
  event_name: 'incremental.run_created',
  event_idempotency_key: 'run-123:incremental.run_created',
  n8n_execution_id: 'execution-123',
  phoenix_collector_url: 'http://trace-emitter:8001/v1/traces',
  phoenix_project_name: 'discord-rag-bot-phase-9c35',
};
const trace = runCodeNode(
  coordinator,
  'Build Correlated Phoenix Span',
  authoritative,
);
const span =
  trace.phoenix_trace_payload.resourceSpans[0].scopeSpans[0].spans[0];
const attributes = Object.fromEntries(
  span.attributes.map(({ key, value }) => [
    key,
    value.stringValue ?? value.intValue ?? value.boolValue,
  ]),
);
assert.strictEqual(span.name, authoritative.event_name);
assert.strictEqual(attributes.incremental_run_id, 'run-123');
assert.strictEqual(attributes.durable_event_id, '42');
assert.strictEqual(trace.phoenix_span_event_id, 42);
assert.strictEqual(attributes.postgres_authoritative, true);
assert.strictEqual(attributes.qdrant_mutations, '0');

assert.deepStrictEqual(
  destinations(core, 'Initialize Runtime State'),
  ['Acquire Execution Lease'],
);
assert.deepStrictEqual(
  destinations(core, 'Acquire Execution Lease'),
  ['Restore Runtime After Lease'],
);
assert.deepStrictEqual(
  destinations(core, 'Restore Runtime After Lease'),
  ['Apply Stage 0 Safety Gate'],
);
assert.deepStrictEqual(
  destinations(core, 'Send Phoenix Embedding Checkpoint'),
  ['Heartbeat Execution Lease'],
);
assert.deepStrictEqual(
  destinations(core, 'Heartbeat Execution Lease'),
  ['Prepare Qdrant Search'],
);
assert.deepStrictEqual(
  destinations(core, 'Gemini Allowed?', 0),
  ['Heartbeat Lease Before Gemini'],
);
assert.deepStrictEqual(
  destinations(core, 'Heartbeat Lease Before Gemini'),
  ['Prepare Gemini Request'],
);
assert.deepStrictEqual(
  destinations(core, 'Return RAG Core Result'),
  ['Release Execution Lease'],
);
assert.deepStrictEqual(
  destinations(core, 'Release Execution Lease'),
  ['Return Released RAG Core Result'],
);

const acquireSql = node(core, 'Acquire Execution Lease').parameters.query;
const heartbeatSql = node(core, 'Heartbeat Execution Lease').parameters.query;
const geminiHeartbeatSql = node(
  core,
  'Heartbeat Lease Before Gemini',
).parameters.query;
const releaseSql = node(core, 'Release Execution Lease').parameters.query;
assert.match(acquireSql, /\brag_acquire_execution_lease\s*\(/);
assert.match(acquireSql, /interval '2 minutes'/);
assert.match(heartbeatSql, /\brag_heartbeat_execution_lease\s*\(/);
assert.match(geminiHeartbeatSql, /\brag_heartbeat_execution_lease\s*\(/);
assert.match(releaseSql, /\brag_release_execution_lease\s*\(/);

const resultTerminals = [
  ['Context Found?', 1],
  ['Gemini Allowed?', 1],
  ['Send Phoenix Gemini Checkpoint', 0],
  ['Safety Gate Passed?', 1],
];
for (const [source, branch] of resultTerminals) {
  assert.deepStrictEqual(
    destinations(core, source, branch),
    ['Return RAG Core Result'],
    `${source} must preserve its existing result path`,
  );
}

const restored = runCodeNode(
  core,
  'Restore Runtime After Lease',
  { ignored: true },
  {
    'Initialize Runtime State': {
      transaction_id: 'tx-1',
      user_query: 'question',
    },
    'Acquire Execution Lease': {
      lease_id: '11111111-1111-1111-1111-111111111111',
      expires_at: '2026-07-28T12:02:00Z',
      runtime_revision: 3,
    },
  },
);
assert.strictEqual(restored.transaction_id, 'tx-1');
assert.strictEqual(
  restored.execution_lease_id,
  '11111111-1111-1111-1111-111111111111',
);

assert.match(coreText, /RAG Core Execution - Phase 8/);
console.log('phase9c35 workflow checks passed');
