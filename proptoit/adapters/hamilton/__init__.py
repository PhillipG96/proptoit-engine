"""Hamilton County, TN reference adapter.

Single source of truth for Hamilton-specific constants: the adapter
directory. Collectors import from here instead of hardcoding URLs,
tables, and field maps.
"""

from __future__ import annotations

from pathlib import Path

from proptoit.county_config import CountyConfig, adapter_extras

HERE = Path(__file__).parent
CONFIG_YAML = HERE / "hamilton.yaml"


def get_config() -> CountyConfig:
    """Load the Hamilton CountyConfig (cached at call sites, not here —
    collectors run once per process)."""
    return CountyConfig.from_yaml(CONFIG_YAML)


def get_extras() -> dict:
    """Adapter-specific keys outside the six CountyConfig fields."""
    return adapter_extras(CONFIG_YAML)
