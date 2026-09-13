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

function runCodeNode(name, input, priorItems = {}) {
  const code = node(name).parameters.jsCode;
  const result = vm.runInNewContext(`(function () { ${code} })()`, {
    $json: input,
    $items(requestedName) {
      assert(priorItems[requestedName], `Unexpected or missing prior item: ${requestedName}`);
      return [{ json: priorItems[requestedName] }];
    },
  });
  return result[0].json;
}

function route(input = {}) {
  return runCodeNode("Set Intake Active Call", {
    trigger_source: "discord_passive",
    user_query: "",
    channel_id: "test-channel",
    passive_enabled: true,
    passive_min_words: 3,
    passive_excluded_channel_ids: [],
    ...input,
  });
}

function assertTelemetry(result, level, intentClass) {
  assert.strictEqual(result.passive_gate_level, level);
  assert.strictEqual(result.passive_intent_class, intentClass);
  assert.match(result.passive_gate_policy_version, /^\S+$/);
  assert(["high", "medium", "low"].includes(result.passive_intent_confidence));
  assert(Array.isArray(result.passive_intent_evidence));
  assert(result.passive_intent_evidence.length > 0);
  assert.match(result.routing_reason, /^\S+$/);
}

function assertIgnored(input, intentClass, reason) {
  const first = route(input);
  const second = route(input);
  assert.strictEqual(first.route_type, "ignored");
  assert.strictEqual(first.should_run_rag, false);
  assert.strictEqual(first.routing_reason, second.routing_reason);
  assert.strictEqual(first.routing_reason, reason);
  assertTelemetry(first, "level_1", intentClass);
  return first;
}

function assertAdmitted(input) {
  const result = route(input);
  assert.strictEqual(result.route_type, "passive_candidate");
  assert.strictEqual(result.should_run_rag, true);
  assertTelemetry(result, "level_2", "knowledge_request");
  assert.strictEqual(result.trigger_source, "discord_passive");
  assert.strictEqual(result.run_mode, "full_answer");
  assert.strictEqual(result.response_mode, "postgres_only");
  assert.strictEqual(result.allow_gemini, true);
  assert.strictEqual(result.allow_discord_post, false);
  assert.strictEqual(result.passive_mode, "shadow");
  return result;
}

function assertLevel3Ignored(input, reason = "passive_not_knowledge_request") {
  const result = route(input);
  assert.strictEqual(result.route_type, "ignored");
  assert.strictEqual(result.should_run_rag, false);
  assert.strictEqual(result.routing_reason, reason);
  assertTelemetry(result, "level_3", "ambiguous");
  return result;
}

// Sanitized production-derived false positives: normal Discord conversation
// must not enter embedding, retrieval, reranking, or generation.
assertIgnored({ user_query: "Thanks, I will take a look." }, "conversation", "passive_conversation_statement");
assertIgnored({ user_query: "The same link should work now. Friday at noon." }, "coordination", "passive_coordination");
assertIgnored({ user_query: "Please include me in the next session." }, "coordination", "passive_coordination");
assertIgnored({ user_query: "We are hiring a technical program manager. Message me for details." }, "job_or_referral", "passive_job_or_referral");
assertIgnored({ user_query: "I am offering paid interview preparation; complete this interest survey." }, "promotion", "passive_promotion");
assertIgnored({ user_query: "I have not used that tool before, but I will look into it." }, "conversation", "passive_conversation_statement");

// Coordination remains a Level 1 exclusion even when written as a well-formed
// question; these messages need a live reply from a member, not community RAG.
assertIgnored({ user_query: "Can anyone check whether Zoom is working?" }, "coordination", "passive_coordination");
assertIgnored({ user_query: "Where is the Zoom link?" }, "coordination", "passive_coordination");
assertIgnored({ user_query: "Has the mock interview been rescheduled?" }, "coordination", "passive_coordination");
assertIgnored({ user_query: "Can I attend the weekly session?" }, "coordination", "passive_coordination");
assertIgnored({ user_query: "Where do I sign up for the next session?" }, "coordination", "passive_coordination");

