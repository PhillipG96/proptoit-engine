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
-- Saved properties — user bookmarks
-- ============================================================================

CREATE TABLE IF NOT EXISTS saved_properties (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    county_id       TEXT NOT NULL DEFAULT 'hamilton',
    property_id     TEXT NOT NULL,  -- parcel ID
    saved_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, county_id, property_id)
);

CREATE INDEX IF NOT EXISTS idx_saved_properties_user ON saved_properties(user_id);
CREATE INDEX IF NOT EXISTS idx_saved_properties_county_property ON saved_properties(county_id, property_id);
CREATE INDEX IF NOT EXISTS idx_saved_properties_property ON saved_properties(property_id);

DO $$
BEGIN
    ALTER TABLE saved_properties ADD COLUMN IF NOT EXISTS county_id TEXT NOT NULL DEFAULT 'hamilton';
    ALTER TABLE saved_properties DROP CONSTRAINT IF EXISTS saved_properties_user_id_property_id_key;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'saved_properties_user_id_county_id_property_id_key'
    ) THEN
        ALTER TABLE saved_properties ADD CONSTRAINT saved_properties_user_id_county_id_property_id_key
            UNIQUE (user_id, county_id, property_id);
    END IF;
END $$;

-- ============================================================================
-- Skip-trace results — cached owner contact lookups from a third-party vendor.
-- Keyed on (county_id, parcel_id, vendor_name) and shared across users so a
-- Property is only looked up (and billed) once per vendor. Contact data is
-- gated behind the Investor tier + an opt-out check in the /api/skip-trace
-- route — never exposed through the public properties API.
-- ============================================================================

CREATE TABLE IF NOT EXISTS skip_trace_results (
    id              SERIAL PRIMARY KEY,
    property_id     INTEGER REFERENCES properties(id) ON DELETE CASCADE,
    county_id       TEXT NOT NULL DEFAULT 'hamilton',
    parcel_id       TEXT NOT NULL,
    vendor_name     TEXT NOT NULL,
    owner_phone     TEXT,
    owner_email     TEXT,
    relatives       JSONB,              -- [{ name, phone, email, relationship }]
    status          TEXT DEFAULT 'success',  -- success | no_match | opted_out
    raw_response    JSONB,
    requested_at    TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,        -- cache TTL (SKIP_TRACE_CACHE_DAYS)
    UNIQUE(county_id, parcel_id, vendor_name)
);

CREATE INDEX IF NOT EXISTS idx_skip_trace_county_parcel ON skip_trace_results(county_id, parcel_id);
CREATE INDEX IF NOT EXISTS idx_skip_trace_parcel ON skip_trace_results(parcel_id);
CREATE INDEX IF NOT EXISTS idx_skip_trace_expires ON skip_trace_results(expires_at);

DO $$
BEGIN
    ALTER TABLE skip_trace_results ADD COLUMN IF NOT EXISTS county_id TEXT NOT NULL DEFAULT 'hamilton';
    ALTER TABLE skip_trace_results DROP CONSTRAINT IF EXISTS skip_trace_results_parcel_id_vendor_name_key;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'skip_trace_results_county_id_parcel_id_vendor_name_key'
    ) THEN
        ALTER TABLE skip_trace_results ADD CONSTRAINT skip_trace_results_county_id_parcel_id_vendor_name_key
            UNIQUE (county_id, parcel_id, vendor_name);
    END IF;
END $$;

-- ============================================================================
-- Skip-trace lookups — compliance audit log: which user accessed which owner's
-- contact data, and when. One row per access (cache hit or miss).
-- ============================================================================

CREATE TABLE IF NOT EXISTS skip_trace_lookups (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    county_id       TEXT NOT NULL DEFAULT 'hamilton',
    parcel_id       TEXT NOT NULL,
    result_id       INTEGER REFERENCES skip_trace_results(id) ON DELETE SET NULL,
    accessed_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_skip_trace_lookups_user ON skip_trace_lookups(user_id);
CREATE INDEX IF NOT EXISTS idx_skip_trace_lookups_county_parcel ON skip_trace_lookups(county_id, parcel_id);
CREATE INDEX IF NOT EXISTS idx_skip_trace_lookups_parcel ON skip_trace_lookups(parcel_id);

DO $$
BEGIN
    ALTER TABLE opt_out_requests ADD COLUMN IF NOT EXISTS county_id TEXT NOT NULL DEFAULT 'hamilton';
    ALTER TABLE skip_trace_lookups ADD COLUMN IF NOT EXISTS county_id TEXT NOT NULL DEFAULT 'hamilton';
END $$;


-- ============================================================================
-- Auth users and MCP API keys
-- ============================================================================

CREATE TABLE IF NOT EXISTS users (
    id                      TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    email                   TEXT NOT NULL UNIQUE,
    password_hash           TEXT NOT NULL,
    name                    TEXT,
    tier                    TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id      TEXT UNIQUE,
    stripe_subscription_id  TEXT,
    subscription_status     TEXT,
    stripe_last_key_value   TEXT,
    mcp_access              BOOLEAN NOT NULL DEFAULT TRUE,
    email_verified_at       TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_stripe_customer ON users(stripe_customer_id);

CREATE TABLE IF NOT EXISTS api_keys (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash            TEXT NOT NULL UNIQUE,
    display_name        TEXT,
    tier                TEXT NOT NULL,
    last_used_at        TIMESTAMPTZ,
    revoked             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    monthly_call_count  INTEGER NOT NULL DEFAULT 0,
    last_reset_at       TIMESTAMPTZ,
    max_monthly_calls   INTEGER NOT NULL DEFAULT 50
);

CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_user_revoked ON api_keys(user_id, revoked);
CREATE INDEX IF NOT EXISTS idx_api_keys_last_reset ON api_keys(last_reset_at);

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_user ON email_verification_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_expires ON email_verification_tokens(expires_at);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user ON password_reset_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_expires ON password_reset_tokens(expires_at);


-- ============================================================================
-- Security — brute force protection
-- ============================================================================
CREATE TABLE IF NOT EXISTS failed_login_attempts (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email      TEXT NOT NULL,
    ip         TEXT,
    user_id    TEXT REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_failed_login_email ON failed_login_attempts(email, created_at);
CREATE INDEX IF NOT EXISTS idx_failed_login_user ON failed_login_attempts(user_id, created_at);

-- ============================================================================
-- Security — audit logging
-- ============================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT REFERENCES users(id) ON DELETE SET NULL,
    event      TEXT NOT NULL,
    details    TEXT,
    ip         TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);

-- ============================================================================
-- Security — two-factor auth
-- ============================================================================
CREATE TABLE IF NOT EXISTS two_factor (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    secret       TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT FALSE,
    backup_codes TEXT NOT NULL DEFAULT '[]',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- Stripe billing — link users to their Stripe customer/subscription so webhooks
-- can map subscription lifecycle events back to a user and adjust their tier.
-- (The users table itself is managed by the Next.js app; these are idempotent
-- ALTERs so a fresh init and an existing DB converge.)
-- ============================================================================

DO $$ BEGIN
    ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id     TEXT;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status    TEXT;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_last_key_value  TEXT;
EXCEPTION WHEN undefined_table THEN
    RAISE NOTICE 'users table not present yet — skipping Stripe columns';
END $$;

DO $$ BEGIN
    CREATE INDEX IF NOT EXISTS idx_users_stripe_customer ON users(stripe_customer_id);
EXCEPTION WHEN undefined_table THEN
    RAISE NOTICE 'users table not present yet — skipping Stripe indexes';
END $$;

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
