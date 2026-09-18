const fs = require('fs');

function load(path) { return JSON.parse(fs.readFileSync(path, 'utf8')); }
function save(path, workflow) { fs.writeFileSync(path, JSON.stringify(workflow, null, 2) + '\n'); }
function node(workflow, name) {
  const found = workflow.nodes.find(item => item.name === name);
  if (!found) throw new Error(`Missing node: ${name}`);
  return found;
}
function replaceOnce(source, oldText, newText, label) {
  if (source.includes(newText)) return source;
  if (!source.includes(oldText)) throw new Error(`Missing ${label}`);
  return source.replace(oldText, newText);
}

const intakePath = 'workflows/n8n/rag-intake-routing-phase-9.json';
const corePath = 'workflows/n8n/rag-core-execution-phase-8.json';
const intake = load(intakePath);
const core = load(corePath);
// n8n's read API returns binaryMode, but its write API rejects it.
delete core.settings.binaryMode;

node(intake, 'Set Intake Active Call').parameters.jsCode = `const state = { ...$json };
const trigger = String(state.trigger_source || 'discord_active');
const text = String(state.user_query || state.content || '').replace(/\\s+/g, ' ').trim();
const activeTrigger = trigger === 'discord_active' || trigger.startsWith('regression') || trigger === 'evaluator_manual';
const directInvocation = state.is_direct_mention === true || state.is_direct_mention === 'true' || state.is_active_call === true || state.is_active_call === 'true';
const excluded = Array.isArray(state.passive_excluded_channel_ids)
  ? state.passive_excluded_channel_ids.map(String)
  : String(state.passive_excluded_channel_ids || '').split(',').map(value => value.trim()).filter(Boolean);
const checks = [
  [state.is_duplicate === true || state.is_duplicate === 'true' || state.event_duplicate === true || (state.capture_candidate === true && state.capture_duplicate === true), 'duplicate_event'],
  [state.author_is_bot === true || state.author_is_bot === 'true' || state.is_webhook === true || state.is_webhook === 'true' || state.is_system_event === true || state.is_system_event === 'true', 'non_member_event'],
  [text.length === 0, 'empty_content'],
  [excluded.includes(String(state.channel_id || '')), 'excluded_channel'],
];
const rejected = checks.find(([matched]) => matched);
const passive = !activeTrigger && !directInvocation;
const ignored = Boolean(rejected) || (passive && state.passive_enabled !== true);
const reason = rejected ? rejected[1] : (ignored ? 'passive_disabled' : (passive ? 'awaiting_gemini_post_decision' : (activeTrigger ? 'active_trigger:' + trigger : 'direct_bot_invocation')));
return [{ json: {
  ...state,
  user_query: text,
  route_type: ignored ? 'ignored' : (passive ? 'passive_candidate' : 'active_call'),
  routing_reason: reason,
  passive_gate_level: ignored ? 'mechanical' : (passive ? 'gemini_pending' : 'bypass'),
  passive_gate_policy_version: 'passive-llm-post-v1',
  passive_intent_class: ignored ? 'not_evaluated' : (passive ? 'pending' : 'bypass'),
  passive_intent_confidence: 'not_applicable',
  passive_intent_evidence: [reason],
  should_run_rag: !ignored,
  trigger_source: passive && !ignored ? 'discord_passive' : state.trigger_source,
  run_mode: passive && !ignored ? 'full_answer' : state.run_mode,
  response_mode: passive && !ignored ? 'postgres_only' : state.response_mode,
  allow_gemini: passive && !ignored ? true : state.allow_gemini,
  allow_discord_post: passive && !ignored ? false : state.allow_discord_post,
  passive_mode: passive && !ignored ? 'shadow' : '',
}}];`;

