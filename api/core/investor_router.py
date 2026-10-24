"""
import logging

logger = logging.getLogger(__name__)

Investor API Router
===================
Endpoints for investor lifecycle: signup, profile CRUD, deal matching,
feedback submission, and digest history.
"""
import jwt as pyjwt
import psycopg2
import psycopg2.extras
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Tuple

import jwt as pyjwt
from .investor_models import (
    InvestorCreate, InvestorProfileCreate, InvestorProfileUpdate,
    InvestorResponse, InvestorProfileResponse,
    FeedbackAction, DigestLogResponse,
)

from .matching import match_deals_for_investor
from .deal_events import log_event
from sanitize import (
    sanitize_email, sanitize_name, sanitize_url_path,
)
from services.deal_package import stream_deal_package_pdf

router = APIRouter(prefix="/investors", tags=["investors"])

DB_DSN = os.getenv("DATABASE_URL")
JWT_SECRET = os.getenv("JWT_SECRET", "")


def _get_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(DB_DSN)


def _require_investor_access(request: Request, investor_id: str) -> None:
    """Verify the session JWT belongs to this investor (or is an admin).

    Same shared HS256 session JWT as the web app / admin router.
    Raises 401 if no/invalid token, 403 if not the owner or admin.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth header")
    jwt_val = auth[len("Bearer "):]

    if not JWT_SECRET:
        raise HTTPException(status_code=500, detail="JWT signing key not configured")

    try:
        payload = pyjwt.decode(jwt_val, JWT_SECRET, algorithms=["HS256"])
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    user_id = payload.get("userId")
    if not user_id:
        raise HTTPException(status_code=401, detail="Malformed session")

    if user_id == investor_id:
        return

    # Allow admins to access any investor.
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT is_admin FROM users WHERE id = %s::uuid", (user_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                raise HTTPException(status_code=403, detail="Not authorized for this investor")
    finally:
        conn.close()


# ── Investor CRUD ─────────────────────────────────────────────────

@router.post("/", response_model=InvestorResponse)
def create_investor(investor: InvestorCreate) -> InvestorResponse:
    """Create a new investor account in trial status (7-day trial)."""
    email = sanitize_email(investor.email)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    full_name = sanitize_name(investor.full_name) if investor.full_name else None

    valid_tiers = {"basic", "pro", "premium"}
    if investor.tier not in valid_tiers:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Must be one of: {valid_tiers}")

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO users (id, email, password_hash, name, tier, subscription_status, created_at, updated_at)
                VALUES (gen_random_uuid(), %s, '$2a$12$placeholderhashforinvestormigration', %s, %s, 'trial', NOW(), NOW())
                RETURNING id, email, tier, subscription_status AS status, created_at, updated_at AS trial_ends_at, name AS full_name
            """, (email, full_name, investor.tier))
            row = cur.fetchone()
            conn.commit()
            if not row:
                raise HTTPException(status_code=500, detail="Failed to create investor")
            return InvestorResponse(**dict(row))
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Investor with this email already exists")
    finally:
        conn.close()


# ── Profile CRUD ──────────────────────────────────────────────────

