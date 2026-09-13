const fs = require('fs');
const assert = require('assert');

const core = JSON.parse(fs.readFileSync('workflows/n8n/rag-core-execution-phase-8.json', 'utf8').replace(/^\uFEFF/, ''));
const regression = JSON.parse(fs.readFileSync('workflows/n8n/rag-regression-batch-runner-phase-8.json', 'utf8').replace(/^\uFEFF/, ''));
const coreText = JSON.stringify(core);
const regressionText = JSON.stringify(regression);

assert(coreText.includes("qdrant_collection || 'rag_active'"), 'core must default retrieval to the stable alias');
assert(coreText.includes('lease_collection || \\"tpm_unite_history\\"'), 'core must retain the Phase 9C control key for leases');
assert(regressionText.includes("body.qdrant_collection || 'rag_active'"), 'regression must default to the serving alias');
for (const field of ['target_corpus_version_id', 'target_manifest_digest', 'target_capture_cutoff_sequence']) {
  assert(regressionText.includes(field), `regression must persist ${field}`);
}
console.log('candidate promotion workflow checks passed');
