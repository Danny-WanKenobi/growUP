-- Shared inference allowance and usage ledger (Baseten/Meta capabilities).
-- Forward-only and idempotent (IF NOT EXISTS guards).
--
-- inference_allowance holds one singleton row (id = 1): the
-- competition-wide token allowance, consumed units, and the protected
-- demonstration reserve. Allowance is granted by explicit owner
-- configuration and never resets automatically.
--
-- inference_usage is a request-status ledger: one row per live call
-- or idempotent replay, holding request and idempotency identity,
-- authenticated principal, requesting agent and capability,
-- dossier/run/package association (nullable: global cockpit requests
-- record no dossier), provider/model/prompt/schema versions, request
-- digest, requested limits, bound/reserved/reported/excess units,
-- estimated cost (application estimate, never provider billing),
-- outcome, live/replay status, and timestamps. Rows transition
-- reserved → completed/failed/unresolved/rejected/released exactly
-- once; spend columns only increase, and reconciliation never
-- double-charges. Ambiguous outcomes stay 'unresolved' with their
-- reservation retained. This ledger is status-tracking, not
-- append-only: outcome and usage columns are updated once by
-- reconciliation.
--
-- extraction_originals retains caller-supplied extraction pages
-- (image bytes or extracted text) keyed by document and page, with
-- digests and source references, so originals stay retrievable under
-- the database's existing access control. Bounded per page and per
-- request by the application; same-locator resubmission must carry
-- the identical digest.
--
-- Additive only: no existing table is altered, no rows are rewritten,
-- and application rollback needs no schema reversal for THIS
-- migration. See README (Migrations & Rollback): migration 014's
-- table rename is NOT rollback-transparent for older code.

CREATE TABLE IF NOT EXISTS inference_allowance (
    id INTEGER PRIMARY KEY,
    allowance_units BIGINT NOT NULL DEFAULT 0,
    used_units BIGINT NOT NULL DEFAULT 0,
    demo_reserve_units BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inference_allowance_singleton CHECK (id = 1),
    CONSTRAINT inference_allowance_nonnegative CHECK (
        allowance_units >= 0 AND used_units >= 0 AND demo_reserve_units >= 0
    )
);

CREATE TABLE IF NOT EXISTS inference_usage (
    request_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    principal TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    capability TEXT NOT NULL DEFAULT '',
    dossier_id UUID,
    dossier_version INTEGER,
    run_id UUID,
    package_entry UUID,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL DEFAULT '',
    request_digest TEXT NOT NULL DEFAULT '',
    requested_max_tokens INTEGER NOT NULL DEFAULT 0,
    bound_input_units BIGINT NOT NULL DEFAULT 0,
    reserved_units BIGINT NOT NULL DEFAULT 0,
    excess_units BIGINT NOT NULL DEFAULT 0,
    reported_input_tokens BIGINT,
    reported_output_tokens BIGINT,
    reported_total_tokens BIGINT,
    estimated_cost_usd NUMERIC,
    outcome TEXT NOT NULL DEFAULT 'reserved',
    live BOOLEAN NOT NULL DEFAULT TRUE,
    replay_of TEXT,
    provider_request_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS extraction_originals (
    doc_id TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    mime TEXT NOT NULL,
    digest TEXT NOT NULL,
    content TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, page_no)
);

CREATE INDEX IF NOT EXISTS inference_usage_agent_capability_idx
    ON inference_usage (agent, capability);
CREATE INDEX IF NOT EXISTS inference_usage_outcome_idx
    ON inference_usage (outcome);
