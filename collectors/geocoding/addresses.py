"""Address parsing helpers (pure, no I/O).

Consolidates the `normalize_address` / `extract_house_number` logic that was
copy-pasted across geocoder.py, geocode_sold.py, interpolate_sold.py and both
geocode_streets.py.
"""
from __future__ import annotations

import re

from .config import UNIT_RE

_HOUSE_RE = re.compile(r"^(\d+)\s+(.+)$")


def normalize_address(addr: str) -> str:
    """Trim and strip condo/unit suffixes used by the assessor feed."""
    if not addr:
        return ""
    return UNIT_RE.sub("", addr.strip()).strip()


def extract_house_number(address: str) -> tuple[int | None, str]:
    """Return ``(house_number, STREET_NAME_UPPER)``.

    House number is ``None`` for addresses that do not start with digits. The
    street is upper-cased so it can be used as a stable grouping key when
    matching target parcels against geocoded reference points.
    """
    if not address:
        return None, ""
    m = _HOUSE_RE.match(address.strip())
    if m:
        return int(m.group(1)), m.group(2).strip().upper()
    return None, address.strip().upper()


def is_numbered(address: str) -> bool:
    """True when the address starts with a house number (Census-geocodable)."""
    return bool(address) and address.lstrip()[:1].isdigit()


def split_numbered(rows: list[dict], key: str = "address") -> tuple[list[dict], list[dict]]:
    """Partition rows into (numbered, bare) by leading house number."""
    numbered, bare = [], []
    for r in rows:
        (numbered if is_numbered(r[key]) else bare).append(r)
    return numbered, bare
