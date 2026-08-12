const fs = require('fs');

const credential = { postgres: { id: 'JcwnINxG4CstrGKy', name: 'Postgres account' } };
const pos = (() => { let x = -1200; return (y = 0) => [x += 220, y]; })();
const code = (name, jsCode, y = 0) => ({ parameters: { jsCode }, id: `p9c5-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`, name, type: 'n8n-nodes-base.code', typeVersion: 2, position: pos(y) });
const pg = (name, query, y = 0) => ({ parameters: { operation: 'executeQuery', query }, id: `p9c5-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`, name, type: 'n8n-nodes-base.postgres', typeVersion: 2.6, position: pos(y), credentials: credential });
const http = (name, url, jsonBody, y = 0) => ({ parameters: { method: 'POST', url, sendHeaders: true, headerParameters: { parameters: [{ name: 'X-RAG-Webhook-Secret', value: '={{ $env.N8N_WEBHOOK_SHARED_SECRET }}' }] }, sendBody: true, specifyBody: 'json', jsonBody, options: { response: { response: { fullResponse: true, neverError: true, responseFormat: 'json' } } } }, id: `p9c5-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`, name, type: 'n8n-nodes-base.httpRequest', typeVersion: 4.2, position: pos(y) });
const iff = (name, leftValue, y = 0) => ({ parameters: { conditions: { options: { caseSensitive: true, leftValue: '', typeValidation: 'strict', version: 2 }, conditions: [{ id: `${name}-condition`, leftValue, rightValue: true, operator: { type: 'boolean', operation: 'true', singleValue: true } }], combinator: 'and' }, options: {} }, id: `p9c5-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`, name, type: 'n8n-nodes-base.if', typeVersion: 2.2, position: pos(y) });
const connect = (workflow, from, to, branch = 0) => {
  workflow.connections[from] ||= { main: [] };
  workflow.connections[from].main[branch] ||= [];
  workflow.connections[from].main[branch].push({ node: to, type: 'main', index: 0 });
};