// Current hiring, rhetorical, and reported questions are not historical
// knowledge requests. The wording is sanitized from the replayed traffic.
assertIgnored({ user_query: "Is anyone hiring?" }, "job_or_referral", "passive_job_or_referral");
assertIgnored({ user_query: "Who cares?" }, "conversation", "passive_rhetorical_question");
assertIgnored({ user_query: "What is this?" }, "conversation", "passive_rhetorical_question");
assertIgnored({ user_query: "Why is this so hard?" }, "conversation", "passive_rhetorical_question");
assertIgnored({ user_query: "When are the next layoffs?" }, "conversation", "passive_rhetorical_question");
assertIgnored({ user_query: "Who cares about this survey?" }, "conversation", "passive_rhetorical_question");
assertIgnored({ user_query: "The interviewer asked, 'How was the interview process?'" }, "conversation", "passive_reported_question");

// Question syntax alone is deliberately insufficient in passive mode. These
// remaining replay categories need another member's live response or are not
// requests that the historical corpus can answer.
assertIgnored({ user_query: "Can you clarify what you mean by scope?" }, "conversation", "passive_direct_clarification");
assertLevel3Ignored({ user_query: "Wondering if this is useful?" });
assertIgnored({ user_query: "What is the latest status of the role?" }, "coordination", "passive_current_or_live_request");
assertIgnored({ user_query: "A member suggested contacting a recruiter. Does that help?" }, "conversation", "passive_reported_advice_or_status");
assertIgnored({ user_query: "What are the topics for the next event?" }, "coordination", "passive_event_topic");
assertIgnored({ user_query: "Who should I contact about this opening?" }, "job_or_referral", "passive_job_or_referral");
assertLevel3Ignored({ user_query: "I am thinking about building an interview tracker. What do people think?" });
assertLevel3Ignored({ user_query: "They often have insight on new roles, so this may be useful." });
assertIgnored({ user_query: "Has anyone contacts replay.Delay or interviewed with them?" }, "job_or_referral", "passive_job_or_referral");

// Level 2 must preserve genuine, corpus-answerable information requests with
// and without a question mark.
assertAdmitted({ user_query: "How have people prepared for a senior technical program manager interview?" });
assertAdmitted({ user_query: "Looking for advice on preparing for a senior technical program manager interview" });
assertAdmitted({ user_query: "Would appreciate recommendations for system design interview practice" });
assertAdmitted({ user_query: "How was the interview process at Meta" });
assertAdmitted({ user_query: "What experiences have members shared about Amazon interviews" });
assertAdmitted({ user_query: "How have members approached referral requests at Amazon?" });
assertAdmitted({ user_query: "Any insights on contract hiring patterns?" });

// Question punctuation is neither required nor sufficient. Coordination and
// rhetorical/unclear questions fail closed at Level 1 or Level 3.
assertIgnored({ user_query: "Does Friday at noon still work?" }, "coordination", "passive_coordination");
assertLevel3Ignored({ user_query: "Interesting approach to this." });

// Duplicate protection precedes every bypass and remains a stable ignored
// outcome even for an explicit mention.
const duplicate = route({
  trigger_source: "discord_active",
  is_direct_mention: true,
  user_query: "How should I prepare?",
  capture_candidate: true,
  capture_eligible: true,
  capture_duplicate: true,
});
assert.strictEqual(duplicate.route_type, "ignored");
assert.strictEqual(duplicate.routing_reason, "duplicate_event");
assert.strictEqual(duplicate.should_run_rag, false);
assertTelemetry(duplicate, "level_1", "ambiguous");

