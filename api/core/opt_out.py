"""Opt-out registry — property owners can request removal from outreach lists."""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict

import logging

logger = logging.getLogger(__name__)

DB_DSN = os.environ.get("DATABASE_URL", "postgresql://proptoit:***@localhost:5432/proptoit")

router = APIRouter()

# Simple in-process rate limit for public opt-out endpoints (60 req / 5 min / IP)
from collections import OrderedDict, defaultdict
from threading import Lock as _Lock
from fastapi import Request as _Request
import time as _time

_OPTOUT_LIMIT = 60
_OPTOUT_WINDOW = 300
_optout_hits: dict[str, list[float]] = defaultdict(list)
_optout_lock = _Lock()


def _optout_rate_limit(request: _Request) -> None:
    ip = request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown")
    now = _time.time()
    with _optout_lock:
        hits = [t for t in _optout_hits[ip] if t > now - _OPTOUT_WINDOW]
        if len(hits) >= _OPTOUT_LIMIT:
            raise HTTPException(429, "Too many requests. Try again later.")
        hits.append(now)
        _optout_hits[ip] = hits
        if len(_optout_hits) > 10000:
            _optout_hits.pop(next(iter(_optout_hits)))


class OptOutRequest(BaseModel):
    owner_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    property_address: Optional[str] = None
    parcel_id: Optional[str] = None
    reason: Optional[str] = None


class OptOutVerifyRequest(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None
    owner_name: Optional[str] = None
    parcel_id: Optional[str] = None


def sanitize_input(value: str) -> str:
    """Sanitize input to prevent SQL injection."""
    if not value:
        return value
    # Remove SQL metacharacters
    return value.replace("'", "").replace("--", "").replace(";", "")


@router.post("/api/opt-out")
def submit_opt_out(req: OptOutRequest, request: _Request) -> Dict[str, str]:
    """Submit an opt-out request."""
    _optout_rate_limit(request)
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        INSERT INTO opt_out_requests (owner_name, email, phone, property_address, parcel_id, reason)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, requested_at
    """, (req.owner_name, req.email, req.phone, req.property_address, req.parcel_id, req.reason))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return {"status": "submitted", "request_id": row["id"], "requested_at": str(row["requested_at"])}


@router.post("/api/opt-out/verify")
def verify_opt_out(req: OptOutVerifyRequest, request: _Request) -> dict:
    """Check if a property owner has opted out."""
    _optout_rate_limit(request)
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    conditions = []
    params = []
    if req.email:
        conditions.append("email = %s")
        params.append(sanitize_input(req.email))
    if req.phone:
        conditions.append("phone = %s")
        params.append(sanitize_input(req.phone))
    if req.owner_name:
        conditions.append("lower(owner_name) = lower(%s)")
        params.append(sanitize_input(req.owner_name))
    if req.parcel_id:
        conditions.append("parcel_id = %s")
        params.append(sanitize_input(req.parcel_id))

    if not conditions:
        cur.close()
        conn.close()
        raise HTTPException(400, "Provide at least one identifier")

    where_clause = " AND ".join(conditions)
    query = (
        "SELECT id, owner_name, email, phone, requested_at "
        "FROM opt_out_requests "
        f"WHERE {where_clause} "
        "ORDER BY requested_at DESC LIMIT 1"
    )
    cur.execute(query, params)
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return {"opted_out": True, "request_id": row["id"], "requested_at": str(row["requested_at"])}
    return {"opted_out": False}


@router.get("/api/opt-out/count")
def opt_out_count() -> Dict[str, int]:
    """Get total opt-out count."""
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM opt_out_requests")
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return {"count": count}
