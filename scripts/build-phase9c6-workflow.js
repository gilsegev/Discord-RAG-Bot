const fs = require('fs');

const output = 'workflows/n8n/rag-incremental-catchup-phase-9c6.json';
const credential = { postgres: { id: 'JcwnINxG4CstrGKy', name: 'Postgres account' } };
let x = -1200;
const pos = (y = 0) => [x += 220, y];
const code = (name, jsCode, y = 0) => ({ parameters: { jsCode }, id: `p9c6-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`, name, type: 'n8n-nodes-base.code', typeVersion: 2, position: pos(y) });
const pg = (name, query, y = 0) => ({ parameters: { operation: 'executeQuery', query }, id: `p9c6-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`, name, type: 'n8n-nodes-base.postgres', typeVersion: 2.6, position: pos(y), credentials: credential });
const iff = (name, leftValue, y = 0) => ({ parameters: { conditions: { options: { caseSensitive: true, leftValue: '', typeValidation: 'strict', version: 2 }, conditions: [{ id: `${name}-condition`, leftValue, rightValue: true, operator: { type: 'boolean', operation: 'true', singleValue: true } }], combinator: 'and' }, options: {} }, id: `p9c6-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`, name, type: 'n8n-nodes-base.if', typeVersion: 2.2, position: pos(y) });
const connect = (workflow, from, to, branch = 0) => {
  workflow.connections[from] ||= { main: [] };
  workflow.connections[from].main[branch] ||= [];
  workflow.connections[from].main[branch].push({ node: to, type: 'main', index: 0 });
};

const workflow = {
  name: 'RAG One-Time Catch-up - Phase 9C.6', active: false,
  nodes: [], connections: {}, settings: { executionOrder: 'v1' },
  staticData: null, meta: { templateCredsSetupCompleted: false }, pinData: {},
};

