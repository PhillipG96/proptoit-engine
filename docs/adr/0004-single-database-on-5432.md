# ADR-0004: Single Source-of-Truth Database (docker Postgres on 5432)

## Status: Accepted (2026-06-20)

## Context

The codebase drifted into a **split-brain** across two PostgreSQL instances:

- **5432** — the docker-compose Postgres (`postgres:16-alpine`, password in `.env`).
  Canonical schema per `db/init.sql` and [ADR-0003](0003-county-scoped-data-model.md):
  `county_id` slug, native `lat`/`lng` + `point` `geocoords_point`, **no PostGIS**.
  All collectors and `collectors/geocoding` write here.
- **5433** — an out-of-band *host* Postgres (`postgresql@16-main`, password in `.env`)
  carrying the **old single-county schema**: `county` display name, PostGIS `geometry`.

On 2026-06-19, commit `d1fe419` ("Fix app runtime issues") repointed the live API
from 5432 → 5433 and rewrote `api/core/queries.py` to use PostGIS `ST_X`/`ST_Y`,
because the app's geo queries failed against the PostGIS-free 5432 DB. `d513b9e`
then moved the assessor scraper to 5433, and `e1792dc` wrote its distress signals
there. The app, the freshest writes, and two **test** users ended up on 5433,
while `db/init.sql`, docker-compose, the geocoding package, `opt_out`, `saved_properties`,
and `two_factor` stayed on 5432.

An enrichment audit showed **5432 is the materially richer database**: 99% `lat`/`lng`
(vs 5% on 5433), 96% `owner_mailing_address` (vs 0%), 100% `postal_code` (vs 0%),
plus the per-property distress flags, foreclosures, the quarterly price index, and
the real user account. 5433's apparent lead in `distress_signals` (8,985 rows) was
95% duplicate `probate` noise — a consequence of 5433 lacking the
`(property_id, signal_type)` unique constraint that 5432 enforces.

## Decision

**The docker-compose Postgres on 5432 is the single source of truth.** The live app,
all collectors, and all scripts target it. We do **not** use PostGIS; geospatial
reads use the native `lat`/`lng` columns and distance math is done in Python
(consistent with the existing Euclidean approach in the Harris collectors).

The out-of-band 5433 host DB is decommissioned (kept cold as a temporary backup only).

## Rationale

- 5432 matches the version-controlled schema (`db/init.sql`) and ADR-0002/0003 (`county_id`, multi-county).
- 5432 is the more enriched dataset on every field that drives the product.
- Native `lat`/`lng` give 99% map coverage vs 5% via PostGIS geometry — simpler *and* better.
- A reproducible, containerized DB beats a hand-provisioned host service.
- Eliminates the class of bug where writers and readers silently target different DBs
  (e.g. opt-out requests landing in a DB the API never reads).

## Consequences

- `api/core/queries.py` reverted off PostGIS: `ST_Y/ST_X(geocoords_point)` → `p.lat`/`p.lng`,
  `LOWER(p.county) = LOWER(%s)` → `p.county_id = %s`.
- All app + collector DSN defaults point at 5432; `proptoit-fastapi.service` sets
  `DATABASE_URL` explicitly so the config is reproducible.
- **Deploy step (not in this PR):** the installed unit at `/etc/systemd/system/proptoit-fastapi.service`
  must have its `DATABASE_URL` updated to 5432 and the service restarted. The stray
  hermes-set 5433 env var must be removed.
- Data unique to 5433 (2 test users, noisy probate signals) is intentionally **not** migrated.
- Re-running geocoding/collectors now enriches the DB the app actually serves.
