# Railway cost of replacing the local reranker with Jev

Measured on October 1, 2026 (America/Chicago) from the authenticated Railway CLI for the `Discord-RAG-Bot` production project and its workspace. Dollar amounts are USD. **The completed end-to-end experiment does not support switching now:** Jev produced two material constraint/intent answer failures. The figures below are conditional costs **if a later Jev design qualifies and the local reranker is actually retired**; the current Railway bill continues while the service remains running.

## Actual billing and usage

Railway bills this workspace on a cycle from the 10th of one month to the 10th of the next. The October 1 usage-alert email's `$50.06` is workspace resource usage accumulated since September 10, not October calendar-month spending or the bot project's cost. The CLI showed `$50.12` when checked later, with Railway's current-cycle forecast at `$66.02`.

| Billing period / scope | Resource usage | Reranker resource usage | Usage without reranker |
| --- | ---: | ---: | ---: |
| Aug 10–Sep 10, completed workspace | $86.22 | $36.77 | $49.45 |
| Aug 10–Sep 10, bot project | $71.01 | $36.77 | $34.24 |
| Sep 10–Oct 10, workspace to date | $50.12 | $14.15 | $35.97 |
| Sep 10–Oct 10, bot project to date | $39.80 | $14.15 | $25.65 |

The previous period's **paid invoice was $84.09**, whereas its recorded resource usage was `$86.22`. The available invoice metadata does not expose enough line-item detail to reconcile the `$2.13` difference. Railway invoices can combine the next subscription charge and prior usage overage, so the paid invoice is not the bot project's run rate. The account has a `$5` monthly subscription item, no spend commitment, and no general credit balance. Railway's Hobby billing policy treats `$5` as an included-usage minimum, not an extra `$5` added to usage above that threshold. The bot's usage alone already exceeds the minimum, so removing the reranker should reduce the workspace bill approximately dollar for dollar.

Current-cycle reranker cost to date is `$14.15`: `$14.0621` memory, `$0.0694` CPU, `$0.0192` model volume, and negligible egress. Its last-seven-day average memory was `1,150 MB`; its 30-day average was `2,470 MB`. The completed prior cycle's reranker cost was `$36.77`, including `$36.65` memory. These measurements show the main savings depend on which memory period is representative after the experiment.

## Projected cost after retirement

| Basis | Reranker cost removed per full cycle | Workspace resource usage after removal, before Jev | Bot project usage after removal, before Jev |
| --- | ---: | ---: | ---: |
| Prior completed cycle, unchanged traffic and other services | $36.77 | $49.45 | $34.24 |
| Current cycle extrapolated to a full 30 days from costs to date | $19.40 | $49.31 | $35.17 |

The current-cycle extrapolation uses `21.883` elapsed days of the September 10–October 10 cycle: bot project `(39.8016 − 14.1506) × 30 / 21.883 = $35.17`; workspace `(50.1214 − 14.1506) × 30 / 21.883 = $49.31`. The removed reranker equivalent is `14.1506 × 30 / 21.883 = $19.40`. These are projections from actual usage, not Railway's own forecast, and assume the observed non-reranker rate persists. The first row is a direct counterfactual against a completed billable usage cycle. Other projects in the workspace explain why workspace cost exceeds bot-project cost.

The reranker's **recent steady run rate is lower** than the current-cycle extrapolation: Railway's published rates are `$10/GB-month` RAM, `$20/vCPU-month` CPU, and `$0.15/GB-month` volume; last-seven-day averages were `1.150 GB` memory, `0.00425 vCPU`, and `0.180 GB` model volume, or `1.150×$10 + 0.00425×$20 + 0.180×$0.15 = $11.61` per 30-day month. If that recent rate persists, a future full-cycle switch saves about `$11.61` of Railway usage. The earlier `$36.77` saving is a measured historical scenario, not a claim about the next invoice.

**Switching during this cycle:** The `$14.15` of reranker charges already accrued remains billable. At the recent `$11.61` monthly run rate, its remaining `8.117` days would cost about `$3.14`. Railway's `$66.02` current workspace forecast would therefore become roughly `$62.88` if the reranker stopped now, before Jev charges and assuming Railway's other forecast components hold. Railway does not publish a bot-project forecast in the CLI; applying the observed non-reranker daily rate to the remaining days gives a bot-project cycle total of `$39.80 + ($39.80 − $14.15) × 8.117 / 21.883 = $49.32` if switched now, versus about `$52.46` if it keeps running. These forecasts are approximate and use different forecast methods for the workspace and project, so they should not be added or subtracted from each other.

