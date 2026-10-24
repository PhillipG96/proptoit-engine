"""Geocoding strategies composed from the package primitives.

- ``run_census``: Census batch for numbered addresses + Nominatim fallback for
  bare names and Census misses. Sink is the DB (native ``point``) or a JSON file.
- ``run_interpolate``: no network; deterministic interpolation of each target
  parcel between two known geocoded points on its street.

Flushes incrementally so progress survives crashes.
"""
from __future__ import annotations

import json
import logging

from . import db
from .addresses import extract_house_number, is_numbered, normalize_address
from .census import census_batch
from .config import CENSUS_BATCH_SIZE
from .interpolate import build_known_points_by_street, interpolate
from .nominatim import NominatimClient

log = logging.getLogger(__name__)


def _flush_coords(conn, by_id, county_id, coords, flushed_ids):
    """Flush newly geocoded IDs to DB, tracking which were already written."""
    new_ids = set(coords.keys()) - flushed_ids
    if not new_ids:
        return 0
    rows = [(lat, lng, by_id[kid]["parcel_id"]) for kid, (lat, lng) in coords.items() if kid in new_ids]
    updated = db.bulk_update_coords(conn, rows, county_id)
    flushed_ids.update(new_ids)
    return updated


def run_census(
    conn,
    county_id: str,
    target: str,
    *,
    limit: int | None = None,
    batch_size: int = CENSUS_BATCH_SIZE,
    sink: str = "db",
    output: str | None = None,
    dry_run: bool = False,
    rate: float | None = None,
    flush_every: int = 500,  # flush to DB every N Nominatim calls
) -> dict:
    props = db.fetch_targets(conn, county_id, target, limit=limit)
    log.info("census: %d targets (county=%s target=%s)", len(props), county_id, target)
    if not props:
        return {"targets": 0, "geocoded": 0}

    by_id = {p["id"]: p for p in props}
    numbered = [p for p in props if is_numbered(p["address"])]
    bare = [p for p in props if not is_numbered(p["address"])]
    log.info("  numbered=%d bare=%d", len(numbered), len(bare))

    if dry_run:
        log.info(
            "  [dry-run] would Census-geocode %d numbered, Nominatim %d bare + any misses",
            len(numbered), len(bare),
        )
        return {"targets": len(props), "geocoded": 0, "dry_run": True}

    coords: dict[int, tuple[float, float]] = {}
    flushed_ids: set[int] = set()

    # Census batch for numbered addresses — flush after each batch.
    census_total = 0
    for i in range(0, len(numbered), batch_size):
        batch = numbered[i : i + batch_size]
        batch_coords = census_batch(batch)
        coords.update(batch_coords)
        census_total += len(batch_coords)
        if sink == "db" and batch_coords:
            _flush_coords(conn, by_id, county_id, coords, flushed_ids)
            log.info("  Census flushed %d so far", len(flushed_ids))

    log.info("  Census matched %d total", len(coords))

    # Nominatim fallback for bare names + Census misses.
    needed = bare + [p for p in numbered if p["id"] not in coords]
    if needed:
        client = NominatimClient(rate=rate) if rate else NominatimClient()
        log.info("  Nominatim fallback: %d addresses", len(needed))
        nominatim_total = 0
        for idx, p in enumerate(needed, 1):
            result = client.geocode_address(
                normalize_address(p["address"]), p.get("city") or "", p.get("state") or ""
            )
            if result:
                coords[p["id"]] = result
                nominatim_total += 1
            # Flush periodically so progress survives crashes
            if sink == "db" and nominatim_total % flush_every == 0:
                _flush_coords(conn, by_id, county_id, coords, flushed_ids)
                log.info("  Nominatim %d / %d (%.1f%%)", idx, len(needed), 100 * idx / len(needed))
        # Final flush for any remaining
        if sink == "db" and nominatim_total > 0:
            _flush_coords(conn, by_id, county_id, coords, flushed_ids)
        log.info("  Nominatim total: %d / %d", nominatim_total, len(needed))

    if sink == "json":
        records = [
            {
                "parcel_id": by_id[i]["parcel_id"],
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "address": by_id[i]["address"],
            }
            for i, (lat, lng) in coords.items()
        ]
        out = output or "/tmp/geocoded.json"
        with open(out, "w") as f:
            json.dump(records, f, indent=2)
        log.info("  wrote %d records -> %s", len(records), out)
        return {"targets": len(props), "geocoded": len(records), "output": out}

    total_geocoded = len(coords)
    nominatim_count = len(coords) - census_total
    log.info("  total geocoded: %d (Census=%d Nominatim=%d)",
             total_geocoded, census_total, nominatim_count)
    return {"targets": len(props), "geocoded": total_geocoded}


def run_interpolate(
    conn,
    county_id: str,
    target: str,
    *,
    limit: int | None = None,
    sink: str = "db",
    dry_run: bool = False,
    **_ignored,
) -> dict:
    props = db.fetch_targets(conn, county_id, target, limit=limit)
    log.info("interpolate: %d targets (county=%s target=%s)", len(props), county_id, target)
    if not props:
        return {"targets": 0, "geocoded": 0}

    known = build_known_points_by_street(db.fetch_known_points(conn, county_id))
    log.info("  streets with known points: %d", len(known))

    updates: list[tuple[float, float, str]] = []
    stats = {"interpolated": 0, "no_house_number": 0, "no_known_street": 0, "not_bracketed": 0}
    for p in props:
        house_num, street = extract_house_number(p["address"])
        if house_num is None:
            stats["no_house_number"] += 1
            continue
        pts = known.get(street)
        if not pts:
            stats["no_known_street"] += 1
            continue
        loc = interpolate(house_num, pts)
        if loc is None:
            stats["not_bracketed"] += 1
            continue
        updates.append((loc[0], loc[1], p["parcel_id"]))
        stats["interpolated"] += 1

    log.info("  %s", stats)
    if dry_run:
        return {"targets": len(props), "geocoded": 0, "dry_run": True, **stats}

    updated = db.bulk_update_coords(conn, updates, county_id) if sink == "db" else 0
    log.info("  updated %d parcels", updated)
    return {"targets": len(props), "geocoded": updated, **stats}
