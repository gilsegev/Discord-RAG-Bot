#!/usr/bin/env node

const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..', '..');
const workflowFiles = [
  'workflows/n8n/rag-core-execution-phase-8.json',
  'workflows/n8n/rag-intake-routing-phase-9.json',
  'workflows/n8n/rag-regression-batch-runner-phase-8.json',
  'workflows/n8n/rag-feedback-correlation-phase-10.json',
  'workflows/n8n/rag-feedback-reconcile-phase-10.json',
];

const marker = '// Runtime service URLs: environment override with Compose fallback.';
const endpoints = [
  {
    literal: 'http://embedder:8000/embed',
    variable: 'runtimeEmbeddingUrl',
    environment: 'EMBEDDING_URL',
  },
  {
    literal: 'http://qdrant:6333',
    variable: 'runtimeQdrantBaseUrl',
    environment: 'QDRANT_BASE_URL',
  },
  {
    literal: 'http://reranker:8002/rerank',
    variable: 'runtimeRerankerUrl',
    environment: 'RERANKER_URL',
  },
  {
    literal: 'http://trace-emitter:8001/v1/traces',
    variable: 'runtimeTraceEmitterUrl',
    environment: 'TRACE_EMITTER_URL',
  },
];

function configureCode(code) {
  if (!code.includes('http://') || code.includes(marker)) return code;

  const used = endpoints.filter(({ literal }) => code.includes(literal));
  if (used.length === 0) return code;

  let configured = code;
  for (const { literal, variable } of used) {
    configured = configured
      .split(`'${literal}'`)
      .join(variable)
      .split(`"${literal}"`)
      .join(variable);
  }

  const declarations = used.map(
    ({ literal, variable, environment }) =>
      `const ${variable} = $env.${environment} || '${literal}';`,
  );
  return `${marker}\n${declarations.join('\n')}\n${configured}`;
}

let changedFiles = 0;
let changedNodes = 0;

for (const relativeFile of workflowFiles) {
  const filename = path.join(root, relativeFile);
  const workflow = JSON.parse(fs.readFileSync(filename, 'utf8'));
  let fileChanged = false;

  for (const node of workflow.nodes || []) {
    if (typeof node?.parameters?.jsCode !== 'string') continue;
    const configured = configureCode(node.parameters.jsCode);
    if (configured === node.parameters.jsCode) continue;
    node.parameters.jsCode = configured;
    fileChanged = true;
    changedNodes += 1;
  }

  if (fileChanged) {
    fs.writeFileSync(filename, `${JSON.stringify(workflow, null, 2)}\n`);
    changedFiles += 1;
  }
}

if (changedFiles === 0) {
  console.log('Workflow service URLs already configured.');
} else {
  console.log(
    `Configured runtime service URLs in ${changedNodes} node(s) across ${changedFiles} workflow file(s).`,
  );
}
