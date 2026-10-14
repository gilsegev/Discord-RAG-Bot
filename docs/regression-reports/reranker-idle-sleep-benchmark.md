# Railway reranker idle sleep and cold-start benchmark

Observed on 2026-10-02 in Railway production. The local CrossEncoder remains the production reranker. The only runtime changes were enabling Railway Serverless sleep for `reranker` and adding three native attempts, five seconds apart, to the existing n8n CrossEncoder HTTP request. Its full-response and fail-closed behavior stayed intact. Discord posting and evaluation-label writes were disabled for every benchmark call.

## Deployment and sleep

Before the change, the reranker was RUNNING with `sleepApplication:false`. Deployment `d967958b-27e9-4708-b122-a454f717f83a` became healthy at 05:26:25 UTC with `sleepApplication:true` in both the service setting and deployed manifest. Railway reported `SLEEPING` at 05:39:38 UTC. A second observed transition was 05:54:42 UTC, and the status was again `SLEEPING` when checked at 06:01:15 UTC before the final cold call. The provider-reported timestamps imply about 13 minutes for the first cycle and about 11 minutes for the second. A strict five-minute sleep was not verified; the native timer varied in these observations.

| RAM observation | MB (Railway raw GB × 1,024) |
|---|---:|
| Previous always-running deployment, before the change | 1,248 MB |
| Fresh deployment while awake but idle, before its first cold request | 416 MB |
| New deployment after real requests, warm sample | about 747 MB |
| New deployment's highest captured cold-wake sample | about 839 MB |

Railway sampled memory every 30 seconds, so the actual cold peak may have been higher than 839 MB. The fresh idle and warm measurements are from different phases of the new deployment and should not be treated as a fixed steady-state footprint.

Railway's deployment state, rather than an HTTP health probe, established sleep before each cold test. The raw 30-second memory series continued to display its last nonzero value while the deployment was `SLEEPING`; this series did **not** measure zero resident RAM or a billing rate of zero. Railway's [Serverless cost guide](https://docs.railway.com/guides/cut-idle-costs-serverless) says slept services do not accrue compute charges. The model volume remains allocated and billable. Read-only usage snapshots were too aggregated to isolate a one-minute sleeping charge.

## Real shared-pipeline tests

All timed requests used one historical, answer-seeking DB question (`1663b20e-0ac8-4c76-826a-1b7c54745c88`) and the production shared intake/core route. The route performed embedding, Qdrant search, Stage 1 gating, CrossEncoder scoring of 20 candidates, dedupe/context assembly, and Gemini generation. The same question and production model endpoints were used each time. `response_mode:ci_artifact`, `allow_discord_post:false`, and `write_eval_labels:false` prevented outward posts and label changes.

| Run | Railway state before call | Rerank stage | Model scoring | Whole pipeline | Outcome |
|---|---|---:|---:|---:|---|
| Pre-fix cold, n8n `39029` | SLEEPING | 1.062 s | no response | 2.855 s | Transport `ECONNREFUSED`; controlled `reranker_failed` refusal. |
| Pre-fix immediate warm, `39031` | awake | 3.266 s | 3.207 s | 23.457 s | Context found; Gemini `MAX_TOKENS` output-integrity failure. |
| Post-fix cold 1, `39035` | SLEEPING | 14.333 s | 3.195 s | 34.999 s | 20 scored candidates, HTTP 200, context found; Gemini `MAX_TOKENS` failure. |
| Post-fix immediate warm, `39038` | awake | 3.145 s | 3.099 s | 23.726 s | Answered; 1,918-character generated answer, Gemini `STOP`; no Discord post. |
| Post-fix cold 2, `39041` | SLEEPING | 14.419 s | 3.272 s | 32.633 s | Answered; 1,974-character generated answer, Gemini `STOP`; no Discord post. |

The corrected cold rerank stage added about 11.2 seconds over the adjacent warm call. This includes wake and retry delay; model scoring itself remained about 3.1–3.3 seconds. Whole-pipeline timings include variable Gemini generation, so the rerank-stage comparison is the cleaner cold-start measure. The first cold failure was a connection refusal before the model received the request, not an HTTP 502. Native retries recovered both later cold calls without changing the rerank scores or context contract. The Gemini `MAX_TOKENS` failures are separate generation behavior and were not changed for this deployment.

One preliminary request was discarded because the private benchmark script selected the entire stored sample rather than a single case; the intake safety gate refused it before retrieval. The selector was corrected and checked to contain exactly one 58-character real DB question before the timed reranker calls above. No discarded call is included in the latency comparison.

## Test

1. Read Railway service status and memory without an HTTP probe; confirm `sleepApplication:true`, tracked deployment manifest true, and explicit `SLEEPING` before a cold test.
2. Send the same historical question through the active shared intake/core with Gemini enabled and Discord/evaluation writes disabled; inspect n8n CrossEncoder node execution and final DB transaction.
3. Repeat immediately while warm, then wait for another explicit `SLEEPING` state and make the same call again.

## Pass when

The service sleeps, both post-fix cold requests reach the CrossEncoder with HTTP 200 and 20 scored candidates, the warm call is faster at the rerank stage, and no Discord post occurs. These conditions passed. A future cold connection failure still fails closed after the bounded attempts; the behavior of an HTTP 502 under the retained `neverError:true` option was not observed and remains unverified.

## Self-review and scope

The implemented flow is:

```text
Railway sleep -> n8n shared intake -> embed/Qdrant
              -> CrossEncoder HTTP (up to 3 attempts)
              -> existing dedupe/context -> Gemini
              -> stored result; Discord disabled in benchmark
```

The diff changes no model code, scoring threshold, candidate order, or other workflow node. The request is inference-only, so a bounded retry does not duplicate writes at the reranker. Live n8n nodes/connections were compared with the tracked workflow before patching; the live update changed only the CrossEncoder node and preserved n8n's remote binary setting. The implementation stayed within the requested production sleep and narrow retry scope. Limitations: strict five-minute sleep was not observed, the first cold transport failure required the retry fix, a possible HTTP 502 remains unverified, Gemini had separate `MAX_TOKENS` failures, and provider memory graphs retained a nonzero value during sleep. No broad Phase 9 suite or synthetic quality test was run.
