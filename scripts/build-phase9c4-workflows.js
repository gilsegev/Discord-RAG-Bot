const fs = require('fs');

const corePath = 'workflows/n8n/rag-core-execution-phase-8.json';
const intakePath = 'workflows/n8n/rag-intake-routing-phase-9.json';
const regressionPath = 'workflows/n8n/rag-regression-batch-runner-phase-8.json';
const coordinatorPath = 'workflows/n8n/rag-incremental-coordinator-phase-9c4.json';

function read(path) { return JSON.parse(fs.readFileSync(path, 'utf8')); }
function write(path, value) { fs.writeFileSync(path, JSON.stringify(value, null, 2) + '\n'); }
function node(workflow, name) {
  const value = workflow.nodes.find((candidate) => candidate.name === name);
  if (!value) throw new Error(`missing node ${name}`);
  return value;
}

const core = read(corePath);
let initCode = node(core, 'Initialize Runtime State').parameters.jsCode;
initCode = initCode.replace(/\n\s*maintenance_validation_run_id: input\.maintenance_validation_run_id \|\| tx\.maintenance_validation_run_id \|\| '',/g, '');
initCode = initCode.replace(
  "    regression_run_id: input.regression_run_id || tx.regression_run_id || '',",
  "    regression_run_id: input.regression_run_id || tx.regression_run_id || '',\n    maintenance_validation_run_id: input.maintenance_validation_run_id || tx.maintenance_validation_run_id || '',",
);
node(core, 'Initialize Runtime State').parameters.jsCode = initCode;
node(core, 'Acquire Execution Lease').parameters.query = `SELECT *
FROM rag_acquire_execution_lease(
  '{{ String($json.qdrant_collection || "tpm_unite_history").replace(/'/g, "''") }}',
  '{{ String($execution.id).replace(/'/g, "''") }}',
  NULLIF('{{ String($json.transaction_id || "").replace(/'/g, "''") }}', ''),
  'RAG Core Execution - Phase 8',
  interval '2 minutes',
  jsonb_build_object(
    'trigger_source', '{{ String($json.trigger_source || "").replace(/'/g, "''") }}',
    'run_mode', '{{ String($json.run_mode || "").replace(/'/g, "''") }}',
    'response_mode', '{{ String($json.response_mode || "").replace(/'/g, "''") }}'
  ),
  NULLIF('{{ String($json.maintenance_validation_run_id || "").replace(/'/g, "''") }}', '')
);`;
core.nodes = core.nodes.filter((value) => !['Lease Admitted?', 'Build Maintenance Gate Result'].includes(value.name));
core.nodes.push({
  parameters: { conditions: { options: { caseSensitive: true, leftValue: '', typeValidation: 'strict', version: 2 }, conditions: [{ id: 'phase9c4-admitted', leftValue: '={{ $json.admitted }}', rightValue: true, operator: { type: 'boolean', operation: 'true', singleValue: true } }], combinator: 'and' }, options: {} },
  id: 'phase9c4-lease-admitted', name: 'Lease Admitted?', type: 'n8n-nodes-base.if', typeVersion: 2.2, position: [-1580, 0],
});
core.nodes.push({
  parameters: { jsCode: `const state = $items('Initialize Runtime State')[0].json;
const passive = state.trigger_source === 'discord_passive';
return [{ json: {
  ...state,
  final_status: 'refused', retrieval_status: 'not_started', response_status: 'not_posted',
  refusal_reason: 'maintenance_in_progress', failure_reason: null,
  refusal_text: 'The knowledge bot is briefly updating its community history. Please try again in a few minutes.',
  discord_response_text: passive ? '' : 'The knowledge bot is briefly updating its community history. Please try again in a few minutes.',
  should_generate: false, maintenance_gate_denied: true,
  allow_discord_post: passive ? false : state.allow_discord_post,
  rag_core_completed_ms: Date.now(), rag_core_workflow: 'RAG Core Execution - Phase 8'
} }];` },
  id: 'phase9c4-maintenance-result', name: 'Build Maintenance Gate Result', type: 'n8n-nodes-base.code', typeVersion: 2, position: [-1340, 180],
});
core.connections['Acquire Execution Lease'] = { main: [[{ node: 'Lease Admitted?', type: 'main', index: 0 }]] };
core.connections['Lease Admitted?'] = { main: [
  [{ node: 'Restore Runtime After Lease', type: 'main', index: 0 }],
  [{ node: 'Build Maintenance Gate Result', type: 'main', index: 0 }],
] };
core.connections['Build Maintenance Gate Result'] = { main: [] };
write(corePath, core);

