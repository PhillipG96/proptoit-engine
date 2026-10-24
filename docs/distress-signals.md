# Distress Signals

Distress signals identify properties where owners may be motivated to sell. PropToIt tracks **6 signal types**, each with a confidence score and source provenance.

## Signal Types

| Signal | Status | Source | Confidence | Count |
|--------|---------|--------|------------|-------|
| Tax Delinquent | 🟢 Real | Hamilton County tax sale PDFs | 0.95 | 216 |
| Pre-Foreclosure | 🟡 Derived | Multi-signal heuristic | 0.80 | 38 |
| Code Violation | 🔴 Proxy | Age + value proxy | 0.60 | 47 |
| Probate | 🟢 Real | Legal notice scraper | 0.79 | 14 |
| Vacancy | 🟢 Heuristic | Multi-signal scoring | 0.55 | 393 |
| FSBO | 🟢 Real | Craigslist listings | 0.70 | 24 |

## Architecture

```
Public sources → collectors → distress_signals table → API → Frontend
```

Each signal type has a dedicated collector that:
1. **Fetches** data from a source (public record, scraper, or heuristic)
2. **Normalizes** it into a standard record format via `collectors/base.py`
3. **Writes** to the `distress_signals` table with source, confidence, and metadata

### Database Schema

```sql
CREATE TABLE distress_signals (
    id            SERIAL PRIMARY KEY,
    property_id   INTEGER REFERENCES properties(id),
    signal_type   VARCHAR NOT NULL,  -- tax_delinquent, pre_foreclosure, code_violation, probate, vacancy, fsbo
    detected_at   TIMESTAMP DEFAULT NOW(),
    source        VARCHAR,           -- data source identifier
    severity      VARCHAR DEFAULT 'medium',
    confidence    NUMERIC,            -- 0.0 to 1.0
    metadata      JSONB,              -- score breakdown, raw data, etc.
    UNIQUE(property_id, signal_type)
);

CREATE TABLE probate_notices (
    id            SERIAL PRIMARY KEY,
    case_number   TEXT NOT NULL UNIQUE,
    decedent_name TEXT NOT NULL,
    filing_date   TEXT,
    death_date    TEXT,
    personal_rep  TEXT,
    raw_context   TEXT,
    scraped_at    TIMESTAMP DEFAULT NOW()
);
```

### Collector Pipeline

```
collectors/hamilton/distress/
├── distress_signals.py      # Main orchestrator (runs all collectors)
├── vacancy.py               # Multi-signal vacancy detector
├── tax_delinquent.py        # County tax sale PDF parser
├── probate_scraper.py       # TFP legal notice scraper
├── foreclosure_auction.py   # County foreclosure listings
├── fsbo_craigslist.py       # Craigslist FSBO scraper
└── ...
```

Run all collectors:
```bash
python3 collectors/hamilton/distress/distress_signals.py --signal all
```

Run a single collector:
```bash
python3 collectors/hamilton/distress/distress_signals.py --signal vacancy
python3 collectors/hamilton/distress/vacancy.py
```

Dry run (preview without writing):
```bash
python3 collectors/hamilton/distress/distress_signals.py --dry-run
python3 collectors/hamilton/distress/vacancy.py --dry-run
```

## Signal Details

### Tax Delinquent 🟢
**Source:** Hamilton County annual delinquent tax sale PDFs
**Collector:** `tax_delinquent.py`
**Method:** Parses county PDFs, extracts parcel IDs, cross-references with properties table
**Confidence:** 0.95 (direct county data)
**Refresh:** Annual (PDF published with tax sale cycle)

### Pre-Foreclosure 🟡
**Source:** Derived from tax delinquent + multi-signal combination
**Method:** Properties with tax delinquency + additional distress signals
**Confidence:** 0.80
**Limitation:** No direct lis pendens access; relies on proxy signals

### Code Violation 🔴
**Source:** Age + value proxy (year_built < 1950, assessed_value < $50K)
**Method:** Identifies old, low-value buildings likely to have code issues
**Confidence:** 0.60
**Limitation:** No access to actual code enforcement records. Old building ≠ code violation.
**Target:** Chattanooga Code Enforcement records (Phase 2 priority)

### Probate 🟢
**Source:** Tennessee Financial Press (TFP) legal notices
**Collector:** `probate_scraper.py`
**Method:** Scrapes legal notices for probate proceedings, matches addresses to parcels
**Confidence:** 0.79
**Refresh:** Weekly

