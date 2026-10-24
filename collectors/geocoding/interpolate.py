"""Deterministic house-number interpolation (pure, no I/O).

A coordinate is produced **only** when the house number can be bracketed by two
known geocoded points on the same street (or matches one exactly). When it
cannot be bracketed, extrapolation is attempted using the average gap between
known points — capped at 3 gaps to avoid runaway estimates.
This is the key data-quality guarantee: interpolated points that feed the
appraisal comp pool are always real-bounded estimates, never random jitter or
street-bbox centers (the behavior the old geocode_streets.py had).
"""
from __future__ import annotations

from collections import defaultdict

from .addresses import extract_house_number

# Maximum extrapolation distance in "house-number gaps".
# E.g. with avg gap=100 and MAX_EXTRAP_GAP=3, we allow up to 300 house numbers
# beyond the nearest known endpoint.
MAX_EXTRAP_GAP = 3


def build_known_points_by_street(rows) -> dict[str, list[tuple[int, float, float]]]:
    """Index geocoded reference parcels by street.

    ``rows`` is an iterable of ``(parcel_id, address, lat, lng)``. Returns
    ``{STREET: sorted [(house_number, lat, lng)]}``.
    """
    by_street: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for row in rows:
        address, lat, lng = row[1], row[2], row[3]
        house_num, street = extract_house_number(address)
        if house_num is not None and lat is not None and lng is not None:
            by_street[street].append((house_num, float(lat), float(lng)))
    for street in by_street:
        by_street[street].sort(key=lambda p: p[0])
    return by_street


def interpolate(house_num, known_points) -> tuple[float, float] | None:
    """Linear interpolation between the two known points bracketing ``house_num``.

    Returns ``None`` when ``house_num`` falls outside the known range on either
    side (cannot bracket) — no fabrication.
    """
    if house_num is None or not known_points:
        return None
    pts = sorted(known_points, key=lambda p: p[0])

    lower = upper = None
    for num, lat, lng in pts:
        if num <= house_num:
            lower = (num, lat, lng)
        if num >= house_num and upper is None:
            upper = (num, lat, lng)
            break

    # Exact match on a known point.
    if lower is not None and lower[0] == house_num:
        return lower[1], lower[2]
    if upper is not None and upper[0] == house_num:
        return upper[1], upper[2]

    # Bracketed strictly between two distinct known points.
    if lower is not None and upper is not None and lower[0] != upper[0]:
        frac = (house_num - lower[0]) / (upper[0] - lower[0])
        return (
            lower[1] + frac * (upper[1] - lower[1]),
            lower[2] + frac * (upper[2] - lower[2]),
        )

    # Only one side known -> extrapolate from the nearest endpoint.
    if len(pts) < 2:
        return None

    avg_gap = (pts[-1][0] - pts[0][0]) / (len(pts) - 1)
    if avg_gap <= 0:
        return None

    if lower is not None and upper is None:
        # house_num > all known points -> extrapolate upward from upper-most
        steps = (house_num - pts[-1][0]) / avg_gap
        if 0 < steps <= MAX_EXTRAP_GAP:
            return (
                pts[-1][1] + steps * (pts[-1][1] - pts[-2][1]),
                pts[-1][2] + steps * (pts[-1][2] - pts[-2][2]),
            )
    if upper is not None and lower is None:
        # house_num < all known points -> extrapolate downward from lower-most
        steps = (pts[0][0] - house_num) / avg_gap
        if 0 < steps <= MAX_EXTRAP_GAP:
            return (
                pts[0][1] - steps * (pts[1][1] - pts[0][1]),
                pts[0][2] - steps * (pts[1][2] - pts[0][2]),
            )

    # Cannot bracket or extrapolate -> no coordinate.
    return None
