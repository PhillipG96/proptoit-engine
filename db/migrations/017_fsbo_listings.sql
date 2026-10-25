-- 017_fsbo_listings.sql
-- ---------------------------------------------------------------------------
-- FSBO listings schema (final-review fix — carried forward to the carve).
--
-- The carve shipped no DDL for `fsbo_listings` even though three free tools
-- read it (api/core/queries.py list_fsbo_listings / count_fsbo, and the
-- knowledge-layer assembler's _fetch_fsbo_listings) and the Craigslist
-- collector writes it (collectors/hamilton/fsbo/fsbo_craigslist.py). Any
-- call hit `relation "fsbo_listings" does not exist`.
--
-- Column set follows what the code actually reads/writes (see also the
-- CREATE TABLE appended to db/init.sql for fresh installs), using
-- db/init.sql conventions (SERIAL PK, TEXT/NUMERIC, TIMESTAMP DEFAULT NOW()).
-- No owner-PII columns (PII rule): listing content only.
--
-- Idempotent: safe to re-run on every `make dev` boot.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fsbo_listings (
    id              SERIAL PRIMARY KEY,
    property_id     INTEGER REFERENCES properties(id) ON DELETE SET NULL,
    county_id       TEXT NOT NULL DEFAULT 'hamilton',
    source          TEXT NOT NULL,
    listing_url     TEXT,
    listing_price   NUMERIC,
    description     TEXT,
    status          TEXT,
    days_on_market  INTEGER,
    listed_at       TIMESTAMP,
    scraped_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fsbo_listings_county ON fsbo_listings(county_id);
CREATE INDEX IF NOT EXISTS idx_fsbo_listings_property ON fsbo_listings(property_id);
CREATE INDEX IF NOT EXISTS idx_fsbo_listings_scraped ON fsbo_listings(scraped_at);
