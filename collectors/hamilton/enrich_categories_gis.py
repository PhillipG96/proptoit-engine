#!/usr/bin/env python3
"""
Enrich property_category from Hamilton County GIS API.

Matches our simple-format parcel_ids (e.g. "001 002") to GIS TAX_MAP_NO
(e.g. "001  002") via double-space normalization.

For alphanumeric-format parcels (e.g. "158H A 005"), falls back to
heuristic classification using bedrooms, sqft, value, and address patterns.

CURRENTUSE → property_category mapping:
  RS  → residential   (single-family)
  CO  → commercial
  EX  → exempt
  MF  → residential   (multi-family)
  IN  → commercial    (industrial)
  AG  → agricultural
  DU  → residential   (duplex)
  0MR → residential
  0CR → residential
  0RI → residential
  ECR → exempt
  OSAP→ exempt
  ECL → exempt
  0EC → commercial
  0CE → commercial
  0AC → commercial
  ECRC→ commercial
  EID → commercial
  (empty) → unknown (use heuristics)
"""

import json
import subprocess
import sys
import time
from urllib.parse import quote

# GIS API config
GIS_BASE = "https://mapsdev.hamiltontn.gov/hcwa03/rest/services/Live_Parcels/MapServer/0/query"
BATCH_SIZE = 1000  # maxRecordCount from server info
QUERY_DELAY = 0.5  # seconds between batches to avoid rate limiting

# DB config (use Docker exec for trust auth)
DB_CONTAINER = "proptoit-postgis"
DB_USER = "proptoit"
DB_NAME = "proptoit"

# CURRENTUSE → property_category mapping
USE_CODE_MAP = {
    "RS":  "residential",
    "MF":  "residential",
    "DU":  "residential",
    "0MR": "residential",
    "0CR": "residential",
    "0RI": "residential",
    "CO":  "commercial",
    "IN":  "commercial",
    "0EC": "commercial",
    "0CE": "commercial",
    "0AC": "commercial",
    "ECRC": "commercial",
    "EID": "commercial",
    "EX":  "exempt",
    "ECR": "exempt",
    "OSAP": "exempt",
    "ECL": "exempt",
    "AG":  "agricultural",
}


def docker_psql(query, tty=False):
    """Run psql inside the Docker container (trust auth)."""
    cmd = ["docker", "exec", DB_CONTAINER, "psql", "-v", "ON_ERROR_STOP=1", "-U", DB_USER, "-d", DB_NAME]
    if tty:
        cmd.append("-t")
    cmd += ["-c", query]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr[:300]}")
    return result.stdout.strip()


def get_simple_parcel_ids():
    """Get all simple-format parcel_ids from the DB."""
    result = docker_psql("""
        SELECT parcel_id FROM properties
        WHERE parcel_id ~ '^[0-9]{1,3} [0-9]{1,3}(\.[0-9]+)?$'
        ORDER BY parcel_id
    """)
    return [line.strip() for line in result.split('\n') if line.strip()]


def gis_query_batch(parcel_ids):
    """Query GIS API for a batch of parcel IDs.
    
    Converts our single-space format to GIS double-space format.
    Returns dict: {normalized_tax_map_no: current_use}
    """
    # Convert single-space to double-space for GIS
    gis_ids = [pid.replace(' ', '  ') for pid in parcel_ids]
    where_parts = ' OR '.join([f"TAX_MAP_NO = '{gid}'" for gid in gis_ids])
    
    url = f"{GIS_BASE}?where={quote(where_parts)}&outFields=CURRENTUSE%2CTAX_MAP_NO&returnGeometry=false&f=json"
    
    result = subprocess.run(
        ["curl", "-s", "--connect-timeout", "30", "--max-time", "60", url],
        capture_output=True, text=True
    )
    
    if result.returncode != 0:
        print(f"  ⚠ curl failed: {result.stderr[:200]}")
        return {}
    
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  ⚠ JSON parse failed")
        return {}
    
    results = {}
    for feature in data.get("features", []):
        attrs = feature["attributes"]
        tax_map_no = attrs.get("TAX_MAP_NO", "")
        current_use = attrs.get("CURRENTUSE", "")
        # Normalize back to single-space for our DB
        normalized = tax_map_no.replace('  ', ' ')
        results[normalized] = current_use
    
    return results


