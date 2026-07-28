#!/usr/bin/env node

const fs = require('node:fs');

const [url, outputFile, cases = 'all'] = process.argv.slice(2);
if (!url || !outputFile) {
  console.error('Usage: run-railway-regression.js URL OUTPUT_FILE [CASES]');
  process.exit(2);
}

const statusFile = `${outputFile}.status.json`;
const expectedTotalCases = Number(process.env.EXPECTED_TOTAL_CASES || (cases === 'all' ? 48 : 0));
const minimumPassCount = Number(process.env.MIN_PASS_COUNT || (cases === 'all' ? 43 : 0));
const maximumFailCount = Number(process.env.MAX_FAIL_COUNT || (cases === 'all' ? 1 : 0));
const maximumReviewCount = Number(process.env.MAX_REVIEW_COUNT || (cases === 'all' ? 4 : 0));

async function main() {
  const headers = { 'content-type': 'application/json' };
  if (process.env.N8N_WEBHOOK_SHARED_SECRET) {
    headers['x-rag-webhook-secret'] = process.env.N8N_WEBHOOK_SHARED_SECRET;
  }
  const response = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      cases,
      mode: 'retrieval_only',
      allow_gemini: false,
      allow_discord_post: false,
      write_eval_labels: false,
      requested_by: 'railway-migration',
    }),
  });

  if (!response.ok) {
    throw new Error(`Regression HTTP ${response.status}: ${await response.text()}`);
  }

  const report = await response.json();
  const qualityChecks = {
    total_cases:
      expectedTotalCases === 0 || Number(report.total_cases) === expectedTotalCases,
    pass_count: Number(report.pass_count) >= minimumPassCount,
    fail_count: Number(report.fail_count) <= maximumFailCount,
    review_count: Number(report.review_count) <= maximumReviewCount,
  };
  const qualityOk = Object.values(qualityChecks).every(Boolean);

  fs.writeFileSync(outputFile, `${JSON.stringify(report, null, 2)}\n`);
  fs.writeFileSync(
    statusFile,
    `${JSON.stringify({
      transport_ok: true,
      quality_ok: qualityOk,
      quality_checks: qualityChecks,
      thresholds: {
        expected_total_cases: expectedTotalCases,
        minimum_pass_count: minimumPassCount,
        maximum_fail_count: maximumFailCount,
        maximum_review_count: maximumReviewCount,
      },
      run_id: report.run_id,
      total_cases: report.total_cases,
      pass_count: report.pass_count,
      fail_count: report.fail_count,
      review_count: report.review_count,
    }, null, 2)}\n`,
  );
  if (!qualityOk) {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  fs.writeFileSync(
    statusFile,
    `${JSON.stringify({
      transport_ok: false,
      quality_ok: false,
      error: error.message,
    }, null, 2)}\n`,
  );
  process.exitCode = 1;
});