const controller = { name: 'RAG Incremental Scheduled Controller - Phase 9C.5', active: false, nodes: [], connections: {}, settings: { executionOrder: 'v1' }, staticData: null, meta: { templateCredsSetupCompleted: false }, pinData: {} };
controller.nodes.push(
  { parameters: { rule: { interval: [{ field: 'cronExpression', expression: "={{ $env.PHASE9C5_CRON || '0 3 * * *' }}" }] } }, id: 'p9c5-schedule', name: 'Low Traffic Schedule', type: 'n8n-nodes-base.scheduleTrigger', typeVersion: 1.2, position: [-1200,-180] },
  { parameters: { httpMethod: 'POST', path: 'rag-incremental-schedule-phase-9c5', responseMode: 'lastNode', options: {} }, id: 'p9c5-webhook', name: 'Schedule Control Webhook', type: 'n8n-nodes-base.webhook', typeVersion: 2, position: [-1200,0], webhookId: 'rag-incremental-schedule-phase-9c5' },
  { parameters: {}, id: 'p9c5-manual', name: 'Manual Dry Run', type: 'n8n-nodes-base.manualTrigger', typeVersion: 1, position: [-1200,180] },
  code('Normalize Schedule Request', `const envelope=$json||{}; const raw=envelope.body||envelope;
const headers=envelope.headers||{}; const secret=String($env.N8N_WEBHOOK_SHARED_SECRET||'');
if(envelope.body&&secret&&String(headers['x-rag-webhook-secret']||'')!==secret) throw new Error('Unauthorized schedule request');
const scheduled=!envelope.body&&!raw.trigger_source&&Boolean(raw.timestamp);
const requested=String(raw.mode||'dry_run').toLowerCase();
const triggerSource=scheduled?'scheduled':(requested==='execute'?'manual_execute':'manual_dry_run');
const attemptId=String(raw.attempt_id||('phase9c5-'+$execution.id)).replace(/[^A-Za-z0-9._:-]/g,'');
return [{json:{attempt_id:attemptId,collection_name:String(raw.collection_name||'tpm_unite_history'),trigger_source:triggerSource,requested_mode:requested}}];`),
  pg('Prepare Scheduled Attempt', `SELECT * FROM rag_prepare_incremental_schedule_attempt(
  '{{ $json.attempt_id }}','{{ $json.collection_name }}','{{ $json.trigger_source }}');`),
  iff('Should Build Plan?', "={{ $json.decision === 'planning' }}"),
  { parameters: { method: 'POST', url: "={{ ($env.INCREMENTAL_WORKER_URL || 'http://incremental-worker:8003') + '/plan' }}", sendHeaders: true, headerParameters: { parameters: [{ name: 'X-Incremental-Worker-Token', value: '={{ $env.INCREMENTAL_WORKER_TOKEN }}' }] }, sendBody: true, specifyBody: 'json', jsonBody: "={{ { collection_name: $items('Normalize Schedule Request')[0].json.collection_name, batch_cutoff_sequence: Number($items('Prepare Scheduled Attempt')[0].json.batch_cutoff_sequence), persist: true } }}", options: { response: { response: { fullResponse: true, neverError: true, responseFormat: 'json' } } } }, id: 'p9c5-plan-worker', name: 'Build Shadow Validated Plan', type: 'n8n-nodes-base.httpRequest', typeVersion: 4.2, position: pos(-120) },
  pg('Attach Plan Safety Gates', `SELECT * FROM rag_attach_incremental_schedule_plan(
  '{{ $items("Normalize Schedule Request")[0].json.attempt_id }}',
  '{{ $items("Build Shadow Validated Plan")[0].json.body.plan_id }}');`),
  iff('Plan Is Safe?', "={{ $json.decision === 'ready' }}"),
  iff('Execution Requested?', "={{ $items('Normalize Schedule Request')[0].json.trigger_source !== 'manual_dry_run' }}"),
  pg('Finish Dry Plan Attempt', `SELECT (rag_finish_incremental_schedule_attempt(
  '{{ $items("Normalize Schedule Request")[0].json.attempt_id }}','ready',NULL,false,
  jsonb_build_object('dry_run',true,'plan_id','{{ $items("Build Shadow Validated Plan")[0].json.body.plan_id }}')
)).*;`,120),
  code('Return Dry Plan Summary', `return [{json:{...$json,qdrant_mutations:false,dry_run:true}}];`,120),
  pg('Mark Attempt Dispatched', `SELECT (rag_finish_incremental_schedule_attempt(
  '{{ $items("Normalize Schedule Request")[0].json.attempt_id }}','dispatched',NULL,false,
  jsonb_build_object('controller_execution_id','{{ $execution.id }}')
)).*;`),
  http('Call Proven Scheduled Runner', 'http://127.0.0.1:5678/webhook/rag-incremental-scheduled-run-phase-9c5', "={{ { attempt_id: $items('Normalize Schedule Request')[0].json.attempt_id, plan_id: $items('Build Shadow Validated Plan')[0].json.body.plan_id, collection_name: $items('Normalize Schedule Request')[0].json.collection_name } }}"),
  pg('Finish Scheduled Attempt', `SELECT (rag_finish_incremental_schedule_attempt(
  '{{ $items("Normalize Schedule Request")[0].json.attempt_id }}',
  CASE WHEN {{ Number($json.statusCode || 0) }} BETWEEN 200 AND 299 AND '{{ String($json.body.status || "").replace(/'/g,"''") }}'='completed' THEN 'completed' ELSE 'failed' END,
  NULLIF('{{ String($json.body.incremental_run_id || "").replace(/'/g,"''") }}',''),
  {{ $json.body.qdrant_mutations === true ? 'true' : 'false' }},
  '{{ JSON.stringify($json.body || {}).replace(/'/g,"''") }}'::jsonb
)).*;`),
  pg('Queue Attempt Outcome Alert', `SELECT (rag_queue_incremental_alert(
  '{{ $json.attempt_id }}',CASE WHEN '{{ $json.decision }}'='completed' THEN 'info' ELSE 'critical' END,
  'incremental_run_{{ $json.decision }}','{{ $json.attempt_id }}:{{ $json.decision }}',
  jsonb_build_object('decision','{{ $json.decision }}','run_id','{{ String($json.incremental_run_id || "").replace(/'/g,"''") }}','qdrant_mutations',{{ $json.qdrant_mutations ? 'true' : 'false' }})
)).*;`),
  code('Build Attempt Summary', `let row={}; try{row=$items('Finish Scheduled Attempt')[0]?.json||{};}catch(e){} if(!row.attempt_id){try{row=$items('Finish Unsafe Plan Attempt')[0]?.json||{};}catch(e){}} return [{json:{attempt_id:row.attempt_id,decision:row.decision,schedule_enabled:row.schedule_enabled,catchup_completed:row.catchup_completed,batch_cutoff_sequence:row.batch_cutoff_sequence,pending_message_count:row.pending_message_count,plan_id:row.plan_id,incremental_run_id:row.incremental_run_id,qdrant_mutations:row.qdrant_mutations,decision_reasons:row.decision_reasons,report:row.report,alert_status:$json.delivery_status}}];`),
  pg('Finish Skipped Attempt', `SELECT (rag_finish_incremental_schedule_attempt(
  '{{ $items("Normalize Schedule Request")[0].json.attempt_id }}','{{ $json.decision }}',NULL,false,
  jsonb_build_object('controller_execution_id','{{ $execution.id }}','dry_run',true)
)).*;`, 180),
  pg('Queue Skipped Alert', `SELECT (rag_queue_incremental_alert(
  '{{ $json.attempt_id }}',
  CASE WHEN '{{ $json.decision }}'='blocked' THEN 'warning' ELSE 'info' END,
  'incremental_schedule_{{ $json.decision }}',
  '{{ $json.attempt_id }}:{{ $json.decision }}',
  jsonb_build_object('decision','{{ $json.decision }}','reasons','{{ JSON.stringify($json.decision_reasons).replace(/'/g,"''") }}'::jsonb)
)).*;`, 180),
  code('Return Skipped Summary', `const attempt=$items('Finish Skipped Attempt')[0].json; const alert=$json; return [{json:{attempt_id:attempt.attempt_id,decision:attempt.decision,schedule_enabled:attempt.schedule_enabled,catchup_completed:attempt.catchup_completed,batch_cutoff_sequence:attempt.batch_cutoff_sequence,pending_message_count:attempt.pending_message_count,qdrant_mutations:false,decision_reasons:attempt.decision_reasons,alert_status:alert.delivery_status}}];`, 180),
  pg('Finish Unsafe Plan Attempt', `SELECT (rag_finish_incremental_schedule_attempt(
  '{{ $items("Normalize Schedule Request")[0].json.attempt_id }}','blocked',NULL,false,
  jsonb_build_object('plan_id','{{ $items("Build Shadow Validated Plan")[0].json.body.plan_id }}')
)).*;`, 60),
  pg('Queue Unsafe Plan Alert', `SELECT (rag_queue_incremental_alert(
  '{{ $json.attempt_id }}','warning','incremental_plan_blocked','{{ $json.attempt_id }}:plan_blocked',
  jsonb_build_object('decision_reasons','{{ JSON.stringify($json.decision_reasons).replace(/'/g,"''") }}'::jsonb)
)).*;`,60),
  code('Return Unsafe Plan Summary', `const row=$items('Finish Unsafe Plan Attempt')[0].json; return [{json:{...row,qdrant_mutations:false,alert_status:$json.delivery_status}}];`, 60),
);
connect(controller,'Low Traffic Schedule','Normalize Schedule Request'); connect(controller,'Schedule Control Webhook','Normalize Schedule Request'); connect(controller,'Manual Dry Run','Normalize Schedule Request');
connect(controller,'Normalize Schedule Request','Prepare Scheduled Attempt'); connect(controller,'Prepare Scheduled Attempt','Should Build Plan?');
connect(controller,'Should Build Plan?','Build Shadow Validated Plan',0); connect(controller,'Should Build Plan?','Finish Skipped Attempt',1);
connect(controller,'Build Shadow Validated Plan','Attach Plan Safety Gates'); connect(controller,'Attach Plan Safety Gates','Plan Is Safe?');
connect(controller,'Plan Is Safe?','Execution Requested?',0); connect(controller,'Plan Is Safe?','Finish Unsafe Plan Attempt',1); connect(controller,'Execution Requested?','Mark Attempt Dispatched',0); connect(controller,'Execution Requested?','Finish Dry Plan Attempt',1); connect(controller,'Finish Dry Plan Attempt','Return Dry Plan Summary');
connect(controller,'Mark Attempt Dispatched','Call Proven Scheduled Runner'); connect(controller,'Call Proven Scheduled Runner','Finish Scheduled Attempt'); connect(controller,'Finish Scheduled Attempt','Queue Attempt Outcome Alert'); connect(controller,'Queue Attempt Outcome Alert','Build Attempt Summary');
connect(controller,'Finish Skipped Attempt','Queue Skipped Alert'); connect(controller,'Queue Skipped Alert','Return Skipped Summary'); connect(controller,'Finish Unsafe Plan Attempt','Queue Unsafe Plan Alert'); connect(controller,'Queue Unsafe Plan Alert','Return Unsafe Plan Summary');

