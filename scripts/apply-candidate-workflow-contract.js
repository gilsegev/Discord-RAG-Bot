const fs = require('fs');
const path = require('path');

const files = [
  'workflows/n8n/rag-core-execution-phase-8.json',
  'workflows/n8n/rag-regression-batch-runner-phase-8.json',
  'workflows/n8n/rag-intake-routing-phase-9.json',
];
for (const name of files) {
  const workflow = JSON.parse(fs.readFileSync(name, 'utf8'));
  for (const node of workflow.nodes || []) {
    for (const key of ['jsCode', 'query']) {
      if (typeof node.parameters?.[key] === 'string') {
        node.parameters[key] = node.parameters[key]
          .replaceAll("body.qdrant_collection || 'tpm_unite_history'", "body.qdrant_collection || 'rag_active'")
          .replaceAll("input.qdrant_collection || tx.qdrant_collection || 'tpm_unite_history'", "input.qdrant_collection || tx.qdrant_collection || 'rag_active'")
          .replaceAll("qdrant_collection: 'tpm_unite_history'", "qdrant_collection: 'rag_active'")
          .replaceAll('$json.qdrant_collection || "tpm_unite_history"', '$json.lease_collection || "tpm_unite_history"');
        if (node.parameters[key].includes("qdrant_collection: input.qdrant_collection || tx.qdrant_collection || 'rag_active',") &&
            !node.parameters[key].includes('lease_collection:')) {
          node.parameters[key] = node.parameters[key].replace(
            "qdrant_collection: input.qdrant_collection || tx.qdrant_collection || 'rag_active',",
            "qdrant_collection: input.qdrant_collection || tx.qdrant_collection || 'rag_active',\n    lease_collection: input.lease_collection || tx.lease_collection || 'tpm_unite_history',"
          );
        }
        node.parameters[key] = node.parameters[key].replace(/(    lease_collection: input\.lease_collection \|\| tx\.lease_collection \|\| 'tpm_unite_history',\n)(?:\1)+/g, '$1');
      }
    }
  }
  if (name.includes('regression-batch')) {
    const loader = workflow.nodes.find(n => String(n.parameters?.jsCode || '').includes('qdrant_collection:'));
    if (!loader.parameters.jsCode.includes('target_corpus_version_id:')) {
      loader.parameters.jsCode = loader.parameters.jsCode.replace(
        "qdrant_collection: body.qdrant_collection || 'rag_active',",
        "qdrant_collection: body.qdrant_collection || 'rag_active',\n    target_corpus_version_id: body.target_corpus_version_id || '',\n    target_manifest_digest: body.target_manifest_digest || '',\n    target_capture_cutoff_sequence: body.target_capture_cutoff_sequence ?? null,"
      );
    }
    const start = workflow.nodes.find(n => String(n.parameters?.query || '').includes('INSERT INTO rag_regression_runs'));
    start.parameters.query = start.parameters.query
      .replace('  case_count,\n  summary_json', '  case_count,\n  target_collection_name,\n  target_corpus_version_id,\n  target_manifest_digest,\n  target_capture_cutoff_sequence,\n  summary_json')
      .replace("  {{ $json.total_case_count }},\n  jsonb_build_object(", "  {{ $json.total_case_count }},\n  '{{ String($json.qdrant_collection || '').replace(/'/g, \"''\") }}',\n  NULLIF('{{ String($json.target_corpus_version_id || '').replace(/'/g, \"''\") }}', ''),\n  NULLIF('{{ String($json.target_manifest_digest || '').replace(/'/g, \"''\") }}', ''),\n  {{ $json.target_capture_cutoff_sequence === null ? 'NULL' : Number($json.target_capture_cutoff_sequence) }},\n  jsonb_build_object(");
  }
  fs.writeFileSync(name, JSON.stringify(workflow, null, 2) + '\n');
}