Jev is billed outside Railway through the planned OpenRouter gateway. [OpenRouter's Jev 1.13 listing](https://openrouter.ai/typesafe/jev-1.13/) publishes `$0.042` per million input tokens, free output, and a `32,000`-token context window for this route. Final all-in cost is **Railway after removal + OpenRouter's measured Jev `usage.cost`**, plus any attributable fee when OpenRouter credits are bought. OpenRouter says model prices pass through without markup; its [Standard pricing](https://openrouter.ai/pricing) lists a `5.5%` platform fee on credit purchases, not on each inference, and its [credit-fee guidance](https://openrouter.ai/blog/insights/governing-team-ai-spend/) says card top-ups have a `$0.80` minimum fee. The account's actual plan, available credits, payment method, and purchase-fee allocation are not yet verified.

A read-only production DB count found `2,457` transactions with reranker-scored retrieval results from September 10 through October 1, and `789` in the last seven days. The recent rate is approximately `3,380` reranker calls per 30 days **including replay-like traffic**. In the wider transaction set, `48` active questions repeated more than ten times each and accounted for `2,419` of `2,433` active events, with a median of 50 repetitions. That pattern is consistent with regression or load replay through Discord, but repetition alone cannot prove origin; the remaining `14` active events and `231` passive events had low repetition. The comparable reranker-reaching subset splits into `2,217` high-repeat active, `13` low-repeat active, and `227` passive transactions. Excluding every high-repeat active event leaves `13 + 227 = 240` low-repeat reranker calls in the observed 22-day period, or approximately `330` per 30 days. This is a conservative organic-like scenario, not a verified count of genuine user requests, because some repeated active calls could be real and some low-repeat events could be tests. In the last seven days, the analogous split was `704` high-repeat active, `0` low-repeat active, and `85` passive reranker calls.

Under the proposed single batched Jev call per reranker-reaching transaction, the cost sensitivity is:

| Jev input tokens per call | Low-repeat scenario, about 330 calls/month | Observed load including replay-like traffic, 3,380 calls/month |
| ---: | ---: | ---: |
| 5,000 | $0.07 | $0.71 |
| 10,000 | $0.14 | $1.42 |
| 20,000 | $0.28 | $2.84 |
| 32,000, OpenRouter's published context cap | $0.44 | $4.54 |

The **150 live paired holdout calls** through Railway n8n all returned valid Jev scores on the first attempt. OpenRouter reported `933,563` input tokens in total, averaging `6,224` per call with a 95th percentile of `8,320`; total `usage.cost` was `$0.039209646`, or `$0.00026139764` per call. The final 30-call calibration was separate and cost `$0.008725836`; it is excluded from the forward unit-cost estimate because the holdout better represents untuned future calls. An earlier preliminary calibration is likewise excluded. At the holdout average and one batched call per reranker-reaching transaction, the 22-day observed workload of `2,457` calls would use approximately `$0.64` of Jev inference, while the `240` low-repeat calls would use approximately `$0.06`. Extrapolating the **recent seven-day** observed rate to 30 days gives about `$0.88`/month including replay-like traffic; extrapolating the **22-day low-repeat** rate gives about `$0.09`/month. The latter is an organic-like sensitivity case, not verified organic demand. These are measured-unit-cost projections rather than actual future invoices, and the failed answer cases block the switch even though the gateway calls succeeded.

The separate 20-case full-answer diagnostic spent **`$0.005689572`** on OpenRouter Jev reranking (`135,466` input tokens). It is a one-time test expense, excluded from the holdout unit cost and monthly forecast. That diagnostic also invoked Gemini for answer generation; its token usage was recorded, but its dollar charge was not measured here and is not included in the Jev estimate.

If a later design passes the answer check and the reranker is retired, the current non-reranker Railway projection implies a full 30-day cycle of about **`$35.17` of Railway bot-project usage plus `$0.09–$0.88` Jev inference**, or `$35.26–$36.05` combined. At workspace level, it would be about **`$49.31` Railway usage plus the same Jev inference**, or `$49.40–$50.19` combined. These do not include any attributable OpenRouter credit-funding fee and depend on unchanged other-service usage. The sensitivity table above shows how larger real prompts could change the Jev portion. **None of these savings are realized while the current reranker remains online.**

Credit-funding cost is separate from inference spend: if this account is on Standard with card top-ups, a purchase incurs `max(5.5% of purchase, $0.80)`. Existing shared credits have already been funded, and the eventual fee can be spread across Jev and other models only after the account's purchases are known. Network egress from n8n to Jev should also be checked; current private reranker egress is negligible, but Jev requests cross a public boundary. A retained always-on local fallback preserves most of the RAM charge and does not realize the projected savings.

## Verification

**Test:** Once the replacement passes its real-query experiment, stop the reranker and retire its `/models` volume; compare the next 24 hours of Railway service usage and the next complete billing cycle with matched traffic, and compare Jev invoices with measured input-token totals.

**Pass when:** Reranker memory, CPU, and volume charges stop accruing; workspace usage falls by the measured reranker run rate after allowing for other-service variation; Jev charges match measured tokens; and the total is lower without a relevance or availability regression.

Sources: authenticated `railway usage --period current/previous --json`, `railway usage projects --project Discord-RAG-Bot --period current/previous --json`, `railway metrics -s reranker --since 7d/30d --json`, and Railway GraphQL billing metadata/invoices, observed October 1, 2026. Raw resource evidence is retained outside the repository under the local temporary directory `discord-rag-jev-railway-billing-2026-10-01`. Pricing and invoice interpretation: [Railway pricing](https://docs.railway.com/pricing) and [Understanding Your Bill](https://docs.railway.com/pricing/understanding-your-bill).

