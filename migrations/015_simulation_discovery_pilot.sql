-- Priscilla cohort simulations, bounded discovery workers, pilot provenance.
-- Forward-only and idempotent (IF NOT EXISTS guards). Requires 013/014.
--
-- priscilla_simulations freezes one deterministic cohort-simulation run:
-- idempotency key, cohort label, frozen eligible package/dossier refs,
-- config version, assumed rates, rounding rules, selection method + seed,
-- actor, status, realized counts, and exclusions with reasons.
-- conversion_events gains a nullable simulation link; simulated rows
-- written by a run carry its ID plus explicit origin in payload. The
-- partial unique index makes event writes idempotent per
-- (simulation, package, kind): concurrent/repeated execution cannot
-- duplicate outcomes. Non-simulation events keep simulation_id NULL.
--
-- pilot_events gains run/record provenance plus an identity-kind fence:
-- rows written before this migration used run-scoped UUIDs and keep
-- identity_kind 'run_scoped' via the column default; new code writes
-- only canonical dossier identities ('canonical'). Quota accounting
-- counts distinct canonical identities only.
--
-- discovery_runs persists bounded continuous-discovery workers: one
-- active worker per scope (partial unique index), policy, config
-- bounds, durable checkpoint, usage, owner/heartbeat fencing for
-- crash recovery, and a stop_requested flag that never releases the
-- slot early (the owning worker exits and marks itself paused).
--
-- Additive only: no existing table is altered in place beyond ADD COLUMN
-- with safe defaults, no rows are rewritten, and application rollback
-- needs no schema reversal for THIS migration. See README (Migrations &
-- Rollback): migration 014's table rename is NOT rollback-transparent
-- for older application code.

CREATE TABLE IF NOT EXISTS priscilla_simulations (
    sim_id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    cohort TEXT NOT NULL DEFAULT '',
    package_ids UUID[] NOT NULL DEFAULT '{}',
    dossier_refs JSONB NOT NULL DEFAULT '[]',
    config_version TEXT NOT NULL DEFAULT 'priscilla-sim-1',
    response_rate TEXT NOT NULL DEFAULT '0.10',
    customer_rate TEXT NOT NULL DEFAULT '0.36',
    rounding TEXT NOT NULL DEFAULT 'floor-responses;half-up-customers;capped',
    selection_method TEXT NOT NULL DEFAULT 'seeded-sha256-prefix',
    seed TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'completed',
    counts JSONB NOT NULL DEFAULT '{}',
    exclusions JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only: simulation runs are never updated or deleted.
CREATE OR REPLACE FUNCTION reject_simulation_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'priscilla_simulations is append-only';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_simulations_append_only ON priscilla_simulations;
CREATE TRIGGER trg_simulations_append_only
    BEFORE UPDATE OR DELETE ON priscilla_simulations
    FOR EACH ROW EXECUTE FUNCTION reject_simulation_mutation();

ALTER TABLE conversion_events
    ADD COLUMN IF NOT EXISTS simulation_id UUID NULL
    REFERENCES priscilla_simulations (sim_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_conversion_sim_package_kind
    ON conversion_events (simulation_id, package_entry, kind)
    WHERE simulation_id IS NOT NULL;

ALTER TABLE pilot_events
    ADD COLUMN IF NOT EXISTS run_id UUID NULL;
ALTER TABLE pilot_events
    ADD COLUMN IF NOT EXISTS record_key TEXT NULL;
ALTER TABLE pilot_events
    ADD COLUMN IF NOT EXISTS identity_kind TEXT NOT NULL DEFAULT 'run_scoped';

CREATE INDEX IF NOT EXISTS idx_pilot_events_canonical_qualified
    ON pilot_events (county, category, dossier_id)
    WHERE transition = 'qualified' AND identity_kind = 'canonical';

CREATE TABLE IF NOT EXISTS discovery_runs (
    run_id UUID PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'pilot-dfw',
    policy TEXT NOT NULL DEFAULT 'pilot',
    status TEXT NOT NULL DEFAULT 'running',
    config JSONB NOT NULL DEFAULT '{}',
    checkpoint JSONB NOT NULL DEFAULT '{}',
    usage JSONB NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    stop_requested BOOLEAN NOT NULL DEFAULT FALSE,
    error TEXT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ NULL
);

-- One active worker per scope; paused/completed/aborted rows stay history.
-- A stop request sets stop_requested but leaves status 'running' so the
-- slot stays held until the owning worker exits; crash recovery adopts
-- only rows whose heartbeat is older than the operator lease.
CREATE UNIQUE INDEX IF NOT EXISTS uq_discovery_runs_active_scope
    ON discovery_runs (scope)
    WHERE status = 'running';
