"""PropToIt shared library package (config, adapters)."""

from .county_config import CountyConfig, load_county_config

__all__ = ["CountyConfig", "load_county_config"]
