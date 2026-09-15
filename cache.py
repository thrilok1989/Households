"""
cache.py

Minimal in-process TTL cache used to avoid re-hitting the Dhan API on
every Streamlit rerun (Streamlit reruns the whole script on most widget
interactions, so naive calls would hammer the API).

This is intentionally simple — a dict with expiry timestamps — rather than
a dependency on an external cache service, since the app is single-process.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional


class TTLCache:
    def __init__(self):
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if not entry:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._store[key] = (time.time() + ttl_seconds, value)

    def get_or_set(self, key: str, ttl_seconds: float, producer: Callable[[], Any]) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = producer()
        self.set(key, value, ttl_seconds)
        return value

    def clear(self) -> None:
        self._store.clear()


# Module-level singleton — Streamlit's script reruns import this module once
# per session (cached via st.cache_resource in app.py), giving it an
# effective lifetime across reruns within a session.
cache = TTLCache()
