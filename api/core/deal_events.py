"""
Deal events pipeline helper.

Provides an idempotent log_event() for the deal_events funnel table
(sent, viewed, pursued, closed).
"""
import os
import psycopg2
import psycopg2.extras

DB_DSN = os.getenv("DATABASE_URL")


def _get_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(DB_DSN)


def log_event(user_id: str, parcel_id: str, county_id: str, event_type: str) -> None:
    """Insert a deal event, idempotent on (user_id, parcel_id, county_id, event_type).

    Args:
        user_id: users.id (uuid)
        parcel_id: county parcel identifier
        county_id: county slug (default 'hamilton')
        event_type: one of sent, viewed, pursued, closed
    """
    valid = {"sent", "viewed", "pursued", "closed"}
    if event_type not in valid:
        raise ValueError(f"Invalid event_type. Must be one of: {valid}")

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO deal_events (user_id, parcel_id, county_id, event_type)
                VALUES (%s::uuid, %s, %s, %s)
                ON CONFLICT (user_id, parcel_id, county_id, event_type) DO NOTHING
                """,
                (user_id, parcel_id, county_id, event_type),
            )
        conn.commit()
    finally:
        conn.close()
