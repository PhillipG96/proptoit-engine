#!/usr/bin/env python3
"""
Finish geocoding all remaining Hamilton County properties.

Strategy (in order):
1. ArcGIS — county's own GIS for authoritative parcel centroids
2. Census batch — for ArcGIS misses with numbered addresses
3. Interpolation — deterministic, no network, for streets with known neighbors

Run: python3 collectors/geocoding/finish_geocoding.py
"""
from __future__ import annotations

import csv
import io
import logging
import re
import subprocess
import sys
import time

import psycopg2
import psycopg2.extras
import requests

log = logging.getLogger("finish_geocoding")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    stream=sys.stdout,
)

# ── Config ──
GIS_URL = "https://mapsdev.hamiltontn.gov/hcwa03/rest/services/Live_Parcels/MapServer/0/query"
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
CENSUS_BENCHMARK = "Public_AR_Current"
GIS_BATCH_SIZE = 50
GIS_DELAY = 0.5
CENSUS_BATCH_SIZE = 200

# ── DB helpers ──
def run_sql(query: str) -> list:
    """Run SQL via docker exec and return rows."""
    cmd = [
        "docker", "exec", "proptoit-postgis",
        "psql", "-U", "proptoit", "-d", "proptoit",
        "-t", "-A", "-F", "|", "-c", query,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"SQL error: {result.stderr}")
    return [line.split("|") for line in result.stdout.strip().split("\n") if line.strip()]

def bulk_update_coords(rows: list[tuple]) -> int:
    """Write (lat, lng, parcel_id) via docker exec bulk update."""
    if not rows:
        return 0
    # Validate lat/lng as floats and escape parcel ids — these come from an
    # external geocoding response and used to be interpolated raw into SQL.
    updates = "; ".join(
        f"UPDATE properties SET lat={float(lat)}, lng={float(lng)}, geocoords_point=point({float(lng)},{float(lat)}), updated_at=now() "
        f"WHERE parcel_id='{str(pid).replace(chr(39), chr(39)*2)}' AND county_id='hamilton'"
        for lat, lng, pid in rows
    )
    cmd = [
        "docker", "exec", "proptoit-postgis",
        "psql", "-v", "ON_ERROR_STOP=1", "-U", "proptoit", "-d", "proptoit",
        "-t", "-A", "-F", "|", "-c", updates,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"bulk_update_coords failed: {result.stderr[:300]}")
    count = sum(1 for line in result.stdout.strip().split("\n") if line.strip().startswith("UPDATE"))
    return count

# ── Parcel ID normalization ──
_SHORT_NUMERIC = re.compile(r'^(\d{3})\s+(\d{3}(?:\.\d+)?)$')
_UNIT_RE = re.compile(r'\s+[CL]\d+\s*$', re.IGNORECASE)

def normalize_parcel_id(parcel_id: str) -> str:
    s = " ".join(parcel_id.split())
    m = _SHORT_NUMERIC.match(s)
    if m:
        return f"{m.group(1)}  {m.group(2)}"
    return s

def parent_parcel_id(parcel_id: str) -> str | None:
    stripped = _UNIT_RE.sub('', parcel_id).strip()
    if stripped != parcel_id:
        return stripped
    return None

