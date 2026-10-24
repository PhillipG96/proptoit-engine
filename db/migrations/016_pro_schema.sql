-- 016_pro_schema.sql
-- ---------------------------------------------------------------------------
-- PropToIt Pro schema (Task 2.2 — carried forward from the Phase 1 carve).
--
-- The pro package's modules reference these tables but the carve shipped no
-- DDL for them: pro/investor_router.py (users, investor_profiles,
-- deal_feedback, digest_logs) and pro/deal_events.py (deal_events). Without
-- them the pro investor endpoints cannot run against a fresh stack.
--
-- Column sets follow what the pro code actually reads/writes
-- (pro/investor_models.py, pro/investor_router.py, pro/deal_events.py,
-- pro/matching.py), using db/init.sql conventions (SERIAL PKs, TEXT/NUMERIC,
-- TIMESTAMP DEFAULT NOW(), JSONB, gen_random_uuid via pgcrypto).
--
-- NOTE ON NAMING: the engine's pro code keys investors off the `users` table
-- (uuid PK, is_admin, subscription_status — see pro/investor_router.py's
-- INSERT INTO users / SELECT is_admin FROM users). There is no separate
-- `investors` table anywhere in pro/, which is why `users` appears here
-- where the carry-forward plan said "investors".
--
-- Array-typed preference columns (target_areas, property_types, …) are
-- TEXT[] because pro/investor_router.py passes raw Python lists via
-- psycopg2 (which adapts lists to Postgres arrays, not JSONB).
--
-- Idempotent: safe to re-run on every `make dev` boot.
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. users — investor identity + subscription state (investors ARE users
--    rows in this engine; referenced throughout pro/investor_router.py)
CREATE TABLE IF NOT EXISTS users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email               TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    name                TEXT,
    tier                TEXT NOT NULL DEFAULT 'basic',            -- basic, pro, premium
    subscription_status TEXT NOT NULL DEFAULT 'trial',            -- trial, active, cancelled
    is_admin            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- 2. investor_profiles — one per investor; the matching engine's criteria
--    (pro/matching.py reads these columns directly)
CREATE TABLE IF NOT EXISTS investor_profiles (
    id                   SERIAL PRIMARY KEY,
    investor_id          UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    deal_type            TEXT NOT NULL DEFAULT 'both',             -- flip, hold, both
    max_total_investment NUMERIC NOT NULL,                         -- purchase + rehab combined
    min_equity_pct       NUMERIC NOT NULL DEFAULT 20,              -- minimum equity spread %
    target_areas         TEXT[] NOT NULL DEFAULT '{}',             -- postal codes
    property_types       TEXT[] NOT NULL DEFAULT '{single_family}', -- single_family, multi_unit, land
    max_rehab_cost       NUMERIC DEFAULT 50000,
    preferred_signals    TEXT[] NOT NULL DEFAULT '{}',             -- distress signals to prefer
    county_ids           TEXT[] NOT NULL DEFAULT '{hamilton}',
    updated_at           TIMESTAMP DEFAULT NOW()
);

-- 3. deal_events — idempotent funnel events; the UNIQUE key below is the
--    ON CONFLICT target in pro/deal_events.py log_event()
CREATE TABLE IF NOT EXISTS deal_events (
    id          SERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parcel_id   TEXT NOT NULL,
    county_id   TEXT NOT NULL DEFAULT 'hamilton',
    event_type  TEXT NOT NULL,                                    -- sent, viewed, pursued, closed
    created_at  TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, parcel_id, county_id, event_type)
);

-- 4. deal_feedback — investor reactions to matched deals; the UNIQUE key
--    below is the ON CONFLICT target in pro/investor_router.py
CREATE TABLE IF NOT EXISTS deal_feedback (
    id          SERIAL PRIMARY KEY,
    investor_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parcel_id   TEXT NOT NULL,
    county_id   TEXT NOT NULL DEFAULT 'hamilton',
    action      TEXT NOT NULL,                                    -- pursue, pass, save, skip
    created_at  TIMESTAMP DEFAULT NOW(),
    UNIQUE(investor_id, parcel_id, county_id, action)
);

-- 5. digest_logs — records of periodic deal digests sent to investors
CREATE TABLE IF NOT EXISTS digest_logs (
    id              SERIAL PRIMARY KEY,
    investor_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sent_at         TIMESTAMP DEFAULT NOW(),
    deal_count      INTEGER DEFAULT 0,
    parcel_ids      JSONB NOT NULL DEFAULT '[]',                 -- array of {parcel_id, county_id}
    delivery_status TEXT DEFAULT 'pending'                       -- pending, sent, failed
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_deal_events_user ON deal_events(user_id);
CREATE INDEX IF NOT EXISTS idx_deal_events_parcel ON deal_events(county_id, parcel_id);
CREATE INDEX IF NOT EXISTS idx_deal_feedback_investor ON deal_feedback(investor_id);
CREATE INDEX IF NOT EXISTS idx_digest_logs_investor ON digest_logs(investor_id);
CREATE INDEX IF NOT EXISTS idx_digest_logs_sent_at ON digest_logs(sent_at);
