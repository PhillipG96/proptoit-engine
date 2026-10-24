"""
import logging

logger = logging.getLogger(__name__)

Geocoded properties endpoint — queries DB directly for geocoded coordinates.
"""
import os
from fastapi import APIRouter
import psycopg2

router = APIRouter()

DB_URL = os.getenv("DATABASE_URL")


@router.get("")
@router.get("/")
def get_geocoded():
    """Return geocoded property coordinates from the DB."""
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT parcel_id, lat, lng, address
            FROM properties
            WHERE lat IS NOT NULL AND lng IS NOT NULL
        """)
        rows = cur.fetchall()
        results = [
            {
                "parcel_id": row[0],
                "lat": float(row[1]),
                "lng": float(row[2]),
                "address": row[3],
            }
            for row in rows
        ]
        cur.close()
        conn.close()
        return results
    except Exception as e:
        return {"error": str(e)}