### Vacancy 🟢
**Source:** Multi-signal scoring heuristic
**Collector:** `vacancy.py`
**Method:** Weighted scoring across 6 indicators (see below)
**Confidence:** 0.55 average (range 0.50–0.85)
**Limitation:** No public vacancy data exists for Chattanooga

### FSBO 🟢
**Source:** Craigslist Chattanooga housing listings
**Collector:** `fsbo_craigslist.py`
**Method:** Scrapes "For Sale By Owner" listings, geo-matches to parcels
**Confidence:** 0.70
**Limitation:** Craigslist only. Missing Zillow FSBO, Facebook Marketplace, Backyard RE.

---

## Vacancy Detection (Deep Dive)

### Why No Real Data?

Chattanooga has **no public vacancy data source**:
- No USPS vacant address list (private data)
- No utility disconnect records (EPB doesn't publish)
- No trash collection non-participation records
- No county GIS vacancy indicators
- No satellite/aerial imagery analysis pipeline

The multi-signal heuristic is the **best available approach**.

### Scoring Model

| Indicator | Weight | Rationale |
|-----------|--------|-----------|
| Tax delinquent | +3 | Unpaid taxes strongly correlate with vacancy |
| Assessed value < $30K | +2 | Neglected properties |
| Not sold 15+ years | +2 | Long-held = disconnected owner |
| No owner mailing address | +1 | Owner disconnected |
| Code violation signal | +1 | Neglected property |
| Year built < 1940 | +1 | Pre-war construction, more likely neglected |

**Threshold:** Score ≥ 5 → vacancy signal created

**Confidence mapping:**
| Score | Confidence |
|-------|------------|
| 5 | 0.50 |
| 6 | 0.65 |
| 7 | 0.75 |
| 8+ | 0.85 |

### Configuration

```python
SCORE_THRESHOLD = 5        # minimum score to flag
LOW_VALUE_THRESHOLD = 30000  # assessed_value < $30K
NOT_SOLD_YEARS = 15         # not sold in 15+ years
OLD_BUILD_YEAR = 1940       # year_built < 1940
```

### Results

- **393 properties** flagged (vs 249 from old single-signal proxy)
- Average confidence: **0.55**
- Source: `vacancy_multi_signal`
- Score distribution varies by threshold calibration

### Running

```bash
# Standalone (recommended)
python3 collectors/hamilton/distress/vacancy.py

# Via orchestrator
python3 collectors/hamilton/distress/distress_signals.py --signal vacancy

# Dry run
python3 collectors/hamilton/distress/vacancy.py --dry-run
```

## API Integration

The API exposes distress signals through:

### Search Filters
```
GET /api/properties?distress_types=vacancy&distress_types=tax_delinquent
```

### Property Detail
```
GET /api/properties/{parcel_id}
→ response includes: isVacant, isTaxDelinquent, distressScore, dealScore
```

### Distress Signals Endpoint
```
GET /api/properties/{parcel_id}/signals
→ returns all signals for a property with source, confidence, metadata
```

### Response Fields

Each property includes:
```json
{
  "distressScore": 3,
  "isVacant": true,
  "isTaxDelinquent": false,
  "preForeclosureFlag": false,
  "taxDelinquentFlag": true,
  "codeViolationFlag": false,
  "probateFlag": false,
  "fsboFlag": false,
  "vacancyHeuristic": true
}
```

## Improving Signals

### Priority Roadmap

| Priority | Signal | Approach |
|----------|---------|----------|
| 1 | Tax Delinquent | ✅ Done — real county PDF data |
| 1 | Code Violation | County code enforcement records |
| 2 | Vacancy | ✅ Heuristic implemented — no real data available |
| 2 | Pre-Foreclosure | County clerk lis pendens access |
| 3 | FSBO | Expand beyond Craigslist |
| 3 | Probate | Scale up from TFP to court records |
| 4 | Evictions | County clerk eviction filings |
| 4 | Liens | Mechanic's lien records |

### Adding New Signals

1. Create collector in `collectors/hamilton/distress/`
2. Implement `fetch_<signal_type>(conn)` returning normalized records
3. Add to `SIGNAL_COLLECTORS` dict in `distress_signals.py`
4. Add signal type to `FLAG_TO_SIGNAL` in `api/core/queries.py`
5. Test with `--dry-run`, then run for real

### Deprecating Proxies

When a real data source replaces a proxy:
1. Keep the proxy collector but mark as DEPRECATED in docstring
2. Remove the proxy from `SIGNAL_COLLECTORS`
3. Delete old proxy signals from DB: `DELETE FROM distress_signals WHERE source = 'proxy_name'`
4. Update docs