const assemble = node(core, 'Assemble Context Contract');
assemble.parameters.jsCode = assemble.parameters.jsCode.replace(
  "  '0. PASSIVE POST DECISION: For a passive message, decide whether an unsolicited bot answer is useful. A request can be phrased without a question mark. Ordinary statements, replies, anecdotes, promotions, and messages directed only at another member are not requests. Referral and community-resource questions are valid requests, but do not disclose personal contact details. If uncertain, set should_post false. Decide intent before drafting an answer. If no useful grounded answer is available, set should_post false.',\n",
  ''
);
assemble.parameters.jsCode = replaceOnce(assemble.parameters.jsCode,
  "  '1. GROUNDING: Answer ONLY from the provided context blocks.",
  "  ...(state.trigger_source === 'discord_passive' ? ['0. PASSIVE POST DECISION: Decide if an unsolicited answer is useful before drafting. A request need not contain a question mark. Statements, anecdotes, promotions, and messages directed only at another member are not requests. Referral and community-resource questions are valid requests, but never disclose personal contact details. If uncertain or no grounded answer is possible, set should_post false.'] : []),\n  '1. GROUNDING: Answer ONLY from the provided context blocks.",
  'passive prompt rule');
assemble.parameters.jsCode = assemble.parameters.jsCode.replace(
  "const prompt = systemPrompt + '\\n\\nFor passive messages, return the structured post decision and leave final_answer empty when should_post is false.\\n\\nContext assembly notes:",
  "const prompt = systemPrompt + (state.trigger_source === 'discord_passive' ? '\\n\\nReturn the structured post decision and leave final_answer empty when should_post is false.' : '') + '\\n\\nContext assembly notes:"
);
assemble.parameters.jsCode = replaceOnce(assemble.parameters.jsCode,
  "const prompt = systemPrompt + '\\n\\nContext assembly notes:",
  "const prompt = systemPrompt + (state.trigger_source === 'discord_passive' ? '\\n\\nReturn the structured post decision and leave final_answer empty when should_post is false.' : '') + '\\n\\nContext assembly notes:",
  'passive prompt instruction');
assemble.parameters.jsCode = assemble.parameters.jsCode.replace(
  /  '7\. SAFETY: Do not surface personal identifying information from the context even if present\.[^\n]+/,
  "  '7. SAFETY: Never surface personal, contact, or identifying information from context. If an answer would require it, decline the answer. Referral-process guidance is allowed when grounded and does not identify or contact a member.',"
);

const contextCondition = node(core, 'Context Found?').parameters.conditions.conditions[0];
contextCondition.leftValue = "={{ $items('Assemble Context Contract')[0].json.should_generate_int === 1 || ($items('Assemble Context Contract')[0].json.trigger_source === 'discord_passive' && $items('Assemble Context Contract')[0].json.allow_gemini === true && $items('Assemble Context Contract')[0].json.run_mode === 'full_answer') ? 1 : 0 }}";

const gemini = node(core, 'Gemini Generation');
gemini.parameters.jsonBody = "={{ { contents: [{ role: 'user', parts: [{ text: $items('Prepare Gemini Request')[0].json.prompt }] }], generationConfig: { temperature: 0.1, maxOutputTokens: 5000, responseMimeType: 'application/json', responseJsonSchema: $items('Prepare Gemini Request')[0].json.trigger_source === 'discord_passive' ? { type: 'object', properties: { should_post: { type: 'boolean' }, intent: { type: 'string', enum: ['request', 'non_request', 'unclear'] }, decision_reason: { type: 'string' }, final_answer: { type: 'string' } }, required: ['should_post','intent','decision_reason','final_answer'], additionalProperties: false } : { type: 'object', properties: { final_answer: { type: 'string', description: 'The complete user-facing answer only. Do not include analysis, drafting notes, or metadata.' } }, required: ['final_answer'], additionalProperties: false } } } }}";

const result = node(core, 'Build Gemini Result');
result.parameters.jsCode = replaceOnce(result.parameters.jsCode,
  "let rawText = '';",
  "const passive = state.trigger_source === 'discord_passive';\nlet shouldPost = passive ? false : null;\nlet postIntent = passive ? 'unclear' : null;\nlet postDecisionReason = '';\nlet rawText = '';",
  'post decision defaults');
