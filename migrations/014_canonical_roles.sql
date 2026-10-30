-- Canonical agent roles (forward-only; requires migration 013 via the
-- ordered runner; re-application is safe).
--
-- Role transition, preserving history:
-- - Uriel owns opportunity/package preparation. The MVP outreach-intake
--   table is renamed, not rebuilt: existing rows keep their values and
--   are labeled 'mvp1-michael-intake' by role_schema, while new rows
--   are 'canonical1-uriel'. An old row naming Michael as outreach must
--   never be read as a financial record.
-- - Michael owns deterministic financial feasibility. Assessments
--   persist as workflow scenario records (migration 012); no finance
--   table is needed here.
-- - Priscilla owns conversion tracking with strict actual/simulated
--   separation (new table below).
-- - Custody receipts gain pipeline_version so stored agent names keep
--   their original meaning ('mvp-1' Michael = outreach intake).
-- - Pilot cells/events gain the canonical downstream stages; the
--   legacy 'michael_ready' value stays valid for existing rows.

ALTER TABLE IF EXISTS michael_intake RENAME TO uriel_packages;
ALTER INDEX IF EXISTS idx_michael_intake_dossier RENAME TO idx_uriel_packages_dossier;

ALTER TABLE uriel_packages
    ADD COLUMN IF NOT EXISTS role_schema TEXT NOT NULL DEFAULT 'mvp1-michael-intake';
ALTER TABLE uriel_packages ALTER COLUMN role_schema SET DEFAULT 'canonical1-uriel';
ALTER TABLE uriel_packages
    ADD COLUMN IF NOT EXISTS package_state TEXT NOT NULL DEFAULT 'prepared'
    CHECK (package_state IN ('draft', 'reviewed', 'prepared'));
ALTER TABLE uriel_packages
    ADD COLUMN IF NOT EXISTS execution_state TEXT NOT NULL DEFAULT 'not_attempted'
    CHECK (execution_state IN ('not_attempted', 'simulated', 'actually_attempted'));

ALTER TABLE custody_receipts
    ADD COLUMN IF NOT EXISTS pipeline_version TEXT NOT NULL DEFAULT 'mvp-1';
ALTER TABLE custody_receipts ALTER COLUMN pipeline_version SET DEFAULT 'canonical-1';

ALTER TABLE pilot_cells
    ADD COLUMN IF NOT EXISTS engineering_assessed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pilot_cells
    ADD COLUMN IF NOT EXISTS financially_assessed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pilot_cells
    ADD COLUMN IF NOT EXISTS package_prepared INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pilot_cells
    ADD COLUMN IF NOT EXISTS tracked_actual INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pilot_cells
    ADD COLUMN IF NOT EXISTS tracked_simulated INTEGER NOT NULL DEFAULT 0;

ALTER TABLE pilot_events DROP CONSTRAINT IF EXISTS pilot_events_transition_check;
ALTER TABLE pilot_events ADD CONSTRAINT pilot_events_transition_check CHECK (transition IN (
    'discovered', 'qualified', 'assessed', 'authorized',
    'incomplete', 'rejected', 'michael_ready',
    'engineering_assessed', 'financially_assessed', 'package_prepared',
    'tracked_actual', 'tracked_simulated'
));

CREATE TABLE IF NOT EXISTS conversion_events (
    event_id UUID PRIMARY KEY,
    package_entry UUID NOT NULL REFERENCES uriel_packages (entry_id),
    kind TEXT NOT NULL CHECK (kind IN ('delivery', 'engagement', 'response', 'conversion')),
    simulated BOOLEAN NOT NULL,
    label TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversion_events_package
    ON conversion_events (package_entry);
CREATE INDEX IF NOT EXISTS idx_conversion_events_simulated
    ON conversion_events (simulated, kind);

DROP TRIGGER IF EXISTS trg_conversion_events_append_only ON conversion_events;
CREATE TRIGGER trg_conversion_events_append_only
    BEFORE UPDATE OR DELETE ON conversion_events
    FOR EACH ROW EXECUTE FUNCTION reject_custody_write();