// The runner is a thin, inspectable sequence of calls to the existing Phase 9C.4
// coordinator and Phase 8 regression runner. It contains no Qdrant mutation code.
const runner = { name: 'RAG Incremental Scheduled Run - Phase 9C.5', active: false, nodes: [], connections: {}, settings: { executionOrder: 'v1' }, staticData: null, meta: { templateCredsSetupCompleted: false }, pinData: {} };
runner.nodes.push(
  { parameters: { httpMethod: 'POST', path: 'rag-incremental-scheduled-run-phase-9c5', responseMode: 'lastNode', options: {} }, id: 'p9c5-run-webhook', name: 'Scheduled Run Webhook', type: 'n8n-nodes-base.webhook', typeVersion: 2, position: [-1200,0], webhookId: 'rag-incremental-scheduled-run-phase-9c5' },
  code('Normalize Run Request', `const envelope=$json||{},raw=envelope.body||envelope,headers=envelope.headers||{}; const secret=String($env.N8N_WEBHOOK_SHARED_SECRET||''); if(secret&&String(headers['x-rag-webhook-secret']||'')!==secret) throw new Error('Unauthorized scheduled run'); const clean=v=>String(v||'').replace(/[^A-Za-z0-9._:-]/g,''); const attempt=clean(raw.attempt_id),plan=clean(raw.plan_id),collection=clean(raw.collection_name||'tpm_unite_history'); if(!attempt||!plan) throw new Error('attempt_id and plan_id required'); return [{json:{attempt_id:attempt,plan_id:plan,collection_name:collection,incremental_run_id:'scheduled-'+attempt+'-'+$execution.id}}];`),
  pg('Verify Ready Attempt', `SELECT a.*,rs.runtime_state,rs.state_revision FROM rag_incremental_schedule_attempts a JOIN rag_runtime_state rs ON rs.collection_name=a.collection_name WHERE a.attempt_id='{{ $json.attempt_id }}' AND a.decision='dispatched' AND (a.schedule_enabled OR a.trigger_source='manual_execute') AND a.catchup_completed AND rs.runtime_state='serving' AND a.plan_id='{{ $json.plan_id }}';`),
  http('Create Incremental Run','http://127.0.0.1:5678/webhook/rag-incremental-phase-9c4',"={{ { action:'create', incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id, plan_id:$items('Normalize Run Request')[0].json.plan_id, collection_name:$items('Normalize Run Request')[0].json.collection_name, requested_by:'phase9c5_schedule' } }}"),
  http('Run Baseline Regression','http://127.0.0.1:5678/webhook/rag-regression-batch',"={{ { cases:'all',mode:'retrieval_only',allow_gemini:false,allow_discord_post:false,write_eval_labels:false,requested_by:'phase9c5_schedule_baseline' } }}"),
  code('Require Accepted Baseline', `const r=$json.body||$json; if(Number(r.case_count||r.total_cases)!==48||Number(r.pass_count)!==43||Number(r.fail_count)!==1||Number(r.review_count)!==4) throw new Error('baseline regression differs from accepted 43/1/4'); return [{json:r}];`),
  http('Record Baseline Regression','http://127.0.0.1:5678/webhook/rag-incremental-phase-9c4',"={{ { action:'record_baseline', incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id, regression_run_id:$json.run_id, regression_result:'passed' } }}"),
  http('Run Replacement Preflight','http://127.0.0.1:5678/webhook/rag-incremental-phase-9c4',"={{ { action:'preflight', incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id } }}"),
  pg('Read Runtime Revision', `SELECT rs.state_revision FROM rag_runtime_state rs WHERE rs.collection_name='{{ $items("Normalize Run Request")[0].json.collection_name }}' AND rs.runtime_state='serving';`),
  http('Begin Drain','http://127.0.0.1:5678/webhook/rag-incremental-phase-9c4',"={{ { action:'begin_drain', incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id, runtime_revision:Number($json.state_revision) } }}"),
  { parameters: { amount: 5, unit: 'seconds' }, id: 'p9c5-wait-drain', name: 'Wait For Active Retrievals', type: 'n8n-nodes-base.wait', typeVersion: 1.1, position: pos(0), webhookId: 'p9c5-wait-drain' },
  pg('Check Drain', `SELECT rag_count_live_execution_leases('{{ $items("Normalize Run Request")[0].json.collection_name }}')=0 AS drained,
  now() > a.started_at + make_interval(secs=>c.max_maintenance_seconds) AS timed_out
  FROM rag_incremental_schedule_attempts a JOIN rag_incremental_schedule_config c USING(collection_name)
  WHERE a.attempt_id='{{ $items("Normalize Run Request")[0].json.attempt_id }}';`),
  iff('Drain Complete?', '={{ $json.drained }}'),
  iff('Drain Timed Out?', '={{ $json.timed_out }}', 160),
  pg('Cancel Timed Out Drain', `SELECT * FROM rag_cancel_incremental_drain('{{ $items("Normalize Run Request")[0].json.incremental_run_id }}','scheduled_drain_timeout');`, 160),
  code('Return Drain Failure', `return [{json:{status:'failed',incremental_run_id:$json.incremental_run_id,qdrant_mutations:false,failure_reason:'scheduled_drain_timeout'}}];`, 160),
  http('Enter Maintenance','http://127.0.0.1:5678/webhook/rag-incremental-phase-9c4',"={{ { action:'enter_maintenance', incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id, runtime_revision:Number(($items('Begin Drain')[0].json.body||$items('Begin Drain')[0].json).runtime_revision) } }}"),
  http('Apply Replacement','http://127.0.0.1:5678/webhook/rag-incremental-phase-9c4',"={{ { action:'apply', incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id, runtime_revision:Number(($json.body||$json).runtime_revision), confirm_apply:'PHASE9C4_APPLY', take_full_snapshot:false } }}"),
  iff('Apply Verified?', "={{ Number($json.statusCode) >= 200 && Number($json.statusCode) < 300 && Number($json.body?.worker_result?.statusCode) >= 200 && Number($json.body?.worker_result?.statusCode) < 300 && $json.body?.structural_verification_result === 'passed' }}"),
  http('Run Post Regression','http://127.0.0.1:5678/webhook/rag-regression-batch',"={{ { cases:'all',mode:'retrieval_only',allow_gemini:false,allow_discord_post:false,write_eval_labels:false,requested_by:'phase9c5_schedule_post',maintenance_validation_run_id:$items('Normalize Run Request')[0].json.incremental_run_id } }}"),
  iff('Post Regression Matches?', "={{ Number($json.body?.case_count || $json.body?.total_cases) === 48 && Number($json.body?.pass_count) === 43 && Number($json.body?.fail_count) === 1 && Number($json.body?.review_count) === 4 }}"),
  http('Record Post Regression','http://127.0.0.1:5678/webhook/rag-incremental-phase-9c4',"={{ { action:'record_regression', incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id, regression_run_id:$json.body.run_id, regression_result:'passed' } }}"),
  http('Reopen Serving','http://127.0.0.1:5678/webhook/rag-incremental-phase-9c4',"={{ { action:'exit_completed', incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id } }}"),
  code('Return Completed Run', `const r=$json.body||$json; return [{json:{status:'completed',incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id,qdrant_mutations:true,run_state:r.run_state,runtime_state:r.runtime_state,runtime_revision:r.runtime_revision}}];`),
  http('Rollback Failed Validation','http://127.0.0.1:5678/webhook/rag-incremental-phase-9c4',"={{ { action:'rollback', incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id } }}",180),
  http('Exit Failed Run','http://127.0.0.1:5678/webhook/rag-incremental-phase-9c4',"={{ { action:'exit_failed', incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id } }}",180),
  code('Return Rolled Back Run', `const r=$json.body||$json; return [{json:{status:'failed',incremental_run_id:$items('Normalize Run Request')[0].json.incremental_run_id,qdrant_mutations:true,rollback_status:'completed',run_state:r.run_state,runtime_state:r.runtime_state}}];`,180),
);
connect(runner,'Scheduled Run Webhook','Normalize Run Request'); connect(runner,'Normalize Run Request','Verify Ready Attempt'); connect(runner,'Verify Ready Attempt','Create Incremental Run'); connect(runner,'Create Incremental Run','Run Baseline Regression'); connect(runner,'Run Baseline Regression','Require Accepted Baseline'); connect(runner,'Require Accepted Baseline','Record Baseline Regression'); connect(runner,'Record Baseline Regression','Run Replacement Preflight'); connect(runner,'Run Replacement Preflight','Read Runtime Revision'); connect(runner,'Read Runtime Revision','Begin Drain'); connect(runner,'Begin Drain','Wait For Active Retrievals'); connect(runner,'Wait For Active Retrievals','Check Drain'); connect(runner,'Check Drain','Drain Complete?'); connect(runner,'Drain Complete?','Enter Maintenance',0); connect(runner,'Drain Complete?','Drain Timed Out?',1); connect(runner,'Drain Timed Out?','Cancel Timed Out Drain',0); connect(runner,'Drain Timed Out?','Wait For Active Retrievals',1); connect(runner,'Cancel Timed Out Drain','Return Drain Failure'); connect(runner,'Enter Maintenance','Apply Replacement'); connect(runner,'Apply Replacement','Apply Verified?'); connect(runner,'Apply Verified?','Run Post Regression',0); connect(runner,'Apply Verified?','Rollback Failed Validation',1); connect(runner,'Run Post Regression','Post Regression Matches?'); connect(runner,'Post Regression Matches?','Record Post Regression',0); connect(runner,'Post Regression Matches?','Rollback Failed Validation',1); connect(runner,'Record Post Regression','Reopen Serving'); connect(runner,'Reopen Serving','Return Completed Run'); connect(runner,'Rollback Failed Validation','Exit Failed Run'); connect(runner,'Exit Failed Run','Return Rolled Back Run');