result.parameters.jsCode = replaceOnce(result.parameters.jsCode,
  "if (keys.length !== 1 || keys[0] !== 'final_answer' || typeof parsed.final_answer !== 'string' || !parsed.final_answer.trim()) throw new Error('invalid_final_answer_contract');\n    rawText = parsed.final_answer.trim();",
  "if (passive) {\n      const validKeys = ['should_post', 'intent', 'decision_reason', 'final_answer'];\n      if (keys.length !== 4 || !validKeys.every(key => keys.includes(key)) || typeof parsed.should_post !== 'boolean' || !['request', 'non_request', 'unclear'].includes(parsed.intent) || typeof parsed.decision_reason !== 'string' || typeof parsed.final_answer !== 'string') throw new Error('invalid_passive_post_contract');\n      shouldPost = parsed.should_post === true && parsed.intent === 'request' && state.should_generate === true;\n      postIntent = parsed.intent;\n      postDecisionReason = parsed.decision_reason.slice(0, 200);\n      if (shouldPost && !parsed.final_answer.trim()) throw new Error('empty_passive_answer');\n      rawText = shouldPost ? parsed.final_answer.trim() : '';\n    } else {\n      if (keys.length !== 1 || keys[0] !== 'final_answer' || typeof parsed.final_answer !== 'string' || !parsed.final_answer.trim()) throw new Error('invalid_final_answer_contract');\n      rawText = parsed.final_answer.trim();\n    }",
  'structured parser');
result.parameters.jsCode = replaceOnce(result.parameters.jsCode,
  "let generationFailed = statusCode < 200 || statusCode >= 300 || !rawText || outputIntegrityFailed;",
  "let generationFailed = statusCode < 200 || statusCode >= 300 || (!rawText && !passive) || outputIntegrityFailed;",
  'nonrequest validity');
result.parameters.jsCode = replaceOnce(result.parameters.jsCode,
  "const effectiveModelRefusal = isModelRefusal || failedCitationGuard;",
  "const effectiveModelRefusal = (passive && !shouldPost) || isModelRefusal || failedCitationGuard;",
  'passive refusal');
result.parameters.jsCode = replaceOnce(result.parameters.jsCode,
  "const failedCitationGuard = !generationFailed && !isModelRefusal && !hasCitation;",
  "const failedCitationGuard = !generationFailed && (!passive || shouldPost) && !isModelRefusal && !hasCitation;",
  'citation only for proposed answer');
result.parameters.jsCode = replaceOnce(result.parameters.jsCode,
  "      if (shouldPost && !parsed.final_answer.trim()) throw new Error('empty_passive_answer');",
  "      if ((parsed.should_post && parsed.intent !== 'request') || (!parsed.should_post && parsed.final_answer.trim()) || (parsed.should_post && (!parsed.final_answer.trim() || state.should_generate !== true))) throw new Error('contradictory_passive_contract');",
  'contradictory structured response');
result.parameters.jsCode = replaceOnce(result.parameters.jsCode,
  "const effectiveRefusalReason = isModelRefusal ? 'gemini_context_insufficient' : (failedCitationGuard ? 'gemini_uncited_answer' : null);",
  "const effectiveRefusalReason = passive && !shouldPost ? 'gemini_post_decision_rejected' : (isModelRefusal ? 'gemini_context_insufficient' : (failedCitationGuard ? 'gemini_uncited_answer' : null));",
  'passive refusal reason');
result.parameters.jsCode = replaceOnce(result.parameters.jsCode,
  "    gemini_response_text: rawText,",
  "    gemini_response_text: rawText,\n    should_post: passive ? shouldPost && !generationFailed && !effectiveModelRefusal : null,\n    post_intent: postIntent,\n    post_decision_reason: postDecisionReason,",
  'decision output');
