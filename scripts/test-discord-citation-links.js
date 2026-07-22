const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const workflow = JSON.parse(
  fs.readFileSync('workflows/n8n/rag-core-execution-phase-8.json', 'utf8')
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

console.log('discord citation link contract ok');
