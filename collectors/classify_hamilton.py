#!/usr/bin/env python3
"""
Batch classify Hamilton County properties.
Maps current_use codes → property_category, then applies heuristics for empty codes.

Hamilton County assessor current_use codes:
  RS  → residential   (single-family, 1,374)
  CO  → commercial    (363)
  EX  → exempt        (83)
  MF  → residential   (multi-family, 31) — was misclassified as commercial
  IN  → commercial    (industrial, 20)
  AG  → agricultural  (6)
  DU  → residential   (duplex, 6)
  0MR → residential   (multi-family residential, 1)
  0CR → residential   (commercial-residential mixed, 3)
  0RI → residential   (retail-residential, 1)
  ECR → residential   (extra commercial/residential, 16) — low value → exempt
  OSAP→ exempt        (parochial/public, 8) — zero value
  ECL → exempt        (1)
  0EC → commercial    (6)
  0CE → commercial    (1)
  0AC → commercial    (1)
  ECRC→ commercial    (1)
  EID → commercial    (1)
  (empty) → 83,081 — needs heuristic classification
"""

import os
import psycopg2

def db_dsn() -> str:
    """Resolve the target DSN at call time — no hardcoded default.

    The operator must be explicit: export DATABASE_URL (e.g. from the repo
    .env) before running. Fails loudly rather than guessing a target.
    """
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL environment variable is required (export it before running)")
    return dsn

# current_use → property_category mapping
USE_CODE_MAP = {
    # Residential
    "RS":  "residential",
    "MF":  "residential",
    "DU":  "residential",
    "0MR": "residential",
    "0CR": "residential",
    "0RI": "residential",
    # Commercial
    "CO":  "commercial",
    "IN":  "commercial",
    "0EC": "commercial",
    "0CE": "commercial",
    "0AC": "commercial",
    "ECRC":"commercial",
    "EID": "commercial",
    # Exempt
    "EX":  "exempt",
    "ECR": "exempt",
    "OSAP":"exempt",
    "ECL": "exempt",
    # Agricultural
    "AG":  "agricultural",
}


def classify_by_use_code(cur, conn):
    """Classify properties with non-empty current_use codes."""
    placeholders = ", ".join(f"'{k}'" for k in USE_CODE_MAP.keys())
    case_expr = "CASE current_use " + " ".join(
        f"WHEN '{k}' THEN '{v}'" for k, v in USE_CODE_MAP.items()
    ) + " END"

    cur.execute(f"""
        UPDATE properties
        SET property_category = {case_expr}, updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND current_use IN ({placeholders})
    """)
    updated = cur.rowcount
    conn.commit()
    return updated


def classify_by_heuristics(cur, conn):
    """Classify remaining unknowns using heuristic rules.
    
    Based on known distributions:
      Residential: median sqft 1,410, median value $244K
      Commercial:  median sqft 3,597, median value $551K
    
    Enhanced rules:
    - Street suffix patterns (DR/LN/ST/AVE/PL/CT/TER/OVAL → residential)
    - Sqft/value thresholds (relaxed from prior version)
    - Address patterns (SUITE/BUILDING → commercial)
    """
    counts = {}

    # Rule 1: Street suffix → residential (strongest signal)
    cur.execute("""
        UPDATE properties
        SET property_category = 'residential', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND (
            address ILIKE '% ST' OR address ILIKE '% STREET'
            OR address ILIKE '% AVE' OR address ILIKE '% AVENUE'
            OR address ILIKE '% DR' OR address ILIKE '% DRIVE'
            OR address ILIKE '% LN' OR address ILIKE '% LANE'
            OR address ILIKE '% CT' OR address ILIKE '% COURT'
            OR address ILIKE '% PL' OR address ILIKE '% PLACE'
            OR address ILIKE '% TER' OR address ILIKE '% TERRACE'
            OR address ILIKE '% OVAL'
            OR address ILIKE '% CIR' OR address ILIKE '% CIRCLE'
            OR address ILIKE '% WAY'
            OR address ILIKE '% RD' OR address ILIKE '% ROAD'
          )
    """)
    counts['r1_suffix'] = cur.rowcount
    conn.commit()

    # Rule 2: Small buildings + moderate value → residential
    cur.execute("""
        UPDATE properties
        SET property_category = 'residential', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND building_sqft IS NOT NULL
          AND building_sqft > 0
          AND building_sqft < 3000
          AND assessed_value IS NOT NULL
          AND assessed_value < 500000
    """)
    counts['r2_small_mod'] = cur.rowcount
    conn.commit()

    # Rule 3: Small buildings, no value data, but has year_built → likely residential
    cur.execute("""
        UPDATE properties
        SET property_category = 'residential', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND building_sqft IS NOT NULL
          AND building_sqft > 0
          AND building_sqft < 3000
          AND year_built IS NOT NULL
          AND year_built > 1800
          AND assessed_value IS NULL
    """)
    counts['r3_small_year'] = cur.rowcount
    conn.commit()

    # Rule 4: Large buildings or high value → commercial
    cur.execute("""
        UPDATE properties
        SET property_category = 'commercial', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND (
              (building_sqft IS NOT NULL AND building_sqft >= 3000)
              OR (assessed_value IS NOT NULL AND assessed_value >= 500000)
          )
    """)
    counts['r4_large'] = cur.rowcount
    conn.commit()

    # Rule 5: Vacant land (building_sqft = 0) → classify by assessed value
    #   Low value (< $100K) → residential vacant land
    cur.execute("""
        UPDATE properties
        SET property_category = 'residential', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND building_sqft = 0
          AND assessed_value IS NOT NULL
          AND assessed_value < 100000
    """)
    counts['r5_land_low'] = cur.rowcount
    conn.commit()

    # Rule 6: Vacant land, high value → commercial
    cur.execute("""
        UPDATE properties
        SET property_category = 'commercial', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND building_sqft = 0
          AND assessed_value IS NOT NULL
          AND assessed_value >= 100000
    """)
    counts['r6_land_high'] = cur.rowcount
    conn.commit()

    # Rule 7: Suite/Building address → commercial
    cur.execute("""
        UPDATE properties
        SET property_category = 'commercial', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND (
            address ILIKE '% SUITE%'
            OR address ILIKE '% STE %'
            OR address ILIKE '% BUILDING%'
            OR address ILIKE '% CENTER%'
            OR address ILIKE '% COMPLEX%'
            OR address ILIKE '% PLAZA%'
          )
    """)
    counts['r7_suite'] = cur.rowcount
    conn.commit()

    # Rule 8: Highway/RD with large lot → commercial
    cur.execute("""
        UPDATE properties
        SET property_category = 'commercial', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND (
            address ILIKE '% HWY%'
            OR address ILIKE '% HIGHWAY%'
            OR address ILIKE '% PKWY%'
            OR address ILIKE '% PARKWAY%'
            OR address ILIKE '% BLVD%'
            OR address ILIKE '% BOULEVARD%'
          )
          AND building_sqft >= 2000
    """)
    counts['r8_hwy'] = cur.rowcount
    conn.commit()

    # Rule 9: Very small sqft (< 500) + low value → residential (garages, sheds, small homes)
    cur.execute("""
        UPDATE properties
        SET property_category = 'residential', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND building_sqft IS NOT NULL
          AND building_sqft > 0
          AND building_sqft < 500
          AND assessed_value < 200000
    """)
    counts['r9_tiny'] = cur.rowcount
    conn.commit()

    # Rule 10: Default — if still unknown, classify as residential (most likely)
    # Hamilton County is ~80% residential by population
    cur.execute("""
        UPDATE properties
        SET property_category = 'residential', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
          AND building_sqft IS NOT NULL
          AND building_sqft > 0
          AND building_sqft < 2000
    """)
    counts['r10_default_small'] = cur.rowcount
    conn.commit()

    # Rule 11: Remaining truly ambiguous — default to residential
    cur.execute("""
        UPDATE properties
        SET property_category = 'residential', updated_at = NOW()
        WHERE county_id = 'hamilton'
          AND property_category = 'unknown'
    """)
    counts['r11_fallback'] = cur.rowcount
    conn.commit()

    return counts


