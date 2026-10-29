-- Grow Up migration 010: append-only identity-review resolutions.
--
-- Records signed human resolutions of PENDING identity-review cases
-- (IR-4A). Each row preserves the full audit basis of one definitive
-- decision: the enrolled reviewer handle, the purpose-bound approval
-- signature (growup-review-v1 domain) plus the exact public key that
-- verifies it (rotation-proof historical verification), and the
-- digest-bound evidence snapshot behind the outcome. One definitive
-- decision per review, enforced by uniqueness.
--
-- Additive only: no existing table is modified, no triggers, no
-- backfill. Decisions resolve queue state; they never merge, delete,
-- or modify property records or listing evidence.

CREATE TABLE IF NOT EXISTS identity_review_decisions (
    decision_id UUID PRIMARY KEY,
    review_id UUID NOT NULL REFERENCES identity_reviews(review_id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL,
    prior_status TEXT NOT NULL,
    resolved_by TEXT NOT NULL,
    resolution_reference TEXT NOT NULL,
    approval_signature TEXT NOT NULL,
    signing_public_key TEXT NOT NULL,
    signing_key_fingerprint TEXT NOT NULL,
    approval_issued_at TIMESTAMPTZ NOT NULL,
    approval_expires_at TIMESTAMPTZ NOT NULL,
    evidence_digest TEXT NOT NULL,
    evidence_snapshot JSONB NOT NULL,
    expected_occurrence_count INTEGER NOT NULL,
    evidence_summary TEXT NOT NULL DEFAULT '',
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT review_decision_outcome_check CHECK (outcome IN ('SAME_PROPERTY', 'DISTINCT_PROPERTIES')),
    CONSTRAINT review_decision_prior_check CHECK (prior_status = 'PENDING'),
    CONSTRAINT review_decision_lifetime_check CHECK (approval_expires_at > approval_issued_at),
    CONSTRAINT review_decision_signature_check CHECK (approval_signature ~ '^[0-9a-f]{128}$'),
    CONSTRAINT review_decision_key_check CHECK (
        signing_public_key ~ '^[0-9a-f]{64}$'
        AND signing_key_fingerprint ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT review_decision_digest_check CHECK (evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT review_decision_count_check CHECK (expected_occurrence_count >= 1),
    CONSTRAINT review_decision_bounded_check CHECK (
        char_length(resolved_by) BETWEEN 1 AND 64
        AND char_length(resolution_reference) BETWEEN 1 AND 256
        AND char_length(evidence_summary) <= 500
    ),
    CONSTRAINT review_decision_one_decision_per_review UNIQUE (review_id)
);

CREATE INDEX IF NOT EXISTS identity_review_decisions_review_idx
    ON identity_review_decisions (review_id ASC, decided_at ASC);
