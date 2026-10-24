-- ============================================================
-- Phase 6 — Admin & Analytics
-- Merges investors into users, adds deal_events funnel + is_admin.
-- DEV (5432) ONLY — users.id is uuid here. Prod (5433) has an older
-- TEXT-id schema; use 014_admin_analytics_prod.sql there instead.
-- ============================================================

BEGIN;

-- 1. is_admin on users
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- 2. deal_events pipeline funnel
CREATE TABLE IF NOT EXISTS deal_events (
    id          SERIAL PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parcel_id   TEXT NOT NULL,
    county_id   TEXT NOT NULL DEFAULT 'hamilton',
    event_type  TEXT NOT NULL,  -- sent, viewed, pursued, closed
    created_at  TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, parcel_id, county_id, event_type)
);
CREATE INDEX IF NOT EXISTS idx_deal_events_user ON deal_events(user_id);
CREATE INDEX IF NOT EXISTS idx_deal_events_type ON deal_events(event_type);
CREATE INDEX IF NOT EXISTS idx_deal_events_created ON deal_events(created_at);

-- 3. Migrate investors -> users (by email). Create users rows for
--    investors that have no matching users row. Placeholder hash so the
--    row is valid; these accounts have no web login.
INSERT INTO users (id, email, password_hash, name, tier, subscription_status, created_at, updated_at)
SELECT
    gen_random_uuid(),
    i.email,
    '$2a$12$placeholderhashforinvestormigration',
    i.full_name,
    i.tier,
    CASE i.status WHEN 'trial' THEN 'trial' ELSE i.status END,
    i.created_at,
    NOW()
FROM investors i
WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.email = i.email);

-- 4. Re-point the three FK columns from int (investors.id) to uuid (users.id).
--    Build a mapping of old investor int id -> new users uuid (by email).
CREATE TEMP TABLE investor_map (
    old_id INTEGER PRIMARY KEY,
    new_id UUID NOT NULL
);
INSERT INTO investor_map (old_id, new_id)
SELECT i.id, u.id
FROM investors i
JOIN users u ON u.email = i.email;

-- investor_profiles: add temp uuid col, populate from map, drop old int col.
ALTER TABLE investor_profiles DROP CONSTRAINT investor_profiles_investor_id_fkey;
ALTER TABLE investor_profiles ADD COLUMN investor_id_new UUID;
UPDATE investor_profiles ip SET investor_id_new = m.new_id
FROM investor_map m WHERE ip.investor_id = m.old_id;
ALTER TABLE investor_profiles DROP COLUMN investor_id;
ALTER TABLE investor_profiles RENAME COLUMN investor_id_new TO investor_id;
ALTER TABLE investor_profiles ADD CONSTRAINT investor_profiles_investor_id_fkey
    FOREIGN KEY (investor_id) REFERENCES users(id) ON DELETE CASCADE;

-- deal_feedback
ALTER TABLE deal_feedback DROP CONSTRAINT deal_feedback_investor_id_fkey;
ALTER TABLE deal_feedback ADD COLUMN investor_id_new UUID;
UPDATE deal_feedback df SET investor_id_new = m.new_id
FROM investor_map m WHERE df.investor_id = m.old_id;
ALTER TABLE deal_feedback DROP COLUMN investor_id;
ALTER TABLE deal_feedback RENAME COLUMN investor_id_new TO investor_id;
ALTER TABLE deal_feedback ADD CONSTRAINT deal_feedback_investor_id_fkey
    FOREIGN KEY (investor_id) REFERENCES users(id) ON DELETE CASCADE;

-- digest_logs
ALTER TABLE digest_logs DROP CONSTRAINT digest_logs_investor_id_fkey;
ALTER TABLE digest_logs ADD COLUMN investor_id_new UUID;
UPDATE digest_logs dl SET investor_id_new = m.new_id
FROM investor_map m WHERE dl.investor_id = m.old_id;
ALTER TABLE digest_logs DROP COLUMN investor_id;
ALTER TABLE digest_logs RENAME COLUMN investor_id_new TO investor_id;
ALTER TABLE digest_logs ADD CONSTRAINT digest_logs_investor_id_fkey
    FOREIGN KEY (investor_id) REFERENCES users(id) ON DELETE CASCADE;

-- 5. Backfill deal_events from existing data.
--    sent: one row per parcel_id in digest_logs.parcel_ids
INSERT INTO deal_events (user_id, parcel_id, county_id, event_type, created_at)
SELECT dl.investor_id, p.parcel, 'hamilton', 'sent', dl.sent_at
FROM digest_logs dl
CROSS JOIN LATERAL jsonb_array_elements_text(dl.parcel_ids) AS p(parcel)
ON CONFLICT (user_id, parcel_id, county_id, event_type) DO NOTHING;

--    pursued: from deal_feedback where action = 'pursue'
INSERT INTO deal_events (user_id, parcel_id, county_id, event_type, created_at)
SELECT investor_id, parcel_id, county_id, 'pursued', created_at
FROM deal_feedback
WHERE action = 'pursue'
ON CONFLICT (user_id, parcel_id, county_id, event_type) DO NOTHING;

-- 6. Drop the old table.
DROP TABLE IF EXISTS investors;

COMMIT;
