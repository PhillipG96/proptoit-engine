"""
AgentMemory — persistent, labeled memory for agent observations.

Labels each memory as: observed, inferred, user_confirmed, stale, rejected,
or authoritative. This prevents model-generated guesses from silently
becoming facts.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

VALID_CONFIDENCE_LEVELS = {
    "observed",      # Directly seen in a source
    "inferred",      # Agent deduced but not verified
    "user_confirmed",# Human verified
    "stale",         # Was valid but now outdated
    "rejected",      # Was wrong or disputed
    "authoritative", # From a governing source (policy, contract)
}

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS agent_memory (
    id              SERIAL PRIMARY KEY,
    parcel_id       VARCHAR(50) NOT NULL,
    content         TEXT NOT NULL,
    confidence      VARCHAR(20) NOT NULL DEFAULT 'inferred',
    source_ref      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_agent_memory_parcel ON agent_memory(parcel_id, is_active);
"""


_ensure_once_done = False


def _ensure_table_once() -> None:
    """Create the agent_memory table exactly once, on the first static call.

    AgentMemory is used entirely through staticmethods, so nothing ever
    instantiates it and the __init__-driven _ensure_table() was dead code —
    the table never existed and save/list raised
    `relation "agent_memory" does not exist`.

    The guard runs at CALL time (never at import time), resolves the DSN
    from the environment at call time like the rest of the repo, and fails
    loudly (raises) instead of swallowing the error. The flag is only set
    after a successful CREATE, so a failed attempt is retried on the next
    call.
    """
    global _ensure_once_done
    if _ensure_once_done:
        return
    conn = AgentMemory._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE)
        conn.commit()
    finally:
        conn.close()
    _ensure_once_done = True


class AgentMemory:
    """Postgres-backed memory layer with confidence labeling."""

    def __init__(self):
        self._ensure_table()

    def _ensure_table(self):
        try:
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(CREATE_TABLE)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Could not create agent_memory table: {e}")

    @staticmethod
    def _get_conn():
        import os
        dsn = os.getenv("DATABASE_URL")
        return psycopg2.connect(dsn)

    @staticmethod
    def list_for_parcel(parcel_id: str) -> list[dict]:
        """Return active memories for a property, sorted by confidence priority."""
        _ensure_table_once()
        conn = AgentMemory._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, parcel_id, content, confidence, source_ref,
                           created_at, updated_at
                    FROM agent_memory
                    WHERE parcel_id = %s AND is_active = TRUE
                    ORDER BY
                        CASE confidence
                            WHEN 'authoritative' THEN 1
                            WHEN 'user_confirmed'  THEN 2
                            WHEN 'observed'         THEN 3
                            WHEN 'inferred'         THEN 4
                        END,
                    updated_at DESC
                """, (parcel_id,))
                rows = cur.fetchall()
        finally:
            conn.close()

        return [
            {
                "id":         r["id"],
                "content":    r["content"],
                "confidence": r["confidence"],
                "sourceRef":  r.get("source_ref"),
                "createdAt":  r["created_at"].isoformat(),
                "updatedAt":  r["updated_at"].isoformat(),
            }
            for r in rows
        ]

    @staticmethod
    def save(parcel_id: str, content: str, confidence: str = "inferred") -> int:
        """Save an observation. Returns the new row ID."""
        _ensure_table_once()
        if confidence not in VALID_CONFIDENCE_LEVELS:
            logger.warning(f"Invalid confidence level '{confidence}', defaulting to 'inferred'")
            confidence = "inferred"

        conn = AgentMemory._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO agent_memory (parcel_id, content, confidence, source_ref)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                """, (parcel_id, content, confidence, None))
                row = cur.fetchone()
                row_id = row[0] if row else -1
            conn.commit()
            return row_id
        finally:
            conn.close()

    @staticmethod
    def update_confidence(memory_id: int, confidence: str) -> bool:
        """Update the confidence label of a memory entry."""
        _ensure_table_once()
        if confidence not in VALID_CONFIDENCE_LEVELS:
            return False

        conn = AgentMemory._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE agent_memory SET confidence = %s, updated_at = NOW()
                    WHERE id = %s
                """, (confidence, memory_id))
                updated = cur.rowcount > 0
            conn.commit()
            return updated
        finally:
            conn.close()

    @staticmethod
    def mark_stale(memory_id: int) -> bool:
        """Deactivate a memory entry (soft delete)."""
        _ensure_table_once()
        conn = AgentMemory._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE agent_memory SET is_active = FALSE, confidence = 'stale', updated_at = NOW()
                    WHERE id = %s
                """, (memory_id,))
                updated = cur.rowcount > 0
            conn.commit()
            return updated
        finally:
            conn.close()
