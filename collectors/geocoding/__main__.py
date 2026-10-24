"""CLI entry point: ``python -m collectors.geocoding``.

Replaces the dozen one-off geocode_*/interpolate_* scripts. Examples:

    # Census batch + Nominatim fallback for sold Hamilton parcels missing coords
    python -m collectors.geocoding --strategy census --county hamilton --target sold

    # Deterministic neighbor interpolation (no network), dry-run first
    python -m collectors.geocoding --strategy interpolate --county hamilton --target sold --dry-run

    # Export geocodes to JSON instead of the DB
    python -m collectors.geocoding --strategy census --target all --sink json --output /tmp/geocoded.json
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import db
from .strategies import run_census, run_interpolate

log = logging.getLogger("collectors.geocoding")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m collectors.geocoding",
        description="Geocode property parcels (Census batch + Nominatim, or deterministic interpolation).",
    )
    p.add_argument("--strategy", choices=["census", "interpolate"], required=True)
    p.add_argument("--county", default="hamilton")
    p.add_argument(
        "--target",
        choices=["missing", "sold", "residential", "all"],
        default="missing",
        help="which parcels to geocode (missing=no lat, sold=recent sale w/o lat, etc.)",
    )
    p.add_argument("--limit", type=int, default=None, help="cap number of target parcels")
    p.add_argument("--rate", type=float, default=None, help="seconds between Nominatim requests")
    p.add_argument("--batch-size", type=int, default=None, help="Census rows per request")
    p.add_argument("--sink", choices=["db", "json"], default="db")
    p.add_argument("--output", default=None, help="JSON path when --sink json")
    p.add_argument("--dry-run", action="store_true", help="report counts without writing")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        stream=sys.stdout,
    )

    conn = db.get_conn()
    try:
        if args.strategy == "census":
            kwargs = dict(
                limit=args.limit,
                sink=args.sink,
                output=args.output,
                dry_run=args.dry_run,
                rate=args.rate,
            )
            if args.batch_size:
                kwargs["batch_size"] = args.batch_size
            result = run_census(conn, args.county, args.target, **kwargs)
        else:  # interpolate
            result = run_interpolate(
                conn,
                args.county,
                args.target,
                limit=args.limit,
                sink=args.sink,
                dry_run=args.dry_run,
            )
        log.info("done: %s", result)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
