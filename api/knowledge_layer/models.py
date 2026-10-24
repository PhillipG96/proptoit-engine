"""
import logging

logger = logging.getLogger(__name__)

Knowledge layer schemas for prepared agent context.

Defines the RetrievalContract (what the agent needs) and ContextBundle
(what the system delivers). This shifts the retrieval problem from
"find similar text" to "assemble task-shaped context."

See: ~/proptoit/docs/architecture/retrieval-contract.md
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ─── Source Provenance ───────────────────────────────────────────────────────

class SourceProvenance(BaseModel):
    """Tracks where a piece of context came from."""
    type: str  # "assessment", "distress_signal", "fsbo_listing", "foreclosure", "agent_memory"
    uri: str
    last_updated: Optional[str] = None


# ─── Context Bundle ──────────────────────────────────────────────────────────

class ContextBundle(BaseModel):
    """The prepared context sent to the agent. Replaces raw search results."""
    work_object: str           # e.g., parcel_id, lead_id
    bundle_id: str             # Unique identifier for caching
    sources: list[SourceProvenance] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    compiled_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    freshness_ttl_hours: int = 24  # How long this bundle remains valid


# ─── Retrieval Contract ──────────────────────────────────────────────────────

class RetrievalContract(BaseModel):
    """
    Defines what an agent needs to receive before it can act.

    The contract is declarative: it lists the sources, structure, and
    constraints. The ContextAssembler fulfills it by pulling from the
    right systems and formatting the bundle.
    """
    task_type: str = Field(description="The work primitive: deal_analysis, fsbo_outreach, foreclosure_monitor, property_search")
    work_object: str = Field(description="The entity being worked on: parcel_id, address, listing_id")
    required_sources: list[str] = Field(
        default_factory=list,
        description="List of source types to include: assessment, distress_signals, fsbo, foreclosures, agent_memory"
    )
    include_prior_memory: bool = True
    freshness_threshold_hours: int = 24  # Reject cached bundles older than this
    county_id: str = "hamilton"  # Which county's data to pull from
    permissions: Optional[dict] = None  # Role-based filtering (future: RBAC)
