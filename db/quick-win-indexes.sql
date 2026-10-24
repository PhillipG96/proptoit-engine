-- Quick-win DB indexes for PropToIt speed improvements.
-- Run: PGPASSWORD=*** psql -h localhost -p 5432 -U proptoit -d proptoit -f db/quick-win-indexes.sql
-- NB: ll_to_earth() lives in earthdistance, which requires cube — create them first.

CREATE EXTENSION IF NOT EXISTS cube;
CREATE EXTENSION IF NOT EXISTS earthdistance;

-- 1. Spatial index for earthdistance (<@>/<->) queries.
-- NB: ll_to_earth() returns cube; cube ships ONLY GiST opclasses
-- (cube_point_ops). A GIN index here fails outright
-- ("data type cube has no default operator class").
CREATE INDEX IF NOT EXISTS idx_properties_lat_lng ON properties USING gist (
    ll_to_earth(lat, lng) cube_point_ops
);

-- 2. Index for distress signal lookups (property detail + search)
CREATE INDEX IF NOT EXISTS idx_distress_signals_property_id ON distress_signals (property_id, county_id);

-- 3. Index for distress signal type lookups
CREATE INDEX IF NOT EXISTS idx_distress_signals_type ON distress_signals (signal_type, county_id);

-- 4. Composite index for property search by county + distress score
CREATE INDEX IF NOT EXISTS idx_properties_county_distress ON properties (county_id, distress_score DESC);

-- 5. Index for property lookups by parcel_id (already exists via PK, but add county composite)
CREATE INDEX IF NOT EXISTS idx_properties_parcel_county ON properties (parcel_id, county_id);

-- 6. Index for appraisal comp lookups (last_sale_price, building_sqft, lat/lng)
CREATE INDEX IF NOT EXISTS idx_properties_appraisal ON properties (county_id, last_sale_price, building_sqft, lat, lng)
WHERE last_sale_price IS NOT NULL AND last_sale_price > 0 AND lat IS NOT NULL AND lng IS NOT NULL;

-- 7. Index for top deals query (distress_score, deal_score)
CREATE INDEX IF NOT EXISTS idx_properties_deal_score ON properties (deal_score DESC, distress_score DESC);

-- 8. Index for FSBO lookups
CREATE INDEX IF NOT EXISTS idx_properties_fsbo ON properties (county_id, fsbo_flag) WHERE fsbo_flag = true;

-- 9. Index for pre-foreclosure lookups
CREATE INDEX IF NOT EXISTS idx_properties_pre_foreclosure ON properties (county_id, pre_foreclosure_flag) WHERE pre_foreclosure_flag = true;

-- 10. Index for vacancy lookups
CREATE INDEX IF NOT EXISTS idx_properties_vacant ON properties (county_id, is_vacant) WHERE is_vacant = true;

-- 11. Index for tax delinquent lookups
CREATE INDEX IF NOT EXISTS idx_properties_tax_delinquent ON properties (county_id, is_tax_delinquent) WHERE is_tax_delinquent = true;

-- 12. GIN index for full-text address search (ILIKE)
CREATE INDEX IF NOT EXISTS idx_properties_address_gin ON properties USING gin (to_tsvector('english', address));

-- 13. Index for owner search (ILIKE)
CREATE INDEX IF NOT EXISTS idx_properties_owner_gin ON properties USING gin (to_tsvector('english', owner_of_record));

-- 14. Index for property category filtering
CREATE INDEX IF NOT EXISTS idx_properties_category ON properties (county_id, property_category);

-- 15. Index for assessed value range queries
CREATE INDEX IF NOT EXISTS idx_properties_assessed_value ON properties (county_id, assessed_value);

-- Verify indexes were created
SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN ('properties', 'distress_signals') AND schemaname = 'public' ORDER BY indexname;
