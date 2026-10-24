"""
ContextAssembler — fulfills RetrievalContracts by assembling ContextBundles.

Instead of asking the LLM to piece together fragments from multiple tools,
the Assembler pre-assembles structured context bundles from our data sources:
- Property details + distress signals
- FSBO listings with property matches
- Foreclosure auctions
- Prior agent memory

The bundle is cached in Postgres so repeated runs don't re-fetch.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from knowledge_layer.models import ContextBundle, RetrievalContract, SourceProvenance
from knowledge_layer.cache import CompiledContextCache

logger = logging.getLogger(__name__)


class ContextAssembler:
    """
    Takes a RetrievalContract and returns a ContextBundle.

    Responsibilities:
    1. Check cache for a valid (non-stale) bundle
    2. If missing, pull data from each required source
    3. Assemble into a structured bundle with provenance
    4. Cache for future runs
    """

    # Map source names to query functions
    SOURCE_RESOLVERS = {
        "assessment":      "_fetch_assessment",
        "distress_signals": "_fetch_distress_signals",
        "fsbo":            "_fetch_fsbo_listings",
        "foreclosures":    "_fetch_foreclosures",
        "agent_memory":    "_fetch_agent_memory",
    }

    def __init__(self, cache: Optional[CompiledContextCache] = None) -> None:
        self.cache = cache or CompiledContextCache()

    def assemble(self, contract: RetrievalContract) -> ContextBundle:
        """Fulfill a retrieval contract: check cache, fetch if needed, return bundle."""
        bundle_id = self._make_bundle_id(contract)

        # Try cache first
        cached = self.cache.get(bundle_id)
        if cached and self._is_fresh(cached, contract.freshness_threshold_hours):
            logger.info(f"Cache HIT for bundle {bundle_id[:8]}")
            return cached
        elif cached:
            logger.info(f"Cache MISS (stale) for bundle {bundle_id[:8]}")

        # Fetch fresh data
        logger.info(f"Assembling bundle {bundle_id[:8]} for {contract.task_type}")
        data, sources = self._fetch_sources(contract, contract.county_id)

        bundle = ContextBundle(
            work_object=contract.work_object,
            bundle_id=bundle_id,
            sources=sources,
            data=data,
            compiled_at=datetime.now().isoformat(),
            freshness_ttl_hours=contract.freshness_threshold_hours,
        )

        # Cache it
        self.cache.set(bundle_id, bundle)
        return bundle

    def _make_bundle_id(self, contract: RetrievalContract) -> str:
        """Deterministic ID so we can cache and look up bundles."""
        raw = json.dumps({
            "task_type": contract.task_type,
            "work_object": contract.work_object,
            "required_sources": sorted(contract.required_sources),
            "county_id": contract.county_id,
        }, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    @staticmethod
    def _is_fresh(bundle: ContextBundle, threshold_hours: int) -> bool:
        compiled = datetime.fromisoformat(bundle.compiled_at)
        cutoff = datetime.now(compiled.tzinfo) - timedelta(hours=threshold_hours)
        return compiled > cutoff

    def _fetch_sources(self, contract: RetrievalContract, county_id: str = "hamilton") -> tuple[dict[str, Any], list[SourceProvenance]]:
        """Pull data from each required source and assemble."""
        from core import queries

        data = {}
        sources = []

        parcel_id = contract.work_object

        if "assessment" in contract.required_sources:
            prop = queries.get_property_by_parcel_id(parcel_id, county_id=county_id)
            if prop:
                data["assessment"] = prop
                sources.append(SourceProvenance(
                    type="assessment",
                    uri=f"postgres://properties WHERE parcel_id={parcel_id}",
                    last_updated=prop.get("updatedAt"),
                ))

        if "distress_signals" in contract.required_sources:
            signals = queries.get_distress_signals_for_parcel(parcel_id, county_id=county_id)
            data["distressSignals"] = signals
            sources.append(SourceProvenance(
                type="distress_signal",
                uri=f"postgres://distress_signals WHERE parcel_id={parcel_id}",
            ))

        if "foreclosures" in contract.required_sources:
            foreclosures = [
                f for f in queries.list_foreclosures(status="at_risk", days_ahead=365, limit=10, county_id=county_id)
                if f["parcelId"] == parcel_id
            ]
            data["foreclosures"] = foreclosures
            sources.append(SourceProvenance(
                type="foreclosure",
                uri="postgres://foreclosures",
            ))

        if "fsbo" in contract.required_sources:
            fsbo_listings = queries.list_fsbo_listings(limit=50, offset=0, county_id=county_id)
            # Filter to listings near or matching this parcel
            matched = [
                listing for listing in fsbo_listings["data"]
                if listing.get("matchedParcelId") == parcel_id
            ]
            data["fsboMatched"] = matched
            sources.append(SourceProvenance(
                type="fsbo_listing",
                uri="postgres://fsbo_listings",
            ))

        if contract.include_prior_memory and "agent_memory" in contract.required_sources:
            prior = self._fetch_agent_memory(parcel_id)
            data["priorMemory"] = prior
            sources.append(SourceProvenance(
                type="agent_memory",
                uri=f"postgres://agent_memory WHERE parcel_id={parcel_id}",
            ))

        return data, sources

    def _fetch_agent_memory(self, parcel_id: str) -> list[dict]:
        """Fetch prior agent memory for this property."""
        from knowledge_layer.memory import AgentMemory
        return AgentMemory.list_for_parcel(parcel_id)

    # Placeholder for write-back — called by agent after task completion
    def write_memory(self, parcel_id: str, content: str, confidence: str = "inferred") -> None:
        """Persist agent-generated observations back to the memory layer."""
        from knowledge_layer.memory import AgentMemory
        AgentMemory.save(parcel_id, content, confidence)
