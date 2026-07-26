const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const workflowPath = "workflows/n8n/rag-intake-routing-phase-9.json";
const workflow = JSON.parse(fs.readFileSync(workflowPath, "utf8"));

function node(name) {
  const found = workflow.nodes.find((candidate) => candidate.name === name);
  assert(found, `Missing workflow node: ${name}`);
  return found;
}

function runCodeNode(name, input) {
  const code = node(name).parameters.jsCode;
  const context = {
    $json: input,
    $items(requestedName) {
      assert.strictEqual(requestedName, "Normalize Intake");
      return [{ json: input }];
    },
  };
  return vm.runInNewContext(`(function () { ${code} })()`, context)[0].json;
}

function nextNode(name) {
  return workflow.connections[name].main[0][0].node;
}

assert.strictEqual(nextNode("Normalize Intake"), "Capture Discord Message");
assert.strictEqual(nextNode("Capture Discord Message"), "Restore Intake After Capture");
assert.strictEqual(nextNode("Restore Intake After Capture"), "Set Intake Active Call");

const captureQuery = node("Capture Discord Message").parameters.query;
assert.match(captureQuery, /INSERT INTO rag_discord_messages/);
assert.match(captureQuery, /ON CONFLICT \(message_id\) DO NOTHING/);
assert.match(captureQuery, /INSERT INTO rag_pending_chunk_work/);
assert.match(captureQuery, /FROM inserted_message/);
assert.match(captureQuery, /capture_duplicate/);

const migration = fs.readFileSync(
  "deploy/phase0/sql/07-phase9c1-incremental-capture-migration.sql",
  "utf8",
);
assert.match(migration, /CREATE TABLE IF NOT EXISTS rag_discord_messages/);
assert.match(migration, /message_id TEXT PRIMARY KEY/);
assert.match(migration, /capture_sequence BIGSERIAL NOT NULL UNIQUE/);
assert.match(migration, /CREATE TABLE IF NOT EXISTS rag_pending_chunk_work/);
assert.match(migration, /source_message_id TEXT NOT NULL UNIQUE/);
assert.match(migration, /REFERENCES rag_discord_messages\(message_id\)/);

const eligible = runCodeNode("Normalize Intake", {
  capture_candidate: true,
  trigger_source: "discord_passive",
  discord_message_id: "100",
  guild_id: "853099205206999050",
  channel_id: "200",
  channel_name: "general",
  parent_channel_id: "200",
  parent_channel_name: "general",
  message_created_at: "2026-07-25T12:00:00Z",
  message_type: "default",
  author_id_hash: "hash",
  author_display_name: "O'Brien",
  user_query: "See <@123> at https://example.com",
  author_is_bot: false,
  is_webhook: false,
  is_system_event: false,
  has_attachments: false,
});
assert.strictEqual(eligible.capture_eligible, true);
assert.strictEqual(eligible.capture_reason, "eligible");
assert.strictEqual(
  eligible.capture_normalized_content,
  "See [mention] at [link]",
);
assert.strictEqual(eligible.normalizer_version, "discord-export-compatible-v1");

const regression = runCodeNode("Normalize Intake", {
  trigger_source: "regression_manual",
  discord_message_id: "regression-1",
  user_query: "A regression question?",
});
assert.strictEqual(regression.capture_eligible, false);
assert.strictEqual(regression.capture_reason, "not_discord_capture_candidate");

const botMessage = runCodeNode("Normalize Intake", {
  ...eligible,
  capture_candidate: true,
  author_is_bot: true,
});
assert.strictEqual(botMessage.capture_eligible, false);
assert.strictEqual(botMessage.capture_reason, "bot_authored");

const missingGuild = runCodeNode("Normalize Intake", {
  ...eligible,
  capture_candidate: true,
  guild_id: undefined,
  discord_guild_id: undefined,
});
assert.strictEqual(missingGuild.capture_eligible, false);
assert.strictEqual(missingGuild.capture_reason, "missing_guild_id");

const wrongGuild = runCodeNode("Normalize Intake", {
  ...eligible,
  capture_candidate: true,
  guild_id: "999",
  discord_guild_id: undefined,
});
assert.strictEqual(wrongGuild.capture_eligible, false);
assert.strictEqual(wrongGuild.capture_reason, "guild_not_allowed");

const invalidTimestamp = runCodeNode("Normalize Intake", {
  ...eligible,
  capture_candidate: true,
  guild_id: "853099205206999050",
  discord_guild_id: undefined,
  message_created_at: "not-a-timestamp",
});
assert.strictEqual(invalidTimestamp.capture_eligible, false);
assert.strictEqual(invalidTimestamp.capture_reason, "invalid_message_created_at");

const duplicateActive = runCodeNode("Set Intake Active Call", {
  ...eligible,
  capture_candidate: true,
  capture_eligible: true,
  capture_duplicate: true,
  trigger_source: "discord_active",
  is_direct_mention: true,
});
assert.strictEqual(duplicateActive.route_type, "ignored");
assert.strictEqual(duplicateActive.routing_reason, "duplicate_event");
assert.strictEqual(duplicateActive.should_run_rag, false);

console.log("phase9c1 capture workflow checks passed");