def _require_investor(investor_id: str) -> dict:
    """Fetch an investor (users row) or raise 404."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s::uuid", (investor_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Investor not found")
            return dict(row)
    finally:
        conn.close()


def _profile_row_to_response(row) -> InvestorProfileResponse:
    """Convert a DB row to the response model."""
    return InvestorProfileResponse(
        id=row["id"],
        investor_id=row["investor_id"],
        deal_type=row["deal_type"],
        max_total_investment=float(row["max_total_investment"]),
        min_equity_pct=float(row["min_equity_pct"]),
        target_areas=row["target_areas"] or [],
        property_types=row["property_types"] or ["single_family"],
        max_rehab_cost=float(row["max_rehab_cost"]) if row["max_rehab_cost"] else None,
        preferred_signals=row["preferred_signals"] or [],
        county_ids=row["county_ids"] or ["hamilton"],
        updated_at=row["updated_at"],
    )


@router.post("/{investor_id}/profile", response_model=InvestorProfileResponse)
def create_profile(investor_id: str, profile: InvestorProfileCreate, request: Request) -> InvestorProfileResponse:
    """Create an investor profile with deal preferences."""
    _require_investor_access(request, investor_id)
    _require_investor(investor_id)

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO investor_profiles (
                    investor_id, deal_type, max_total_investment, min_equity_pct,
                    target_areas, property_types, max_rehab_cost,
                    preferred_signals, county_ids
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (
                investor_id,
                profile.deal_type,
                profile.max_total_investment,
                profile.min_equity_pct,
                profile.target_areas,
                profile.property_types,
                profile.max_rehab_cost,
                profile.preferred_signals,
                profile.county_ids,
            ))
            row = cur.fetchone()
            conn.commit()
            return _profile_row_to_response(row)
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Profile already exists for this investor")
    finally:
        conn.close()


@router.get("/{investor_id}/profile", response_model=InvestorProfileResponse)
def get_profile(investor_id: str, request: Request) -> InvestorProfileResponse:
    _require_investor_access(request, investor_id)
    """Get an investor's profile."""
    _require_investor(investor_id)

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM investor_profiles WHERE investor_id = %s
            """, (investor_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Profile not found")
            return _profile_row_to_response(row)
    finally:
        conn.close()


@router.put("/{investor_id}/profile", response_model=InvestorProfileResponse)
def update_profile(investor_id: str, profile: InvestorProfileUpdate, request: Request) -> InvestorProfileResponse:
    _require_investor_access(request, investor_id)
    """Partially update an investor profile."""
    _require_investor(investor_id)

    # Build dynamic update from non-None fields
    updates: dict[str, Any] = {}
    for field in [
        "deal_type", "max_total_investment", "min_equity_pct",
        "target_areas", "property_types", "max_rehab_cost",
        "preferred_signals", "county_ids",
    ]:
        val = getattr(profile, field, None)
        if val is not None:
            updates[field] = val

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            set_clauses = []
            params = []
            for field, val in updates.items():
                set_clauses.append(f"{field} = %s")
                params.append(val)

            params.append(investor_id)
            query = (
                f"UPDATE investor_profiles SET {', '.join(set_clauses)}, "
                f"updated_at = NOW() WHERE investor_id = %s RETURNING *"
            )
            cur.execute(query, params)
            row = cur.fetchone()
            conn.commit()

            if not row:
                raise HTTPException(status_code=404, detail="Profile not found")
            return _profile_row_to_response(row)
    finally:
        conn.close()


# ── Deal Matching ────────────────────────────────────────────────

@router.post("/{investor_id}/matches", response_model=List[Dict[str, Any]])
def get_matches(investor_id: str, request: Request) -> List[Dict[str, Any]]:
    _require_investor_access(request, investor_id)
    """Run the matching engine for this investor and return ranked deals."""
    _require_investor(investor_id)
    return match_deals_for_investor(investor_id)


# ── Feedback ──────────────────────────────────────────────────────

class FeedbackSubmit(BaseModel):
    """Extended feedback with parcel context."""
    action: str  # pursue, pass, save, skip
    parcel_id: str
    county_id: str = "hamilton"


@router.post("/{investor_id}/feedback/{parcel_id}")
def submit_feedback_with_parcel(
    request: Request,
    investor_id: str,
    parcel_id: str,
    action: str,
    county_id: str = "hamilton",
) -> Dict[str, str]:
    """Record investor feedback on a specific deal.

    Args via path/query:
        investor_id: investor record ID
        parcel_id: parcel identifier (path param)
        action: pursue, pass, save, skip (query param)
        county_id: county slug (query param, default 'hamilton')
    """
    _require_investor(investor_id)
    _require_investor_access(request, investor_id)

    valid_actions = {"pursue", "pass", "save", "skip"}
    if action not in valid_actions:
        raise HTTPException(status_code=400, detail=f"Invalid action. Must be one of: {valid_actions}")

    parcel_id = sanitize_url_path(parcel_id)
    if not parcel_id:
        raise HTTPException(status_code=400, detail="Invalid parcel_id")

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO deal_feedback (investor_id, parcel_id, county_id, action)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (investor_id, parcel_id, county_id, action)
                DO NOTHING
                RETURNING id
            """, (investor_id, parcel_id, county_id, action))
            row = cur.fetchone()
            conn.commit()
    finally:
        conn.close()

    # Log 'pursued' event when action is pursue (idempotent)
    if action == "pursue":
        try:
            log_event(investor_id, parcel_id, county_id, "pursued")
        except Exception:
            pass

    if not row:
        return {"status": "already_recorded", "action": action}
    return {"status": "recorded", "id": row["id"], "action": action}