workflow.nodes.push(
  { parameters: { httpMethod: 'POST', path: 'rag-incremental-catchup-phase-9c6', responseMode: 'lastNode', options: {} }, id: 'p9c6-webhook', name: 'Catch-up Webhook', type: 'n8n-nodes-base.webhook', typeVersion: 2, position: [-1200, 0], webhookId: 'rag-incremental-catchup-phase-9c6' },
  code('Normalize Catch-up Request', `const envelope=$json||{},raw=envelope.body||envelope,headers=envelope.headers||{};
const secret=String($env.N8N_WEBHOOK_SHARED_SECRET||'');
if(secret&&String(headers['x-rag-webhook-secret']||'')!==secret) throw new Error('Unauthorized catch-up request');
const clean=v=>String(v||'').replace(/[^A-Za-z0-9._:-]/g,'');
const attempt=clean(raw.attempt_id),collection=clean(raw.collection_name||'tpm_unite_history');
const cutoff=Number(raw.batch_cutoff_sequence);
const reason=String(raw.history_gap_reason||'').trim();
if(!attempt||!Number.isSafeInteger(cutoff)||cutoff<=0) throw new Error('attempt_id and positive integer cutoff required');
if(raw.confirm_catchup!=='PHASE9C6_CATCHUP') throw new Error('catch-up confirmation phrase required');
if(raw.accept_history_gap!==true||!reason) throw new Error('accepted history-gap reason required');
return [{json:{attempt_id:attempt,collection_name:collection,batch_cutoff_sequence:cutoff,history_gap_reason:reason}}];`),
  pg('Admit Fixed Catch-up', `SELECT * FROM rag_prepare_phase9c6_catchup_attempt(
  '{{ $json.attempt_id }}','{{ $json.collection_name }}',{{ $json.batch_cutoff_sequence }},true,
  '{{ String($json.history_gap_reason).replace(/'/g,"''") }}');`),
  iff('Catch-up Admitted?', "={{ $json.decision === 'planning' }}"),
  { parameters: { method: 'POST', url: "={{ ($env.INCREMENTAL_WORKER_URL || 'http://incremental-worker:8003') + '/plan' }}", sendHeaders: true, headerParameters: { parameters: [{ name: 'X-Incremental-Worker-Token', value: '={{ $env.INCREMENTAL_WORKER_TOKEN }}' }] }, sendBody: true, specifyBody: 'json', jsonBody: "={{ { collection_name: $items('Normalize Catch-up Request')[0].json.collection_name, batch_cutoff_sequence: Number($items('Normalize Catch-up Request')[0].json.batch_cutoff_sequence), persist: true } }}", options: { response: { response: { fullResponse: true, neverError: true, responseFormat: 'json' } } } }, id: 'p9c6-plan', name: 'Build Shadow Validated Plan', type: 'n8n-nodes-base.httpRequest', typeVersion: 4.2, position: pos(-120) },
  pg('Attach Plan Safety Gates', `SELECT * FROM rag_attach_incremental_schedule_plan(
  '{{ $items("Normalize Catch-up Request")[0].json.attempt_id }}',
  '{{ $items("Build Shadow Validated Plan")[0].json.body.plan_id }}');`),
  iff('Plan Is Safe?', "={{ $json.decision === 'ready' }}"),
  pg('Mark Catch-up Dispatched', `SELECT (rag_finish_incremental_schedule_attempt(
  '{{ $items("Normalize Catch-up Request")[0].json.attempt_id }}','dispatched',NULL,false,
  jsonb_build_object('controller_execution_id','{{ $execution.id }}')
)).*;`),
  { parameters: { method: 'POST', url: 'http://127.0.0.1:5678/webhook/rag-incremental-scheduled-run-phase-9c5', sendHeaders: true, headerParameters: { parameters: [{ name: 'X-RAG-Webhook-Secret', value: '={{ $env.N8N_WEBHOOK_SHARED_SECRET }}' }] }, sendBody: true, specifyBody: 'json', jsonBody: "={{ { attempt_id: $items('Normalize Catch-up Request')[0].json.attempt_id, plan_id: $items('Build Shadow Validated Plan')[0].json.body.plan_id, collection_name: $items('Normalize Catch-up Request')[0].json.collection_name } }}", options: { timeout: 3600000, response: { response: { fullResponse: true, neverError: true, responseFormat: 'json' } } } }, id: 'p9c6-runner', name: 'Call Proven Scheduled Runner', type: 'n8n-nodes-base.httpRequest', typeVersion: 4.2, position: pos(0) },
  iff('Catch-up Completed?', "={{ Number($json.statusCode) >= 200 && Number($json.statusCode) < 300 && $json.body?.status === 'completed' }}"),
  pg('Complete Catch-up Lock', `SELECT * FROM rag_complete_phase9c6_catchup(
  '{{ $items("Normalize Catch-up Request")[0].json.attempt_id }}',
  '{{ String($items("Call Proven Scheduled Runner")[0].json.body.incremental_run_id).replace(/'/g,"''") }}',
  jsonb_build_object('controller_execution_id','{{ $execution.id }}','targeted_retrieval_pending',true));`),
  code('Return Catch-up Success', `return [{json:{...$json,status:'completed',qdrant_mutations:true,accepted_history_gap:true}}];`),
  pg('Finish Failed Catch-up', `SELECT (rag_finish_incremental_schedule_attempt(
  '{{ $items("Normalize Catch-up Request")[0].json.attempt_id }}','failed',
  NULLIF('{{ String($items("Call Proven Scheduled Runner")[0].json.body?.incremental_run_id||"").replace(/'/g,"''") }}',''),
  {{ $items("Call Proven Scheduled Runner")[0].json.body?.qdrant_mutations === true ? 'true' : 'false' }},
  '{{ JSON.stringify($items("Call Proven Scheduled Runner")[0].json.body||{}).replace(/'/g,"''") }}'::jsonb
)).*;`, 160),
  code('Return Catch-up Failure', `return [{json:{...$json,status:'failed'}}];`, 160),
  pg('Finish Unsafe Catch-up', `SELECT (rag_finish_incremental_schedule_attempt(
  '{{ $items("Normalize Catch-up Request")[0].json.attempt_id }}','blocked',NULL,false,
  jsonb_build_object('plan_id','{{ $items("Build Shadow Validated Plan")[0].json.body.plan_id }}')
)).*;`, 100),
  code('Return Unsafe Catch-up', `return [{json:{...$json,status:'blocked',qdrant_mutations:false}}];`, 100),
  code('Return Admission Block', `return [{json:{...$json,status:'blocked',qdrant_mutations:false}}];`, 180),
);

connect(workflow, 'Catch-up Webhook', 'Normalize Catch-up Request');
connect(workflow, 'Normalize Catch-up Request', 'Admit Fixed Catch-up');
connect(workflow, 'Admit Fixed Catch-up', 'Catch-up Admitted?');
connect(workflow, 'Catch-up Admitted?', 'Build Shadow Validated Plan', 0);
connect(workflow, 'Catch-up Admitted?', 'Return Admission Block', 1);
connect(workflow, 'Build Shadow Validated Plan', 'Attach Plan Safety Gates');
connect(workflow, 'Attach Plan Safety Gates', 'Plan Is Safe?');
connect(workflow, 'Plan Is Safe?', 'Mark Catch-up Dispatched', 0);
connect(workflow, 'Plan Is Safe?', 'Finish Unsafe Catch-up', 1);
connect(workflow, 'Finish Unsafe Catch-up', 'Return Unsafe Catch-up');
connect(workflow, 'Mark Catch-up Dispatched', 'Call Proven Scheduled Runner');
connect(workflow, 'Call Proven Scheduled Runner', 'Catch-up Completed?');
connect(workflow, 'Catch-up Completed?', 'Complete Catch-up Lock', 0);
connect(workflow, 'Catch-up Completed?', 'Finish Failed Catch-up', 1);
connect(workflow, 'Complete Catch-up Lock', 'Return Catch-up Success');
connect(workflow, 'Finish Failed Catch-up', 'Return Catch-up Failure');

fs.writeFileSync(output, JSON.stringify(workflow, null, 2) + '\n');
