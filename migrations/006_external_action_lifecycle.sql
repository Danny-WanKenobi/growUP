-- MVP-004C-A Phase 3C-3B-1: transaction-safe external-action lifecycle.
-- Forward-only and idempotent (IF NOT EXISTS guards). No changes to
-- existing tables, including external_actions and agent_events.
--
-- Append-oriented execution-attempt history: one row per claim, never
-- updated except to record that attempt's own outcome. Attempt rows
-- are the durable record of which executor took which attempt and
-- what that attempt's recorded outcome was. History is never
-- rewritten: a new attempt is a new row, and action deletion is
-- refused while attempts reference it (ON DELETE RESTRICT).
--
-- outcome is NULL while an attempt's result is unrecorded; the
-- pairing CHECK keeps outcome/outcome_recorded_at consistent.
-- attempt_number is 1-based per action (UNIQUE per action) and equals
-- the action's attempt_count at claim time.

CREATE TABLE IF NOT EXISTS external_action_attempts (
    attempt_id UUID PRIMARY KEY,
    action_id UUID NOT NULL REFERENCES external_actions(action_id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL,
    executor_id TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome TEXT NULL,
    outcome_recorded_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT external_action_attempts_number_check CHECK (attempt_number > 0),
    CONSTRAINT external_action_attempts_executor_check CHECK (executor_id <> ''),
    CONSTRAINT external_action_attempts_outcome_check CHECK (
        outcome IN ('SUCCEEDED', 'FAILED', 'UNKNOWN')
    ),
    CONSTRAINT external_action_attempts_outcome_pair_check CHECK (
        (outcome IS NULL AND outcome_recorded_at IS NULL)
        OR (outcome IS NOT NULL AND outcome_recorded_at IS NOT NULL)
    ),
    CONSTRAINT external_action_attempts_number_unique UNIQUE (action_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS external_action_attempts_action_idx
    ON external_action_attempts (action_id ASC, attempt_number ASC);
CREATE INDEX IF NOT EXISTS external_action_attempts_open_idx
    ON external_action_attempts (action_id ASC)
    WHERE outcome IS NULL;