# ── ArcGIS ──
def poly_centroid(rings: list) -> tuple[float, float] | None:
    if not rings or len(rings[0]) < 3:
        return None
    ring = rings[0]
    area = 0.0
    cx = 0.0
    cy = 0.0
    n = len(ring)
    for i in range(n - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    area /= 2.0
    if abs(area) < 1e-12:
        lngs = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        return sum(lats) / len(lats), sum(lngs) / len(lngs)
    cx /= (6.0 * area)
    cy /= (6.0 * area)
    return cy, cx  # (lat, lng)

def fetch_gis_centroids(parcel_ids: list[str]) -> dict[str, tuple[float, float]]:
    quoted = ",".join(f"'{pid}'" for pid in parcel_ids)
    params = {
        "where": f"TAX_MAP_NO IN ({quoted})",
        "outFields": "TAX_MAP_NO",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    try:
        r = requests.get(GIS_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("GIS error: %s", e)
        return {}
    results = {}
    for feature in data.get("features", []):
        tax_map_no = feature.get("attributes", {}).get("TAX_MAP_NO")
        geometry = feature.get("geometry")
        if not tax_map_no or not geometry:
            continue
        rings = geometry.get("rings")
        if not rings:
            continue
        centroid = poly_centroid(rings)
        if centroid:
            results[tax_map_no] = centroid
    return results

def geocode_arcgis(parcels: list[dict]) -> dict[int, tuple[float, float]]:
    """Geocode parcels via ArcGIS. Returns {db_id: (lat, lng)}."""
    db_map: dict[str, int] = {}
    parent_map: dict[str, list[int]] = {}
    for p in parcels:
        normalized = normalize_parcel_id(p["parcel_id"])
        db_map[normalized] = p["id"]
        parent = parent_parcel_id(normalized)
        if parent:
            parent_map.setdefault(parent, []).append(p["id"])

    primary_ids = set(db_map.keys())
    extra_parents = set(parent_map.keys()) - primary_ids
    parcel_ids = list(primary_ids | extra_parents)
    total = len(parcel_ids)
    results: dict[int, tuple[float, float]] = {}

    for start in range(0, total, GIS_BATCH_SIZE):
        batch = parcel_ids[start:start + GIS_BATCH_SIZE]
        centroids = fetch_gis_centroids(batch)

        for tax_map_no, (lat, lng) in centroids.items():
            db_id = db_map.get(tax_map_no)
            if db_id is not None:
                results[db_id] = (lat, lng)
            for child_id in parent_map.get(tax_map_no, []):
                results[child_id] = (lat, lng)

        pct = min(start + GIS_BATCH_SIZE, total)
        log.info("  ArcGIS %d/%d queried, %d matched so far", pct, total, len(results))
        time.sleep(GIS_DELAY)

    return results

# ── Census ──
def build_census_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    for r in rows:
        addr = r["address"].replace('"', "'")
        buf.write(f'{r["id"]},"{addr}","{r.get("city","")}","{r.get("state","")}","{r.get("postal_code","")}"\n')
    return buf.getvalue()

def parse_census_response(text: str) -> dict[int, tuple[float, float]]:
    results = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 6 or row[2].strip().lower() != "match":
            continue
        try:
            db_id = int(row[0].strip())
            lng, lat = row[5].strip().split(",")
            results[db_id] = (float(lat), float(lng))
        except (ValueError, IndexError):
            continue
    return results

def geocode_census(parcels: list[dict]) -> dict[int, tuple[float, float]]:
    """Geocode numbered addresses via Census batch."""
    if not parcels:
        return {}
    all_results = {}
    for i in range(0, len(parcels), CENSUS_BATCH_SIZE):
        batch = parcels[i:i + CENSUS_BATCH_SIZE]
        payload = build_census_csv(batch).encode()
        try:
            resp = requests.post(
                CENSUS_URL,
                files={"addressFile": ("addresses.csv", payload, "text/csv")},
                data={"benchmark": CENSUS_BENCHMARK, "returntype": "locations"},
                timeout=180,
            )
            resp.raise_for_status()
            results = parse_census_response(resp.text)
            all_results.update(results)
            log.info("  Census batch %d: %d/%d matched", i // CENSUS_BATCH_SIZE + 1, len(results), len(batch))
        except requests.RequestException as e:
            log.error("Census API error: %s", e)
    return all_results

# ── Interpolation ──
def geocode_interpolation(parcels: list[dict]) -> dict[int, tuple[float, float]]:
    """Interpolate coordinates for parcels sharing streets with known neighbors."""
    known_rows = run_sql("""
        SELECT parcel_id, address, lat, lng
        FROM properties WHERE county_id = 'hamilton' AND lat IS NOT NULL
        AND address ~ '^[0-9]'
    """)
    known_by_street: dict[str, list[tuple[int, float, float]]] = {}
    for pid, addr, lat, lng in known_rows:
        if isinstance(lat, str):
            lat = float(lat)
        if isinstance(lng, str):
            lng = float(lng)
        street = re.sub(r'^[0-9]+\s+', '', addr)
        match = re.match(r'^(\d+)', addr)
        hnum = int(match.group(1)) if match else 0
        known_by_street.setdefault(street, []).append((hnum, lat, lng))

    for street in known_by_street:
        known_by_street[street].sort()

    results = {}
    for p in parcels:
        addr = p["address"]
        match = re.match(r'^(\d+)\s+(.*)', addr)
        if not match:
            continue
        hnum = int(match.group(1))
        street = match.group(2).strip()
        pts = known_by_street.get(street)
        if not pts:
            continue
        loc = None
        for i in range(len(pts) - 1):
            if pts[i][0] <= hnum <= pts[i+1][0]:
                t = (hnum - pts[i][0]) / (pts[i+1][0] - pts[i][0])
                lat = pts[i][1] + t * (pts[i+1][1] - pts[i][1])
                lng = pts[i][2] + t * (pts[i+1][2] - pts[i][2])
                loc = (lat, lng)
                break
        if loc is None and len(pts) >= 2:
            avg_gap = (pts[-1][0] - pts[0][0]) / (len(pts) - 1)
            if avg_gap > 0:
                if hnum > pts[-1][0]:
                    steps = (hnum - pts[-1][0]) / avg_gap
                    if steps <= 3:
                        lat = pts[-1][1] + steps * (pts[-1][1] - pts[-2][1])
                        lng = pts[-1][2] + steps * (pts[-1][2] - pts[-2][2])
                        loc = (lat, lng)
                elif hnum < pts[0][0]:
                    steps = (pts[0][0] - hnum) / avg_gap
                    if steps <= 3:
                        lat = pts[0][1] - steps * (pts[1][1] - pts[0][1])
                        lng = pts[0][2] - steps * (pts[1][2] - pts[0][2])
                        loc = (lat, lng)
        if loc is None:
            lat = sum(p[1] for p in pts) / len(pts)
            lng = sum(p[2] for p in pts) / len(pts)
            loc = (lat, lng)
        if loc:
            results[p["id"]] = loc

    return results

# ── Main ──
def main():
    parcels = run_sql("""
        SELECT id, parcel_id, address, city, state, postal_code
        FROM properties
        WHERE county_id = 'hamilton'
          AND lat IS NULL
          AND address IS NOT NULL AND address <> ''
        ORDER BY id
    """)
    parcels = [
        {"id": int(r[0]), "parcel_id": r[1], "address": r[2],
         "city": r[3] or "", "state": r[4] or "", "postal_code": r[5] or ""}
        for r in parcels
    ]
    total = len(parcels)
    log.info("Found %d missing parcels", total)

    if not total:
        log.info("All parcels already geocoded!")
        return

    by_id = {p["id"]: p for p in parcels}
    all_coords: dict[int, tuple[float, float]] = {}

    # ── Phase 1: ArcGIS ──
    log.info("=== Phase 1: ArcGIS (authoritative parcel centroids) ===")
    gis_coords = geocode_arcgis(parcels)
    all_coords.update(gis_coords)
    log.info("ArcGIS matched %d / %d", len(gis_coords), total)

    remaining = [p for p in parcels if p["id"] not in all_coords]
    log.info("Remaining after ArcGIS: %d", len(remaining))

    # ── Phase 2: Census (numbered only) ──
    numbered_remaining = [p for p in remaining if re.match(r'^\d+', p["address"])]
    if numbered_remaining:
        log.info("=== Phase 2: Census batch (%d numbered addresses) ===", len(numbered_remaining))
        census_coords = geocode_census(numbered_remaining)
        all_coords.update(census_coords)
        log.info("Census matched %d", len(census_coords))
        remaining = [p for p in remaining if p["id"] not in all_coords]

    # ── Phase 3: Interpolation ──
    if remaining:
        log.info("=== Phase 3: Interpolation (%d remaining) ===", len(remaining))
        interp_coords = geocode_interpolation(remaining)
        all_coords.update(interp_coords)
        log.info("Interpolation matched %d", len(interp_coords))
        remaining = [p for p in remaining if p["id"] not in all_coords]

    # ── Write to DB ──
    log.info("=== Writing %d coordinates to DB ===", len(all_coords))
    rows = [(lat, lng, by_id[kid]["parcel_id"]) for kid, (lat, lng) in all_coords.items()]

    chunk_size = 500
    total_written = 0
    failed_chunks = 0
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]
        # Geocoding (the slow, network-bound part) is already done for ALL
        # rows; a psql error on one chunk must not discard the rest.
        # ON_ERROR_STOP keeps each chunk all-or-nothing, so a failed chunk
        # is retried statement-by-statement to isolate the bad parcel(s).
        try:
            total_written += bulk_update_coords(chunk)
        except RuntimeError as exc:
            failed_chunks += 1
            log.warning("Chunk at %d failed as a whole (%s); isolating rows", i, exc)
            for row in chunk:
                try:
                    total_written += bulk_update_coords([row])
                except RuntimeError as exc2:
                    log.error("Unwriteable parcel %s: %s", row[2], str(exc2)[:150])
        log.info("  Wrote %d / %d", i + len(chunk), len(rows))

    log.info("=== Complete! ===")
    log.info("Geocoded this run: %d (%d chunks needed isolation)", total_written, failed_chunks)

    # Final counts
    rows = run_sql("SELECT COUNT(*) FROM properties WHERE county_id='hamilton'")
    total_props = int(rows[0][0]) if rows else 0
    rows = run_sql("SELECT COUNT(*) FROM properties WHERE county_id='hamilton' AND lat IS NOT NULL")
    total_geocoded = int(rows[0][0]) if rows else 0
    rows = run_sql("SELECT COUNT(*) FROM properties WHERE county_id='hamilton' AND lat IS NULL")
    still_missing = int(rows[0][0]) if rows else 0

    log.info("Final: %d / %d geocoded (%.1f%%), %d still missing",
             total_geocoded, total_props, 100 * total_geocoded / total_props if total_props else 0,
             still_missing)

if __name__ == "__main__":
    main()
