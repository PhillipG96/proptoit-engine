# ADR-0003: County-Scoped Data Model

## Status: Accepted (2026-05-31)

## Context

Parcel IDs are unique within a county but not across counties. `09500123456` in Hamilton County is a different property than `09500123456` in Davidson County. The existing schema uses `parcel_id` as a global unique key, which breaks with multi-county expansion.

## Decision

Add `county_id` (TEXT) as a required column on every data table. Use composite key `(county_id, parcel_id)` for uniqueness where applicable. All queries scoped to the selected county via the location filter.

## Rationale

- **Parcel IDs are county-local** — no way around this, county assessors use independent numbering
- **Composite key is explicit** — makes the county-scoping visible in the schema
- **Simple migration** — existing Hamilton County data gets `county_id = 'hamilton'`
- **No foreign key issues** — county_id is a TEXT slug, not a FK to a counties table (avoids join complexity)

## Consequences

- Every INSERT/UPDATE must include `county_id`
- All API endpoints filter by `county_id` from the location context
- Comp sales must be county-local (a comp in Miami doesn't apply to Nashville)
- Deal scores are calculated per-county
- FSBO Craigslist feeds are county-specific (different CL URLs)
- Street view cache directory structure: `data/street_views/{county_id}/{parcel_id}.jpg`
