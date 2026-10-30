-- Shared operational dossier custody, Michael intake queue, pilot ledger.
-- Forward-only and idempotent (IF NOT EXISTS guards; triggers dropped
-- before creation so re-application is safe).
--
-- One dossier identity per operational property (dossier_id is a
-- deterministic UUID derived from the property UUID by the
-- application, so every agent computes the same identity with no
-- registry table and no merge step). Versions are append-only.
--
-- Pre-handoff snapshots are immutable frozen copies of dossier
-- versions. Snapshots older than the retention window move to
-- custody_snapshots_archive (same shape); receipts, versions, and
-- intake entries are permanent and are never moved or deleted.
--
-- custody_receipts authenticates every handoff -- forward, return, and
-- human review -- under one contract (see app/custody.py). There is
-- deliberately no foreign key from receipts to snapshots: a receipt
-- stays valid after its snapshot is archived, and retrieval checks
-- the hot table first, then the archive.
--
-- Additive only: no existing table is altered, no rows are rewritten,
-- and application rollback needs no schema reversal.

CREATE TABLE IF NOT EXISTS dossier_versions (
    dossier_id UUID NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    property_id UUID NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    digest TEXT NOT NULL,
    supersedes INTEGER NULL,
    amendment JSONB NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dossier_id, version)
);
CREATE INDEX IF NOT EXISTS idx_dossier_versions_property
    ON dossier_versions (property_id);

CREATE TABLE IF NOT EXISTS custody_snapshots (
    snapshot_id UUID PRIMARY KEY,
    dossier_id UUID NOT NULL,
    version INTEGER NOT NULL,
    payload JSONB NOT NULL,
    digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (dossier_id, version)
        REFERENCES dossier_versions (dossier_id, version),
    UNIQUE (dossier_id, version)
);
CREATE INDEX IF NOT EXISTS idx_custody_snapshots_dossier
    ON custody_snapshots (dossier_id, version);

CREATE TABLE IF NOT EXISTS custody_snapshots_archive (
    snapshot_id UUID PRIMARY KEY,
    dossier_id UUID NOT NULL,
    version INTEGER NOT NULL,
    payload JSONB NOT NULL,
    digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (dossier_id, version)
        REFERENCES dossier_versions (dossier_id, version),
    UNIQUE (dossier_id, version)
);
CREATE INDEX IF NOT EXISTS idx_custody_snapshots_archive_dossier
    ON custody_snapshots_archive (dossier_id, version);

CREATE TABLE IF NOT EXISTS custody_receipts (
    receipt_id UUID PRIMARY KEY,
    dossier_id UUID NOT NULL,
    version INTEGER NOT NULL,
    snapshot_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('forward', 'return', 'review')),
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    mac TEXT NOT NULL,
    prev_receipt UUID NULL REFERENCES custody_receipts (receipt_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (dossier_id, version)
        REFERENCES dossier_versions (dossier_id, version)
);
CREATE INDEX IF NOT EXISTS idx_custody_receipts_dossier
    ON custody_receipts (dossier_id, created_at);

-- Durable Michael intake queue: the coordinated-workflow stop point.
-- status has exactly one value; outreach execution is not built.
CREATE TABLE IF NOT EXISTS michael_intake (
    entry_id UUID PRIMARY KEY,
    dossier_id UUID NOT NULL,
    version INTEGER NOT NULL,
    snapshot_id UUID NOT NULL,
    receipt_id UUID NOT NULL REFERENCES custody_receipts (receipt_id),
    demo_mode BOOLEAN NOT NULL,
    simulated BOOLEAN NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status = 'queued'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (dossier_id, version)
        REFERENCES dossier_versions (dossier_id, version),
    UNIQUE (dossier_id, version)
);
CREATE INDEX IF NOT EXISTS idx_michael_intake_dossier
    ON michael_intake (dossier_id, version);

-- Resumable 36-cell pilot ledger: 12 counties x 3 categories.
CREATE TABLE IF NOT EXISTS pilot_state (
    state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
    status TEXT NOT NULL DEFAULT 'stopped'
        CHECK (status IN ('stopped', 'running')),
    weights JSONB NOT NULL DEFAULT
        '{"high_rise": 1, "commercial_property": 1, "warehouse_industrial": 1}',
    limits JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot_cells (
    county TEXT NOT NULL,
    category TEXT NOT NULL,
    target_qualified INTEGER NOT NULL DEFAULT 4,
    discovered INTEGER NOT NULL DEFAULT 0,
    qualified INTEGER NOT NULL DEFAULT 0,
    assessed INTEGER NOT NULL DEFAULT 0,
    authorized INTEGER NOT NULL DEFAULT 0,
    incomplete INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    michael_ready INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (county, category)
);

CREATE TABLE IF NOT EXISTS pilot_events (
    event_id UUID PRIMARY KEY,
    county TEXT NOT NULL,
    category TEXT NOT NULL,
    dossier_id UUID NOT NULL,
    transition TEXT NOT NULL CHECK (transition IN (
        'discovered', 'qualified', 'assessed', 'authorized',
        'incomplete', 'rejected', 'michael_ready'
    )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (county, category) REFERENCES pilot_cells (county, category),
    UNIQUE (dossier_id, transition)
);
CREATE INDEX IF NOT EXISTS idx_pilot_events_cell
    ON pilot_events (county, category);

-- Append-only enforcement. Versions, receipts, archived snapshots, and
-- intake entries reject UPDATE and DELETE. Hot snapshots reject UPDATE
-- but allow DELETE so the archival job can move rows to the archive
-- table in the same transaction that inserts the archival copy.
CREATE OR REPLACE FUNCTION reject_custody_write() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'custody table % is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reject_custody_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'custody table % is immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dossier_versions_append_only ON dossier_versions;
CREATE TRIGGER trg_dossier_versions_append_only
    BEFORE UPDATE OR DELETE ON dossier_versions
    FOR EACH ROW EXECUTE FUNCTION reject_custody_write();

DROP TRIGGER IF EXISTS trg_custody_receipts_append_only ON custody_receipts;
CREATE TRIGGER trg_custody_receipts_append_only
    BEFORE UPDATE OR DELETE ON custody_receipts
    FOR EACH ROW EXECUTE FUNCTION reject_custody_write();

DROP TRIGGER IF EXISTS trg_custody_archive_append_only ON custody_snapshots_archive;
CREATE TRIGGER trg_custody_archive_append_only
    BEFORE UPDATE OR DELETE ON custody_snapshots_archive
    FOR EACH ROW EXECUTE FUNCTION reject_custody_write();

DROP TRIGGER IF EXISTS trg_michael_intake_append_only ON michael_intake;
CREATE TRIGGER trg_michael_intake_append_only
    BEFORE UPDATE OR DELETE ON michael_intake
    FOR EACH ROW EXECUTE FUNCTION reject_custody_write();

DROP TRIGGER IF EXISTS trg_custody_snapshots_immutable ON custody_snapshots;
CREATE TRIGGER trg_custody_snapshots_immutable
    BEFORE UPDATE ON custody_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_custody_update();