const intake = read(intakePath);
let requestCode = node(intake, 'Build RAG Core Request').parameters.jsCode;
requestCode = requestCode.replace(/\n\s*maintenance_validation_run_id: input\.maintenance_validation_run_id \|\| '',/g, '');
requestCode = requestCode.replace(
  "  regression_run_id: input.regression_run_id || '',",
  "  regression_run_id: input.regression_run_id || '',\n  maintenance_validation_run_id: input.maintenance_validation_run_id || '',",
);
node(intake, 'Build RAG Core Request').parameters.jsCode = requestCode;
write(intakePath, intake);

const regression = read(regressionPath);
let loadCode = node(regression, 'Load Regression Cases').parameters.jsCode;
loadCode = loadCode.replace(/\n\s*maintenance_validation_run_id: body\.maintenance_validation_run_id \|\| '',/g, '');
loadCode = loadCode.replace(
  "    regression_run_id: regressionRunId,",
  "    regression_run_id: regressionRunId,\n    maintenance_validation_run_id: body.maintenance_validation_run_id || '',",
);
node(regression, 'Load Regression Cases').parameters.jsCode = loadCode;
write(regressionPath, regression);

const postgresCredential = { postgres: { id: 'JcwnINxG4CstrGKy', name: 'Postgres account' } };
const normalizeCode = `const raw = ($json && $json.body) ? $json.body : ($json || {});
const secret = String($env.N8N_WEBHOOK_SHARED_SECRET || '');
const headers = ($json && $json.headers) || {};
if (secret && String(headers['x-rag-webhook-secret'] || headers['X-RAG-Webhook-Secret'] || '') !== secret) throw new Error('Unauthorized webhook request');
const clean = (value, fallback, label) => { const text=String(value ?? fallback ?? '').trim(); if(!text || !/^[A-Za-z0-9._:-]+$/.test(text)) throw new Error(label+' contains unsupported characters'); return text; };
const action=String(raw.action || 'status').toLowerCase();
const allowed=['create','record_baseline','begin_drain','enter_maintenance','preflight','apply','record_regression','rollback','exit_completed','exit_failed','status'];
if(!allowed.includes(action)) throw new Error('unsupported Phase 9C.4 action');
const runId=clean(raw.incremental_run_id,'phase9c4-'+String($execution.id),'incremental_run_id');
const planId=clean(raw.plan_id,'manual-shadow-plan','plan_id');
const collection=clean(raw.collection_name,'tpm_unite_history','collection_name');
const revision=Number(raw.runtime_revision ?? 0); if(!Number.isInteger(revision)||revision<0) throw new Error('runtime_revision must be a non-negative integer');
const mutating=['begin_drain','enter_maintenance','apply','rollback','exit_completed','exit_failed'];
if(mutating.includes(action) && String($env.PHASE9C4_ENABLED || '').toLowerCase()!=='true') throw new Error('PHASE9C4_ENABLED is not true');
if(action==='apply' && raw.confirm_apply!=='PHASE9C4_APPLY') throw new Error('apply confirmation phrase is required');
const regressionId=raw.regression_run_id ? clean(raw.regression_run_id,'','regression_run_id') : '';
const regressionResult=String(raw.regression_result || 'passed').toLowerCase();
if(!['passed','failed'].includes(regressionResult)) throw new Error('regression_result must be passed or failed');
const esc=(value)=>String(value).replace(/'/g,"''");
const metadata=JSON.stringify({phase:'9C.4',requested_by:String(raw.requested_by||'developer').slice(0,200),n8n_execution_id:String($execution.id)}).replace(/'/g,"''");
let sql;
if(action==='create') sql=\`SELECT * FROM rag_create_incremental_run('\${esc(runId)}','\${esc(planId)}','\${esc(collection)}','\${metadata}'::jsonb);\`;
else if(action==='record_baseline') sql=\`SELECT * FROM rag_record_incremental_regression('\${esc(runId)}','\${esc(regressionId)}','\${esc(regressionResult)}',true);\`;
else if(action==='begin_drain') sql=\`SELECT * FROM rag_begin_incremental_drain('\${esc(runId)}',\${revision});\`;
else if(action==='enter_maintenance') sql=\`SELECT * FROM rag_enter_incremental_maintenance('\${esc(runId)}',\${revision});\`;
else if(action==='apply') sql=\`SELECT * FROM rag_mark_incremental_replacing('\${esc(runId)}',\${revision});\`;
else if(action==='record_regression') sql=\`SELECT * FROM rag_record_incremental_regression('\${esc(runId)}','\${esc(regressionId)}','\${esc(regressionResult)}',false);\`;
else if(action==='exit_completed') sql=\`SELECT * FROM rag_exit_incremental_maintenance('\${esc(runId)}','completed','{}'::jsonb);\`;
else if(action==='exit_failed') sql=\`SELECT * FROM rag_exit_incremental_maintenance('\${esc(runId)}','failed','{"failure_step":"operator_exit","failure_reason":"phase9c4_failed"}'::jsonb);\`;
else sql=\`SELECT r.incremental_run_id,r.run_state,rs.runtime_state,rs.state_revision AS runtime_revision FROM rag_incremental_runs r JOIN rag_runtime_state rs ON rs.collection_name=r.collection_name WHERE r.incremental_run_id='\${esc(runId)}';\`;
return [{json:{action,incremental_run_id:runId,plan_id:planId,collection_name:collection,runtime_revision:revision,lifecycle_sql:sql,worker_needed:['preflight','apply','rollback'].includes(action),worker_path:action==='rollback'?'rollback':action,take_full_snapshot:raw.take_full_snapshot===true,fail_after_step:raw.fail_after_step||null,phoenix_collector_url:$env.TRACE_EMITTER_URL||'http://trace-emitter:8001/v1/traces'}}];`;