fs.writeFileSync('workflows/n8n/rag-incremental-scheduled-controller-phase-9c5.json', JSON.stringify(controller,null,2)+'\n');
fs.writeFileSync('workflows/n8n/rag-incremental-scheduled-run-phase-9c5.json', JSON.stringify(runner,null,2)+'\n');

const alerts = { name: 'RAG Incremental Operator Alerts - Phase 9C.5', active: false, nodes: [], connections: {}, settings: { executionOrder: 'v1' }, staticData: null, meta: { templateCredsSetupCompleted: false }, pinData: {} };
alerts.nodes.push(
  { parameters: { rule: { interval: [{ field: 'minutes', minutesInterval: 5 }] } }, id: 'p9c5-alert-schedule', name: 'Alert Outbox Schedule', type: 'n8n-nodes-base.scheduleTrigger', typeVersion: 1.2, position: [-800,-100] },
  { parameters: {}, id: 'p9c5-alert-manual', name: 'Manual Alert Check', type: 'n8n-nodes-base.manualTrigger', typeVersion: 1, position: [-800,100] },
  pg('Load Queued Incremental Alerts', `SELECT alert_id,attempt_id,severity,alert_code,alert_payload,created_at
FROM rag_incremental_operator_alerts WHERE delivery_status='queued'
ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,created_at LIMIT 20;`),
  code('Prepare Alert Delivery', `return $input.all().map(item=>{const a=item.json; const configured=Boolean(String($env.INCREMENTAL_ALERT_WEBHOOK_URL||'').trim()); const reasons=a.alert_payload?.reasons||[]; return {json:{...a,configured,alert_url:String($env.INCREMENTAL_ALERT_WEBHOOK_URL||''),content:'['+String(a.severity).toUpperCase()+'] Incremental ingestion '+a.alert_code+' (attempt '+a.attempt_id+')'+(reasons.length?' — '+reasons.join(', '):'')}};});`),
  iff('Alert Destination Configured?', '={{ $json.configured }}'),
  { parameters: { method: 'POST', url: '={{ $json.alert_url }}', sendBody: true, specifyBody: 'json', jsonBody: '={{ { content: $json.content } }}', options: { response: { response: { fullResponse: true, neverError: true, responseFormat: 'json' } } } }, id: 'p9c5-send-alert', name: 'Send Operator Alert', type: 'n8n-nodes-base.httpRequest', typeVersion: 4.2, position: pos(-100) },
  pg('Record Alert Delivery', `UPDATE rag_incremental_operator_alerts SET
delivery_status=CASE WHEN {{ Number($json.statusCode || 0) }} BETWEEN 200 AND 299 THEN 'sent' ELSE 'failed' END,
delivery_attempts=delivery_attempts+1,delivered_at=CASE WHEN {{ Number($json.statusCode || 0) }} BETWEEN 200 AND 299 THEN now() ELSE delivered_at END,
last_error=CASE WHEN {{ Number($json.statusCode || 0) }} BETWEEN 200 AND 299 THEN NULL ELSE 'http_status_{{ Number($json.statusCode || 0) }}' END
WHERE alert_id='{{ $("Prepare Alert Delivery").item.json.alert_id }}'::uuid RETURNING alert_id,delivery_status,delivery_attempts;`),
  code('Keep Alert Queued', `return [{json:{alert_id:$json.alert_id,delivery_status:'queued',reason:'INCREMENTAL_ALERT_WEBHOOK_URL_not_configured'}}];`,100),
);
connect(alerts,'Alert Outbox Schedule','Load Queued Incremental Alerts'); connect(alerts,'Manual Alert Check','Load Queued Incremental Alerts'); connect(alerts,'Load Queued Incremental Alerts','Prepare Alert Delivery'); connect(alerts,'Prepare Alert Delivery','Alert Destination Configured?'); connect(alerts,'Alert Destination Configured?','Send Operator Alert',0); connect(alerts,'Alert Destination Configured?','Keep Alert Queued',1); connect(alerts,'Send Operator Alert','Record Alert Delivery');
fs.writeFileSync('workflows/n8n/rag-incremental-operator-alerts-phase-9c5.json', JSON.stringify(alerts,null,2)+'\n');
