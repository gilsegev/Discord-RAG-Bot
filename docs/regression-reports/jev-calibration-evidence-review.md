# Jev calibration evidence review

Status: the final 30 real paired Railway calibration queries have been reviewed and the Jev gate is frozen for the untouched holdout. The experiment used the production context assembly contract and identical Stage 1 candidates for both arms. All 30 Jev calls returned HTTP 200. This is calibration evidence, not a switch decision.

The review inspected original questions, complete selected contexts, and full Stage 1 candidate texts with both scores for substantive questions and decision disagreements. A passage repeating the historical query received no evidence credit unless it also contained a responsive reply. Previous `answered`/`refused` status was a sampling stratum, not a truth label.

| Calibration observation | CrossEncoder | Jev |
|---|---|---|
| Amazon compensation mechanics | Refused | Selected direct explanations of compensation targets, stock assumptions, and vesting. |
| Amazon TPM interview preparation | Selected direct guidance | Selected direct guidance. |
| Google team-match duration | Selected direct time ranges | Selected direct time ranges. |
| Amazon senior level | Selected mostly indirect comparisons | Selected an explicit Amazon level mapping. |
| AWS IAM team interview | Selected a team-specific reply | Selected the same team-specific reply. |
| Instacart wait time after full loop | Advanced with a passage echoing the question but giving no wait-time answer | Safely refused under the revised general rubric and gate. |
| Recent Google Technical Judgment interview | Selected a 2026 reply with guidance | Selected 2021–2022 discussions despite an explicit last-6-to-9-months constraint; the 2026 candidate scored 1.96, below the gate. |

The 30 cases produced 20 agreements to answer, four agreements to refuse, and six different decisions. In five cases CrossEncoder answered while Jev refused: the unsupported Instacart wait-time echo, an unclear lowball-offer definition, two statements without a question, and a historical referral offer that does not establish present availability. In the sixth, Jev selected direct Amazon compensation evidence while CrossEncoder refused. Full candidate review found no clear false refusal among those five, but the recent-Google ranking remains a known Jev miss. The preliminary calibration's first three HTTP 422 errors came from the local Windows client sending non-ASCII text incorrectly; all succeeded after UTF-8 transport correction and were not Jev model errors.

## Frozen threshold and remaining risk

Freeze `jev_score_threshold > 2.0` and the experiment's weak-signal level at 2.5 before the 150-query holdout. The gate preserves the reviewed direct IAM-team reply at 2.18 while excluding low-scoring unrelated results. Raising it enough to exclude every questionable passage would also discard useful evidence, and a scalar gate cannot recover a recent passage Jev ranks below older passages. The generic score rubric now asks for exact named company, role, and time constraints; there are no case-specific rules or temporal parser.

The holdout must measure direct support and unsupported non-refusals on genuine answer-seeking requests, including time and named-entity constraints, with refusals and statements reported separately. Review all selected passages and inspect full candidates when an apparent false refusal occurs. If the recent-Google pattern recurs enough to violate the predeclared direct-evidence retention or safety limits, do not promote Jev.

## Test

Apply the frozen rule to all 150 untouched real database queries through the Railway n8n test workflows, then review the real full-answer sample with Discord posting disabled. Compare evidence, decisions, latency, errors, and measured costs against the current reranker.

## Pass when

The holdout meets the predeclared evidence, unsupported-answer, latency, and cost criteria in the architecture report; the known recency miss is disclosed and does not recur beyond the allowed quality margin.
