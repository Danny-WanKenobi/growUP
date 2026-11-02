# GUADE operating envelope — proposal for owner approval

Status: **PROPOSED, not active.** The activation envelope
(`guade/config.example.json`, ledger in `guade/state/`) is exhausted
(3/3 dispatches, 6/6 coach calls, 7,066 tokens) and is retained as
read-only activation evidence. It must never be reset or reused.

## Proposed structure

- **Separate ledger:** operating runs use `guade/state-operating/`
  (already git-ignored), initialized only after owner approval.
- **Coach (Baseten `meta-models/muse-spark-open-1026`):** proposed
  20 calls/day, 200/month; 150k tokens/day, 1.5M/month; per-call cap
  unchanged at 1,024 tokens.
- **Engineer (Muse Wonder via `muse exec`):** proposed 10
  dispatches/day, 100/month; 20 steps/task; 1 correction retry/task.
- **Spending:** a $50/month planning figure is included in
  `config.operating.example.json` as an *estimate only*. The Baseten
  price for this model is **unverified**; the binding controls are
  the call/token/dispatch ceilings. Observed activation usage:
  7,066 coach tokens across 6 calls (927–1,419 per call).
- **Fail-closed:** counters reserve before every provider call;
  any exhausted daily or cumulative limit stops work with a recorded
  reason. No automatic replenishment.
- **Boundaries:** development only; no production access; Git writes
  only as separately authorized; live property acquisition (M03A)
  always needs its own collection envelope.

## Runtime work required before activation (not yet built)

1. Daily-window counters in `Ledger` (UTC day buckets, durable
   across restarts) enforcing the per-day ceilings.
2. A read-only `report` CLI command summarizing tasks completed,
   coach/dispatch usage, and remaining headroom per period.
3. Offline tests for daily/cumulative exhaustion and restart
   durability.

## Owner decisions required

1. Approve or amend the proposed call/token/dispatch ceilings.
2. Confirm an acceptable Baseten dollar cap and, if possible, the
   actual per-token price or grant balance so the dollar figure can
   be verified rather than estimated.
3. Authorize the runtime work item above as the first operating
   task once the envelope is approved.
