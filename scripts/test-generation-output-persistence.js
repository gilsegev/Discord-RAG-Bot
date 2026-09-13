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

const finalize = node('Finalize Posted Transaction').parameters.query;
const readFinal = node('Read Final Transaction').parameters.query;
const migration = fs.readFileSync(
  'deploy/phase0/sql/14-generation-output-persistence-migration.sql', 'utf8'
);
const freshSchema = fs.readFileSync(
  'deploy/phase0/sql/01-ragbot-schema.sql', 'utf8'
);

for (const field of [
  'generated_answer',
  'final_response_text',
  'generation_model',
  'generation_metadata',
]) {
  assert(finalize.includes(field), `final transaction does not write ${field}`);
  assert(readFinal.includes(field), `final transaction result does not read ${field}`);
  assert(migration.includes(field), `migration does not add ${field}`);
  assert(freshSchema.includes(field), `fresh schema does not define ${field}`);
}

assert(finalize.includes("run_mode || ''"));
assert(finalize.includes("= 'full_answer'"));
assert(finalize.includes("ELSE NULL"));
assert(finalize.includes("THEN '{}'::jsonb"));
assert(finalize.includes("gemini_response_text || ''"));
assert(finalize.includes("citation_guard_failed"));
assert(finalize.includes("response_truncated"));
assert(migration.includes('ADD COLUMN IF NOT EXISTS'));

console.log('generation output persistence contract checks passed');
