const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');

const REPO_ROOT = path.resolve(__dirname, '..');
const WORKFLOWS_DIR = path.join(REPO_ROOT, 'workflows', 'n8n');
const ENV_FILES = ['.env', '.env.local'];

function loadEnv() {
  for (const file of ENV_FILES) {
    const envPath = path.join(REPO_ROOT, file);
    if (!fs.existsSync(envPath)) continue;

    const content = fs.readFileSync(envPath, 'utf8');
    for (const rawLine of content.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line || line.startsWith('#')) continue;

      const match = line.match(/^([^=]+)=(.*)$/);
      if (!match) continue;

      const key = match[1].trim();
      let value = match[2].trim();
      if (
        (value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))
      ) {
        value = value.slice(1, -1);
      }
      if (!process.env[key]) process.env[key] = value;
    }
  }
}

loadEnv();

const N8N_API_URL = normalizeApiUrl(process.env.N8N_API_URL);
const N8N_API_KEY = process.env.N8N_API_KEY;
const ACTION = process.argv[2];
const TARGET = process.argv[3];

if (!['pull', 'push', 'list'].includes(ACTION)) {
  console.error('Usage: npm run n8n:<pull|push|list> [-- optional-file-or-workflow-name]');
  console.error('');
  console.error('Examples:');
  console.error('  npm run n8n:list');
  console.error('  npm run n8n:push -- workflows/n8n/rag-active-call-phase-4-stage-1-retrieval-gate.json');
  console.error('  npm run n8n:pull');
  process.exit(1);
}

if (!N8N_API_URL || !N8N_API_KEY) {
  console.error('Missing N8N_API_URL or N8N_API_KEY.');
  console.error('');
  console.error('Create .env.local in the repo root with:');
  console.error('  N8N_API_URL=http://127.0.0.1:5679/api/v1');
  console.error('  N8N_API_KEY=<n8n personal API key>');
  process.exit(1);
}

function normalizeApiUrl(value) {
  if (!value) return value;
  return value.replace(/\/+$/, '');
}

function slugify(value) {
  return String(value)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

function request(method, urlString, body = null) {
  return new Promise((resolve, reject) => {
    const parsedUrl = new URL(urlString);
    const data = body ? JSON.stringify(body) : null;
    const options = {
      method,
      hostname: parsedUrl.hostname,
      port: parsedUrl.port || (parsedUrl.protocol === 'https:' ? 443 : 80),
      path: parsedUrl.pathname + parsedUrl.search,
      headers: {
        'X-N8N-API-KEY': N8N_API_KEY,
        Accept: 'application/json',
      },
    };

    if (data) {
      options.headers['Content-Type'] = 'application/json';
      options.headers['Content-Length'] = Buffer.byteLength(data);
    }

    const client = parsedUrl.protocol === 'https:' ? https : http;
    const req = client.request(options, (res) => {
      let responseBody = '';
      res.on('data', (chunk) => {
        responseBody += chunk;
      });
      res.on('end', () => {
        const isJson = responseBody && res.headers['content-type']?.includes('application/json');
        const parsed = isJson ? JSON.parse(responseBody) : responseBody;
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(parsed || null);
          return;
        }
        reject(new Error(`HTTP ${res.statusCode}: ${responseBody}`));
      });
    });

    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}

function sanitizeWorkflow(workflow) {
  return {
    id: workflow.id,
    name: workflow.name,
    active: workflow.active,
    nodes: workflow.nodes || [],
    connections: workflow.connections || {},
    settings: workflow.settings || {},
    staticData: workflow.staticData || null,
    meta: workflow.meta || {},
  };
}

function payloadForPush(workflow) {
  return {
    name: workflow.name,
    nodes: workflow.nodes || [],
    connections: workflow.connections || {},
    settings: workflow.settings || {},
    staticData: workflow.staticData || null,
  };
}

function readJsonFile(file) {
  const content = fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '');
  return JSON.parse(content);
}

function workflowFiles() {
  if (!fs.existsSync(WORKFLOWS_DIR)) return [];

  const files = fs
    .readdirSync(WORKFLOWS_DIR)
    .filter((file) => file.endsWith('.json'))
    .map((file) => path.join(WORKFLOWS_DIR, file));

  if (!TARGET) return files;

  const targetPath = path.resolve(REPO_ROOT, TARGET);
  return files.filter((file) => {
    if (path.resolve(file) === targetPath) return true;
    const data = readJsonFile(file);
    return data.name === TARGET || path.basename(file) === TARGET;
  });
}

async function remoteWorkflowMaps() {
  const response = await request('GET', `${N8N_API_URL}/workflows`);
  const workflows = response.data || [];
  const byId = new Map();
  const byName = new Map();

  for (const workflow of workflows) {
    byId.set(String(workflow.id), workflow);
    byName.set(workflow.name, workflow);
  }

  return { workflows, byId, byName };
}

async function list() {
  const { workflows } = await remoteWorkflowMaps();
  console.log(`Remote workflows at ${N8N_API_URL}:`);
  for (const workflow of workflows) {
    console.log(`- ${workflow.name} (${workflow.id}) active=${workflow.active}`);
  }
}

async function pull() {
  fs.mkdirSync(WORKFLOWS_DIR, { recursive: true });
  const { workflows } = await remoteWorkflowMaps();
  const selected = TARGET
    ? workflows.filter((workflow) => workflow.name === TARGET || String(workflow.id) === TARGET)
    : workflows;

  if (TARGET && selected.length === 0) {
    throw new Error(`No remote workflow matched "${TARGET}".`);
  }

  console.log(`Pulling ${selected.length} workflow(s) from ${N8N_API_URL}`);
  for (const workflow of selected) {
    const full = await request('GET', `${N8N_API_URL}/workflows/${workflow.id}`);
    const sanitized = sanitizeWorkflow(full);
    const file = path.join(WORKFLOWS_DIR, `${slugify(sanitized.name)}.json`);
    fs.writeFileSync(file, `${JSON.stringify(sanitized, null, 2)}\n`);
    console.log(`Saved ${path.relative(REPO_ROOT, file)}`);
  }
}

async function push() {
  fs.mkdirSync(WORKFLOWS_DIR, { recursive: true });
  const files = workflowFiles();
  if (files.length === 0) {
    throw new Error(TARGET ? `No local workflow matched "${TARGET}".` : 'No local workflow JSON files found.');
  }

  const { byId, byName } = await remoteWorkflowMaps();
  console.log(`Pushing ${files.length} workflow(s) to ${N8N_API_URL}`);

  for (const file of files) {
    const workflow = readJsonFile(file);
    const remote =
      (workflow.id && byId.get(String(workflow.id))) ||
      byName.get(workflow.name);
    const payload = payloadForPush(workflow);

    if (remote) {
      await request('PUT', `${N8N_API_URL}/workflows/${remote.id}`, payload);
      console.log(`Updated ${workflow.name} (${remote.id})`);
      continue;
    }

    const created = await request('POST', `${N8N_API_URL}/workflows`, payload);
    const sanitized = sanitizeWorkflow({ ...workflow, id: created.id, active: created.active });
    fs.writeFileSync(file, `${JSON.stringify(sanitized, null, 2)}\n`);
    console.log(`Created ${workflow.name} (${created.id}) and wrote id to ${path.relative(REPO_ROOT, file)}`);
  }
}

(async () => {
  try {
    if (ACTION === 'list') await list();
    if (ACTION === 'pull') await pull();
    if (ACTION === 'push') await push();
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
})();
