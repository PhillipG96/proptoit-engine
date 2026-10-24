-- Export a PII-safe 250-parcel Hamilton County sample for the open-source repo.
--
-- Run against the PRIVATE production database (postgres:16 on localhost:5433):
--   sudo -u postgres psql -p 5433 -d proptoit -tA -f scripts/export_sample.sql \
--        > sample/hamilton_subset.json
--
-- Selection (deterministic, ordered by md5(id) so re-runs are stable):
--   * 94 parcels WITH distress signals and lat/lng  (all that have coords)
--   * 56 parcels WITH distress signals, no coords
--   * 100 parcels WITHOUT signals, stratified by assessed_value band
--     (<100k, 100-200k, 200-300k, 300-450k, 450k+ — 20 each, coords preferred)
--
-- PII rule (hard): owner_of_record / owner_mailing_address are forced to NULL.
-- The opt_out_requests registry is checked defensively; opted-out parcels are
-- never picked. Distress-signal rows are public-record derived and included.
-- Sweep of all signal metadata keys (2026-10-24) found exactly two person-name
-- keys, both on probate rows: 'decedent_name' and 'owner_of_record' (metadata
-- copy of the owner). Both are stripped below. 'death_date' and 'case_number'
-- are public court record and stay.
WITH dsig AS (
    -- one row per (property_id, signal_type): prod has duplicate signal rows
    -- (213 raw -> 207 distinct across the 250-parcel subset)
    SELECT DISTINCT ON (ds.property_id, ds.signal_type)
           ds.property_id,
           ds.signal_type::text AS signal_type,
           ds.detected_at,
           ds.source,
           ds.severity,
           ds.confidence,
           (ds.metadata - 'decedent_name' - 'owner_of_record') AS metadata
    FROM distress_signals ds
    ORDER BY ds.property_id, ds.signal_type, ds.detected_at DESC NULLS LAST
),
sig AS (
    SELECT d.property_id,
           json_agg(json_build_object(
               'signal_type', d.signal_type,
               'detected_at', d.detected_at,
               'source',      d.source,
               'severity',    d.severity,
               'confidence',  d.confidence,
               'metadata',    d.metadata
           ) ORDER BY d.signal_type) AS signals
    FROM dsig d
    GROUP BY 1
),
has_signal AS (
    SELECT DISTINCT ds.property_id
    FROM distress_signals ds
),
eligible AS (
    SELECT p.id, p.lat, p.assessed_value
    FROM properties p
    WHERE p.county_id = 'hamilton'
      AND NOT EXISTS (SELECT 1 FROM opt_out_requests o WHERE o.parcel_id = p.parcel_id)
),
picked AS (
    -- distress parcels with coordinates (all of them)
    (SELECT e.id FROM eligible e
     JOIN has_signal h ON h.property_id = e.id
     WHERE e.lat IS NOT NULL
     ORDER BY md5(e.id::text) LIMIT 94)
    UNION ALL
    -- distress parcels without coordinates
    (SELECT e.id FROM eligible e
     JOIN has_signal h ON h.property_id = e.id
     WHERE e.lat IS NULL
     ORDER BY md5(e.id::text) LIMIT 56)
    UNION ALL
    -- no-distress, stratified by assessed-value band, coordinates preferred
    (SELECT e.id FROM eligible e
     WHERE e.lat IS NOT NULL AND COALESCE(e.assessed_value, 0) < 100000
       AND NOT EXISTS (SELECT 1 FROM has_signal h WHERE h.property_id = e.id)
     ORDER BY md5(e.id::text) LIMIT 20)
    UNION ALL
    (SELECT e.id FROM eligible e
     WHERE e.lat IS NOT NULL AND COALESCE(e.assessed_value, 0) >= 100000 AND COALESCE(e.assessed_value, 0) < 200000
       AND NOT EXISTS (SELECT 1 FROM has_signal h WHERE h.property_id = e.id)
     ORDER BY md5(e.id::text) LIMIT 20)
    UNION ALL
    (SELECT e.id FROM eligible e
     WHERE e.lat IS NOT NULL AND COALESCE(e.assessed_value, 0) >= 200000 AND COALESCE(e.assessed_value, 0) < 300000
       AND NOT EXISTS (SELECT 1 FROM has_signal h WHERE h.property_id = e.id)
     ORDER BY md5(e.id::text) LIMIT 20)
    UNION ALL
    (SELECT e.id FROM eligible e
     WHERE e.lat IS NOT NULL AND COALESCE(e.assessed_value, 0) >= 300000 AND COALESCE(e.assessed_value, 0) < 450000
       AND NOT EXISTS (SELECT 1 FROM has_signal h WHERE h.property_id = e.id)
     ORDER BY md5(e.id::text) LIMIT 20)
    UNION ALL
    (SELECT e.id FROM eligible e
     WHERE e.lat IS NOT NULL AND COALESCE(e.assessed_value, 0) >= 450000
       AND NOT EXISTS (SELECT 1 FROM has_signal h WHERE h.property_id = e.id)
     ORDER BY md5(e.id::text) LIMIT 20)
)
SELECT json_agg(doc)
FROM (
    SELECT json_build_object(
        'county_id',            'hamilton',
        'parcel_id',            p.parcel_id,
        'address',              p.address,
        'city',                 p.city,
        'state',                p.state,
        'postal_code',          p.postal_code,
        'lot_size_sqft',        p.lot_size_sqft,
        'building_sqft',        p.building_sqft,
        'year_built',           p.year_built,
        'bedrooms',             p.bedrooms,
        'bathrooms',            p.bathrooms,
        'last_sale_price',      p.last_sale_price,
        'last_sale_date',       p.last_sale_date,
        'assessed_value',       p.assessed_value,
        'tax_amount',           p.tax_amount,
        'owner_of_record',      NULL::text,   -- PII: never ship owner names
        'owner_mailing_address',NULL::text,   -- PII: never ship mailing addresses
        'owner_occupant',       p.owner_occupant,
        'lat',                  p.lat,
        'lng',                  p.lng,
        'distress_score',       COALESCE(p.distress_score, 0),
        'deal_score',           COALESCE(p.deal_score, 0),
        'signals',              COALESCE(s.signals, '[]'::json)
    ) AS doc
    FROM picked pk
    JOIN properties p ON p.id = pk.id
    LEFT JOIN sig s ON s.property_id = p.id
) x;
