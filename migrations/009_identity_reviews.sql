-- IR-3A: durable identity-review queue for Gabriel.
-- Forward-only and idempotent (IF NOT EXISTS guards). No changes to
-- existing tables, no backfill: historical decisions stay as they are
-- and are never rewritten into queue rows.
--
-- identity_reviews holds one row per exact-match identity question.
-- Only one PENDING row may exist per deduplication key (partial unique
-- index); RESOLVED rows are preserved history and never updated by the
-- queue writer. identity_review_occurrences links every counted
-- encounter to its permanent G2 decision event, so each occurrence is
-- individually retrievable and occurrence_count always equals the
-- number of linked occurrence rows.
--
-- Operational (intake/discovery) reviews always carry at least one
-- conflicting operational property id. Research reviews carry a
-- run-scoped candidate key instead and must never fabricate
-- operational property ids; IR-3A writes no research rows, but the
-- schema already enforces that shape for IR-3C.

CREATE TABLE IF NOT EXISTS identity_reviews (
    review_id UUID PRIMARY KEY,
    dedup_key TEXT NOT NULL,
    source TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    observed_identity JSONB NOT NULL,
    provider TEXT NULL,
    external_id TEXT NULL,
    listing_url TEXT NULL,
    conflicting_property_ids UUID[] NOT NULL DEFAULT '{}',
    candidate_key TEXT NULL,
    identity_disputes JSONB NULL,
    decision_event_id TEXT NOT NULL REFERENCES agent_events(event_id),
    run_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    last_event_id TEXT NOT NULL,
    supersedes_id UUID NULL REFERENCES identity_reviews(review_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT identity_reviews_source_check CHECK (
        source IN ('intake', 'discovery', 'research')
    ),
    CONSTRAINT identity_reviews_status_check CHECK (
        status IN ('PENDING', 'RESOLVED')
    ),
    CONSTRAINT identity_reviews_occurrence_check CHECK (
        occurrence_count >= 1
    ),
    CONSTRAINT identity_reviews_operational_check CHECK (
        source = 'research'
        OR (
            cardinality(conflicting_property_ids) >= 1
            AND candidate_key IS NULL
            AND identity_disputes IS NULL
        )
    ),
    CONSTRAINT identity_reviews_research_check CHECK (
        source <> 'research'
        OR (
            conflicting_property_ids = '{}'
            AND candidate_key IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS identity_reviews_pending_dedup_idx
    ON identity_reviews (dedup_key) WHERE status = 'PENDING';

CREATE INDEX IF NOT EXISTS identity_reviews_status_updated_idx
    ON identity_reviews (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS identity_review_occurrences (
    occurrence_id UUID PRIMARY KEY,
    review_id UUID NOT NULL REFERENCES identity_reviews(review_id),
    event_id TEXT NOT NULL UNIQUE REFERENCES agent_events(event_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS identity_review_occurrences_review_idx
    ON identity_review_occurrences (review_id);
