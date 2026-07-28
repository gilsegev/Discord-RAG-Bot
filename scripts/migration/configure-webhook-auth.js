#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");

const workflowDirectory = path.resolve("workflows/n8n");
const authGuard = [
  "const requiredWebhookSecret = String($env.N8N_WEBHOOK_SHARED_SECRET || '');",
  "if (requiredWebhookSecret) {",
  "  const requestHeaders = ($json && $json.headers) || {};",
  "  const providedWebhookSecret = String(requestHeaders['x-rag-webhook-secret'] || requestHeaders['X-RAG-Webhook-Secret'] || '');",
  "  if (providedWebhookSecret !== requiredWebhookSecret) throw new Error('Unauthorized webhook request');",
  "}",
  "",
].join("\n");

const workflowChanges = [
  {
    file: "rag-intake-routing-phase-9.json",
    codeNode: "Normalize Intake",
  },
  {
    file: "rag-regression-batch-runner-phase-8.json",
    codeNode: "Load Regression Cases",
    requestNode: "Call Intake Workflow",
  },
  {
    file: "rag-feedback-correlation-phase-10.json",
    codeNode: "Normalize Feedback",
  },
];

let changedNodes = 0;
for (const change of workflowChanges) {
  const workflowPath = path.join(workflowDirectory, change.file);
  const workflow = JSON.parse(fs.readFileSync(workflowPath, "utf8"));
  const codeNode = workflow.nodes.find((node) => node.name === change.codeNode);
  if (!codeNode) {
    throw new Error(`Missing ${change.codeNode} in ${change.file}`);
  }

  if (!codeNode.parameters.jsCode.includes("N8N_WEBHOOK_SHARED_SECRET")) {
    codeNode.parameters.jsCode = authGuard + codeNode.parameters.jsCode;
    changedNodes += 1;
  }

  if (change.requestNode) {
    const requestNode = workflow.nodes.find((node) => node.name === change.requestNode);
    if (!requestNode) {
      throw new Error(`Missing ${change.requestNode} in ${change.file}`);
    }
    requestNode.parameters.sendHeaders = true;
    requestNode.parameters.headerParameters ||= { parameters: [] };
    requestNode.parameters.headerParameters.parameters ||= [];
    const headers = requestNode.parameters.headerParameters.parameters;
    if (!headers.some((header) => header.name.toLowerCase() === "x-rag-webhook-secret")) {
      headers.push({
        name: "X-RAG-Webhook-Secret",
        value: "={{ $env.N8N_WEBHOOK_SHARED_SECRET }}",
      });
      changedNodes += 1;
    }
  }

  fs.writeFileSync(workflowPath, `${JSON.stringify(workflow, null, 2)}\n`);
}

console.log(`Configured webhook authentication in ${changedNodes} workflow node(s).`);
