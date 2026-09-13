const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const workflow = JSON.parse(fs.readFileSync(
  'workflows/n8n/rag-core-execution-phase-8.json', 'utf8'
));
const gate = workflow.nodes.find(node => node.name === 'Build Stage 1 Retrieval Gate');
const node = workflow.nodes.find(node => node.name === 'Build Dedupe And Context Decision');
assert(gate.parameters.jsCode.includes('root_message_id: p.root_message_id ? String(p.root_message_id) : null'));

const code = node.parameters.jsCode;
const start = code.indexOf('function stablePointId');
const end = code.indexOf('const base =');
assert(start >= 0 && end > start);
const context = {};
vm.createContext(context);
vm.runInContext(`${code.slice(start, end)}\nthis.applyDedupe = applyDedupe;`, context);

function candidate(id, root, messageIds, score) {
  return {
    qdrant_point_id: id,
    root_message_id: root,
    message_ids: messageIds,
    reranker_score: score,
    rerank_rank: Number(id),
  };
}

let result = context.applyDedupe([
  candidate('1', 'root', ['a', 'b'], 3),
  candidate('2', 'root', ['a', 'b'], 2),
], 0.5);
assert.strictEqual(result.dropped.length, 1);
assert.strictEqual(result.dropped[0].dedupe_reason, 'reply_root_overlap');

result = context.applyDedupe([
  candidate('1', 'root', ['a', 'b'], 3),
  candidate('2', 'root', ['c', 'd'], 2),
], 0.5);
assert.strictEqual(result.kept.length, 2);

result = context.applyDedupe([
  candidate('1', null, ['a', 'b'], 3),
  candidate('2', null, ['a', 'b'], 2),
], 0.5);
assert.strictEqual(result.dropped[0].dedupe_reason, 'message_overlap');

result = context.applyDedupe([
  candidate('1', 'root', ['c', 'd'], 4),
  candidate('2', 'other', ['a', 'b'], 3),
  candidate('3', 'root', ['a', 'b'], 2),
], 0.5);
assert.strictEqual(result.dropped.length, 1);
assert.strictEqual(result.dropped[0].dedupe_reason, 'message_overlap');

result = context.applyDedupe([
  candidate('1', 'other', ['a', 'b'], 4),
  candidate('2', 'root', ['c', 'd'], 3),
  candidate('3', 'root', ['a', 'b'], 2),
], 0.5);
assert.strictEqual(result.dropped.length, 1);
assert.strictEqual(result.dropped[0].dedupe_reason, 'message_overlap');

console.log('Issue #17 root and fallback dedupe tests passed');
