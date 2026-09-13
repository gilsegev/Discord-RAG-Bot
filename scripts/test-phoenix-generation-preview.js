const assert = require('assert');
const fs = require('fs');

const workflow = JSON.parse(fs.readFileSync(
  'workflows/n8n/rag-intake-routing-phase-9.json', 'utf8'
));
const node = name => {
  const value = workflow.nodes.find(candidate => candidate.name === name);
  assert(value, `missing node ${name}`);
  return value;
};
const checkpoint = node('Build Phoenix Final Checkpoint').parameters.jsCode;
const finalize = node('Finalize Posted Transaction').parameters.query;

assert.match(finalize, /generated_answer = CASE/);
assert.match(finalize, /run_mode.*full_answer/);

const checkpointFiles = [
  'workflows/n8n/rag-core-execution-phase-8.json',
  'workflows/n8n/rag-intake-routing-phase-9.json',
];
const spanSeeds = [];
for (const file of checkpointFiles) {
  const candidate = JSON.parse(fs.readFileSync(file, 'utf8'));
  for (const workflowNode of candidate.nodes) {
    const code = workflowNode.parameters?.jsCode || '';
    for (const match of code.matchAll(/makeSpan\([^,]+,\s*'([0-9a-f]{16})'/g)) {
      spanSeeds.push({ file, node: workflowNode.name, seed: match[1] });
    }
  }
}
assert(spanSeeds.some(({ seed }) => seed === '7100000000000000'), 'missing generation-preview span seed');
assert.strictEqual(
  new Set(spanSeeds.map(({ seed }) => seed)).size,
  spanSeeds.length,
  `Phoenix checkpoint span ID collision: ${spanSeeds.map(({ seed, node }) => `${seed}:${node}`).join(', ')}`
);

function checkpointResult(transaction, core = { run_mode: transaction.run_mode }) {
  const values = {
    'Build RAG Core Request': [{ json: core }],
  };
  const items = name => {
    if (values[name]) return values[name];
    throw new Error(`unavailable node: ${name}`);
  };
  const execute = new Function('$env', '$items', '$json', 'btoa', checkpoint);
  const btoa = value => Buffer.from(value, 'binary').toString('base64');
  return execute({}, items, transaction, btoa)[0].json.phoenix_trace_payload;
}

function spans(payload) {
  return payload.resourceSpans[0].scopeSpans[0].spans;
}

function generationSpan(payload) {
  return spans(payload).find(span => span.name === 'rag.generation_preview');
}

function attributes(span) {
  return Object.fromEntries(span.attributes.map(({ key, value }) => [
    key,
    value.stringValue ?? value.intValue ?? value.doubleValue ?? value.boolValue,
  ]));
}

function fullTransaction(output, overrides = {}) {
  return {
    transaction_id: '11111111-2222-3333-4444-555555555555',
    query_hash: 'a'.repeat(64),
    run_mode: 'full_answer',
    status: 'answered',
    response_status: 'not_posted',
    final_response_text: output,
    generated_answer: output,
    generation_model: 'gemini-test',
    ...overrides,
  };
}

const secretCases = [
  ['unclosed fenced code', 'Before ```token=do-not-show', 'do-not-show'],
  ['private key', '-----BEGIN PRIVATE KEY-----\nprivate-key-material', 'private-key-material'],
  ['Discord token', 'M'.repeat(24) + '.abcdef.A'.repeat(1) + 'b'.repeat(26), 'M'.repeat(24)],
  ['JWT', 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturevalue123456', 'eyJhbGci'],
  ['Bearer token', 'Bearer abcdefghijklmnopqrstuvwxyz012345', 'abcdefghijklmnopqrstuvwxyz012345'],
  ['AWS key', 'AKIA1234567890ABCDEF', 'AKIA1234567890ABCDEF'],
  ['assignment', 'api_key=super-secret-value password: another-secret', 'super-secret-value'],
];

for (const [name, output, forbidden] of secretCases) {
  const span = generationSpan(checkpointResult(fullTransaction(output)));
  assert(span, `${name}: expected a generation preview`);
  const preview = attributes(span).generation_preview;
  assert(!preview.includes(forbidden), `${name}: preview exposed secret material`);
  assert.strictEqual(attributes(span).generation_preview_redacted, true, `${name}: preview must report redaction`);
}

const familyEmoji = '👨‍👩‍👧‍👦';
const longOutput = 'a'.repeat(270) + familyEmoji + 'b'.repeat(40);
const longPreview = attributes(generationSpan(checkpointResult(fullTransaction(longOutput)))).generation_preview;
assert(Array.from(longPreview).length <= 280, 'preview exceeds the 280-code-point limit');
assert(longPreview.endsWith('…'), 'truncated preview must end in an ellipsis');
assert(longPreview.includes(familyEmoji), 'preview split a grapheme emoji');

const retrievalOnly = fullTransaction('injected answer must not be traced', { run_mode: 'retrieval_only' });
assert.strictEqual(generationSpan(checkpointResult(retrievalOnly)), undefined, 'retrieval-only output emitted a preview');

const passive = fullTransaction('safe passive answer', { response_status: 'not_posted' });
const passivePayload = checkpointResult(passive);
const passiveSpan = generationSpan(passivePayload);
assert(passiveSpan, 'passive full-answer transaction omitted preview');
const passiveAttributes = attributes(passiveSpan);
assert.strictEqual(passiveAttributes.transaction_id, passive.transaction_id, 'preview lost transaction correlation');
assert.strictEqual(passiveAttributes.response_status, 'not_posted', 'preview changed passive no-post status');
assert.strictEqual(attributes(spans(passivePayload)[0]).transaction_id, passive.transaction_id, 'root span lost transaction correlation');

console.log('Phoenix generation preview behavioral checks passed');
