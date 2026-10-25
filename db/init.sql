-- Initial Setup for PropToIt MVP
-- Plain PostgreSQL (PostGIS unavailable on ARM64)

-- Create database
-- Database already created by docker-compose (POSTGRES_DB=proptoit)
-- This script is run inside the proptoit database context

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================================
-- Property table
-- ============================================================================

CREATE TABLE IF NOT EXISTS properties (
    id              SERIAL PRIMARY KEY,
    county_id       TEXT NOT NULL DEFAULT 'hamilton',
    parcel_id       TEXT NOT NULL,
    address         TEXT NOT NULL,
    city            TEXT DEFAULT 'Chattanooga',
    state           TEXT DEFAULT 'TN',
    postal_code     TEXT,
    lot_size_sqft   NUMERIC,
    building_sqft   NUMERIC,
    year_built      INTEGER,
    bedrooms        INTEGER,
    bathrooms       NUMERIC,
    last_sale_price NUMERIC,
    last_sale_date  DATE,
    assessed_value  NUMERIC,
    tax_amount      NUMERIC,
    owner_of_record TEXT,
    owner_mailing_address TEXT,
    owner_occupant  BOOLEAN DEFAULT FALSE,
    updated_at      TIMESTAMP DEFAULT NOW(),
    created_at      TIMESTAMP DEFAULT NOW(),
    CONSTRAINT uq_properties_county_parcel UNIQUE (county_id, parcel_id)
);

CREATE INDEX IF NOT EXISTS idx_properties_county ON properties(county_id);
CREATE INDEX IF NOT EXISTS idx_properties_parcel_id ON properties(parcel_id);
CREATE INDEX IF NOT EXISTS idx_properties_address ON properties(address);
CREATE INDEX IF NOT EXISTS idx_properties_assessed_value ON properties(assessed_value);

-- ============================================================================
-- Distress signals table
-- ============================================================================

CREATE TABLE IF NOT EXISTS distress_signals (
    id              SERIAL PRIMARY KEY,
    property_id     INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    signal_type     TEXT NOT NULL,
    detected_at     TIMESTAMP DEFAULT NOW(),
    source_url      TEXT,
    notes           TEXT,
    UNIQUE(property_id, signal_type)
);

CREATE INDEX IF NOT EXISTS idx_distress_signals_property ON distress_signals(property_id);
CREATE INDEX IF NOT EXISTS idx_distress_signals_type ON distress_signals(signal_type);

-- ============================================================================
-- Probate notices — raw legal-notice records used to produce probate signals
-- ============================================================================