# ── Digest History ────────────────────────────────────────────────

@router.get("/{investor_id}/digests", response_model=List[DigestLogResponse])
def get_digests(investor_id: str, request: Request) -> List[DigestLogResponse]:
    _require_investor_access(request, investor_id)
    """Get digest delivery history for an investor."""
    _require_investor(investor_id)

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM digest_logs
                WHERE investor_id = %s
                ORDER BY sent_at DESC
            """, (investor_id,))
            rows = cur.fetchall()
            return [
                DigestLogResponse(
                    id=r["id"],
                    investor_id=r["investor_id"],
                    sent_at=r["sent_at"],
                    deal_count=r["deal_count"],
                    parcel_ids=r["parcel_ids"] or [],
                    delivery_status=r["delivery_status"],
                )
                for r in rows
            ]
    finally:
        conn.close()


# ── Deal Package (PDF) ────────────────────────────────────────────

@router.get("/{investor_id}/deal-package")
def get_deal_package_pdf(
    request: Request,
    investor_id: str,
    parcel_id: str = Query(..., description="County parcel ID"),
    county_id: str = Query(default="hamilton"),
) -> Response:
    """Generate and stream a one-page deal package PDF for a matched deal.

    Returns the PDF as an inline download with branding, deal score,
    financials, property details, distress signals, and rehab scope.
    """
    _require_investor_access(request, investor_id)
    _require_investor(investor_id)
    parcel_id = sanitize_url_path(parcel_id)
    if not parcel_id:
        raise HTTPException(status_code=400, detail="Invalid parcel_id")

    # Log 'viewed' event (idempotent)
    try:
        log_event(investor_id, parcel_id, county_id, "viewed")
    except Exception:
        pass

    try:
        pdf_bytes, filename = stream_deal_package_pdf(parcel_id, county_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename={filename}",
            "Content-Length": str(len(pdf_bytes)),
        },
    )


# ── Digest Generation ────────────────────────────────────────────

@router.post("/{investor_id}/generate-digest")
def generate_digest(investor_id: str, request: Request) -> Dict[str, Any]:
    _require_investor_access(request, investor_id)
    """Generate and send a digest for a specific investor immediately.

    Runs the matching engine, generates PDFs, sends email, and logs the result.
    """
    _require_investor(investor_id)

    from services.deal_package import assemble_deal_package
    from services.email import send_digest_email
    from core.matching import match_deals_for_investor
    import json
    from datetime import datetime

    # 1. Run matching
    try:
        matches = match_deals_for_investor(investor_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Matching failed: {e}")

    if not matches:
        return {"status": "no_matches", "deal_count": 0}

    # 2. Generate PDFs
    pdf_attachments = []
    deal_summaries = []
    parcel_ids = []

    for match in matches[:5]:
        parcel_id = match.get("parcelId", "")
        county_id = match.get("countyId", "hamilton")
        try:
            package = assemble_deal_package(parcel_id, county_id)
            if "error" not in package:
                deal_summaries.append(package["property"])
                parcel_ids.append(parcel_id)
                safe_parcel = parcel_id.replace("/", "_").replace("\\", "_")
                pdf_attachments.append((f"Deal_{safe_parcel}.pdf", package["pdf_bytes"]))
        except Exception:
            pass

    if not pdf_attachments:
        return {"status": "no_pdfs", "deal_count": len(matches)}

    # 3. Get investor email
    investor = _require_investor(investor_id)
    email = investor["email"]
    full_name = investor.get("name") or email.split("@")[0]

    # 4. Send email
    sent = send_digest_email(
        to_email=email,
        investor_name=full_name,
        deals=deal_summaries,
        pdf_attachments=pdf_attachments,
    )

    # 5. Log
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO digest_logs (investor_id, sent_at, deal_count, parcel_ids, delivery_status)
                VALUES (%s, %s, %s, %s::jsonb, %s)
            """, (
                investor_id,
                datetime.now(),
                len(matches),
                json.dumps(parcel_ids),
                "sent" if sent else "failed",
            ))
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "sent" if sent else "failed",
        "deal_count": len(matches),
        "pdf_count": len(pdf_attachments),
        "parcel_ids": parcel_ids,
    }