const coordinator = {
  name: 'RAG Incremental Coordinator - Phase 9C.4', active: false,
  nodes: [
    { parameters: { httpMethod: 'POST', path: 'rag-incremental-phase-9c4', responseMode: 'lastNode', options: {} }, id: 'p9c4-webhook', name: 'Coordinator Webhook', type: 'n8n-nodes-base.webhook', typeVersion: 2, position: [-900,0], webhookId: 'rag-incremental-phase-9c4' },
    { parameters: {}, id: 'p9c4-manual', name: 'Manual Trigger', type: 'n8n-nodes-base.manualTrigger', typeVersion: 1, position: [-900,-180] },
    { parameters: { jsCode: normalizeCode }, id: 'p9c4-normalize', name: 'Normalize Coordinator Command', type: 'n8n-nodes-base.code', typeVersion: 2, position: [-660,0] },
    { parameters: { operation: 'executeQuery', query: '={{ $json.lifecycle_sql }}' }, id: 'p9c4-lifecycle', name: 'Apply Lifecycle Operation', type: 'n8n-nodes-base.postgres', typeVersion: 2.6, position: [-420,0], credentials: postgresCredential },
    { parameters: { conditions: { options: { caseSensitive: true, leftValue: '', typeValidation: 'strict', version: 2 }, conditions: [{ id: 'p9c4-worker-condition', leftValue: "={{ $items('Normalize Coordinator Command')[0].json.worker_needed }}", rightValue: true, operator: { type: 'boolean', operation: 'true', singleValue: true } }], combinator: 'and' }, options: {} }, id: 'p9c4-worker-needed', name: 'Worker Needed?', type: 'n8n-nodes-base.if', typeVersion: 2.2, position: [-180,0] },
    { parameters: { method: 'POST', url: "={{ ($env.INCREMENTAL_WORKER_URL || 'http://incremental-worker:8003') + '/' + $items('Normalize Coordinator Command')[0].json.worker_path }}", sendHeaders: true, headerParameters: { parameters: [{ name: 'X-Incremental-Worker-Token', value: '={{ $env.INCREMENTAL_WORKER_TOKEN }}' }] }, sendBody: true, specifyBody: 'json', jsonBody: "={{ { incremental_run_id: $items('Normalize Coordinator Command')[0].json.incremental_run_id, take_full_snapshot: $items('Normalize Coordinator Command')[0].json.take_full_snapshot, fail_after_step: $items('Normalize Coordinator Command')[0].json.fail_after_step } }}", options: { response: { response: { fullResponse: true, neverError: true, responseFormat: 'json' } } } }, id: 'p9c4-worker', name: 'Call Incremental Worker', type: 'n8n-nodes-base.httpRequest', typeVersion: 4.2, position: [60,-100] },
    { parameters: { operation: 'executeQuery', query: `SELECT r.*,rs.runtime_state,rs.state_revision AS runtime_revision
FROM rag_incremental_runs r JOIN rag_runtime_state rs ON rs.collection_name=r.collection_name
WHERE r.incremental_run_id='{{ $items("Normalize Coordinator Command")[0].json.incremental_run_id }}';` }, id: 'p9c4-read', name: 'Read Authoritative Run', type: 'n8n-nodes-base.postgres', typeVersion: 2.6, position: [300,-100], credentials: postgresCredential },
    { parameters: { jsCode: `const command=$items('Normalize Coordinator Command')[0].json; let db; try{db=$items('Read Authoritative Run')[0]?.json;}catch(e){db=$json||{};} let worker=null; try{worker=$items('Call Incremental Worker')[0]?.json||null;}catch(e){} return [{json:{...db,action:command.action,worker_result:worker,postgres_authoritative:true,qdrant_mutations:command.action==='apply',coordinator_workflow:'RAG Incremental Coordinator - Phase 9C.4',phoenix_collector_url:command.phoenix_collector_url}}];` }, id: 'p9c4-result', name: 'Build Coordinator Result', type: 'n8n-nodes-base.code', typeVersion: 2, position: [540,0] },
    { parameters: { jsCode: `const state=$json; const text=JSON.stringify(state); let hash=2166136261>>>0; for(let i=0;i<text.length;i++) hash=Math.imul(hash^text.charCodeAt(i),16777619)>>>0; const hex=(hash.toString(16).padStart(8,'0')).repeat(4); const bytes=(value)=>{let binary='';for(let i=0;i<value.length;i+=2)binary+=String.fromCharCode(parseInt(value.slice(i,i+2),16));return btoa(binary);}; const now=String(Date.now()*1000000); const attrs=(object)=>Object.entries(object).map(([key,value])=>({key,value:typeof value==='boolean'?{boolValue:value}:{stringValue:String(value??'')}})); const span={traceId:bytes(hex),spanId:bytes(hex.slice(0,16)),name:'incremental.phase9c4.'+state.action,kind:1,startTimeUnixNano:now,endTimeUnixNano:now,attributes:attrs({incremental_run_id:state.incremental_run_id,action:state.action,run_state:state.run_state,runtime_state:state.runtime_state,postgres_authoritative:true,qdrant_mutations:state.qdrant_mutations}),status:{code:1}}; return [{json:{...state,phoenix_trace_payload:{resourceSpans:[{resource:{attributes:attrs({'service.name':'discord-rag-bot','phoenix.project.name':'discord-rag-bot-phase-9c4'})},scopeSpans:[{scope:{name:'n8n.phase9c4.coordinator',version:'1.0.0'},spans:[span]}]}]}}}];` }, id: 'p9c4-trace', name: 'Build Correlated Phoenix Span', type: 'n8n-nodes-base.code', typeVersion: 2, position: [760,0] },
    { parameters: { method: 'POST', url: '={{ $json.phoenix_collector_url }}', sendBody: true, specifyBody: 'json', jsonBody: '={{ $json.phoenix_trace_payload }}', options: { response: { response: { fullResponse: true, neverError: true, responseFormat: 'json' } } } }, id: 'p9c4-send-trace', name: 'Send Correlated Phoenix Span', type: 'n8n-nodes-base.httpRequest', typeVersion: 4.2, position: [980,0], onError: 'continueRegularOutput' },
    { parameters: { jsCode: `const state=$items('Build Correlated Phoenix Span')[0].json; const {phoenix_trace_payload,...result}=state; return [{json:result}];` }, id: 'p9c4-return', name: 'Return Coordinator Result', type: 'n8n-nodes-base.code', typeVersion: 2, position: [1200,0] },
  ],
  connections: {
    'Coordinator Webhook': { main: [[{node:'Normalize Coordinator Command',type:'main',index:0}]] },
    'Manual Trigger': { main: [[{node:'Normalize Coordinator Command',type:'main',index:0}]] },
    'Normalize Coordinator Command': { main: [[{node:'Apply Lifecycle Operation',type:'main',index:0}]] },
    'Apply Lifecycle Operation': { main: [[{node:'Worker Needed?',type:'main',index:0}]] },
    'Worker Needed?': { main: [[{node:'Call Incremental Worker',type:'main',index:0}],[{node:'Build Coordinator Result',type:'main',index:0}]] },
    'Call Incremental Worker': { main: [[{node:'Read Authoritative Run',type:'main',index:0}]] },
    'Read Authoritative Run': { main: [[{node:'Build Coordinator Result',type:'main',index:0}]] },
    'Build Coordinator Result': { main: [[{node:'Build Correlated Phoenix Span',type:'main',index:0}]] },
    'Build Correlated Phoenix Span': { main: [[{node:'Send Correlated Phoenix Span',type:'main',index:0}]] },
    'Send Correlated Phoenix Span': { main: [[{node:'Return Coordinator Result',type:'main',index:0}]] },
  },
  settings: { executionOrder: 'v1' }, staticData: null, meta: { templateCredsSetupCompleted: false }, pinData: {},
};
write(coordinatorPath, coordinator);
