-- B5-2A.2b-0: commercial-property category migration and downtown attribute.
-- Forward-only and idempotent (IF EXISTS / IF NOT EXISTS guards).
--
-- The canonical category downtown_commercial is retired in favor of
-- commercial_property. The retired value stays legal in the CHECK
-- constraint so historical rows remain readable; new persistence must
-- use commercial_property (enforced by application-level normalization).
-- Stored values are never rewritten by this migration.
--
-- is_downtown is a separate evidence-supported attribute: FALSE means
-- downtown priority is NOT established (non-downtown or uncertain).
-- Existing rows receive FALSE; no backfill to TRUE ever occurs here.
-- Backward compatible with the running release: application SQL uses
-- explicit column lists, and all previously permitted category values
-- remain permitted.

ALTER TABLE IF EXISTS properties
    DROP CONSTRAINT IF EXISTS properties_category_check;

ALTER TABLE IF EXISTS properties
    ADD CONSTRAINT properties_category_check CHECK (category IN (
        'high_rise', 'commercial_property', 'downtown_commercial',
        'warehouse_industrial', 'empty_lot', 'other'
    ));

ALTER TABLE IF EXISTS properties
    ADD COLUMN IF NOT EXISTS is_downtown BOOLEAN NOT NULL DEFAULT FALSE;
