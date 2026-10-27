-- MVP-004B-2: persistent Brave research request accounting.
-- Forward-only and idempotent (IF NOT EXISTS guards). No changes to
-- existing tables. One row per UTC calendar month tracks metered
-- usage against the configured ceiling; reservations are made with a
-- single conditional UPDATE so concurrent runs cannot overspend.

CREATE TABLE IF NOT EXISTS research_budget (
    research_month TEXT PRIMARY KEY,
    used INTEGER NOT NULL DEFAULT 0,
    ceiling INTEGER NOT NULL DEFAULT 100,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT research_budget_used_check CHECK (used >= 0),
    CONSTRAINT research_budget_ceiling_check CHECK (ceiling >= 1)
);
