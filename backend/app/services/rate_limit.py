from collections import defaultdict, deque
from datetime import datetime, timedelta
from threading import Lock


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int, window_sec: int, now: datetime) -> bool:
        cutoff = now - timedelta(seconds=window_sec)
        with self._lock:
            events = self._hits[key]
            while events and events[0] < cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True

    def is_allowed(self, key: str, limit: int, window_sec: int, now: datetime) -> bool:
        cutoff = now - timedelta(seconds=window_sec)
        with self._lock:
            events = self._hits[key]
            while events and events[0] < cutoff:
                events.popleft()
            return len(events) < limit

    def record(self, key: str, now: datetime) -> None:
        with self._lock:
            self._hits[key].append(now)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
