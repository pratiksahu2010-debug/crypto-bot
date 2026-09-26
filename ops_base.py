"""ops_base.py - default plumbing between a feed and the shared engine."""
import logging
import threading
import time

log = logging.getLogger("ops")


class OpsBase:
    def __init__(self):
        self._ready = threading.Event()
        self.engine = None

    def ready(self):
        return self._ready.is_set()

    def boot(self):
        raise NotImplementedError

    def symbols(self):
        raise NotImplementedError

    def fetch_frames(self, symbols):
        raise NotImplementedError

    def live_price(self, symbol):
        return None

    def recent_move_pct(self, symbol, seconds):
        return None

    def on_feed_outage(self):
        pass

    def morning_reset_extra(self):
        pass

    def extra_jobs(self):
        return []

    def symbol_count(self):
        return len(self.symbols())

    def status(self):
        return {}

    @staticmethod
    def retry_until(fn, what, delay=60):
        """Blocks until fn() returns truthy - a bot must never stay half-booted forever."""
        while True:
            try:
                if fn():
                    return
            except Exception:
                log.exception(f"{what} raised")
            log.error(f"{what} failed - retrying in {delay}s")
            time.sleep(delay)
