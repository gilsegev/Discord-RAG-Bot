# Alerting
**Status:** Draft for team review  
**Related:** Observability Design

## Purpose
Alerts should catch failures that require action, not every interesting metric movement.
The first version should focus on:
- system availability
- bad retrieval/refusal behavior
- user-visible quality failures
- sustained latency problems
- observability pipeline failures

## Alert Routing

### Routing Terms
**Maintainer notification** means an immediate alert for urgent failures. In v1, send these to a private Discord ops channel. For confirmed quality failures, also create or link a GitHub issue.
**Maintainer digest** means a non-urgent summary for review. In v1, include these in the weekly `#bot-metrics` digest with links to Phoenix traces or Postgres views.

### Critical
User-visible failure or safety/grounding risk.
Destination:
- maintainer notification
- GitHub issue for quality failures

### Warning
Degradation that needs review but is not immediately blocking.
Destination:
- `#bot-metrics`
- maintainer digest

### Info
Useful trend with no immediate action required.
Destination:
- weekly digest only

## Starting Alert Thresholds
These are starting values. Tune them after real traffic.

### Critical Alerts
| Alert | Threshold |
|---|---|
| Qdrant unavailable | Any failed Qdrant call for an active request |
| Gemini unavailable | Any failed Gemini call for an active request |
| Discord dispatch failed | Any failed response send after answer generation |
| Postgres write failure | Any failed transaction or feedback write |
| Missed refusal | Any confirmed case where weak/no context produced an answer |
| No-context violation | Any no-context case that answered instead of refusing |
Action:
- alert maintainer
- mark transaction `failed`, when applicable
- create review issue for quality failures

### Warning Alerts
| Alert | Threshold |
|---|---|
| p95 active-call latency | Greater than 5 seconds over a weekly window |
| CrossEncoder rerank latency | p95 greater than 800 ms over a weekly window |
| Qdrant retrieval latency | p95 greater than 300 ms over a weekly window |
| Negative feedback rate | Greater than 25%, with `n >= 10` weekly feedback items |
| No-context rate spike | Greater than 30% of valid queries in a week |
| Low-score refusal spike | Greater than 30% of valid queries in a week |
| Phoenix ingestion failure | More than 5 consecutive failed trace writes |
Action:
- post to `#bot-metrics` or maintainer digest
- review Phoenix traces
- tune workflow, thresholds, or corpus coverage

### Info Alerts
| Alert | Threshold |
|---|---|
| Feedback unmatched | More than 10 unmatched feedback events in a week |
Action:
- include in weekly digest
- review feedback correlation workflow

## Refusal And Quality Alerts
Refusal failures are high priority because the previous bot failed by answering beyond retrieved context.
Critical cases:
- Bot answered when no usable context existed.
- Bot answered when all retrieval/reranker scores were below threshold.
- Bot fabricated a claim not supported by retrieved context.
- Bot exposed PII or unsafe content.

Warning cases:
- Bot refused despite apparently useful retrieved context.
- Refusals cluster in one channel.
- Refusals increase after a prompt/retrieval change.

## Latency Alerts
The active-call SLO is:
- **p95 end-to-end latency < 5 seconds**

Stage targets:
| Stage | Warning |
|---|---|
| n8n routing and query prep | p95 > 100 ms |
| Qdrant retrieval | p95 > 300 ms |
| CrossEncoder rerank | p95 > 800 ms |
| Dedupe and context assembly | p95 > 150 ms |
| Observability writes | p95 > 200 ms |
| Gemini generation | p95 > 3,000 ms |
| Discord response dispatch | p95 > 300 ms |

## Alert Implementation
n8n should own alert triggering for v1.
Implementation path:
1. Write transaction, retrieval, refusal, feedback, and latency events to Postgres.
2. Emit Phoenix spans for trace inspection.
3. Run scheduled n8n checks against Postgres rollups.
4. Post warning/info alerts to Discord.
5. Create GitHub issues for critical quality failures.

Phoenix should be used to inspect why an alert fired. Postgres should be the source for threshold checks.

## Open Questions
- Which Discord channel receives warning alerts?
- Who receives critical maintainer notifications?
- Should critical quality failures automatically open GitHub issues?
- Should passive-listener failures alert immediately or only appear in weekly metrics?
- What threshold should trigger paging, if any?
