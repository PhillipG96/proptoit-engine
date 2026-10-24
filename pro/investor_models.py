"""
import logging

logger = logging.getLogger(__name__)

Investor service — Pydantic models
===================================
Request/response schemas for investor profiles, deal matching,
feedback, and digest tracking.
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class InvestorCreate(BaseModel):
    """Create a new investor account (auto-enrolled in trial)."""
    email: str
    full_name: Optional[str] = None
    tier: str = "basic"  # basic, pro, premium


class InvestorProfileCreate(BaseModel):
    """Initial investor profile with deal preferences."""
    deal_type: str = "both"  # flip, hold, both
    max_total_investment: float
    min_equity_pct: float = 20.0
    target_areas: List[str] = []
    property_types: List[str] = ["single_family"]
    max_rehab_cost: Optional[float] = 50000.0
    preferred_signals: List[str] = []
    county_ids: List[str] = ["hamilton"]


class InvestorProfileUpdate(BaseModel):
    """Partial update — all fields optional."""
    deal_type: Optional[str] = None
    max_total_investment: Optional[float] = None
    min_equity_pct: Optional[float] = None
    target_areas: Optional[List[str]] = None
    property_types: Optional[List[str]] = None
    max_rehab_cost: Optional[float] = None
    preferred_signals: Optional[List[str]] = None
    county_ids: Optional[List[str]] = None


class InvestorResponse(BaseModel):
    """Investor identity + subscription state."""
    id: str
    email: str
    tier: str
    status: Optional[str] = None
    created_at: datetime
    trial_ends_at: Optional[datetime] = None
    full_name: Optional[str] = None


class InvestorProfileResponse(BaseModel):
    """Full investor profile with deal preferences."""
    id: int
    investor_id: str
    deal_type: str
    max_total_investment: float
    min_equity_pct: float
    target_areas: List[str]
    property_types: List[str]
    max_rehab_cost: Optional[float]
    preferred_signals: List[str]
    county_ids: List[str]
    updated_at: datetime


class FeedbackAction(BaseModel):
    """Investor feedback on a matched deal."""
    action: str  # pursue, pass, save, skip


class DigestLogResponse(BaseModel):
    """Record of a digest email/notification sent to an investor."""
    id: int
    investor_id: str
    sent_at: datetime
    deal_count: int
    parcel_ids: List[dict]
    delivery_status: str