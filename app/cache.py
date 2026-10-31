"""A tiny, version-aware response cache.

Aggregates and model fits are pure functions of (query parameters, dataset). The
dataset only changes when an ingestion run writes something, at which point the
``data_version`` is bumped. Keying the cache on that version means entries never
need explicit invalidation and can never serve stale numbers.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class VersionedCache:
    def __init__(self, max_entries: int = 512):
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(namespace: str, version: int, params: dict[str, Any]) -> str:
        blob = json.dumps(params, sort_keys=True, default=str)
        return f"{namespace}:{version}:{hashlib.sha1(blob.encode()).hexdigest()}"

    def get_or_compute(self, key: str, compute: Callable[[], T]) -> T:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return self._data[key]
        value = compute()  # computed outside the lock; a rare double-compute is harmless
        with self._lock:
            self.misses += 1
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._data), "hits": self.hits, "misses": self.misses}
