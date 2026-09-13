const fs = require('fs');

const path = 'workflows/n8n/rag-regression-batch-runner-phase-8.json';
const workflow = JSON.parse(fs.readFileSync(path, 'utf8'));
const node = name => workflow.nodes.find(item => item.name === name);

workflow.nodes = workflow.nodes.filter(item => !['Normalize Regression Target', 'Authorize Regression Target'].includes(item.name));
workflow.nodes.push({
  parameters: { jsCode: `const requiredWebhookSecret = String($env.N8N_WEBHOOK_SHARED_SECRET || '');
if (requiredWebhookSecret) {
  const headers = ($json && $json.headers) || {};
  const provided = String(headers['x-rag-webhook-secret'] || headers['X-RAG-Webhook-Secret'] || '');
  if (provided !== requiredWebhookSecret) throw new Error('Unauthorized webhook request');
}
const body = ($json && $json.body) || $json || {};
const collection = String(body.qdrant_collection || 'tpm_unite_history').trim();
const runId = String(body.regression_run_id || 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r=Math.floor(Math.random()*16); return (c==='x'?r:(r&3)|8).toString(16); })).trim();
const serving = collection === 'tpm_unite_history';
const required = ['candidate_authorization_id','candidate_id','target_corpus_version_id','target_manifest_digest','target_capture_cutoff_sequence'];
if (!serving) {
  const missing = required.filter(key => body[key] === undefined || body[key] === null || String(body[key]).trim() === '');
  if (missing.length) throw new Error('Candidate regression target requires authorization binding: ' + missing.join(','));
  const cutoff = Number(body.target_capture_cutoff_sequence);
  if (!Number.isSafeInteger(cutoff) || cutoff < 0) throw new Error('target_capture_cutoff_sequence must be a non-negative integer');
}
return [{json:{serving_target:serving, regression_run_id:runId, qdrant_collection:collection,
  candidate_authorization_id:serving?null:String(body.candidate_authorization_id), candidate_id:serving?null:String(body.candidate_id),
  target_corpus_version_id:serving?null:String(body.target_corpus_version_id), target_manifest_digest:serving?null:String(body.target_manifest_digest),
  target_capture_cutoff_sequence:serving?null:Number(body.target_capture_cutoff_sequence)}}];` },
  id: 'candidate-regression-normalize', name: 'Normalize Regression Target', type: 'n8n-nodes-base.code', typeVersion: 2, position: [-780, 0],
});
workflow.nodes.push({
  parameters: { operation: 'executeQuery', query: `SELECT CASE WHEN {{ $json.serving_target }} THEN true ELSE rag_consume_candidate_regression_authorization(
  NULLIF('{{ String($json.candidate_authorization_id || '').replace(/'/g, "''") }}','')::uuid,
  NULLIF('{{ String($json.regression_run_id).replace(/'/g, "''") }}','')::uuid,
  '{{ String($json.candidate_id || '').replace(/'/g, "''") }}'::text,
  '{{ String($json.qdrant_collection).replace(/'/g, "''") }}'::text,
  '{{ String($json.target_corpus_version_id || '').replace(/'/g, "''") }}'::text,
  '{{ String($json.target_manifest_digest || '').replace(/'/g, "''") }}'::text,
  {{ $json.target_capture_cutoff_sequence === null ? 'NULL' : Number($json.target_capture_cutoff_sequence) }}::bigint
) END AS admitted;` },
  id: 'candidate-regression-authorize', name: 'Authorize Regression Target', type: 'n8n-nodes-base.postgres', typeVersion: 2.6, position: [-560, 0],
  credentials: node('Ensure Regression Run').credentials,
});

const load = node('Load Regression Cases');
load.parameters.jsCode = load.parameters.jsCode.replace(
  "const body = $json.body || $json || {};",
  "const body = $items('Regression Batch Webhook')[0].json.body || $items('Regression Batch Webhook')[0].json || {};",
).replace(
  "const regressionRunId = body.regression_run_id || uuid();",
  "const regressionRunId = $items('Normalize Regression Target')[0].json.regression_run_id;",
);

workflow.connections['Regression Batch Webhook'] = {main:[[{node:'Normalize Regression Target',type:'main',index:0}]]};
workflow.connections['Normalize Regression Target'] = {main:[[{node:'Authorize Regression Target',type:'main',index:0}]]};
workflow.connections['Authorize Regression Target'] = {main:[[{node:'Load Regression Cases',type:'main',index:0}]]};

fs.writeFileSync(path, JSON.stringify(workflow, null, 2) + '\n');
