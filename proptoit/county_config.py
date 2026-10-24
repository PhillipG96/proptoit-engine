#!/usr/bin/env python3
"""
CountyConfig — per-county configuration for the collector layer.

County-specific constants (parcel table, assessor site, GIS endpoints,
scraped-row field maps) live in committed YAML files under
``proptoit/adapters/<county>/<county>.yaml`` instead of being hardcoded
in collector modules. Adding a new county = adding an adapter package
with a YAML file; no collector code changes.

Usage:
    from proptoit.county_config import CountyConfig
    cfg = CountyConfig.from_yaml("proptoit/adapters/hamilton/hamilton.yaml")
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: Repo root = parent of this package (proptoit/ sits at the repo root).
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Environment variable selecting the active county when a collector does
#: not name one explicitly.
COUNTY_ENV_VAR = "PROPTOIT_COUNTY"

#: Keys required in a county YAML file.
REQUIRED_KEYS = frozenset(
    {"name", "state", "parcel_table", "assessor_url", "gis_endpoints", "field_map"}
)

#: Default adapter directory (relative to the repo root).
ADAPTERS_DIR = REPO_ROOT / "proptoit" / "adapters"


@dataclass(frozen=True)
class CountyConfig:
    """Static, per-county configuration loaded from an adapter YAML file."""

    name: str
    state: str
    parcel_table: str
    assessor_url: str
    gis_endpoints: dict[str, str] = field(default_factory=dict)
    field_map: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CountyConfig":
        """Load a CountyConfig from an adapter YAML file.

        Raises ValueError when required keys are missing; unrecognized keys
        are ignored (adapters may carry extra keys for their own use).
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"County config not found: {path}")
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"County config {path} is not a YAML mapping")
        missing = REQUIRED_KEYS - data.keys()
        if missing:
            raise ValueError(
                f"County config {path} missing required keys: {sorted(missing)}"
            )
        return cls(
            name=str(data["name"]),
            state=str(data["state"]),
            parcel_table=str(data["parcel_table"]),
            assessor_url=str(data["assessor_url"]),
            gis_endpoints={str(k): str(v) for k, v in (data["gis_endpoints"] or {}).items()},
            field_map={str(k): int(v) for k, v in (data["field_map"] or {}).items()},
        )


def load_county_config(county: str | None = None, adapters_root: Path = ADAPTERS_DIR) -> CountyConfig:
    """Resolve a county name to its adapter YAML and load it.

    ``county=None`` falls back to the PROPTOIT_COUNTY env var, then to the
    reference adapter (hamilton) so existing behavior is unchanged.
    """
    name = county or os.environ.get(COUNTY_ENV_VAR) or "hamilton"
    path = Path(adapters_root) / name / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No adapter config for county {name!r}: {path}")
    return CountyConfig.from_yaml(path)


def adapter_extras(path: str | Path) -> dict[str, Any]:
    """Raw YAML for an adapter, including keys CountyConfig ignores.

    Adapters may keep county-specific extras (code maps, city defaults)
    alongside the six CountyConfig fields; this reads them back.
    """
    data = yaml.safe_load(Path(path).read_text()) or {}
    return {k: v for k, v in data.items() if k not in REQUIRED_KEYS}
