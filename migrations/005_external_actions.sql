-- MVP-004C-A Phase 3C-3A: consequential external-action ledger.
-- Forward-only and idempotent (IF NOT EXISTS guards). No changes to
-- existing tables.
--
-- Records durable AUTHORIZATION of consequential external actions
-- (outreach, contracts, external instructions). Authorization and
-- execution are distinct: this table proves an action was authorized,
-- never that it ran. Execution, retries, and state advancement belong
-- to Phase 3C-3B; in 3C-3A every row is created AUTHORIZED.
--
-- History preservation: property_id references properties(id) with
-- ON DELETE SET NULL, so removing a property never deletes the
-- consequential action history attached to it.
--
-- request_fingerprint is a non-sensitive, immutable hash of the
-- intended action (algorithm-prefixed hex digest). The raw request
-- details are never stored here: idempotency-key reuse with different
-- details is detected by fingerprint mismatch, and low-entropy
-- request details must still be treated as non-sensitive only.
-- updated_at is maintained explicitly by writers; no triggers.

CREATE TABLE IF NOT EXISTS external_actions (
    action_id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    action_type TEXT NOT NULL,
    agent TEXT NOT NULL,
    property_id UUID NULL REFERENCES properties(id) ON DELETE SET NULL,
    request_fingerprint TEXT NOT NULL,
    authorized_by TEXT NOT NULL,
    authorization_reference TEXT NOT NULL,
    authorized_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'AUTHORIZED',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT external_actions_state_check CHECK (state IN (
        'AUTHORIZED', 'PENDING', 'ATTEMPTING', 'SUCCEEDED', 'FAILED', 'UNKNOWN'
    )),
    CONSTRAINT external_actions_attempts_check CHECK (attempt_count >= 0),
    CONSTRAINT external_actions_nonempty_check CHECK (
        idempotency_key <> ''
        AND action_type <> ''
        AND agent <> ''
        AND request_fingerprint <> ''
        AND authorized_by <> ''
        AND authorization_reference <> ''
    )
);

CREATE INDEX IF NOT EXISTS external_actions_state_idx
    ON external_actions (state, created_at ASC, action_id ASC);
CREATE INDEX IF NOT EXISTS external_actions_property_idx
    ON external_actions (property_id)
    WHERE property_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS external_actions_agent_time_idx
    ON external_actions (agent, created_at DESC, action_id DESC);
