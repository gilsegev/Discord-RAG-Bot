const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const workflow = JSON.parse(
  fs.readFileSync('workflows/n8n/rag-core-execution-phase-8.json', 'utf8')
);
const intakeWorkflow = JSON.parse(
  fs.readFileSync('workflows/n8n/rag-intake-routing-phase-9.json', 'utf8')
);
const feedbackWorkflow = JSON.parse(
  fs.readFileSync('workflows/n8n/rag-feedback-correlation-phase-10.json', 'utf8')
);

function codeFor(name) {
  const node = workflow.nodes.find((item) => item.name === name);
  assert(node, `missing workflow node: ${name}`);
  return node.parameters.jsCode;
}

function runCode(name, currentJson, nodeItems) {
  const sandbox = {
    $json: currentJson,
    $items: (nodeName) => [{ json: nodeItems[nodeName] }],
    Date,
    Set,
    Map,
    Math,
    Number,
    String,
    Array,
    JSON,
    Error,
    btoa,
  };
  return vm.runInNewContext(`(function () {${codeFor(name)}})()`, sandbox)[0].json;
}

const candidate = {
  qdrant_point_id: 'point-1',
  rank: 1,
  rerank_rank: 1,
  rank_after_dedupe: 1,
  reranker_score: 4.2,
  dedupe_status: 'kept',
  channel_id: '111111111111111111',
  channel_name: 'tpm-interview-resources',
  thread_name: null,
  first_message_id: '222222222222222222',
  message_ids: ['222222222222222222'],
  start_ts: '2024-06-18T12:00:00Z',
  end_ts: '2024-06-18T12:05:00Z',
  text: 'A grounded source message.',
  payload: { authors: ['member'] },
};

const baseState = {
  should_generate: true,
  user_query: 'How does the interview work?',
  discord_guild_id: '853099205206999050',
  context_k: 5,
  context_budget_min_result_count: 1,
  context_token_budget: 2200,
  reranker_score_threshold: 0,
  weak_reranker_score_threshold: 2,
  candidates: [candidate],
  reranked_candidates: [candidate],
  deduped_candidates: [candidate],
  dropped_dedupe_candidates: [],
};

const assembled = runCode('Assemble Context Contract', {}, {
  'Build Dedupe And Context Decision': baseState,
});
const expectedCitation = '(<#111111111111111111>, [message](https://discord.com/channels/853099205206999050/111111111111111111/222222222222222222), 2024-06-18)';
assert(assembled.context_block.includes(expectedCitation));
assert(assembled.prompt.includes('copy the complete Citation value'));
assert(assembled.prompt.includes('Return only the final Discord-ready answer'));

const linkedResult = runCode(
  'Build Gemini Result',
  {
    statusCode: 200,
    body: { candidates: [{ content: { parts: [{ text: `Members shared this. ${expectedCitation}` }] } }] },
  },
  { 'Prepare Gemini Request': { ...assembled, gemini_started_ms: Date.now() } }
);
assert.strictEqual(linkedResult.has_citation, true);
assert.strictEqual(linkedResult.citation_guard_failed, false);

const missingDateCitation = expectedCitation.replace(', 2024-06-18)', ')');
const normalizedResult = runCode(
  'Build Gemini Result',
  {
    statusCode: 200,
    body: { candidates: [{ content: { parts: [{ text: `Members shared this. ${missingDateCitation}` }] } }] },
  },
  { 'Prepare Gemini Request': { ...assembled, gemini_started_ms: Date.now() } }
);
assert.strictEqual(normalizedResult.citation_normalized_count, 1);
assert.strictEqual(normalizedResult.has_citation, true);
assert.strictEqual(normalizedResult.citation_guard_failed, false);
assert(normalizedResult.discord_response_text.includes(expectedCitation));

const plainResult = runCode(
  'Build Gemini Result',
  {
    statusCode: 200,
    body: { candidates: [{ content: { parts: [{ text: 'Members shared this. (#tpm-interview-resources, 2024-06-18)' }] } }] },
  },
  { 'Prepare Gemini Request': { ...assembled, gemini_started_ms: Date.now() } }
);
assert.strictEqual(plainResult.has_citation, false);
assert.strictEqual(plainResult.citation_guard_failed, true);

const inventedLinkResult = runCode(
  'Build Gemini Result',
  {
    statusCode: 200,
    body: { candidates: [{ content: { parts: [{ text: 'Unsupported source. (<#333333333333333333>, [message](https://discord.com/channels/853099205206999050/333333333333333333/444444444444444444), 2024-06-18)' }] } }] },
  },
  { 'Prepare Gemini Request': { ...assembled, gemini_started_ms: Date.now() } }
);
assert.strictEqual(inventedLinkResult.has_citation, false);
assert.strictEqual(inventedLinkResult.citation_guard_failed, true);

const longAnswer = (`A long grounded claim ${'x'.repeat(400)} ${expectedCitation}\n`).repeat(5);
const truncatedResult = runCode(
  'Build Gemini Result',
  {
    statusCode: 200,
    body: { candidates: [{ content: { parts: [{ text: longAnswer }] } }] },
  },
  { 'Prepare Gemini Request': { ...assembled, gemini_started_ms: Date.now() } }
);
assert.strictEqual(truncatedResult.citation_guard_failed, false);
assert.strictEqual(truncatedResult.discord_response_truncated, true);
assert(truncatedResult.discord_response_character_count <= 1900);
assert(truncatedResult.discord_response_text.endsWith(expectedCitation));

const intakeNormalize = intakeWorkflow.nodes.find((item) => item.name === 'Normalize Intake');
const intakeBuildCore = intakeWorkflow.nodes.find((item) => item.name === 'Build RAG Core Request');
const intakeExecuteCore = intakeWorkflow.nodes.find((item) => item.name === 'Execute RAG Core');
assert(intakeNormalize.parameters.jsCode.includes("input.discord_guild_id || input.guild_id"));
assert(intakeNormalize.parameters.jsCode.includes("if (!String(merged.gemini_url || '').trim())"));
assert(intakeBuildCore.parameters.jsCode.includes('discord_guild_id: input.discord_guild_id'));
assert.strictEqual(
  intakeExecuteCore.parameters.workflowInputs.value.discord_guild_id,
  "={{ $('Build RAG Core Request').item.json.discord_guild_id }}"
);

const feedbackNormalize = feedbackWorkflow.nodes.find((item) => item.name === 'Normalize Feedback');
const feedbackTrace = feedbackWorkflow.nodes.find((item) => item.name === 'Build Feedback Trace');
const feedbackResult = feedbackWorkflow.nodes.find((item) => item.name === 'Return Feedback Result');
assert(feedbackNormalize.parameters.jsCode.includes("discord_response_link: discordResponseLink"));
assert(feedbackTrace.parameters.jsCode.includes('discord_response_link: result.discord_response_link'));
assert(feedbackResult.parameters.jsCode.includes('discord_response_link: state.discord_response_link'));

console.log('discord citation link contract ok');
