-- MVP-008: persistent source observations for Gabriel discovery.
-- Forward-only and idempotent (IF NOT EXISTS guards). No changes to
-- existing tables, no backfill: historical provenance strings stay
-- unverified and are never rewritten into links.
--
-- A source observation records that one provider object (e.g. OSM
-- element "way/1001" seen via Overpass) was resolved to one operational
-- property. It is an observation record, not a verified identity claim:
-- one row per canonical (source, external_id). A source object that
-- legitimately describes several buildings cannot take a second row;
-- such conflicts are reported for human identity review instead.
-- A future multi-building design would add a separate association
-- table; this migration intentionally does not.
--
-- The UNIQUE constraint exists for race safety: two concurrent runs
-- observing the same new element serialize on it (ON CONFLICT
-- DO NOTHING), so only one transaction commits a property for it.

CREATE TABLE IF NOT EXISTS source_observations (
    id UUID PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    first_observed TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_observed TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT source_observations_identity_check CHECK (
        btrim(source) <> '' AND btrim(external_id) <> ''
    ),
    CONSTRAINT source_observations_one_link_per_object UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS source_observations_property_idx
    ON source_observations (property_id);
