const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const workflow = JSON.parse(fs.readFileSync(
  'workflows/n8n/rag-core-execution-phase-8.json', 'utf8'
));
const node = workflow.nodes.find(candidate => candidate.name === 'Build Gemini Result');
const resultNode = workflow.nodes.find(candidate => candidate.name === 'Return RAG Core Result');
const generationNode = workflow.nodes.find(candidate => candidate.name === 'Gemini Generation');

assert(node, 'missing Build Gemini Result node');
assert(resultNode, 'missing Return RAG Core Result node');
assert(generationNode, 'missing Gemini Generation node');

const generationRequest = generationNode.parameters.jsonBody;
assert(generationRequest.includes('responseMimeType'));
assert(generationRequest.includes('responseJsonSchema'));
assert(generationRequest.includes('final_answer'));

const state = {
  allow_discord_post: false,
  gemini_started_ms: Date.now() - 5,
  refusal_text: 'I do not have enough context.',
  retrieval_status: 'context_found',
};

function run(response) {
  const result = vm.runInNewContext(
    `(function () { ${node.parameters.jsCode}\n})()`,
    {
      $json: response,
      $items: name => {
        assert.strictEqual(name, 'Prepare Gemini Request');
        return [{ json: state }];
      },
    }
  );
  return result[0].json;
}

function responseFor(parts) {
  return {
    statusCode: 200,
    body: {
      candidates: [{ content: { parts } }],
      usageMetadata: {},
    },
  };
}

function finalize(gemini) {
  const result = vm.runInNewContext(
    `(function () { ${resultNode.parameters.jsCode}\n})()`,
    {
      $json: gemini,
      $items: name => {
        if (name === 'Build Gemini Result') return [{ json: gemini }];
        if (name === 'Assemble Context Contract') return [{ json: state }];
        throw new Error(`unexpected dependency: ${name}`);
      },
    }
  );
  return result[0].json;
}

const cleanAnswer = 'TPM Unite members recommend mapping dependencies early (#tpm-tradecraft, 2024-05-01).';
const clean = run(responseFor([
  { thought: true, text: 'Citation checking: this must never be exposed.' },
  { text: JSON.stringify({ final_answer: cleanAnswer }) },
]));
assert.strictEqual(clean.final_status, 'answered');
assert.strictEqual(clean.gemini_response_text, cleanAnswer);
assert.strictEqual(clean.discord_response_text, cleanAnswer);
assert.strictEqual(clean.output_integrity_failed, false);
assert(!clean.gemini_response_text.includes('Citation checking'));

const observedDraftingFixture = 'TPM Unite members recommend mapping dependencies early (#tpm-tradecraft, 2024-05-01).\nCorrection: cite the source.\nReady to write the response.';
const drafting = run(responseFor([
  { text: JSON.stringify({ final_answer: observedDraftingFixture }) },
]));
assert.strictEqual(drafting.final_status, 'failed');
assert.strictEqual(drafting.gemini_failure_reason, 'gemini_output_integrity_failed');
assert.strictEqual(drafting.gemini_response_text, '');
assert.strictEqual(drafting.discord_response_text, '');
assert.strictEqual(drafting.output_integrity_failed, true);
const finalDrafting = finalize(drafting);
assert.strictEqual(finalDrafting.final_status, 'failed');
assert.strictEqual(finalDrafting.failure_reason, 'gemini_output_integrity_failed');
assert.strictEqual(finalDrafting.gemini_response_text, '');
assert.strictEqual(finalDrafting.discord_response_text, '');

const draftingBeforeAnswer = run(responseFor([
  { text: JSON.stringify({ final_answer: `Analysis:\n${cleanAnswer}` }) },
]));
assert.strictEqual(draftingBeforeAnswer.final_status, 'failed');
assert.strictEqual(draftingBeforeAnswer.gemini_failure_reason, 'gemini_output_integrity_failed');
assert.strictEqual(draftingBeforeAnswer.discord_response_text, '');

const thoughtOnly = run(responseFor([
  { thought: true, text: 'Internal reasoning that must never become an answer.' },
]));
assert.strictEqual(thoughtOnly.final_status, 'failed');
assert.strictEqual(thoughtOnly.gemini_failure_reason, 'gemini_output_integrity_failed');
assert.strictEqual(thoughtOnly.output_integrity_failed, true);
assert.strictEqual(thoughtOnly.gemini_response_text, '');
assert.strictEqual(thoughtOnly.discord_response_text, '');

const malformed = run(responseFor([
  { text: `${JSON.stringify({ final_answer: cleanAnswer })}\nReady to write the response.` },
]));
assert.strictEqual(malformed.final_status, 'failed');
assert.strictEqual(malformed.gemini_failure_reason, 'gemini_output_integrity_failed');
assert.strictEqual(malformed.discord_response_text, '');

const formattedAnswer = 'Use the `STAR` format for examples (#tpm-interview-resources, 2024-05-01).';
const formatted = run(responseFor([
  { text: JSON.stringify({ final_answer: formattedAnswer }) },
]));
assert.strictEqual(formatted.final_status, 'answered');
assert.strictEqual(formatted.discord_response_text, formattedAnswer);

console.log('output integrity guard checks passed');