// Active triggers, direct mentions, and regression traffic bypass passive
// intent policy and retain their caller-facing execution policy.
for (const { expectedReason, ...input } of [
  {
    expectedReason: "active_trigger:discord_active",
    trigger_source: "discord_active",
    user_query: "Thanks, I will take a look.",
    run_mode: "full_answer",
    response_mode: "discord_test",
    allow_gemini: true,
    allow_discord_post: true,
  },
  {
    expectedReason: "direct_bot_invocation",
    is_direct_mention: true,
    user_query: "Please include me in the next session.",
    run_mode: "full_answer",
    response_mode: "discord_test",
    allow_gemini: true,
    allow_discord_post: true,
  },
  {
    expectedReason: "active_trigger:regression_manual",
    trigger_source: "regression_manual",
    user_query: "We are hiring a technical program manager.",
    run_mode: "retrieval_only",
    response_mode: "ci_artifact",
    allow_gemini: false,
    allow_discord_post: false,
  },
]) {
  const bypass = route(input);
  assert.strictEqual(bypass.route_type, "active_call");
  assert.strictEqual(bypass.should_run_rag, true);
  assert.strictEqual(bypass.routing_reason, expectedReason);
  assertTelemetry(bypass, "bypass", "ambiguous");
  assert.strictEqual(bypass.run_mode, input.run_mode);
  assert.strictEqual(bypass.response_mode, input.response_mode);
  assert.strictEqual(bypass.allow_gemini, input.allow_gemini);
  assert.strictEqual(bypass.allow_discord_post, input.allow_discord_post);
  assert.strictEqual(bypass.passive_mode, "");
}

// Routing telemetry writes all gate fields on both terminal branches.
for (const name of ["Log Routed Event", "Log Ignored Event"]) {
  const query = node(name).parameters.query;
  for (const field of [
    "passive_gate_level",
    "passive_gate_policy_version",
    "passive_intent_class",
    "passive_intent_confidence",
    "passive_intent_evidence",
  ]) {
    assert.match(query, new RegExp(`'${field}'`));
  }
  assert.match(query, /'reason'/);
}

// The terminal ignored result is a review surface as well as a response. It
// must preserve the complete decision contract rather than only the reason.
const ignoredRoute = assertLevel3Ignored({ user_query: "Interesting approach to this." });
const ignoredTerminal = runCodeNode(
  "Return Ignored Result",
  {},
  {
    "Create Transaction": { transaction_id: "ignored-transaction", user_query: ignoredRoute.user_query },
    "Set Intake Active Call": ignoredRoute,
  },
);
for (const field of [
  "passive_gate_level",
  "passive_gate_policy_version",
  "passive_intent_class",
  "passive_intent_confidence",
  "passive_intent_evidence",
]) {
  assert.deepStrictEqual(ignoredTerminal[field], ignoredRoute[field], `terminal result lost ${field}`);
}

// Admitted gate telemetry must survive the shared-core request and be mapped
// into the core invocation, where later traces and review tooling can use it.
const admittedRoute = assertAdmitted({ user_query: "Looking for advice on system design interview practice" });
const coreRequest = runCodeNode(
  "Build RAG Core Request",
  {},
  {
    "Create Transaction": {
      transaction_id: "admitted-transaction",
      user_query: admittedRoute.user_query,
      channel_id: "test-channel",
    },
    "Set Intake Active Call": admittedRoute,
  },
);
for (const field of [
  "passive_gate_level",
  "passive_gate_policy_version",
  "passive_intent_class",
  "passive_intent_confidence",
  "passive_intent_evidence",
]) {
  assert.deepStrictEqual(coreRequest[field], admittedRoute[field], `shared-core request lost ${field}`);
  assert.strictEqual(
    node("Execute RAG Core").parameters.workflowInputs.value[field],
    `={{ $('Build RAG Core Request').item.json.${field} }}`,
    `shared-core invocation does not map ${field}`,
  );
}

console.log("passive intent gate checks passed");
