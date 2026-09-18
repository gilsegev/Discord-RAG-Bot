const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const core = JSON.parse(fs.readFileSync('workflows/n8n/rag-core-execution-phase-8.json', 'utf8'));
const intake = JSON.parse(fs.readFileSync('workflows/n8n/rag-intake-routing-phase-9.json', 'utf8'));
const node = (workflow, name) => workflow.nodes.find(item => item.name === name);
const code = (workflow, name) => node(workflow, name).parameters.jsCode;
const condition = (workflow, name) => node(workflow, name).parameters.conditions.conditions[0].leftValue;

// Local gate fixtures reproduce the issue's 20-result/zero-selected shape.
// They do not replay the production corpus or call Gemini.

function evaluate(expression, state, dependency) {
  const source = expression.slice(3, -2);
  return vm.runInNewContext(source, {
    $json: state,
    $items: name => {
      assert.strictEqual(name, dependency);
      return [{ json: state }];
    },
  });
}

function runCode(source, json, dependencies = {}) {
  return vm.runInNewContext(`(function(){${source}})()`, {
    $json: json,
    $items: name => {
      assert(Object.hasOwn(dependencies, name), `Unexpected dependency: ${name}`);
      return [{ json: dependencies[name] }];
    },
  })[0].json;
}

function replay(message, passingCount) {
  const route = runCode(code(intake, 'Set Intake Active Call'), {
    trigger_source: 'discord_passive',
    user_query: message,
    channel_id: 'general',
    passive_enabled: true,
    passive_excluded_channel_ids: [],
  });
  assert.strictEqual(route.route_type, 'passive_candidate');

  // The September 18 no-post replay recorded 20 raw results for each issue case.
  const candidates = Array.from({ length: 20 }, (_, index) => ({
    qdrant_point_id: String(index + 1),
    rank: index + 1,
    retrieval_score: 0.8 - index / 100,
    channel_id: '123',
    channel_name: 'general',
    first_message_id: String(1000 + index),
    message_ids: [String(1000 + index)],
    text: `Community discussion ${index + 1}`,
    payload: { authors: ['member'] },
  }));
  const base = {
    ...route,
    trigger_source: 'discord_passive',
    user_query: message,
    run_mode: 'full_answer',
    allow_gemini: true,
    allow_discord_post: false,
    candidates,
    passed_stage_1_candidates: candidates,
    context_k: 5,
    context_token_budget: 2200,
    reranker_started_ms: Date.now(),
  };
  const response = {
    statusCode: 200,
    body: {
      results: candidates.map((candidate, index) => ({
        id: candidate.qdrant_point_id,
        reranker_score: index < passingCount ? 3 - index / 10 : -1 - index / 10,
      })),
    },
  };
  const ranked = runCode(code(core, 'Build Dedupe And Context Decision'), response, {
    'Prepare Reranker Request': base,
  });
  const context = runCode(code(core, 'Assemble Context Contract'), ranked, {
    'Build Dedupe And Context Decision': ranked,
  });
  const contextFound = evaluate(condition(core, 'Context Found?'), context, 'Assemble Context Contract');
  const result = runCode(code(core, 'Return RAG Core Result'), context, {
    'Build Gemini Result': undefined,
    'Assemble Context Contract': context,
  });
  return { context, contextFound, result };
}

const examples = [
  { name: 'Cursor Enterprise', message: 'Anyone here have a good contact at cursor? I want to start a pilot of cursor enterprise', passingCount: 0 },
  { name: 'C-level interview', message: 'Anyone here have recommendations for interviews with c-level? Have a final interview coming up with an exec that recently joined the company. Will be supporting their org. Looking for tips / suggestions.', passingCount: 0 },
  { name: 'Netflix referral', message: 'Hello, can anyone help with a referral at Netflix?', passingCount: 5 },
  { name: 'Think-Cell statement', message: 'We use a PPT plug in called Think-Cell. Super user friendly drag-drop but every gantt chart is independent so really annoying to manage. Tried MS project but quite clunky like the other MS products.', passingCount: 1 },
  { name: 'Zoom follow-up', message: 'The same link should work now. Fridays 12pm', passingCount: 4 },
  { name: 'Yesterday layoffs question', message: 'Did netflic have layoff? Saw a few “my last day” posts', passingCount: 1 },
  { name: 'Yesterday Atlassian question', message: 'Anyone here from Atlassian?', passingCount: 5 },
  { name: 'Yesterday Apple referral', message: 'Can someone please help me with a referral?', passingCount: 5 },
  { name: 'Yesterday Apple follow-up', message: 'Want to be a little more specific? lol', passingCount: 0 },
];

for (const example of examples) {
  const { context, contextFound, result } = replay(example.message, example.passingCount);
  const expected = example.passingCount > 0;
  assert.strictEqual(Boolean(contextFound), expected, example.name);
  assert.strictEqual(context.selected_context_count, example.passingCount, example.name);
  assert.strictEqual(context.should_generate, expected, example.name);
  if (!expected) {
    assert.strictEqual(result.final_status, 'refused', example.name);
    assert.strictEqual(result.refusal_reason, 'reranker_no_passing_candidates', example.name);
    assert.strictEqual(result.should_post, false, example.name);
    assert.strictEqual(result.discord_response_text, '', example.name);
    assert.strictEqual(result.gemini_expected, false, example.name);
    assert.strictEqual(result.gemini_ran, false, example.name);
  } else {
    assert.strictEqual(result.gemini_expected, true, example.name);
    assert.strictEqual(result.gemini_ran, false, example.name);
  }
  assert.strictEqual(result.response_status, 'not_posted', example.name);
  console.log(`${example.name}: ${expected ? 'Gemini eligible' : 'refused before Gemini'}; no Discord post`);
}

const postCondition = condition(intake, 'Should Post Discord?');
assert.strictEqual(evaluate(postCondition, {
  allow_discord_post: true, trigger_source: 'discord_passive', should_post: undefined,
}, null), false);
assert.strictEqual(evaluate(postCondition, {
  allow_discord_post: true, trigger_source: 'discord_passive', should_post: false,
}, null), false);
assert.strictEqual(evaluate(postCondition, {
  allow_discord_post: true, trigger_source: 'discord_passive', should_post: true,
}, null), true);
assert.strictEqual(evaluate(postCondition, {
  allow_discord_post: true, trigger_source: 'discord_active',
}, null), true);

const activeNoContext = { should_generate_int: 0, trigger_source: 'discord_active' };
assert.strictEqual(evaluate(condition(core, 'Context Found?'), activeNoContext, 'Assemble Context Contract'), 0);
const activeContext = {
  ...activeNoContext,
  should_generate: false,
  final_status: 'refused',
  retrieval_status: 'no_context',
  refusal_reason: 'reranker_no_passing_candidates',
  refusal_text: 'No context.',
};
const activeResult = runCode(code(core, 'Return RAG Core Result'), activeContext, {
  'Build Gemini Result': undefined,
  'Assemble Context Contract': activeContext,
});
assert.strictEqual(activeResult.final_status, 'refused');
assert.strictEqual(activeResult.discord_response_text, 'No context.');
assert.strictEqual(activeResult.should_post, undefined);
console.log('Active no-context and passive dispatch guards: passed');