result.parameters.jsCode = replaceOnce(result.parameters.jsCode,
  "    retrieval_status: generationFailed ? state.retrieval_status : (effectiveModelRefusal ? 'no_context' : 'context_found'),",
  "    retrieval_status: generationFailed || (passive && !shouldPost) ? state.retrieval_status : (effectiveModelRefusal ? 'no_context' : 'context_found'),",
  'preserve actual retrieval outcome');

const returnCore = node(core, 'Return RAG Core Result');
returnCore.parameters.jsCode = replaceOnce(returnCore.parameters.jsCode,
  "} else if (!discordText && finalStatus === 'refused') {",
  "} else if (!discordText && finalStatus === 'refused' && !(state.trigger_source === 'discord_passive' && state.should_post === false)) {",
  'no refusal text for non-request');

const safety = node(core, 'Apply Stage 0 Safety Gate');
safety.parameters.jsCode = replaceOnce(safety.parameters.jsCode,
  'const safetyRefusal = Boolean(safetyCategory);',
  "const safetyRefusal = Boolean(safetyCategory) && state.trigger_source !== 'discord_passive';",
  'passive safety decision belongs to Gemini');

node(intake, 'Should Post Discord?').parameters.conditions.conditions[0].leftValue = "={{ $json.allow_discord_post && !$json.output_integrity_failed && $json.should_post !== false }}";
const capture = node(intake, 'Capture Discord Message');
capture.parameters.query = replaceOnce(capture.parameters.query,
  'WITH inserted_message AS (',
  "WITH claimed_event AS (\n  INSERT INTO rag_intake_event_claims (guild_id, message_id)\n  SELECT '{{ String($json.discord_guild_id || '').replace(/'/g, \"''\") }}', '{{ String($json.discord_message_id || '').replace(/'/g, \"''\") }}'\n  WHERE {{ $json.capture_candidate && $json.discord_message_id ? 'TRUE' : 'FALSE' }}\n  ON CONFLICT (guild_id, message_id) DO NOTHING\n  RETURNING message_id\n), inserted_message AS (",
  'durable event claim');
capture.parameters.query = replaceOnce(capture.parameters.query,
  'SELECT\n  EXISTS(SELECT 1 FROM inserted_message) AS capture_inserted,',
  "SELECT\n  ({{ $json.capture_candidate && $json.discord_message_id ? 'TRUE' : 'FALSE' }} AND NOT EXISTS(SELECT 1 FROM claimed_event)) AS event_duplicate,\n  EXISTS(SELECT 1 FROM inserted_message) AS capture_inserted,",
  'durable duplicate result');
const restoreCapture = node(intake, 'Restore Intake After Capture');
restoreCapture.parameters.jsCode = replaceOnce(restoreCapture.parameters.jsCode,
  '  capture_duplicate: capture.capture_duplicate === true,',
  '  capture_duplicate: capture.capture_duplicate === true,\n  event_duplicate: capture.event_duplicate === true,',
  'carry durable duplicate result');
const finalize = node(intake, 'Finalize Posted Transaction');
finalize.parameters.query = replaceOnce(finalize.parameters.query,
  "'response_truncated', {{ $json.discord_response_truncated === true }}",
  "'response_truncated', {{ $json.discord_response_truncated === true }},\n      'should_post', {{ $json.should_post === true }},\n      'post_intent', '{{ String($json.post_intent || '').replace(/'/g, \"''\") }}',\n      'post_decision_reason', '{{ String($json.post_decision_reason || '').replace(/'/g, \"''\") }}'",
  'decision persistence');
finalize.parameters.query = replaceOnce(finalize.parameters.query,
  "WHEN '{{ String($json.run_mode || '').replace(/'/g, \"''\") }}' <> 'full_answer' OR '{{ String($json.gemini_response_text || '').replace(/'/g, \"''\") }}' = '' THEN '{}'::jsonb",
  "WHEN '{{ String($json.run_mode || '').replace(/'/g, \"''\") }}' <> 'full_answer' THEN '{}'::jsonb",
  'decision persistence for empty answer');

save(intakePath, intake);
save(corePath, core);
