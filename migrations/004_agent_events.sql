-- MVP-004C-A: shared agent activity-event index.
-- Forward-only and idempotent (IF NOT EXISTS guards). No changes to
-- existing tables. Stores routine activity and permanent decision /
-- evidence / handoff events for cross-agent history; full research
-- payloads are intentionally NOT duplicated here (see MVP-004C-B).
-- Permanent rows must never be auto-deleted; cleanup statements always
-- carry an explicit retention_class guard.

CREATE TABLE IF NOT EXISTS agent_events (
    event_id TEXT PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    agent TEXT NOT NULL,
    run_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL DEFAULT '',
    property_id UUID NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    summary TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    evidence_refs JSONB NOT NULL DEFAULT '[]',
    parent_event_id TEXT NULL,
    retention_class TEXT NOT NULL,
    CONSTRAINT agent_events_severity_check CHECK (severity IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
    CONSTRAINT agent_events_retention_check CHECK (retention_class IN ('diagnostic', 'activity', 'permanent'))
);

CREATE INDEX IF NOT EXISTS agent_events_agent_time_idx
    ON agent_events (agent, occurred_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS agent_events_run_idx
    ON agent_events (run_id, occurred_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS agent_events_correlation_idx
    ON agent_events (correlation_id, occurred_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS agent_events_property_idx
    ON agent_events (property_id, occurred_at DESC, event_id DESC)
    WHERE property_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS agent_events_type_time_idx
    ON agent_events (event_type, occurred_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS agent_events_retention_time_idx
    ON agent_events (retention_class, occurred_at DESC, event_id DESC);
