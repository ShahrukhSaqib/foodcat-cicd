"""A tiny in-memory cache with an expiry time (the 'popular menus' box in the diagram).

On AWS this would be ElastiCache (Redis). The idea is identical: keep hot, rarely
changing data in memory so the database is not asked the same question thousands
of times during a lunch rush.
"""
import threading
import time


class CacheDown(Exception):
    """Raised when the cache is unavailable. Callers must fall back to the database."""


class TTLCache:
    def __init__(self, ttl=30):
        self.ttl = ttl
        self.down = False  # flip this to rehearse a cache failure
        self._items = {}
        self._lock = threading.Lock()

    def get(self, key):
        """Return the cached value, or None when missing or expired."""
        if self.down:
            raise CacheDown("cache is down")
        with self._lock:
            hit = self._items.get(key)
            if hit and hit[0] > time.time():
                return hit[1]
            self._items.pop(key, None)
            return None

    def set(self, key, value):
        if self.down:
            raise CacheDown("cache is down")
        with self._lock:
            self._items[key] = (time.time() + self.ttl, value)

    def clear(self):
        with self._lock:
            self._items.clear()
