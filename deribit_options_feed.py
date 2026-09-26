"""
deribit_options_feed.py
-------------------------
Real crypto OPTIONS TRADE SIGNAL via Deribit's public API - free, no key, no auth, globally
accessible (Deribit is not geo-blocked the way Binance is). Deribit is the dominant crypto
options venue.

SCOPE, DELIBERATELY LIMITED: crypto options only have meaningful liquidity for BTC and ETH
(everything else in your spot list has no real options market to reference at all). This
module is never used for other coins; they keep working exactly as before, just without an
options section in their alerts.

WHAT THIS RETURNS: not just context anymore - a full trade signal for the nearest matching
option (call for LONG, put for SHORT): the real live entry premium, a target premium and a
stop premium projected from the option's own delta (how much the premium moves per $1 of
spot move) against a configurable expected spot move, so the number is grounded in the
option's actual sensitivity rather than an arbitrary guess. It does not model theta/gamma
decay through the trade - options lose value with time even if spot stays flat, so these are
same-session, fast-moving trade ideas, not multi-day holds.
"""

import logging
import time
from datetime import datetime, timezone

import requests

import config

log = logging.getLogger("deribit_feed")

DERIBIT_API_BASE = "https://www.deribit.com/api/v2/public"

# Maps your Binance spot pairs to Deribit's currency codes. Only currencies
# with genuine, reliably-listed options markets belong here.
SYMBOL_TO_DERIBIT_CURRENCY = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
}


