-- ============================================================
-- Skip trace vendor columns + audit table
-- ============================================================
-- Adds vendor-ready columns to skip_trace_results so the
-- Next.js route (/api/skip-trace) can query them directly.
-- Also creates skip_trace_lookups for compliance audit logging
-- and opt_out_requests for TCPA opt-out tracking.

-- 1. Add vendor columns to skip_trace_results
ALTER TABLE skip_trace_results
    ADD COLUMN IF NOT EXISTS county_id    TEXT,
    ADD COLUMN IF NOT EXISTS parcel_id    TEXT,
    ADD COLUMN IF NOT EXISTS vendor_name  TEXT DEFAULT 'assessor_bulk',
    ADD COLUMN IF NOT EXISTS owner_phone  TEXT,
    ADD COLUMN IF NOT EXISTS owner_email  TEXT,
    ADD COLUMN IF NOT EXISTS relatives    JSONB,
    ADD COLUMN IF NOT EXISTS status       TEXT DEFAULT 'success',
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS expires_at   TIMESTAMPTZ;

-- Backfill county_id and parcel_id from the properties FK
UPDATE skip_trace_results st
SET
    county_id    = p.county_id,
    parcel_id    = p.parcel_id
FROM properties p
WHERE st.property_id = p.id::TEXT
  AND (st.county_id IS NULL OR st.parcel_id IS NULL);

-- Map existing phone_number -> owner_phone (vendor column)
UPDATE skip_trace_results
SET owner_phone = phone_number
WHERE phone_number IS NOT NULL AND owner_phone IS NULL;

-- Map existing email -> owner_email (vendor column) — already the same column name
-- (email column exists in both, no rename needed)

-- 2. Create skip_trace_lookups audit table
CREATE TABLE IF NOT EXISTS skip_trace_lookups (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    county_id   TEXT NOT NULL,
    parcel_id   TEXT NOT NULL,
    result_id   UUID REFERENCES skip_trace_results(id),
    looked_up_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_skip_trace_lookups_user
    ON skip_trace_lookups(user_id, looked_up_at DESC);
CREATE INDEX IF NOT EXISTS idx_skip_trace_lookups_parcel
    ON skip_trace_lookups(county_id, parcel_id);

-- 3. Create opt_out_requests table
CREATE TABLE IF NOT EXISTS opt_out_requests (
    id          BIGSERIAL PRIMARY KEY,
    county_id   TEXT NOT NULL DEFAULT 'hamilton',
    parcel_id   TEXT NOT NULL,
    owner_name  TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source      TEXT,
    UNIQUE (county_id, parcel_id, owner_name)
);

CREATE INDEX IF NOT EXISTS idx_opt_out_parcel
    ON opt_out_requests(county_id, parcel_id);

-- 4. Add composite unique index for vendor cache dedup
-- (vendor_name + county_id + parcel_id = one active result per vendor)
CREATE UNIQUE INDEX IF NOT EXISTS idx_skip_trace_vendor_unique
    ON skip_trace_results(county_id, parcel_id, vendor_name)
    WHERE county_id IS NOT NULL AND vendor_name IS NOT NULL;
