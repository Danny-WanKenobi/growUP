-- Grow Up migration 007: append-only reconciliation decisions.
--
-- Records human-approved, evidence-gated resolutions of UNKNOWN
-- external actions (Phase 3C-3B-3B). Each row preserves the full
-- audit basis of one definitive decision: the verified human
-- principal, the purpose-bound approval signature plus the exact
-- public key that verifies it (rotation-proof historical
-- verification), and the digest-bound evidence behind the outcome.
-- One definitive decision per attempt, enforced by uniqueness.
--
-- Additive only: no existing table is modified, no triggers, no
-- backfill. GRW-EXT-001/GRW-EXT-002: decisions resolve outcomes,
-- they never requeue, retry, or execute actions.

CREATE TABLE IF NOT EXISTS reconciliation_decisions (
    decision_id UUID PRIMARY KEY,
    action_id UUID NOT NULL REFERENCES external_actions(action_id) ON DELETE RESTRICT,
    attempt_id UUID NOT NULL REFERENCES external_action_attempts(attempt_id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL,
    asserts_outcome TEXT NOT NULL,
    prior_outcome TEXT NOT NULL,
    reconciled_by TEXT NOT NULL,
    reconciliation_reference TEXT NOT NULL,
    approval_signature TEXT NOT NULL,
    signing_public_key TEXT NOT NULL,
    signing_key_fingerprint TEXT NOT NULL,
    approval_issued_at TIMESTAMPTZ NOT NULL,
    approval_expires_at TIMESTAMPTZ NOT NULL,
    evidence_kind TEXT NOT NULL,
    evidence_source TEXT NOT NULL,
    evidence_observed_at TIMESTAMPTZ NOT NULL,
    evidence_digest TEXT NOT NULL,
    evidence_summary TEXT NOT NULL DEFAULT '',
    artifact_ref TEXT NOT NULL,
    provider_reference TEXT NULL,
    storage_ref TEXT NULL,
    verification_tier TEXT NOT NULL DEFAULT 'human_reviewed',
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT reconciliation_outcome_check CHECK (outcome IN ('SUCCEEDED', 'FAILED')),
    CONSTRAINT reconciliation_asserts_check CHECK (
        asserts_outcome IN ('SUCCEEDED', 'FAILED')
        AND asserts_outcome = outcome
    ),
    CONSTRAINT reconciliation_prior_check CHECK (prior_outcome = 'UNKNOWN'),
    CONSTRAINT reconciliation_kind_check CHECK (
        evidence_kind IN ('provider_receipt', 'provider_status_reference')
    ),
    CONSTRAINT reconciliation_tier_check CHECK (verification_tier = 'human_reviewed'),
    CONSTRAINT reconciliation_lifetime_check CHECK (approval_expires_at > approval_issued_at),
    CONSTRAINT reconciliation_signature_check CHECK (approval_signature ~ '^[0-9a-f]{128}$'),
    CONSTRAINT reconciliation_key_check CHECK (
        signing_public_key ~ '^[0-9a-f]{64}$'
        AND signing_key_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT reconciliation_digest_check CHECK (evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT reconciliation_bounded_check CHECK (
        char_length(reconciled_by) BETWEEN 1 AND 64
        AND char_length(reconciliation_reference) BETWEEN 1 AND 256
        AND char_length(evidence_source) BETWEEN 1 AND 200
        AND char_length(evidence_summary) <= 500
        AND char_length(artifact_ref) BETWEEN 1 AND 500
        AND (provider_reference IS NULL OR char_length(provider_reference) BETWEEN 1 AND 500)
        AND (storage_ref IS NULL OR char_length(storage_ref) BETWEEN 1 AND 500)
    ),
    CONSTRAINT reconciliation_one_decision_per_attempt UNIQUE (attempt_id)
);

CREATE INDEX IF NOT EXISTS reconciliation_decisions_action_idx
    ON reconciliation_decisions (action_id ASC, decided_at ASC);
