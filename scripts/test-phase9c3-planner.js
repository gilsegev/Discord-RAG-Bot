const assert = require('assert');
const fs = require('fs');

const planner = fs.readFileSync('ingestion/incremental_planner.py', 'utf8');
const migration = fs.readFileSync(
  'deploy/phase0/sql/09-phase9c3-shadow-plans-migration.sql',
  'utf8',
);
const executionPlan = fs.readFileSync('docs/n8n execution plan.md', 'utf8');

assert.match(planner, /def coalesce_work\(/);
assert.match(planner, /def create_shadow_plan\(/);
assert.match(planner, /chunk_records\(selected\)/);
assert.match(planner, /def embed_shadow\(/);
assert.match(planner, /def benchmark_embedder\(/);
assert.match(planner, /rendered\["qdrant_mutations"\]\s*=\s*0/);
assert.doesNotMatch(planner, /\.upsert\(/);
assert.doesNotMatch(planner, /\.delete\(/);
assert.doesNotMatch(planner, /\.set_payload\(/);
assert.match(migration, /CREATE TABLE IF NOT EXISTS rag_chunk_replacement_plans/);
assert.match(migration, /CREATE TABLE IF NOT EXISTS rag_chunk_replacement_plan_groups/);
assert.match(executionPlan, /### Phase 9C\.3: Offline planner and shadow rechunking/);
assert.match(executionPlan, /111\.92 chunks\/minute/);

console.log('phase9c3 planner specification checks passed');