def _get(endpoint: str, params: dict, retry: bool = True):
    if config.DRY_RUN:
        return None  # handled by mock functions below
    try:
        resp = requests.get(f"{DERIBIT_API_BASE}/{endpoint}", params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result")
    except Exception as e:
        if retry:
            log.warning(f"[DERIBIT] {endpoint} failed, retrying once in 2s: {e}")
            time.sleep(2)
            return _get(endpoint, params, retry=False)
        log.error(f"[DERIBIT] {endpoint} failed after retry: {e}")
        return None


def _pick_strike(same_expiry: list, spot_price: float, option_type: str):
    """ATM = closest strike to spot. OTM1 = one strike further out-of-the-money (cheaper
    premium, higher leverage/delta-adjusted risk) - selection is config.OPTION_STRIKE_SELECTION."""
    same_expiry = sorted(same_expiry, key=lambda i: i["strike"])
    atm_idx = min(range(len(same_expiry)), key=lambda i: abs(same_expiry[i]["strike"] - spot_price))
    if getattr(config, "OPTION_STRIKE_SELECTION", "ATM") == "OTM1":
        # call: next strike ABOVE spot is more OTM as index increases; put: next strike BELOW
        step = 1 if option_type == "call" else -1
        idx = atm_idx + step
        if 0 <= idx < len(same_expiry):
            return same_expiry[idx]
    return same_expiry[atm_idx]


def get_atm_option(symbol: str, direction: str):
    """
    Returns a real, live option trade signal matching the spot signal's direction (CALL for
    LONG, PUT for SHORT), or None if this symbol has no options market on Deribit.

    Dict keys: instrument_name, option_type, strike, expiry_date, days_to_expiry,
    mark_price_underlying, mark_price_usd, mark_iv, delta, underlying_price,
    target_premium_usd, stop_premium_usd, target_pct_spot, stop_pct_spot.
    """
    currency = SYMBOL_TO_DERIBIT_CURRENCY.get(symbol)
    if not currency:
        return None  # no real options market for this coin - not an error

    if config.DRY_RUN:
        return _mock_option(currency, direction)

    option_type = "call" if direction == "LONG" else "put"

    instruments = _get("get_instruments", {"currency": currency, "kind": "option", "expired": "false"})
    if not instruments:
        log.info(f"[DERIBIT] No active option instruments listed for {currency} right now")
        return None

    matching = [i for i in instruments if i.get("option_type") == option_type]
    if not matching:
        return None

    matching.sort(key=lambda i: i["expiration_timestamp"])
    nearest_expiry = matching[0]["expiration_timestamp"]
    same_expiry = [i for i in matching if i["expiration_timestamp"] == nearest_expiry]

    index = _get("get_index_price", {"index_name": f"{currency.lower()}_usd"})
    spot_price = index.get("index_price") if index else None
    if spot_price is None:
        same_expiry.sort(key=lambda i: i["strike"])
        chosen = same_expiry[len(same_expiry) // 2]
    else:
        chosen = _pick_strike(same_expiry, spot_price, option_type)

    ticker = _get("ticker", {"instrument_name": chosen["instrument_name"]})
    if not ticker:
        return None

    expiry_dt = datetime.fromtimestamp(chosen["expiration_timestamp"] / 1000, tz=timezone.utc)
    days_to_expiry = (expiry_dt - datetime.now(timezone.utc)).days

    mark_price_underlying = ticker.get("mark_price")
    underlying_price = ticker.get("underlying_price") or spot_price
    mark_price_usd = (mark_price_underlying * underlying_price) if (mark_price_underlying and underlying_price) else None
    delta = (ticker.get("greeks") or {}).get("delta")

    target_pct = getattr(config, "OPTION_TARGET_MOVE_PCT", 1.5)
    stop_pct = getattr(config, "OPTION_STOP_MOVE_PCT", 0.7)
    stop_floor_pct = getattr(config, "OPTION_STOP_FLOOR_PCT", 40)
    target_premium_usd = stop_premium_usd = None
    if delta and mark_price_usd and underlying_price:
        d = abs(delta)
        target_premium_usd = mark_price_usd + d * underlying_price * target_pct / 100.0
        proj_stop = mark_price_usd - d * underlying_price * stop_pct / 100.0
        floor = mark_price_usd * (1 - stop_floor_pct / 100.0)
        stop_premium_usd = max(proj_stop, floor)

    return {
        "instrument_name": chosen["instrument_name"], "option_type": option_type.upper(),
        "strike": chosen["strike"], "expiry_date": expiry_dt.strftime("%d %b %Y"),
        "days_to_expiry": max(days_to_expiry, 0), "mark_price_underlying": mark_price_underlying,
        "mark_price_usd": mark_price_usd, "mark_iv": ticker.get("mark_iv"), "delta": delta,
        "underlying_price": underlying_price, "target_premium_usd": target_premium_usd,
        "stop_premium_usd": stop_premium_usd, "target_pct_spot": target_pct, "stop_pct_spot": stop_pct,
    }


def _mock_option(currency: str, direction: str):
    """DRY_RUN synthetic option data - no network calls."""
    import random
    base = {"BTC": 60000, "ETH": 3000}.get(currency, 100)
    spot = base * random.uniform(0.97, 1.03)
    strike = round(spot / 500) * 500 if currency == "BTC" else round(spot / 50) * 50
    premium_usd = round(spot * random.uniform(0.01, 0.08), 2)
    delta = round(random.uniform(0.3, 0.6), 3) * (1 if direction == "LONG" else -1)
    target_pct = getattr(config, "OPTION_TARGET_MOVE_PCT", 1.5)
    stop_pct = getattr(config, "OPTION_STOP_MOVE_PCT", 0.7)
    stop_floor_pct = getattr(config, "OPTION_STOP_FLOOR_PCT", 40)
    d = abs(delta)
    target_premium_usd = premium_usd + d * spot * target_pct / 100.0
    stop_premium_usd = max(premium_usd - d * spot * stop_pct / 100.0, premium_usd * (1 - stop_floor_pct / 100.0))
    return {
        "instrument_name": f"{currency}-MOCKEXP-{strike}-{'C' if direction == 'LONG' else 'P'}",
        "option_type": "CALL" if direction == "LONG" else "PUT", "strike": strike, "expiry_date": "mock expiry",
        "days_to_expiry": random.randint(1, 14), "mark_price_underlying": round(random.uniform(0.01, 0.08), 4),
        "mark_price_usd": premium_usd, "mark_iv": round(random.uniform(40, 90), 1), "delta": delta,
        "underlying_price": round(spot, 2), "target_premium_usd": round(target_premium_usd, 2),
        "stop_premium_usd": round(stop_premium_usd, 2), "target_pct_spot": target_pct, "stop_pct_spot": stop_pct,
    }
