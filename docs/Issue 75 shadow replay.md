# Issue 75 no-post shadow replay

**Date:** 2026-09-18 UTC. **Deployment:** none; the proposed workflow change stayed in an isolated checkout.

## Evidence

Four Discord questions created on September 17 in the America/Chicago timezone were replayed through the shared intake and retrieval path with `allow_discord_post=false` and capture disabled. A second passive no-post replay recorded the current Gemini decision. The five examples from issue 75 were inspected from their earlier no-post transactions. Every observed run had 20 raw retrieval results and `response_status=not_posted`.

| Message | Selected chunks | Current passive decision | Proposed gate |
| --- | ---: | --- | --- |
| Yesterday: Netflix layoff question | 1 | Answer; `should_post=true` | Continue to Gemini |
| Yesterday: Atlassian question | 5 | Refuse; `should_post=false` | Continue to Gemini |
| Yesterday: Apple referral request | 5 | Answer; `should_post=true` | Continue to Gemini |
| Yesterday: Apple follow-up | 0 | Gemini declined after 974 tokens | Refuse before Gemini |
| Issue 75: Cursor Enterprise contact | 0 | Gemini declined after 1,421 tokens | Refuse before Gemini |
| Issue 75: C-level interview | 0 | Gemini declined after 1,318 tokens | Refuse before Gemini |
| Issue 75: Netflix referral | 5 | Answer; `should_post=true` | Continue to Gemini |
| Issue 75: Think-Cell statement | 1 | Refuse; `should_post=false` | Continue to Gemini |
| Issue 75: Zoom follow-up | 4 | Refuse; `should_post=false` | Continue to Gemini |

The yesterday passive transaction IDs are `82b4a11b-7793-4566-a246-544452b52233`, `9524cb7c-f8b5-4226-87fb-687cfd8d650d`, `7e9e6866-2370-47f1-8660-6724e6e3b9b5`, and `131e7371-9eca-4ac9-a321-c5126ccd4b0d`, in table order. The five issue transactions are `e4163138-818f-400d-83f4-f2612e31daf7`, `0e573193-64ff-4eb4-9d58-51676ce58483`, `c32792fb-2010-4001-9ec0-912eeae95b0a`, `97bfd9cb-ece9-490d-aa6c-b8bc74abcb90`, and `1c076f57-c196-42a3-8a85-a6768ada9f0d`, in table order.

## What this verifies

The recorded retrieval results supply both sides of the gate: three zero-context cases and six context-bearing cases. The local gate test uses the recorded messages and selected-chunk counts to execute the proposed n8n gate and final-result code; zero-context cases become explicit retrieval refusals with `should_post=false`, while context-bearing cases remain Gemini-eligible. The active no-context negative case and passive Discord dispatch guard also pass.

The patched workflow has not been installed in n8n. The replay therefore verifies the decision against real recorded retrieval outcomes, while the local test verifies the changed workflow code. It does not constitute an end-to-end execution of the patched workflow in a deployed shadow environment. A short no-post canary after workflow sync should confirm the persisted result before enabling Discord posting.
