-- ============================================================
-- Phase 6 — Admin & Analytics (PROD variant)
-- Prod (5433) has an OLDER schema than dev:
--   * users.id is TEXT (not uuid)
--   * users has no subscription_status / is_admin / mcp_access
--   * the single investor (test@example.com) ALREADY exists in users
-- So: no UUID cast, no new users row. Add missing cols, create
-- deal_events (text user_id), re-point the 3 FKs to users.id (text),
-- backfill 'sent' events, drop investors.
-- ============================================================

BEGIN;

-- 1. Missing columns on users (prod lacks these)
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mcp_access BOOLEAN NOT NULL DEFAULT FALSE;

-- 2. deal_events (user_id is TEXT to match prod users.id)
CREATE TABLE IF NOT EXISTS deal_events (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parcel_id   TEXT NOT NULL,
    county_id   TEXT NOT NULL DEFAULT 'hamilton',
    event_type  TEXT NOT NULL,  -- sent, viewed, pursued, closed
    created_at  TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, parcel_id, county_id, event_type)
);
CREATE INDEX IF NOT EXISTS idx_deal_events_user ON deal_events(user_id);
CREATE INDEX IF NOT EXISTS idx_deal_events_type ON deal_events(event_type);
CREATE INDEX IF NOT EXISTS idx_deal_events_created ON deal_events(created_at);

-- 3. Re-point the 3 FK columns from investors.id (int) to users.id (text),
--    mapping by email. The investor already exists in users.
CREATE TEMP TABLE investor_map (
    old_id INTEGER PRIMARY KEY,
    new_id TEXT NOT NULL
);
INSERT INTO investor_map (old_id, new_id)
SELECT i.id, u.id
FROM investors i
JOIN users u ON u.email = i.email;

-- investor_profiles
ALTER TABLE investor_profiles DROP CONSTRAINT investor_profiles_investor_id_fkey;
ALTER TABLE investor_profiles ADD COLUMN investor_id_new TEXT;
UPDATE investor_profiles ip SET investor_id_new = m.new_id
FROM investor_map m WHERE ip.investor_id = m.old_id;
ALTER TABLE investor_profiles DROP COLUMN investor_id;
ALTER TABLE investor_profiles RENAME COLUMN investor_id_new TO investor_id;
ALTER TABLE investor_profiles ADD CONSTRAINT investor_profiles_investor_id_fkey
    FOREIGN KEY (investor_id) REFERENCES users(id) ON DELETE CASCADE;

-- deal_feedback
ALTER TABLE deal_feedback DROP CONSTRAINT deal_feedback_investor_id_fkey;
ALTER TABLE deal_feedback ADD COLUMN investor_id_new TEXT;
UPDATE deal_feedback df SET investor_id_new = m.new_id
FROM investor_map m WHERE df.investor_id = m.old_id;
ALTER TABLE deal_feedback DROP COLUMN investor_id;
ALTER TABLE deal_feedback RENAME COLUMN investor_id_new TO investor_id;
ALTER TABLE deal_feedback ADD CONSTRAINT deal_feedback_investor_id_fkey
    FOREIGN KEY (investor_id) REFERENCES users(id) ON DELETE CASCADE;

-- digest_logs
ALTER TABLE digest_logs DROP CONSTRAINT digest_logs_investor_id_fkey;
ALTER TABLE digest_logs ADD COLUMN investor_id_new TEXT;
UPDATE digest_logs dl SET investor_id_new = m.new_id
FROM investor_map m WHERE dl.investor_id = m.old_id;
ALTER TABLE digest_logs DROP COLUMN investor_id;
ALTER TABLE digest_logs RENAME COLUMN investor_id_new TO investor_id;
ALTER TABLE digest_logs ADD CONSTRAINT digest_logs_investor_id_fkey
    FOREIGN KEY (investor_id) REFERENCES users(id) ON DELETE CASCADE;

-- 4. Backfill 'sent' deal events from digest_logs.parcel_ids
INSERT INTO deal_events (user_id, parcel_id, county_id, event_type, created_at)
SELECT dl.investor_id, p.parcel, 'hamilton', 'sent', dl.sent_at
FROM digest_logs dl
CROSS JOIN LATERAL jsonb_array_elements_text(dl.parcel_ids) AS p(parcel)
ON CONFLICT (user_id, parcel_id, county_id, event_type) DO NOTHING;

-- 5. Drop the old table.
DROP TABLE IF EXISTS investors;

COMMIT;
