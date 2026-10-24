"""
CompiledContextCache — persists ContextBundles in Postgres.

Prevents the agent from rediscovering the same context every run.
Stale bundles are replaced on next fetch.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

import psycopg2
import psycopg2.extras

from knowledge_layer.models import ContextBundle, SourceProvenance

logger = logging.getLogger(__name__)


class _Encoder(json.JSONEncoder):
    """JSON encoder that handles Decimal, datetime, and other DB types."""
    def default(self, o) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS compiled_context (
    bundle_id     VARCHAR(64) PRIMARY KEY,
    work_object   VARCHAR(255) NOT NULL,
    task_type     VARCHAR(100) NOT NULL,
    sources       JSONB NOT NULL DEFAULT '[]',
    data          JSONB NOT NULL DEFAULT '{}',
    compiled_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    freshness_ttl_hours INT NOT NULL DEFAULT 24,
    is_stale      BOOLEAN NOT NULL DEFAULT FALSE
);
"""


class CompiledContextCache:
    """Postgres-backed cache for compiled context bundles."""

    def __init__(self):
        self._ensure_table()

    def _get_conn(self):
        import os
        dsn = os.getenv("DATABASE_URL")
        return psycopg2.connect(dsn)

    def _ensure_table(self):
        """Create the compiled_context table if it doesn't exist."""
        try:
            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(CREATE_TABLE)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Could not create compiled_context table: {e}")

    def get(self, bundle_id: str) -> Optional[ContextBundle]:
        """Retrieve a cached bundle by ID. Returns None if missing or stale."""
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT bundle_id, work_object, sources, data,
                           compiled_at, freshness_ttl_hours, is_stale
                    FROM compiled_context
                    WHERE bundle_id = %s AND is_stale = FALSE
                """, (bundle_id,))
                row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            return None

        sources_raw = [SourceProvenance(**s) for s in (row["sources"] if isinstance(row["sources"], list) else json.loads(row["sources"]))]
        return ContextBundle(
            work_object=row["work_object"],
            bundle_id=row["bundle_id"],
            sources=sources_raw,
            data=(row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])),
            compiled_at=row["compiled_at"].isoformat(),
            freshness_ttl_hours=row["freshness_ttl_hours"],
        )

    def set(self, bundle_id: str, bundle: ContextBundle) -> None:
        """Cache a bundle. Upserts if already present."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO compiled_context
                        (bundle_id, work_object, task_type, sources, data, compiled_at, freshness_ttl_hours, is_stale)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (bundle_id) DO UPDATE SET
                        sources = EXCLUDED.sources,
                        data = EXCLUDED.data,
                        compiled_at = EXCLUDED.compiled_at,
                        is_stale = FALSE
                """, (
                    bundle_id,
                    bundle.work_object,
                    "deal_analysis",  # Task type would come from contract
                    json.dumps([s.model_dump() for s in bundle.sources], cls=_Encoder),
                    json.dumps(bundle.data, cls=_Encoder),
                    datetime.fromisoformat(bundle.compiled_at),
                    bundle.freshness_ttl_hours,
                    False,
                ))
            conn.commit()
        finally:
            conn.close()

    def mark_stale(self, bundle_id: str) -> bool:
        """Mark a bundle as stale instead of deleting (audit trail)."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE compiled_context SET is_stale = TRUE
                    WHERE bundle_id = %s
                """, (bundle_id,))
                updated = cur.rowcount > 0
            conn.commit()
            return updated
        finally:
            conn.close()
