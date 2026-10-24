"""
import logging

logger = logging.getLogger(__name__)

Core Pydantic models for ProToIt API
Defined once to avoid duplication between endpoints and MCP schema
"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, date

# ── Property model (matches SCHEMA.sql) ──
class PropertyBase(BaseModel):
    """Base property model with all required fields"""
    parcel_id: str = Field(..., description="Hamilton County parcel ID")
    county: str = Field(default="Hamilton")
    address: str = Field(..., description="Street address")
    city: str = Field(default="Chattanooga")
    state: str = Field(default="TN")
    postal_code: str
    lot_size_sqft: Optional[float] = None
    building_sqft: Optional[float] = None
    year_built: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    last_sale_price: Optional[float] = None
    last_sale_date: Optional[date] = None
    assessed_value: Optional[float] = None
    tax_amount: Optional[float] = None
    owner_of_record: Optional[str] = None
    owner_mailing_address: Optional[str] = None
    owner_occupant: bool = False

class PropertyCreate(PropertyBase):
    """Schema for creating a new property"""
    pass

class PropertyUpdate(PropertyBase):
    """Schema for updating a property (all fields optional)"""
    parcel_id: Optional[str] = None

class PropertyResponse(PropertyBase):
    """Full property response with metadata"""
    id: str
    geocoords_point: Optional[list] = None  # lat/lng [lat, lng]
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True

# ── Tax history model ──
class TaxHistoryBase(BaseModel):
    property_id: str
    tax_year: int = Field(..., ge=1900, le=2100)
    assessed_value: float
    tax_amount: float
    source: str = "hamilton_co_assessor"

class TaxHistoryResponse(TaxHistoryBase):
    id: str
    scraped_at: datetime
    
    class Config:
        from_attributes = True

# ── Distress signal model ──
class DistressSignalBase(BaseModel):
    property_id: str
    distress_type: str  # pre_foreclosure, foreclosure_auction, tax_delinquent, etc.
    severity: Optional[str] = None
    details: Optional[str] = None
    source: Optional[str] = None

class DistressSignalResponse(DistressSignalBase):
    id: str
    reported_at: datetime
    
    class Config:
        from_attributes = True

# ── Search/query models ──
class PropertyQuery(BaseModel):
    """Query parameters for property search"""
    parcel_id: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_built: Optional[int] = None
    max_built: Optional[int] = None
    min_bedrooms: Optional[int] = None
    min_bathrooms: Optional[float] = None
    distress_types: Optional[list] = None
    has_distress: Optional[bool] = None
    owner_occupant: Optional[bool] = None
    has_geom: Optional[bool] = None
    
    # Geo bounds for radius search
    lat: Optional[float] = None
    lng: Optional[float] = None
    radius_km: Optional[float] = 50.0  # default 50km radius
    
    # Pagination
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

# ── API response wrapper ──
class APIResponse(BaseModel):
    """Standard API response envelope"""
    success: bool = True
    data: Optional[object] = None
    message: Optional[str] = None
    pagination: Optional[dict] = None
    
    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "data": {},
                "message": None,
                "pagination": {"total": 100, "limit": 10, "offset": 0}
            }
        }
