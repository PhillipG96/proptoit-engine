-- 017: free-tier api_keys columns (code/schema reconciliation)
--
-- free_tier_auth.py reads/writes hourly_call_count, last_hourly_reset and
-- email_hash, but the live table never had them -> generate_free_key,
-- validate_free_key, _reset_hourly and get_all_keys_for_email all threw
-- UndefinedColumn on the live DB (reproduced 2026-10-09).
--
-- Additive + nullable (counter defaults to 0); existing keys are untouched.
-- Apply to 5432 (live per systemd EnvironmentFile) and 5433 if still used.

ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS email_hash        TEXT,
    ADD COLUMN IF NOT EXISTS hourly_call_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_hourly_reset TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_api_keys_email_hash ON api_keys (email_hash) WHERE email_hash IS NOT NULL;  -- matches api/scripts/migrate_free_tier.py
