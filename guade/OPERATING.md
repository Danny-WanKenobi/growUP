# GUADE operating envelope — owner-approved 2026-11-02

Status: **APPROVED with limits below; no billable run yet executed
under it.** The activation envelope (`guade/config.example.json`,
ledger in `guade/state/`) is exhausted (3/3 dispatches, 6/6 coach
calls, 7,066 tokens) and is retained as read-only activation
evidence. It must never be reset or reused. The earlier proposed
$50/month configuration was **not** activated.

## Approved operating ceiling (owner, 2026-11-02)

- **Separate ledger:** `guade/state-operating/` (git-ignored),
  configured by `guade/config.operating.example.json`.
- **Baseten Super-Coach:** maximum **$10** additional spend,
  maximum **10 calls**, 100k token ceiling, 1,024 tokens/call.
- **Muse Wonder:** maximum **5 dispatches**, 20 steps/task,
  1 correction retry/task.
- **Scope:** Dallas P1 integration and Gabriel → Raphael
  engineering. Development only.

## Dollar enforceability

Published Baseten pricing for `meta-models/muse-spark-open-1026`
($1.25 per 1M input tokens, $4.25 per 1M output; multiply
corroborated on 2026-11-02, not confirmed on a Baseten invoice)
bounds worst-case spend under these ceilings to a few cents:
10 calls × 1,024 output tokens ≈ $0.05 of output, plus bounded
prompt input. The call/token ceilings are therefore the binding
controls and keep usage reliably within $10. Usage is still
reserved before every call and reconciled from response usage.

## Not built (deferred unless separately tasked)

Daily-window counters and a `report` CLI command were proposed
earlier and remain unbuilt; the approved envelope above does not
include daily limits. Engineering under this ceiling currently
proceeds directly (no billable GUADE dispatch is required to do
the work), and every billable run must state its ledger headroom
before starting.