def fetch_all_gis_data(parcel_ids):
    """Fetch CURRENTUSE for all parcel IDs in batches."""
    all_results = {}
    total = len(parcel_ids)
    num_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    
    print(f"Fetching GIS data for {total:,} parcels in {num_batches} batches...")
    
    for i in range(0, total, BATCH_SIZE):
        batch = parcel_ids[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        
        results = gis_query_batch(batch)
        all_results.update(results)
        
        # Progress every 10 batches
        if batch_num % 10 == 0 or batch_num == num_batches:
            pct = min(i + BATCH_SIZE, total) / total * 100
            print(f"  Batch {batch_num}/{num_batches} ({pct:.0f}%) — {len(all_results):,} matches so far")
        
        time.sleep(QUERY_DELAY)
    
    return all_results


def apply_gis_data(gis_results):
    """Apply GIS CURRENTUSE data to the database via COPY."""
    # Build SQL UPDATE statements
    updates = []
    for parcel_id, current_use in gis_results.items():
        category = USE_CODE_MAP.get(current_use, "unknown")
        updates.append((parcel_id, current_use, category))
    
    if not updates:
        print("No GIS data to apply.")
        return 0
    
    # Use a single UPDATE with a CASE expression for efficiency
    case_parts = []
    for parcel_id, current_use, category in updates:
        # Escape single quotes in parcel_id (shouldn't have any, but safe)
        safe_pid = parcel_id.replace("'", "''")
        case_parts.append(f"WHEN '{safe_pid}' THEN '{category}'")
    
    case_expr = "CASE parcel_id " + " ".join(case_parts) + " END"
    
    # Also build the current_use SET
    case_parts_use = []
    for parcel_id, current_use, _ in updates:
        safe_pid = parcel_id.replace("'", "''")
        safe_use = current_use.replace("'", "''")
        case_parts_use.append(f"WHEN '{safe_pid}' THEN '{safe_use}'")
    
    case_expr_use = "CASE parcel_id " + " ".join(case_parts_use) + " END"
    
    # Build WHERE clause
    pids = [u[0].replace("'", "''") for u in updates]
    where_list = ", ".join([f"'{p}'" for p in pids])
    
    query = f"""
        UPDATE properties
        SET property_category = {case_expr},
            current_use = {case_expr_use},
            updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND parcel_id IN ({where_list});
    """
    
    docker_psql(query, tty=False)
    print(f"  Applied {len(updates):,} GIS records to database")
    return len(updates)


def classify_by_heuristics():
    """Classify alphanumeric-format parcels using heuristics.
    
    Rules (in priority order):
    1. bedrooms > 0 → residential
    2. building_sqft < 2000 + assessed_value < 300000 → residential
    3. building_sqft >= 3000 OR assessed_value >= 500000 → commercial
    4. Vacant land (sqft=0) + value < 100000 → residential
    5. Vacant land + value >= 100000 → commercial
    6. Address has SUITE/STE/BUILDING → commercial
    7. Default: leave as unknown
    """
    counts = {}
    
    # Rule 1: bedrooms → residential
    r = docker_psql("""
        UPDATE properties
        SET property_category = 'residential', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category IS NULL
          AND bedrooms IS NOT NULL AND bedrooms > 0;
    """)
    counts['bedrooms'] = int(r) if r.isdigit() else 0
    
    # Rule 2: Small building + moderate value → residential
    r = docker_psql("""
        UPDATE properties
        SET property_category = 'residential', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category IS NULL
          AND building_sqft IS NOT NULL
          AND building_sqft > 0
          AND building_sqft < 2000
          AND assessed_value IS NOT NULL
          AND assessed_value < 300000;
    """)
    counts['small_mod'] = int(r) if r.isdigit() else 0
    
    # Rule 3: Large building or high value → commercial
    r = docker_psql("""
        UPDATE properties
        SET property_category = 'commercial', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category IS NULL
          AND (
              (building_sqft IS NOT NULL AND building_sqft >= 3000)
              OR (assessed_value IS NOT NULL AND assessed_value >= 500000)
          );
    """)
    counts['large'] = int(r) if r.isdigit() else 0
    
    # Rule 4: Vacant land + low value → residential
    r = docker_psql("""
        UPDATE properties
        SET property_category = 'residential', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category IS NULL
          AND building_sqft = 0
          AND assessed_value IS NOT NULL
          AND assessed_value < 100000;
    """)
    counts['land_low'] = int(r) if r.isdigit() else 0
    
    # Rule 5: Vacant land + high value → commercial
    r = docker_psql("""
        UPDATE properties
        SET property_category = 'commercial', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category IS NULL
          AND building_sqft = 0
          AND assessed_value IS NOT NULL
          AND assessed_value >= 100000;
    """)
    counts['land_high'] = int(r) if r.isdigit() else 0
    
    # Rule 6: Suite/Building address → commercial
    r = docker_psql("""
        UPDATE properties
        SET property_category = 'commercial', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category IS NULL
          AND (
            address ILIKE '% SUITE%'
            OR address ILIKE '% STE %'
            OR address ILIKE '% BUILDING%'
            OR address ILIKE '% CENTER%'
            OR address ILIKE '% COMPLEX%'
          );
    """)
    counts['suite'] = int(r) if r.isdigit() else 0
    
    return counts


def show_distribution():
    """Print final category distribution."""
    result = docker_psql("""
        SELECT property_category, COUNT(*) as cnt,
               ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 1) as pct
        FROM properties
        WHERE county_id = 'hamilton'
        GROUP BY property_category
        ORDER BY cnt DESC
    """)
    
    print(f"\n{'Category':<20} {'Count':>10} {'%':>6}")
    print(f"{'─' * 38}")
    for line in result.split('\n'):
        parts = line.split('|')
        if len(parts) == 3:
            print(f"{parts[0].strip():<20} {parts[1].strip():>10} {parts[2].strip():>6}")


def main():
    print("=" * 60)
    print("Hamilton County Property Category Enrichment")
    print("=" * 60)
    
    # Pre-enrichment stats
    total = int(docker_psql("SELECT COUNT(*) FROM properties WHERE county_id='hamilton';"))
    unknown = int(docker_psql("SELECT COUNT(*) FROM properties WHERE county_id='hamilton' AND property_category IS NULL;"))
    print(f"\nTotal: {total:,}  Unknown: {unknown:,} ({unknown*100.0/total:.1f}%)")
    
    # Step 1: GIS enrichment for simple-format parcels
    print("\n--- Step 1: GIS CURRENTUSE enrichment ---")
    parcel_ids = get_simple_parcel_ids()
    print(f"Found {len(parcel_ids):,} simple-format parcels")
    
    gis_results = fetch_all_gis_data(parcel_ids)
    print(f"GIS returned {len(gis_results):,} records")
    
    # Show CURRENTUSE distribution from GIS
    use_counts = {}
    for use in gis_results.values():
        use_counts[use] = use_counts.get(use, 0) + 1
    print(f"\nCURRENTUSE distribution from GIS:")
    for code, cnt in sorted(use_counts.items(), key=lambda x: -x[1]):
        print(f"  {code!r}: {cnt:,}")
    
    applied = apply_gis_data(gis_results)
    print(f"Applied {applied:,} GIS records")
    
    # Step 2: Heuristic classification for remaining unknowns
    print("\n--- Step 2: Heuristic classification ---")
    counts = classify_by_heuristics()
    for rule, cnt in counts.items():
        print(f"  {rule}: {cnt:,}")
    
    # Post-enrichment stats
    print("\n--- Results ---")
    unknown_after = int(docker_psql("SELECT COUNT(*) FROM properties WHERE county_id='hamilton' AND property_category IS NULL;"))
    print(f"Remaining unknown: {unknown_after:,}")
    
    show_distribution()
    print("\nDone!")


if __name__ == "__main__":
    main()
