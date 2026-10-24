"""
Auth endpoints — free-tier key management.

  POST /api/auth/key        — Generate a new free-tier API key
  GET  /api/auth/keys       — List all keys for an email
  GET  /api/auth/usage      — Get usage stats for a key
  POST /api/auth/revoke     — Revoke a key
"""

import logging
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, HTTPException, Query, Request

import free_tier_auth

logger = logging.getLogger("proptoit.auth.routes")
router = APIRouter()


@router.post("/key")
async def generate_key(
    request: Request,
    email: str = Query(..., description="Email address for the API key"),
    display_name: Optional[str] = Query(None, description="Optional key name"),
) -> Dict[str, Any]:
    """Generate a free-tier API key.

    Returns the raw key — save it securely, it won't be shown again.
    Free tier: 60 requests/hour, 1,000 requests/month.
    """
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")

    raw_key, info = free_tier_auth.generate_free_key(email, display_name)
    if raw_key is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "key_limit_exceeded",
                "message": f"Maximum {free_tier_auth.FREE_TIER_LIMITS['max_keys_per_email']} free keys per email. Revoke an existing key first.",
            },
        )

    return {
        "success": True,
        "key": raw_key,
        "warning": "Save this key securely — it will not be shown again.",
        "limits": {
            "hourly": info["hourly_limit"],
            "monthly": info["monthly_limit"],
        },
        "info": info,
    }


@router.get("/keys")
async def list_keys(
    request: Request,
    email: str = Query(..., description="Email address to look up keys for"),
) -> Dict[str, list]:
    """List all free-tier API keys associated with an email.

    Requires a valid API key in the Authorization/x-api-key header whose
    owner email matches the requested email — prevents enumeration of
    arbitrary accounts' key metadata by email address alone.
    """
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")

    raw_key = request.headers.get("x-api-key")
    auth = request.headers.get("authorization", "")
    if not raw_key and auth.startswith("Bearer "):
        raw_key = auth[7:]
    if not raw_key:
        raise HTTPException(
            status_code=401,
            detail="API key required in Authorization or x-api-key header",
        )

    validated = free_tier_auth.validate_free_key(raw_key)
    if validated is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    key_owner_email = validated[0]
    if key_owner_email.lower().strip() != email.lower().strip():
        raise HTTPException(
            status_code=403,
            detail="Provided key does not belong to this email",
        )

    keys = free_tier_auth.get_all_keys_for_email(email)
    if keys is None:
        raise HTTPException(status_code=500, detail="Failed to retrieve keys")

    return {
        "success": True,
        "email": email,
        "keys": keys,
        "total": len(keys),
    }


@router.get("/usage")
async def get_usage(
    request: Request,
    api_key: str = Query(..., description="API key to check usage for"),
) -> Dict[str, Any]:
    """Get usage statistics for an API key."""
    usage = free_tier_auth.get_key_usage(api_key)
    if usage is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    hourly_pct = round(usage["hourly_used"] / usage["hourly_limit"] * 100, 1)
    monthly_pct = round(usage["monthly_used"] / usage["monthly_limit"] * 100, 1)

    return {
        "success": True,
        "tier": "free",
        "email": usage["email"],
        "hourly": {
            "used": usage["hourly_used"],
            "limit": usage["hourly_limit"],
            "percent": hourly_pct,
            "reset_in_seconds": 3600,
        },
        "monthly": {
            "used": usage["monthly_used"],
            "limit": usage["monthly_limit"],
            "percent": monthly_pct,
            "reset_on": "1st of each month",
        },
        "last_used_at": usage["last_used_at"],
        "created_at": usage["created_at"],
    }


@router.post("/revoke")
async def revoke_key_endpoint(
    request: Request,
) -> Dict[str, str]:
    """Revoke a free-tier API key. Key passed in body or x-api-key header (never query param)."""
    api_key = request.headers.get("x-api-key")
    if not api_key:
        try:
            body = await request.json()
            api_key = body.get("api_key")
        except Exception:
            api_key = None
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key required in body or x-api-key header")
    success = free_tier_auth.revoke_key(api_key)
    if not success:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return {
        "success": True,
        "message": "API key revoked successfully.",
    }
