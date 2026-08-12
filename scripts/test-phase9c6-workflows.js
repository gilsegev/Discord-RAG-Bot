const assert = require('assert');
const fs = require('fs');

const catchup = JSON.parse(fs.readFileSync('workflows/n8n/rag-incremental-catchup-phase-9c6.json'));
const runner = JSON.parse(fs.readFileSync('workflows/n8n/rag-incremental-scheduled-run-phase-9c5.json'));
const node = (workflow, name) => {
  const found = workflow.nodes.find(candidate => candidate.name === name);
  assert(found, `missing node ${name}`);
  return found;
};
const destinations = (workflow, name, branch = 0) =>
  (workflow.connections[name]?.main?.[branch] || []).map(value => value.node);

assert.strictEqual(catchup.active, false);
assert.strictEqual(node(catchup, 'Catch-up Webhook').parameters.path, 'rag-incremental-catchup-phase-9c6');
assert.match(node(catchup, 'Normalize Catch-up Request').parameters.jsCode, /PHASE9C6_CATCHUP/);
assert.match(node(catchup, 'Normalize Catch-up Request').parameters.jsCode, /accept_history_gap/);
assert.match(node(catchup, 'Admit Fixed Catch-up').parameters.query, /rag_prepare_phase9c6_catchup_attempt/);
assert.match(node(catchup, 'Build Shadow Validated Plan').parameters.url, /INCREMENTAL_WORKER_URL/);
assert.match(node(catchup, 'Attach Plan Safety Gates').parameters.query, /rag_attach_incremental_schedule_plan/);
assert.match(node(catchup, 'Call Proven Scheduled Runner').parameters.url, /scheduled-run-phase-9c5/);
assert.match(node(catchup, 'Complete Catch-up Lock').parameters.query, /rag_complete_phase9c6_catchup/);
assert.deepStrictEqual(destinations(catchup, 'Catch-up Admitted?', 1), ['Return Admission Block']);
assert.deepStrictEqual(destinations(catchup, 'Plan Is Safe?', 1), ['Finish Unsafe Catch-up']);
assert.deepStrictEqual(destinations(catchup, 'Catch-up Completed?', 0), ['Complete Catch-up Lock']);
assert.deepStrictEqual(destinations(catchup, 'Catch-up Completed?', 1), ['Finish Failed Catch-up']);
assert(!JSON.stringify(catchup).includes('/points/'));
assert(!JSON.stringify(catchup).includes('schedule_enabled=true'));

assert.match(node(runner, 'Verify Ready Attempt').parameters.query, /accepted_history_gap/);
assert.match(node(runner, 'Apply Replacement').parameters.jsonBody, /take_full_snapshot/);
assert.match(node(runner, 'Apply Replacement').parameters.jsonBody, /9C\.6/);

console.log('phase9c6 workflow checks passed');
