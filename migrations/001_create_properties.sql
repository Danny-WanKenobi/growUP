-- MVP-002: authoritative operational property record for the seven-agent pipeline.
-- Repeatable: safe to run more than once (IF NOT EXISTS guards).
--
-- EVIDENCE POLICY (owner-approved): numeric facts are stored as explicit
-- ESTIMATED / VERIFIED pairs within one record. Unknown values are NULL --
-- never zero, never invented. Hypothetical values are not permitted here;
-- hypothetical scenarios belong in separate future storage, not this table.
-- data_status (verified / estimated / mixed / unknown) is derived
-- server-side from the populated pairs, never trusted from clients.

CREATE TABLE IF NOT EXISTS properties (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    street_address TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT,
    category TEXT NOT NULL,
    building_area_sqft_estimated DOUBLE PRECISION,
    building_area_sqft_verified DOUBLE PRECISION,
    vacant_area_sqft_estimated DOUBLE PRECISION,
    vacant_area_sqft_verified DOUBLE PRECISION,
    occupancy TEXT,
    gross_monthly_rent_income_estimated NUMERIC,
    gross_monthly_rent_income_verified NUMERIC,
    owner TEXT,
    owner_monthly_costs_estimated NUMERIC,
    owner_monthly_costs_verified NUMERIC,
    property_value_estimated NUMERIC,
    property_value_verified NUMERIC,
    liens_total_estimated NUMERIC,
    liens_total_verified NUMERIC,
    months_on_market_estimated INTEGER,
    months_on_market_verified INTEGER,
    months_vacant_estimated INTEGER,
    months_vacant_verified INTEGER,
    provenance TEXT,
    field_evidence JSONB,
    data_status TEXT NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT properties_category_check CHECK (category IN (
        'high_rise', 'downtown_commercial', 'warehouse_industrial',
        'empty_lot', 'other'
    )),
    CONSTRAINT properties_data_status_check CHECK (data_status IN (
        'verified', 'estimated', 'mixed', 'unknown'
    )),
    CONSTRAINT properties_provenance_check CHECK (
        provenance IS NULL OR btrim(provenance) <> ''
    ),
    CONSTRAINT properties_building_est_check CHECK (
        building_area_sqft_estimated IS NULL OR building_area_sqft_estimated >= 0
    ),
    CONSTRAINT properties_building_ver_check CHECK (
        building_area_sqft_verified IS NULL OR building_area_sqft_verified >= 0
    ),
    CONSTRAINT properties_vacant_est_check CHECK (
        vacant_area_sqft_estimated IS NULL OR vacant_area_sqft_estimated >= 0
    ),
    CONSTRAINT properties_vacant_ver_check CHECK (
        vacant_area_sqft_verified IS NULL OR vacant_area_sqft_verified >= 0
    ),
    CONSTRAINT properties_vacant_within_building_est_check CHECK (
        vacant_area_sqft_estimated IS NULL
        OR building_area_sqft_estimated IS NULL
        OR vacant_area_sqft_estimated <= building_area_sqft_estimated
    ),
    CONSTRAINT properties_vacant_within_building_ver_check CHECK (
        vacant_area_sqft_verified IS NULL
        OR building_area_sqft_verified IS NULL
        OR vacant_area_sqft_verified <= building_area_sqft_verified
    ),
    CONSTRAINT properties_rent_est_check CHECK (
        gross_monthly_rent_income_estimated IS NULL OR gross_monthly_rent_income_estimated >= 0
    ),
    CONSTRAINT properties_rent_ver_check CHECK (
        gross_monthly_rent_income_verified IS NULL OR gross_monthly_rent_income_verified >= 0
    ),
    CONSTRAINT properties_costs_est_check CHECK (
        owner_monthly_costs_estimated IS NULL OR owner_monthly_costs_estimated >= 0
    ),
    CONSTRAINT properties_costs_ver_check CHECK (
        owner_monthly_costs_verified IS NULL OR owner_monthly_costs_verified >= 0
    ),
    CONSTRAINT properties_value_est_check CHECK (
        property_value_estimated IS NULL OR property_value_estimated >= 0
    ),
    CONSTRAINT properties_value_ver_check CHECK (
        property_value_verified IS NULL OR property_value_verified >= 0
    ),
    CONSTRAINT properties_liens_est_check CHECK (
        liens_total_estimated IS NULL OR liens_total_estimated >= 0
    ),
    CONSTRAINT properties_liens_ver_check CHECK (
        liens_total_verified IS NULL OR liens_total_verified >= 0
    ),
    CONSTRAINT properties_mom_est_check CHECK (
        months_on_market_estimated IS NULL OR months_on_market_estimated >= 0
    ),
    CONSTRAINT properties_mom_ver_check CHECK (
        months_on_market_verified IS NULL OR months_on_market_verified >= 0
    ),
    CONSTRAINT properties_vac_months_est_check CHECK (
        months_vacant_estimated IS NULL OR months_vacant_estimated >= 0
    ),
    CONSTRAINT properties_vac_months_ver_check CHECK (
        months_vacant_verified IS NULL OR months_vacant_verified >= 0
    )
);

CREATE INDEX IF NOT EXISTS properties_category_idx ON properties (category);
CREATE INDEX IF NOT EXISTS properties_created_at_idx ON properties (created_at DESC);