def fix_edge_cases(cur, conn):
    """Fix known misclassifications from use-code mapping."""
    # 0CE was mapped as exempt by CASE (because '0CE' matched 'EX' substring in some SQL engines)
    cur.execute("""
        UPDATE properties SET property_category = 'commercial', updated_at = NOW()
        WHERE county_id = 'hamilton' AND current_use = '0CE'
    """)
    fix1 = cur.rowcount
    conn.commit()

    # ECRE (extra commercial/residential, very low value ~$5.6K) → exempt
    cur.execute("""
        UPDATE properties SET property_category = 'exempt', updated_at = NOW()
        WHERE county_id = 'hamilton' AND current_use = 'ECRE'
    """)
    fix2 = cur.rowcount
    conn.commit()

    return fix1, fix2


def show_distribution(cur):
    """Print final category distribution."""
    cur.execute("""
        SELECT property_category, COUNT(*) as cnt,
               ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 1) as pct
        FROM properties
        WHERE county_id = 'hamilton'
        GROUP BY property_category
        ORDER BY cnt DESC
    """)
    print(f"\n{'Category':<20} {'Count':>10} {'%':>6}")
    print(f"{'─' * 38}")
    for row in cur.fetchall():
        print(f"{row[0]:<20} {row[1]:>10,} {row[2]:>5.1f}%")


def main():
    print("=" * 60)
    print("Hamilton County Property Classification")
    print("=" * 60)

    conn = psycopg2.connect(db_dsn())
    cur = conn.cursor()

    # Pre-classification stats
    print("\n--- Before ---")
    cur.execute("SELECT COUNT(*) FROM properties WHERE county_id='hamilton'")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM properties WHERE county_id='hamilton' AND property_category='unknown'")
    unknown = cur.fetchone()[0]
    print(f"Total: {total:,}  Unknown: {unknown:,} ({unknown*100.0/total:.1f}%)")

    # Step 1: Map current_use codes
    print("\n--- Step 1: current_use code mapping ---")
    r1 = classify_by_use_code(cur, conn)
    print(f"  Mapped by use code: {r1:,}")

    # Step 2: Heuristic classification
    print("\n--- Step 2: Heuristic classification ---")
    counts = classify_by_heuristics(cur, conn)
    for rule, count in counts.items():
        print(f"  {rule}: {count:,}")

    # Step 3: Fix edge cases
    print("\n--- Step 3: Edge case fixes ---")
    f1, f2 = fix_edge_cases(cur, conn)
    print(f"  0CE → commercial: {f1:,}")
    print(f"  ECRE → exempt: {f2:,}")

    # Post-classification stats
    print("\n--- After ---")
    cur.execute("SELECT COUNT(*) FROM properties WHERE county_id='hamilton' AND property_category='unknown'")
    remaining = cur.fetchone()[0]
    print(f"Remaining unknown: {remaining:,} ({remaining*100.0/total:.1f}%)")

    show_distribution(cur)

    cur.close()
    conn.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
