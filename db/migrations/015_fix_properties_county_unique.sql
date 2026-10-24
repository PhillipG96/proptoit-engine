-- 015_fix_properties_county_unique.sql
-- ---------------------------------------------------------------------------
-- Fix: properties carried BOTH UNIQUE(parcel_id) and UNIQUE(county_id, parcel_id).
-- The single-column UNIQUE(parcel_id) is a Hamilton-only leftover. It is strictly
-- stronger than the composite key and breaks multi-county ingestion: an
-- `ON CONFLICT (parcel_id)` upsert from a second county would silently DO UPDATE
-- the first county's row instead of inserting the new county's row (no error).
--
-- This migration:
--   1. Ensures the composite UNIQUE(county_id, parcel_id) exists (the correct key).
--   2. Drops the stale single-column UNIQUE(parcel_id).
--
-- Safe: verified no cross-county parcel_id duplicates exist in dev or prod.
-- Idempotent: safe to run on both dev (5432) and prod (5433).
-- ---------------------------------------------------------------------------

BEGIN;

-- 1. Add composite unique if missing (no-op on dev, where it already exists)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_properties_county_parcel'
    ) THEN
        ALTER TABLE properties
            ADD CONSTRAINT uq_properties_county_parcel UNIQUE (county_id, parcel_id);
    END IF;
END $$;

-- 2. Drop the stale single-column unique (redundant + dangerous)
ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_parcel_id_key;

COMMIT;
