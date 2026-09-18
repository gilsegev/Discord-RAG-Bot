const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const intake = JSON.parse(fs.readFileSync('workflows/n8n/rag-intake-routing-phase-9.json', 'utf8'));
const core = JSON.parse(fs.readFileSync('workflows/n8n/rag-core-execution-phase-8.json', 'utf8'));
const code = (workflow, name) => workflow.nodes.find(node => node.name === name).parameters.jsCode;
const routeCode = code(intake, 'Set Intake Active Call');
const resultCode = code(core, 'Build Gemini Result');

function route(user_query, extra = {}) {
  return vm.runInNewContext(`(function(){${routeCode}})()`, {
    $json: { trigger_source: 'discord_passive', user_query, channel_id: 'general', passive_enabled: true, passive_excluded_channel_ids: [], ...extra },
  })[0].json;
}

for (const message of [
  'We use Think-Cell, but every Gantt chart is independent and annoying to manage.',
  'Anyone here have a good contact at Cursor? I want to start a pilot of Cursor Enterprise.',
  'Anyone here have recommendations for interviews with C-level executives?',
  'Can anyone help with a referral at Netflix?',
]) assert.strictEqual(route(message).route_type, 'passive_candidate');

assert.strictEqual(route('hello', { is_duplicate: true }).routing_reason, 'duplicate_event');
assert.strictEqual(route('hello', { author_is_bot: true }).routing_reason, 'non_member_event');
assert.strictEqual(route(' ').routing_reason, 'empty_content');
assert.strictEqual(route('hello', { passive_excluded_channel_ids: ['general'] }).routing_reason, 'excluded_channel');

function geminiResult(payload, state = {}) {
  return vm.runInNewContext(`(function(){${resultCode}})()`, {
    $json: { statusCode: 200, body: { candidates: [{ content: { parts: [{ text: JSON.stringify(payload) }] } }] } },
    $items: name => {
      assert.strictEqual(name, 'Prepare Gemini Request');
      return [{ json: { trigger_source: 'discord_passive', should_generate: true, gemini_started_ms: Date.now(), refusal_text: 'No context.', retrieval_status: 'context_found', ...state } }];
    },
  })[0].json;
}

const statement = geminiResult({ should_post: false, intent: 'non_request', decision_reason: 'Statement only', final_answer: '' });
assert.strictEqual(statement.should_post, false);
assert.strictEqual(statement.final_status, 'refused');
assert.strictEqual(statement.refusal_reason, 'gemini_post_decision_rejected');
assert.strictEqual(statement.generation_failed, false);
assert.strictEqual(statement.citation_guard_failed, false);
assert.strictEqual(statement.discord_response_text, '');

const answer = geminiResult({ should_post: true, intent: 'request', decision_reason: 'Explicit request', final_answer: 'Members discussed this (#general, 2024-01-01).' });
assert.strictEqual(answer.should_post, true);
assert.strictEqual(answer.final_status, 'answered');

const inconsistent = geminiResult({ should_post: true, intent: 'non_request', decision_reason: 'Contradictory', final_answer: 'Do not use' });
assert.strictEqual(inconsistent.should_post, false);
assert.strictEqual(inconsistent.final_status, 'failed');
assert.strictEqual(inconsistent.output_integrity_failed, true);

const noContext = geminiResult({ should_post: true, intent: 'request', decision_reason: 'Valid request', final_answer: 'Do not use' }, { should_generate: false });
assert.strictEqual(noContext.should_post, false);
assert.strictEqual(noContext.final_status, 'failed');

const validNoContext = geminiResult({ should_post: false, intent: 'request', decision_reason: 'No grounded answer', final_answer: '' }, { should_generate: false });
assert.strictEqual(validNoContext.final_status, 'refused');
assert.strictEqual(validNoContext.should_post, false);

const hiddenAnswer = geminiResult({ should_post: false, intent: 'non_request', decision_reason: 'Statement', final_answer: 'Not allowed' });
assert.strictEqual(hiddenAnswer.output_integrity_failed, true);

assert.match(code(core, 'Assemble Context Contract'), /state\.trigger_source === 'discord_passive'/);
assert.match(code(core, 'Apply Stage 0 Safety Gate'), /state\.trigger_source !== 'discord_passive'/);
assert.match(intake.nodes.find(node => node.name === 'Capture Discord Message').parameters.query, /rag_intake_event_claims/);
assert.match(code(intake, 'Restore Intake After Capture'), /event_duplicate/);
assert.strictEqual(route('Hi', { event_duplicate: true }).routing_reason, 'duplicate_event');

function runCoreNode(name, state, dependency) {
  return vm.runInNewContext(`(function(){${code(core, name)}})()`, {
    $json: state,
    $items: requested => {
      assert.strictEqual(requested, dependency);
      return [{ json: state }];
    },
  })[0].json;
}

const contactQuestion = 'Anyone know the recruiter email for Netflix?';
const passiveSafety = runCoreNode('Apply Stage 0 Safety Gate', { trigger_source: 'discord_passive', user_query: contactQuestion }, null);
const activeSafety = runCoreNode('Apply Stage 0 Safety Gate', { trigger_source: 'discord_active', user_query: contactQuestion }, null);
assert.strictEqual(passiveSafety.safety_gate_status, 'passed');
assert.strictEqual(activeSafety.safety_gate_status, 'refused');

const activePrompt = runCoreNode('Assemble Context Contract', { trigger_source: 'discord_active', user_query: 'What is TPM?', should_generate: false, refusal_reason: 'no_context' }, 'Build Dedupe And Context Decision').prompt;
const passivePrompt = runCoreNode('Assemble Context Contract', { trigger_source: 'discord_passive', user_query: 'I use Think-Cell.', should_generate: false, refusal_reason: 'no_context' }, 'Build Dedupe And Context Decision').prompt;
assert(!activePrompt.includes('PASSIVE POST DECISION'));
assert(passivePrompt.includes('PASSIVE POST DECISION'));

const malformed = geminiResult({ final_answer: 'No decision' });
assert.strictEqual(malformed.generation_failed, true);
assert.strictEqual(malformed.output_integrity_failed, true);

console.log('passive LLM post decision ok');
