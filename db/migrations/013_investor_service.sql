-- ============================================================
-- Investor Service — Phase 1 Schema
-- ============================================================
-- Creates tables for investor profiles, deal matching,
-- feedback tracking, and digest delivery logging.

-- 1. investors — core investor identity and subscription state
CREATE TABLE IF NOT EXISTS investors (
    id              SERIAL PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    tier            TEXT NOT NULL DEFAULT 'basic',  -- basic, pro, premium
    status          TEXT NOT NULL DEFAULT 'trial',  -- trial, active, cancelled
    created_at      TIMESTAMP DEFAULT NOW(),
    trial_ends_at   TIMESTAMP,
    stripe_customer_id TEXT,
    full_name       TEXT
);

-- 2. investor_profiles — one per investor, stores deal preferences
CREATE TABLE IF NOT EXISTS investor_profiles (
    id              SERIAL PRIMARY KEY,
    investor_id     INTEGER NOT NULL REFERENCES investors(id) ON DELETE CASCADE,
    deal_type       TEXT NOT NULL DEFAULT 'both',  -- flip, hold, both
    max_total_investment NUMERIC NOT NULL,  -- purchase + rehab combined
    min_equity_pct  NUMERIC NOT NULL DEFAULT 20,  -- minimum equity spread %
    target_areas    JSONB NOT NULL DEFAULT '[]',  -- array of neighborhood/zip strings
    property_types  JSONB NOT NULL DEFAULT '["single_family"]',  -- single_family, multi_unit, land
    max_rehab_cost  NUMERIC DEFAULT 50000,
    preferred_signals JSONB NOT NULL DEFAULT '[]',  -- pre_foreclosure, probate, code_violation, etc.
    county_ids      JSONB NOT NULL DEFAULT '["hamilton"]',
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 3. deal_feedback — investor reactions to matched deals
CREATE TABLE IF NOT EXISTS deal_feedback (
    id              SERIAL PRIMARY KEY,
    investor_id     INTEGER NOT NULL REFERENCES investors(id) ON DELETE CASCADE,
    parcel_id       TEXT NOT NULL,
    county_id       TEXT NOT NULL DEFAULT 'hamilton',
    action          TEXT NOT NULL,  -- pursue, pass, save, skip
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(investor_id, parcel_id, county_id, action)
);

-- 4. digest_logs — records of periodic deal digests sent to investors
CREATE TABLE IF NOT EXISTS digest_logs (
    id              SERIAL PRIMARY KEY,
    investor_id     INTEGER NOT NULL REFERENCES investors(id) ON DELETE CASCADE,
    sent_at         TIMESTAMP DEFAULT NOW(),
    deal_count      INTEGER DEFAULT 0,
    parcel_ids      JSONB NOT NULL DEFAULT '[]',  -- array of {parcel_id, county_id}
    delivery_status TEXT DEFAULT 'pending'  -- pending, sent, failed
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_investor_profiles_investor ON investor_profiles(investor_id);
CREATE INDEX IF NOT EXISTS idx_deal_feedback_investor ON deal_feedback(investor_id);
CREATE INDEX IF NOT EXISTS idx_digest_logs_investor ON digest_logs(investor_id);
CREATE INDEX IF NOT EXISTS idx_digest_logs_sent_at ON digest_logs(sent_at);