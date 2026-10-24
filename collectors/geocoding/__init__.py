"""PropToIt geocoding package.

A single home for the geocoding primitives that used to be copy-pasted across a
dozen scripts in ``collectors/`` and ``scripts/``. Run it as a CLI:

    python -m collectors.geocoding --strategy census      --county hamilton --target sold
    python -m collectors.geocoding --strategy interpolate  --county hamilton --target sold --dry-run

or import the pieces directly.
"""
from __future__ import annotations

from .addresses import extract_house_number, is_numbered, normalize_address, split_numbered
from .census import census_batch, parse_census_response
from .interpolate import build_known_points_by_street, interpolate
from .nominatim import NominatimClient
from .strategies import run_census, run_interpolate

__all__ = [
    "normalize_address",
    "extract_house_number",
    "is_numbered",
    "split_numbered",
    "census_batch",
    "parse_census_response",
    "NominatimClient",
    "build_known_points_by_street",
    "interpolate",
    "run_census",
    "run_interpolate",
]
