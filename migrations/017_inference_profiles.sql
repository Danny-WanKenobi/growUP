-- Throttle-profile and pricing provenance for the inference ledger.
-- Forward-only and idempotent (ADD COLUMN IF NOT EXISTS guards).
--
-- Migration 016 (applied) must already exist; this migration only
-- adds nullable-with-default columns to inference_usage, so older
-- rows keep their meaning and no rows are rewritten.
--
-- profile_name records the request-scoped throttle profile that
-- governed the call (low/medium/high/extra_high); effective limits
-- record the material bounds after administrator ceilings, so later
-- configuration changes cannot rewrite what a historical row meant.
-- pricing_basis retains the estimate provenance (reference rates,
-- multiplier, planning rates, status, source, checked date, and the
-- per-call estimate label) as JSONB for the same reason.

ALTER TABLE inference_usage
    ADD COLUMN IF NOT EXISTS profile_name TEXT NOT NULL DEFAULT '';
ALTER TABLE inference_usage
    ADD COLUMN IF NOT EXISTS effective_max_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE inference_usage
    ADD COLUMN IF NOT EXISTS effective_max_input_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE inference_usage
    ADD COLUMN IF NOT EXISTS pricing_basis JSONB;
