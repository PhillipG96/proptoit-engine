# ADR-0002: Multi-County Architecture — Location Filter

## Status: Accepted (2026-05-31)

## Context

PropToIt was built for Hamilton County only. The user wants to expand to 10 counties across FL, TN, GA, TX. Three architectural options were considered:

1. **Location filter** (Option A): One codebase, one DB, location dropdown. All data in same tables with `county_id` column.
2. **Per-county subdomain** (Option B): One codebase, one DB, `chattanooga.prop toit.com`, `houston.prop toit.com`.
3. **Per-county deployment** (Option C): Separate deployment per county, separate DB.

## Decision

**Option A: Location filter.** One codebase, one database, location dropdown in the UI. All queries scoped to `county_id`. URL updates to `/chattanooga`, `/houston`, etc. for shareability.

## Rationale

- **Fastest path to 10 counties** — minimal code changes, existing collectors become templates
- **Shared comp pool** — each county has its own comp pool but the infrastructure is unified
- **Single deployment** — one server, one DB, one codebase to maintain
- **Location filter is sufficient for 10 counties** — a dropdown handles 10 items fine; subdomains add complexity without clear benefit
- **Can add subdomains later** if needed — the `county_id` column already exists

## Consequences

- All existing tables need a `county_id` column added
- `parcel_id` is no longer a unique global key — composite key `(county_id, parcel_id)` required
- Comp analysis, deal scoring, FSBO all scoped to county
- Existing Hamilton County data gets `county_id = 'hamilton'`
- Frontend needs a location selector component
- URL routing needs county prefix or query parameter
