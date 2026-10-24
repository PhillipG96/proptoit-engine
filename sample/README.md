# Sample dataset — Hamilton County, TN (250 parcels)

PII-safe bundle for the open-source local stack. Loaded by `make dev`
(see `scripts/load_sample.py`), so you normally never touch this file.

## Contents

- `hamilton_subset.json` — 250 parcels + their distress-signal rows
  (150 with ≥1 signal across all six types: vacancy, pre_foreclosure,
  tax_delinquent, code_violation, fsbo, probate; 100 without signals).
  Value bands span <100k to >$1M; 194/250 rows have lat/lng.

## PII policy (hard rule)

- `owner_of_record` / `owner_mailing_address` are **always NULL** — names,
  phones, mailing addresses and emails never ship in this repo.
- `opt_out_requests` is shipped **empty** (the table exists in the schema so
  the API's opt-out path works, but it contains no rows).
- Distress-signal rows are public-record derived (auction notices, tax
  delinquency lists, code violations) and are included.
- The source export (`scripts/export_sample.sql`) checks the private
  opt-out registry and skips opted-out parcels defensively.

## Provenance / regeneration

The JSON is a deterministic (md5(id)-ordered) export of the private
Hamilton County production database, transformed to the open-source
schema in `db/init.sql`. See `scripts/export_sample.sql` for the exact
selection query. You do NOT need the private DB to use this repo —
`make dev` loads the committed JSON.
