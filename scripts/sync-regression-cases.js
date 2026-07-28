const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const repoRoot = path.resolve(__dirname, "..");
const casesPath = path.join(repoRoot, "scripts", "regression_questions.jsonl");
const workflowPath = path.join(
  repoRoot,
  "workflows",
  "n8n",
  "rag-regression-batch-runner-phase-8.json",
);
const checkOnly = process.argv.includes("--check");

const fileContent = fs.readFileSync(casesPath, "utf8");
const cases = fileContent
  .split(/\r?\n/)
  .map((line) => line.trim())
  .filter(Boolean)
  .map((line, index) => {
    try {
      return JSON.parse(line);
    } catch (error) {
      throw new Error(
        `Invalid JSON on ${path.relative(repoRoot, casesPath)} line ${index + 1}: ${error.message}`,
      );
    }
  });

const ids = cases.map((item) => item.id);
if (new Set(ids).size !== ids.length) {
  throw new Error("Regression case IDs must be unique.");
}

const workflow = JSON.parse(fs.readFileSync(workflowPath, "utf8"));
const loader = workflow.nodes.find((node) => node.name === "Load Regression Cases");
if (!loader?.parameters?.jsCode) {
  throw new Error("Could not find the Load Regression Cases code node.");
}

const startMarker = "const allCases = ";
const endMarker = ";\nconst questionFile";
const start = loader.parameters.jsCode.indexOf(startMarker);
const end = loader.parameters.jsCode.indexOf(endMarker, start);
if (start < 0 || end < 0) {
  throw new Error("Could not locate the embedded allCases array.");
}

const normalizedFileContent = fileContent.replace(/\r\n/g, "\n");
const canonicalContent = normalizedFileContent.endsWith("\n")
  ? normalizedFileContent
  : `${normalizedFileContent}\n`;
const questionFileHash = crypto
  .createHash("sha256")
  .update(canonicalContent)
  .digest("hex");
const serializedCases = JSON.stringify(cases, null, 2);
let nextCode =
  loader.parameters.jsCode.slice(0, start) +
  startMarker +
  serializedCases +
  loader.parameters.jsCode.slice(end);
nextCode = nextCode.replace(
  /const questionFileHash = '[a-f0-9]+';/,
  `const questionFileHash = '${questionFileHash}';`,
);

if (nextCode === loader.parameters.jsCode) {
  console.log(`Regression runner matches ${cases.length} canonical cases.`);
  process.exit(0);
}

if (checkOnly) {
  console.error(
    `Regression runner is out of sync with ${cases.length} canonical cases.`,
  );
  process.exit(1);
}

loader.parameters.jsCode = nextCode;
fs.writeFileSync(workflowPath, `${JSON.stringify(workflow, null, 2)}\n`);
console.log(`Synced ${cases.length} canonical cases into the regression runner.`);
