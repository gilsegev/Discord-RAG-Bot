const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const workflow = JSON.parse(fs.readFileSync('workflows/n8n/rag-regression-batch-runner-phase-8.json', 'utf8'));
const node = name => workflow.nodes.find(item => item.name === name);
const normalizeCode = node('Normalize Regression Target').parameters.jsCode;

function normalize(body, secret = '', provided = '') {
  const source = `(function(){${normalizeCode}})()`;
  return vm.runInNewContext(source, {$json:{body,headers:{'x-rag-webhook-secret':provided}}, $env:{N8N_WEBHOOK_SHARED_SECRET:secret}})[0].json;
}

assert.equal(normalize({}).serving_target, true, 'ordinary serving regression remains admitted');
assert.throws(() => normalize({}, 'required', 'wrong'), /Unauthorized webhook request/);
assert.throws(() => normalize({qdrant_collection:'candidate'}), /requires authorization binding/);
const bound = normalize({qdrant_collection:'candidate', candidate_authorization_id:'a', candidate_id:'c',
  target_corpus_version_id:'v', target_manifest_digest:'d', target_capture_cutoff_sequence:7});
assert.equal(bound.qdrant_collection, 'candidate');
assert.equal(bound.target_capture_cutoff_sequence, 7);

const now = new Date('2026-09-13T00:00:00Z');
const authorization = {authorization_id:'a', candidate_id:'c', collection_name:'candidate', corpus_version_id:'v',
  manifest_digest:'d', frozen_capture_sequence:7, expires_at:new Date('2026-09-14T00:00:00Z'), consumed_at:null};
const admitted = (record, target) => record.authorization_id === target.candidate_authorization_id &&
  record.candidate_id === target.candidate_id && record.collection_name === target.qdrant_collection &&
  record.corpus_version_id === target.target_corpus_version_id && record.manifest_digest === target.target_manifest_digest &&
  record.frozen_capture_sequence === target.target_capture_cutoff_sequence && record.expires_at > now && record.consumed_at === null;
assert(admitted(authorization, bound), 'exact candidate binding is admitted');
assert(!admitted(authorization, {...bound, candidate_id:'wrong'}), 'wrong candidate is rejected');
assert(!admitted({...authorization, expires_at:now}, bound), 'expired authorization is rejected');
assert(!admitted({...authorization, consumed_at:new Date()}, bound), 'consumed authorization is rejected');

const sql = fs.readFileSync('deploy/phase0/sql/16-candidate-regression-authorization-gate.sql', 'utf8');
assert(!/AS\s+authorization\b/i.test(sql), 'PostgreSQL reserved word AUTHORIZATION must not be used as an alias');
for (const predicate of ['auth.expires_at > now()', 'auth.consumed_at IS NULL',
  'auth.candidate_id = p_candidate_id', 'auth.collection_name = p_collection_name',
  'auth.corpus_version_id = p_corpus_version_id', 'auth.manifest_digest = p_manifest_digest',
  'auth.frozen_capture_sequence = p_frozen_capture_sequence']) assert(sql.includes(predicate), predicate);
assert(sql.includes("candidate.status = 'regression_authorized'"));
assert.deepEqual(workflow.connections['Regression Batch Webhook'].main[0][0].node, 'Normalize Regression Target');
assert.deepEqual(workflow.connections['Authorize Regression Target'].main[0][0].node, 'Load Regression Cases');
console.log('candidate regression authorization gate tests passed');
