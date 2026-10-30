-- MVP workflow persistence: durable Gabriel -> Raphael -> Onesimus runs.
-- Forward-only and idempotent (IF NOT EXISTS guards).
--
-- workflow_runs carries one evaluation run: stable id, idempotency key,
-- current stage/status, correlation, approved limits, usage, and errors.
-- workflow_records carries typed stage artifacts (research artifacts,
-- dossier versions, qualifications, decisions, scenarios, model runs)
-- as versioned JSONB rows with content digests. Rewriting an identical
-- version is a no-op; rewriting with a different digest is a conflict
-- (enforced by the application, which compares digests on replays).
--
-- Additive only: no existing table is altered, no rows are rewritten,
-- and application rollback needs no schema reversal.

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    stage TEXT NOT NULL DEFAULT 'created',
    status TEXT NOT NULL DEFAULT 'open',
    correlation_id TEXT NOT NULL DEFAULT '',
    limits JSONB NOT NULL DEFAULT '{}',
    usage JSONB NOT NULL DEFAULT '{}',
    errors JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workflow_records (
    record_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES workflow_runs (run_id),
    kind TEXT NOT NULL,
    record_key TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    payload JSONB NOT NULL DEFAULT '{}',
    digest TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, kind, record_key, version)
);

CREATE INDEX IF NOT EXISTS idx_workflow_records_run
    ON workflow_records (run_id);
CREATE INDEX IF NOT EXISTS idx_workflow_records_run_kind
    ON workflow_records (run_id, kind);
