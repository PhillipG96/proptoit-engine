"""
Admin API Router
================
JWT-gated endpoints for the single admin (Phillip).
Verifies the shared session JWT (same signing key as web app),
then checks users.is_admin.
"""
import os
import jwt as pyjwt
import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["admin"])

DB_DSN = os.getenv("DATABASE_URL")
JWT_SECRET = os.getenv("JWT_SECRET", "")


def _get_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(DB_DSN)


def _require_admin(request: Request) -> str:
    """Verify the session JWT and confirm the user is an admin.

    Returns the admin's user id (uuid string).
    Raises 401 if no/invalid JWT, 403 if not an admin.
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

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT is_admin FROM users WHERE id = %s::uuid", (user_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                raise HTTPException(status_code=403, detail="Admin access required")
    finally:
        conn.close()

    return user_id


@router.get("/investors")
def admin_investors(request: Request):
    """List all investors with tier and subscription status."""
    _require_admin(request)
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, email, name, tier, subscription_status, is_admin, created_at
                FROM users
                ORDER BY created_at DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"investors": rows}


@router.get("/pipeline")
def admin_pipeline(request: Request):
    """Deal pipeline funnel: counts by event type + per-investor breakdown."""
    _require_admin(request)
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Funnel counts
            cur.execute("""
                SELECT event_type, count(*) AS count
                FROM deal_events
                GROUP BY event_type
            """)
            funnel = {r["event_type"]: r["count"] for r in cur.fetchall()}

            # Per-investor breakdown
            cur.execute("""
                SELECT u.id, u.email, u.name, u.tier,
                       COUNT(de.id) FILTER (WHERE de.event_type = 'sent') AS sent,
                       COUNT(de.id) FILTER (WHERE de.event_type = 'viewed') AS viewed,
                       COUNT(de.id) FILTER (WHERE de.event_type = 'pursued') AS pursued,
                       COUNT(de.id) FILTER (WHERE de.event_type = 'closed') AS closed
                FROM users u
                LEFT JOIN deal_events de ON de.user_id = u.id
                GROUP BY u.id, u.email, u.name, u.tier
                ORDER BY sent DESC
            """)
            investors = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"funnel": funnel, "investors": investors}


@router.get("/revenue")
def admin_revenue(request: Request):
    """MRR computed locally from tier + subscription_status."""
    _require_admin(request)
    tier_prices = {"basic": 250, "pro": 350, "premium": 500}
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, email, name, tier, subscription_status
                FROM users
                WHERE subscription_status IN ('active', 'trial')
            """)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    mrr = 0
    for r in rows:
        if r["subscription_status"] == "active":
            mrr += tier_prices.get(r["tier"], 0)

    return {
        "mrr": mrr,
        "active_count": sum(1 for r in rows if r["subscription_status"] == "active"),
        "trial_count": sum(1 for r in rows if r["subscription_status"] == "trial"),
        "investors": rows,
    }


@router.post("/deals/{parcel_id}/close")
def admin_close_deal(parcel_id: str, request: Request, county_id: str = "hamilton", user_id: str = None):
    """Manually mark a deal as closed (admin action)."""
    admin_id = _require_admin(request)
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id query param required")

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO deal_events (user_id, parcel_id, county_id, event_type)
                VALUES (%s::uuid, %s, %s, 'closed')
                ON CONFLICT (user_id, parcel_id, county_id, event_type) DO NOTHING
                """,
                (user_id, parcel_id, county_id),
            )
        conn.commit()
    finally:
        conn.close()
    return {"status": "closed", "parcel_id": parcel_id, "user_id": user_id}
