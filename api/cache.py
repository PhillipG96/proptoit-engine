"""Minimal in-memory TTL cache for expensive API operations.

ponytail: dict + lock covers single-process FastAPI. If we ever run
multiple workers needing shared cache, swap for Redis (upgrade path).
"""

import time
import threading
from typing import Any, Optional
from functools import wraps


class SimpleCache:
    """In-memory TTL cache (get/set/clear used by core/queries and search)."""

    def __init__(self, maxsize: int = 512, default_ttl: float = 300.0):
        self.maxsize = maxsize
        self.default_ttl = default_ttl
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key in self._store:
                timestamp, value = self._store[key]
                if time.time() - timestamp < self.default_ttl:
                    return value
                del self._store[key]
            return None

    def set(self, key: str, value: Any, ttl: Optional[float] = None):
        with self._lock:
            if len(self._store) >= self.maxsize:
                oldest_key = min(self._store, key=lambda k: self._store[k][0])
                del self._store[oldest_key]
            self._store[key] = (time.time(), value)

    def clear(self):
        with self._lock:
            self._store.clear()


# Global cache instance
cache = SimpleCache(maxsize=512, default_ttl=300.0)


def cached(ttl: Optional[float] = None, key_prefix: str = ""):
    """Decorator that caches a function's return value (default TTL 300s)."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            cache_key = f"{key_prefix}{func.__name__}({args}, {kwargs})"
            result = cache.get(cache_key)
            if result is not None:
                return result
            result = func(*args, **kwargs)
            cache.set(cache_key, result, ttl=ttl)
            return result
        return wrapper
    return decorator
