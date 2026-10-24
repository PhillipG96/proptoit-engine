"""County adapters.

Each subpackage adapts the generic collector layer to one county:
``<county>/<county>.yaml`` holds the CountyConfig fields plus any
adapter-specific extras. The reference adapter is ``hamilton``.
"""

from __future__ import annotations

from pathlib import Path

from proptoit.county_config import ADAPTERS_DIR, CountyConfig, load_county_config

__all__ = ["ADAPTERS_DIR", "CountyConfig", "load_county_config", "available_counties"]


def available_counties(adapters_root: Path = ADAPTERS_DIR) -> list[str]:
    """Counties that have a committed adapter config."""
    root = Path(adapters_root)
    if not root.is_dir():
        return []
    return sorted(p.parent.name for p in root.glob("*/*.yaml"))
