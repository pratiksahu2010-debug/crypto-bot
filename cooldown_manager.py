"""cooldown_manager.py - cooldown policy (confirmed / early / momentum)."""

import logging
from datetime import timedelta

import config
import storage as _st

log = logging.getLogger("cooldown")


class CooldownManager:
    def __init__(self, storage):
        self.storage = storage

    # ---- confirmed ----------------------------------------------------
    def can_alert(self, symbol: str) -> bool:
        return not self.storage.is_in_cooldown(symbol)

    def start_cooldown(self, symbol: str):
        self.storage.record_alert_sent(symbol)

    # ---- early --------------------------------------------------------
    def can_alert_early(self, symbol: str) -> bool:
        return not self.storage.is_in_early_cooldown(symbol, config.EARLY_COOLDOWN_HOURS)

    def start_early_cooldown(self, symbol: str):
        self.storage.record_early_alert_sent(symbol)

    # ---- momentum -----------------------------------------------------
    def can_alert_momentum(self, symbol: str, direction: str, price: float, ref_level: float = 0.0) -> bool:
        """No time cooldown: only materially new momentum events are allowed."""
        st = self.storage.get_momentum_state(symbol)
        if st is None:
            return True
        _ts, last_px, last_dir, last_ref = st
        if last_dir != direction or last_px <= 0 or price <= 0:
            return True
        sign = 1 if direction == "LONG" else -1
        extension = (price - last_px) / last_px * 100.0 * sign
        if extension >= getattr(config, "MOM_ESCALATION_PCT", 0.75):
            return True
        if ref_level > 0 and last_ref > 0:
            ref_change = abs(ref_level - last_ref) / last_ref * 100.0
            if ref_change >= getattr(config, "MOM_REF_CHANGE_PCT", 0.25):
                return True
        return False

    def start_momentum_cooldown(self, symbol: str, direction: str, price: float, ref_level: float = 0.0):
        # API-compatible name; no timer is started.
        self.storage.record_momentum_alert(symbol, direction, price, ref_level)

    def reset_all(self):
        self.storage.reset_all_cooldowns()
        log.info("[COOLDOWN] All cooldowns (confirmed + early + momentum) reset")
