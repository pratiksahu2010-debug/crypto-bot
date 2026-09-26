"""
tape.py - tiny in-memory tick tape. Websocket callbacks push prices in; the momentum
"radar" asks "how far has this symbol moved in the last N seconds?" - zero API calls.
"""
import threading
import time
from collections import defaultdict, deque


class PriceTape:
    def __init__(self, keep_seconds: int = 1200):
        self.keep = keep_seconds
        self._d = defaultdict(deque)
        self._lock = threading.Lock()
        self.last_tick_ts = 0.0

    def update(self, key: str, price: float, ts: float = None):
        if price is None or price <= 0:
            return
        ts = ts or time.time()
        with self._lock:
            dq = self._d[key]
            dq.append((ts, float(price)))
            cutoff = ts - self.keep
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            self.last_tick_ts = ts

    def latest(self, key: str, max_age: float = None):
        with self._lock:
            dq = self._d.get(key)
            if not dq:
                return None
            ts, px = dq[-1]
        if max_age is not None and time.time() - ts > max_age:
            return None
        return px

    def move_pct(self, key: str, seconds: int):
        """% change between the oldest tick inside the window and the latest tick."""
        now = time.time()
        with self._lock:
            dq = self._d.get(key)
            if not dq or now - dq[-1][0] > 90:      # stale tape -> no opinion
                return None
            window = [(t, p) for t, p in dq if t >= now - seconds]
        if len(window) < 2 or window[-1][0] - window[0][0] < seconds * 0.4:
            return None
        base = window[0][1]
        return (window[-1][1] - base) / base * 100.0 if base else None

    def seconds_since_last_tick(self):
        return time.time() - self.last_tick_ts if self.last_tick_ts else None
