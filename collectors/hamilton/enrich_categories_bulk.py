#!/usr/bin/env python3
"""
Enrich property_category from Hamilton County GIS API.

Matches ALL parcel_ids to GIS MAP+GROUP_+PARCEL fields:
  - Simple: "001 002" → MAP="001", PARCEL="002"
  - Alphanumeric: "014K A 001" → MAP="014K", GROUP_="A", PARCEL="001"

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
BATCH_SIZE = 100  # Optimal batch size for POST queries
QUERY_DELAY = 0.1  # seconds between batches

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
    cmd = ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME]
    if tty:
        cmd.append("-t")
    cmd += ["-c", query]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr[:300]}")
    return result.stdout.strip()


def parse_parcel_id(parcel_id):
    """Parse parcel_id into (MAP, GROUP_, PARCEL) for GIS query.
    
    Simple format:  "001 002" → ("001", "", "002")
    Alphanumeric:   "014K A 001" → ("014K", "A", "001")
    Sub-parcels:    "001 003.01" → ("001", "", "003.01")
    """
    parts = parcel_id.split()
    
    if len(parts) == 2:
        # Simple: MAP PARCEL
        return (parts[0], "", parts[1])
    elif len(parts) == 3:
        # Alphanumeric: MAP GROUP_ PARCEL
        return (parts[0], parts[1], parts[2])
    else:
        # Edge case: try to handle
        return (parts[0], "", " ".join(parts[1:]))


def gis_query_batch(parcel_ids):
    """Query GIS API for a batch of parcel IDs using MAP+GROUP_+PARCEL.
    
    Returns dict: {parcel_id: current_use}
    """
    where_parts = []
    
    for pid in parcel_ids:
        map_num, group_num, parcel_num = parse_parcel_id(pid)
        safe_map = map_num.replace("'", "''")
        safe_group = group_num.replace("'", "''")
        safe_parcel = parcel_num.replace("'", "''")
        
        if group_num:
            # Alphanumeric: MAP + GROUP_ + PARCEL
            where_parts.append(
                f"(MAP = '{safe_map}' AND GROUP_ = '{safe_group}' AND PARCEL = '{safe_parcel}')"
            )
        else:
            # Simple: MAP + PARCEL (GROUP_ is empty)
            where_parts.append(
                f"(MAP = '{safe_map}' AND PARCEL = '{safe_parcel}')"
            )
    
    if not where_parts:
        return {}
    
    where_clause = ' OR '.join(where_parts)
    data = f"where={where_clause}&outFields=MAP%2CGROUP_%2CPARCEL%2CTAX_MAP_NO%2CCURRENTUSE&returnGeometry=false&f=json"
    
    result = subprocess.run(
        ["curl", "-s", "--connect-timeout", "30", "--max-time", "60",
         "-X", "POST", "-d", data, GIS_BASE],
        capture_output=True, text=True
    )
    
    if result.returncode != 0 or not result.stdout.startswith('{'):
        return {}
    
    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    
    results = {}
    for feature in d.get("features", []):
        attrs = feature["attributes"]
        map_num = attrs.get("MAP", "").strip()
        group_num = attrs.get("GROUP_", "").strip()
        parcel_num = attrs.get("PARCEL", "").strip()
        current_use = attrs.get("CURRENTUSE", "")
        
        # Reconstruct our parcel_id format
        if group_num:
            pid = f"{map_num} {group_num} {parcel_num}"
        else:
            pid = f"{map_num} {parcel_num}"
        
        results[pid] = current_use
    
    return results


def fetch_all_gis_data(parcel_ids):
    """Fetch CURRENTUSE for all parcel IDs in batches."""
    all_results = {}
    total = len(parcel_ids)
    num_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    
    print(f"  Fetching GIS data for {total:,} parcels in {num_batches} batches...")
    
    for i in range(0, total, BATCH_SIZE):
        batch = parcel_ids[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        
        results = gis_query_batch(batch)
        all_results.update(results)
        
        if batch_num % 20 == 0 or batch_num == num_batches:
            pct = min(i + BATCH_SIZE, total) / total * 100
            print(f"  Batch {batch_num}/{num_batches} ({pct:.0f}%) — {len(all_results):,} matches")
        
        time.sleep(QUERY_DELAY)
    
    return all_results


def apply_gis_data(gis_results):
    """Apply GIS CURRENTUSE data to the database."""
    if not gis_results:
        print("  No GIS data to apply.")
        return 0
    
    # Build UPDATE statements in chunks to avoid massive queries
    chunk_size = 200
    total_updated = 0
    
    for i in range(0, len(gis_results), chunk_size):
        chunk = list(gis_results.items())[i:i + chunk_size]
        
        # Build CASE expressions
        case_cat_parts = []
        case_use_parts = []
        pids = []
        
        for parcel_id, current_use in chunk:
            category = USE_CODE_MAP.get(current_use, "unknown")
            safe_pid = parcel_id.replace("'", "''")
            safe_use = current_use.replace("'", "''")
            case_cat_parts.append(f"WHEN '{safe_pid}' THEN '{category}'")
            case_use_parts.append(f"WHEN '{safe_pid}' THEN '{safe_use}'")
            pids.append(f"'{safe_pid}'")
        
        query = f"""
            UPDATE properties
            SET property_category = CASE parcel_id {' '.join(case_cat_parts)} END,
                current_use = CASE parcel_id {' '.join(case_use_parts)} END,
                updated_at = NOW()
            WHERE county_id = 'hamilton'
              AND parcel_id IN ({', '.join(pids)});
        """
        
        docker_psql(query, tty=False)
        total_updated += len(chunk)
    
    print(f"  Applied {total_updated:,} GIS records")
    return total_updated


def classify_by_heuristics():
    """Classify remaining unknown properties using heuristics."""
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
    
    # Step 0: Starting enrichment (no reset)
    print("\n--- Step 0: Starting enrichment ---")
    
    # Step 1: Get all parcel IDs that need enrichment
    print("\n--- Step 1: Get parcel IDs ---")
    result = docker_psql("""
        SELECT parcel_id FROM properties
        WHERE county_id = 'hamilton'
          AND (property_category IS NULL OR current_use IS NULL OR current_use = '')
        ORDER BY parcel_id
    """)
    parcel_ids = [line.strip() for line in result.split('\n') if line.strip()]
    print(f"  Found {len(parcel_ids):,} parcels to query")
    
    # Step 2: GIS enrichment
    print("\n--- Step 2: GIS CURRENTUSE lookup ---")
    gis_results = fetch_all_gis_data(parcel_ids)
    applied = apply_gis_data(gis_results)
    
    # Count results
    has_code = sum(1 for v in gis_results.values() if v and v in USE_CODE_MAP)
    no_code = sum(1 for v in gis_results.values() if not v or v not in USE_CODE_MAP)
    print(f"  With CURRENTUSE code: {has_code:,}")
    print(f"  Without code (empty): {no_code:,}")
    
    # Step 3: Heuristic classification for remaining unknowns
    print("\n--- Step 3: Heuristic classification ---")
    counts = classify_by_heuristics()
    for rule, count in counts.items():
        print(f"  {rule}: {count:,}")
    
    # Step 4: Show results
    print("\n--- Results ---")
    show_distribution()
    
    # Check remaining unknowns
    unknown = docker_psql("SELECT COUNT(*) FROM properties WHERE county_id='hamilton' AND property_category IS NULL;")
    total = docker_psql("SELECT COUNT(*) FROM properties WHERE county_id='hamilton';")
    print(f"\nRemaining unknown: {unknown} / {total}")
    
    print("\nDone!")


if __name__ == "__main__":
    main()