CREATE TABLE IF NOT EXISTS probate_notices (
    id              SERIAL PRIMARY KEY,
    case_number     TEXT NOT NULL UNIQUE,
    decedent_name   TEXT NOT NULL,
    filing_date     TEXT,
    death_date      TEXT,
    personal_rep    TEXT,
    raw_context     TEXT,
    scraped_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_probate_notices_decedent ON probate_notices(decedent_name);
CREATE INDEX IF NOT EXISTS idx_probate_notices_scraped ON probate_notices(scraped_at);

-- ============================================================================
-- Foreclosures table — auction data from Hamilton County
-- ============================================================================

CREATE TABLE IF NOT EXISTS foreclosures (
    id                  SERIAL PRIMARY KEY,
    property_id         INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    auction_date        DATE,
    auction_location    TEXT,
    min_bid             NUMERIC,
    status              TEXT DEFAULT 'upcoming',
    source              TEXT,
    auction_url         TEXT,
    recorded_at         TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_foreclosures_property ON foreclosures(property_id);
CREATE INDEX IF NOT EXISTS idx_foreclosures_auction_date ON foreclosures(auction_date);
CREATE INDEX IF NOT EXISTS idx_foreclosures_status ON foreclosures(status);

-- ============================================================================
-- Data provenance table — tracks where each record came from
-- ============================================================================

CREATE TABLE IF NOT EXISTS data_provenance (
    id              SERIAL PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    source          TEXT NOT NULL,
    extracted_at    TIMESTAMP DEFAULT NOW(),
    raw_data        JSONB
);

CREATE INDEX IF NOT EXISTS idx_provenance_entity ON data_provenance(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_provenance_source ON data_provenance(source);

-- ============================================================================
-- Add missing columns to existing tables (idempotent)
-- ============================================================================

-- properties: county identity + native geocoords
DO $$ BEGIN
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS county_id TEXT NOT NULL DEFAULT 'hamilton';
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION;
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS lng DOUBLE PRECISION;
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS geocoords_point POINT;
END $$;

DO $$
BEGIN
    ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_parcel_id_key;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_properties_county_parcel'
    ) THEN
        ALTER TABLE properties ADD CONSTRAINT uq_properties_county_parcel
            UNIQUE (county_id, parcel_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_properties_county ON properties(county_id);

-- distress_signals: source, severity, confidence, metadata (for pipeline enrichment)
DO $$ BEGIN
    ALTER TABLE distress_signals ADD COLUMN IF NOT EXISTS source TEXT;
    ALTER TABLE distress_signals ADD COLUMN IF NOT EXISTS severity TEXT DEFAULT 'medium';
    ALTER TABLE distress_signals ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;
    ALTER TABLE distress_signals ADD COLUMN IF NOT EXISTS metadata JSONB;
END $$;

-- properties: distress signal flags (for fast investor queries)
DO $$ BEGIN
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS pre_foreclosure_flag BOOLEAN DEFAULT FALSE;
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS code_violation_flag BOOLEAN DEFAULT FALSE;
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS probate_flag BOOLEAN DEFAULT FALSE;
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS fsbo_flag BOOLEAN DEFAULT FALSE;
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS vacancy_heuristic BOOLEAN DEFAULT FALSE;
END $$;

-- properties: distress_score computed from flags
DO $$ BEGIN
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS distress_score INTEGER DEFAULT 0;
END $$;

-- properties: deal_score composite (0-100), populated by calculate_deal_scores().
-- Declared before the category views below so their SELECT * exposes it.
DO $$ BEGIN
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS deal_score INTEGER DEFAULT 0;
END $$;

-- properties: is_vacant and is_tax_delinquent (already exist but ensure)
DO $$ BEGIN
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS is_vacant BOOLEAN DEFAULT FALSE;
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS is_tax_delinquent BOOLEAN DEFAULT FALSE;
END $$;

-- Indexes for distress signal columns
CREATE INDEX IF NOT EXISTS idx_properties_is_vacant ON properties(is_vacant) WHERE is_vacant = TRUE;
CREATE INDEX IF NOT EXISTS idx_properties_is_tax_delinquent ON properties(is_tax_delinquent) WHERE is_tax_delinquent = TRUE;
CREATE INDEX IF NOT EXISTS idx_properties_distress_score ON properties(distress_score) WHERE distress_score > 0;
CREATE INDEX IF NOT EXISTS idx_properties_deal_score ON properties(deal_score) WHERE deal_score > 0;
CREATE INDEX IF NOT EXISTS idx_properties_pre_foreclosure ON properties(pre_foreclosure_flag) WHERE pre_foreclosure_flag = TRUE;
CREATE INDEX IF NOT EXISTS idx_properties_code_violation ON properties(code_violation_flag) WHERE code_violation_flag = TRUE;
CREATE INDEX IF NOT EXISTS idx_properties_probate ON properties(probate_flag) WHERE probate_flag = TRUE;
CREATE INDEX IF NOT EXISTS idx_properties_fsbo ON properties(fsbo_flag) WHERE fsbo_flag = TRUE;

-- properties: property_category (residential|commercial|exempt|agricultural),
-- populated by the collectors' category enrichment (e.g. classify_hamilton).
DO $$ BEGIN
    ALTER TABLE properties ADD COLUMN IF NOT EXISTS property_category TEXT;
END $$;

-- ============================================================================
-- Property category views (backed by property_category column)
-- ============================================================================

CREATE OR REPLACE VIEW residential_properties AS
SELECT * FROM properties WHERE property_category = 'residential';

CREATE OR REPLACE VIEW commercial_properties AS
SELECT * FROM properties WHERE property_category = 'commercial';

CREATE OR REPLACE VIEW exempt_properties AS
SELECT * FROM properties WHERE property_category = 'exempt';

CREATE OR REPLACE VIEW agricultural_properties AS
SELECT * FROM properties WHERE property_category = 'agricultural';

-- ============================================================================
-- Opt-out registry — property owners can request removal from outreach lists
-- ============================================================================

CREATE TABLE IF NOT EXISTS opt_out_requests (
    id              SERIAL PRIMARY KEY,
    county_id       TEXT NOT NULL DEFAULT 'hamilton',
    owner_name      TEXT NOT NULL,
    email           TEXT,
    phone           TEXT,
    property_address TEXT,
    parcel_id       TEXT,
    reason          TEXT,
    requested_at    TIMESTAMP DEFAULT NOW(),
    processed_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_opt_out_county_parcel ON opt_out_requests(county_id, parcel_id);
CREATE INDEX IF NOT EXISTS idx_opt_out_owner ON opt_out_requests(owner_name);
CREATE INDEX IF NOT EXISTS idx_opt_out_parcel ON opt_out_requests(parcel_id);

-- ============================================================================
-- Appraisal — quarterly median $/sqft price index (per county)
-- Built by scripts/build_quarterly_index.py from our own sales; read by
-- api/core/appraisal.py to time-adjust comparable sales. Keep this definition
-- in sync with the CREATE TABLE in build_quarterly_index.py.
-- ============================================================================
CREATE TABLE IF NOT EXISTS quarterly_price_index (
    county_id   TEXT NOT NULL,
    quarter     TEXT NOT NULL,
    median_psf  NUMERIC(12,2),
    mean_psf    NUMERIC(12,2),
    n_sales     INTEGER,
    min_psf     NUMERIC(12,2),
    max_psf     NUMERIC(12,2),
    updated_at  TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (county_id, quarter)
);

-- ============================================================================
-- FSBO listings — For-Sale-By-Owner listings scraped from Craigslist
-- Chattanooga. Written by collectors/hamilton/fsbo/fsbo_craigslist.py
-- (DELETE+INSERT per run), read by api/core/queries.py list_fsbo_listings()
-- and the knowledge-layer assembler. property_id is a nullable best-effort
-- match to properties; listing content only — no owner PII (PII rule).
-- ============================================================================
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
