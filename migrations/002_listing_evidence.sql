-- MVP-004A: commercial listing intake evidence (append-only side tables).
-- Forward-only and idempotent (IF NOT EXISTS guards). No changes to the
-- properties table or its semantics.
--
-- Why new tables instead of existing columns: asking rent is not rental
-- income, an advertised owner is not ownership, and listing type, listing
-- date, price history, and per-observation distress attribution have no
-- safe existing home. Forcing them into unrelated columns would corrupt
-- the estimated/verified record, so intake preserves them here, linked
-- to the property they describe.

CREATE TABLE IF NOT EXISTS listing_evidence (
    id UUID PRIMARY KEY,
    property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    listing_type TEXT NOT NULL DEFAULT 'unknown',
    payload JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT listing_evidence_type_check CHECK (
        listing_type IN ('sale', 'lease', 'auction', 'unknown')
    )
);

CREATE INDEX IF NOT EXISTS listing_evidence_property_idx
    ON listing_evidence (property_id);

CREATE TABLE IF NOT EXISTS distress_observations (
    id UUID PRIMARY KEY,
    property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    indicator_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'preliminary',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT distress_indicator_check CHECK (indicator_type IN (
        'price_reduction', 'extended_listing', 'advertised_vacancy',
        'tax_delinquency', 'foreclosure_proceeding', 'repeated_listing',
        'other'
    )),
    CONSTRAINT distress_status_check CHECK (status IN ('preliminary', 'verified'))
);

CREATE INDEX IF NOT EXISTS distress_observations_property_idx
    ON distress_observations (property_id);
