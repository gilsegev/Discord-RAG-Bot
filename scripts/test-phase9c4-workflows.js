const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const coordinator = JSON.parse(fs.readFileSync('workflows/n8n/rag-incremental-coordinator-phase-9c4.json', 'utf8'));
const core = JSON.parse(fs.readFileSync('workflows/n8n/rag-core-execution-phase-8.json', 'utf8'));
const intake = JSON.parse(fs.readFileSync('workflows/n8n/rag-intake-routing-phase-9.json', 'utf8'));
const regression = JSON.parse(fs.readFileSync('workflows/n8n/rag-regression-batch-runner-phase-8.json', 'utf8'));

function node(workflow, name) {
  const found = workflow.nodes.find((value) => value.name === name);
  assert(found, `missing ${name}`);
  return found;
}
function destinations(workflow, name, branch = 0) {
  return (workflow.connections[name]?.main?.[branch] || []).map((value) => value.node);
}
function runCode(workflow, name, input, env = {}, items = {}) {
  const code = node(workflow, name).parameters.jsCode;
  const context = {
    $json: input, $env: env, $execution: { id: 'exec-1' },
    $items(requested) { return [{ json: items[requested] || {} }]; },
  };
  return vm.runInNewContext(`(function(){${code}})()`, context)[0].json;
}

assert.strictEqual(coordinator.active, false);
assert(node(coordinator, 'Coordinator Webhook'));
assert(node(coordinator, 'Manual Trigger'));
assert.deepStrictEqual(destinations(coordinator, 'Apply Lifecycle Operation'), ['Worker Needed?']);
assert.deepStrictEqual(destinations(coordinator, 'Worker Needed?', 0), ['Call Incremental Worker']);
assert.deepStrictEqual(destinations(coordinator, 'Worker Needed?', 1), ['Build Coordinator Result']);
assert.match(node(coordinator, 'Call Incremental Worker').parameters.url, /INCREMENTAL_WORKER_URL/);
assert.match(node(coordinator, 'Call Incremental Worker').parameters.headerParameters.parameters[0].value, /INCREMENTAL_WORKER_TOKEN/);

const status = runCode(coordinator, 'Normalize Coordinator Command', {
  action: 'status', incremental_run_id: 'run-1', plan_id: 'plan-1',
});
assert.strictEqual(status.worker_needed, false);
assert.match(status.lifecycle_sql, /rag_incremental_runs/);
assert.throws(() => runCode(coordinator, 'Normalize Coordinator Command', {
  action: 'apply', incremental_run_id: 'run-1', plan_id: 'plan-1',
  runtime_revision: 2, confirm_apply: 'PHASE9C4_APPLY',
}, { PHASE9C4_ENABLED: 'false' }), /PHASE9C4_ENABLED/);
const apply = runCode(coordinator, 'Normalize Coordinator Command', {
  action: 'apply', incremental_run_id: 'run-1', plan_id: 'plan-1',
  runtime_revision: 2, confirm_apply: 'PHASE9C4_APPLY', take_full_snapshot: true,
}, { PHASE9C4_ENABLED: 'true' });
assert.strictEqual(apply.worker_needed, true);
assert.strictEqual(apply.worker_path, 'apply');
assert.match(apply.lifecycle_sql, /rag_mark_incremental_replacing/);

assert.deepStrictEqual(destinations(core, 'Acquire Execution Lease'), ['Lease Admitted?']);
assert.deepStrictEqual(destinations(core, 'Lease Admitted?', 0), ['Restore Runtime After Lease']);
assert.deepStrictEqual(destinations(core, 'Lease Admitted?', 1), ['Build Maintenance Gate Result']);
assert.match(node(core, 'Acquire Execution Lease').parameters.query, /maintenance_validation_run_id/);
assert.match(node(core, 'Acquire Execution Lease').parameters.query, /rag_acquire_execution_lease/);
const maintenance = runCode(core, 'Build Maintenance Gate Result', {}, {}, {
  'Initialize Runtime State': { trigger_source: 'discord_active', allow_discord_post: true },
});
assert.strictEqual(maintenance.refusal_reason, 'maintenance_in_progress');
assert.strictEqual(maintenance.retrieval_status, 'not_started');
assert.match(maintenance.discord_response_text, /briefly updating/);
const passive = runCode(core, 'Build Maintenance Gate Result', {}, {}, {
  'Initialize Runtime State': { trigger_source: 'discord_passive', allow_discord_post: false },
});
assert.strictEqual(passive.discord_response_text, '');
assert.strictEqual(passive.allow_discord_post, false);

assert.match(node(intake, 'Build RAG Core Request').parameters.jsCode, /maintenance_validation_run_id/);
assert.match(
  node(intake, 'Execute RAG Core').parameters.workflowInputs.value.maintenance_validation_run_id,
  /maintenance_validation_run_id/,
);
assert.match(node(regression, 'Load Regression Cases').parameters.jsCode, /maintenance_validation_run_id/);
console.log('phase9c4 workflow checks passed');
