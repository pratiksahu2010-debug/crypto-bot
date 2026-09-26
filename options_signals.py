"""Crypto options signal module (Deribit public market data).

Produces a CALL/PUT candidate for BTC/ETH only.  It is deliberately an
alert/data module: it never places orders.  The selector prefers liquid,
near-ATM options with enough time to expiry and avoids wide spreads.
"""
import logging
import time
from datetime import datetime, timezone
from functools import lru_cache

import requests
import config

log = logging.getLogger("options")
BASE = "https://www.deribit.com/api/v2/public"
SYMBOL_TO_CCY = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}


def _get(method, params):
    if config.DRY_RUN:
        return None
    try:
        r = requests.get(f"{BASE}/{method}", params=params, timeout=8)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as exc:
        log.warning("[OPTIONS] %s failed: %s", method, exc)
        return None


_instrument_cache = {}


def _instruments(currency):
    now = time.time()
    cached = _instrument_cache.get(currency)
    if cached and now - cached[0] < getattr(config, "OPTION_INSTRUMENT_CACHE_SECONDS", 300):
        return cached[1]
    data = _get("get_instruments", {"currency": currency, "kind": "option", "expired": "false"}) or []
    _instrument_cache[currency] = (now, data)
    return data


def _fmt_exp(ts):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%d %b %Y")


def _days(ts):
    return max(0.0, (datetime.fromtimestamp(ts / 1000, tz=timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 86400)


def _strike_distance(strike, spot):
    return abs(strike / spot - 1.0) * 100 if spot else 999.0


def _candidate_score(q, spot, direction):
    """Higher is better. Score is selection quality, not win probability."""
    days = q["days_to_expiry"]
    delta = abs(q.get("delta") or 0)
    spread = q.get("spread_pct")
    dist = _strike_distance(q["strike"], spot)
    oi = q.get("open_interest") or 0
    volume = q.get("volume_24h") or 0

    score = 0.0
    # Prefer useful directional sensitivity, but not ultra-deep ITM/OTM.
    score += max(0.0, 3.0 - abs(delta - 0.50) * 8.0)
    score += max(0.0, 3.0 - dist * 0.8)
    # 2-14 days is a practical fast-trade window; do not force expiry-day risk.
    if 2 <= days <= 14:
        score += 3.0
    elif 1 <= days < 2:
        score += 0.5
    elif days > 14:
        score += 1.0
    if spread is not None:
        if spread <= getattr(config, "OPTION_MAX_SPREAD_PCT", 8.0):
            score += 2.0
        elif spread <= 15:
            score += 0.5
        else:
            score -= 3.0
    if oi >= getattr(config, "OPTION_MIN_OPEN_INTEREST", 0.0):
        score += 1.0
    if volume >= getattr(config, "OPTION_MIN_VOLUME_24H", 0.0):
        score += 1.0
    return score


def _build_quote(inst, ticker, spot):
    if not ticker:
        return None
    greeks = ticker.get("greeks") or {}
    bid = ticker.get("best_bid_price")
    ask = ticker.get("best_ask_price")
    mark = ticker.get("mark_price")
    # Use mark for informational premium; executable BUY reference is ask.
    entry = ask if ask is not None and ask > 0 else mark
    spread_pct = None
    if bid and ask and ask > 0:
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
    days = _days(inst["expiration_timestamp"])
    option_type = str(inst.get("option_type", "")).upper()
    q = {
        "instrument_name": inst["instrument_name"],
        "option_type": option_type,
        "strike": float(inst["strike"]),
        "expiry_date": _fmt_exp(inst["expiration_timestamp"]),
        "days_to_expiry": round(days, 2),
        "underlying_price": float(ticker.get("underlying_price") or ticker.get("index_price") or spot),
        "mark_price_underlying": mark,
        "bid_price_underlying": bid,
        "ask_price_underlying": ask,
        "entry_price_underlying": entry,
        "mark_iv": ticker.get("mark_iv"),
        "bid_iv": ticker.get("bid_iv"),
        "ask_iv": ticker.get("ask_iv"),
        "delta": greeks.get("delta"),
        "gamma": greeks.get("gamma"),
        "theta": greeks.get("theta"),
        "vega": greeks.get("vega"),
        "open_interest": ticker.get("open_interest"),
        "volume_24h": (ticker.get("stats") or {}).get("volume"),
        "spread_pct": spread_pct,
        "contract_size": inst.get("contract_size"),
        "min_trade_amount": inst.get("min_trade_amount"),
        "tick_size": inst.get("tick_size"),
        "state": ticker.get("state"),
    }
    # Deribit option prices are quoted in the underlying coin. Convert to USD.
    if entry is not None and q["underlying_price"]:
        q["entry_premium_usd"] = entry * q["underlying_price"]
        q["bid_premium_usd"] = bid * q["underlying_price"] if bid else None
        q["ask_premium_usd"] = ask * q["underlying_price"] if ask else None
        q["mark_premium_usd"] = mark * q["underlying_price"] if mark else None
    else:
        q["entry_premium_usd"] = q["bid_premium_usd"] = q["ask_premium_usd"] = q["mark_premium_usd"] = None
    return q


def _project_levels(q, direction):
    entry = q.get("entry_premium_usd")
    spot = q.get("underlying_price")
    delta = abs(q.get("delta") or 0)
    if not entry or not spot or not delta:
        return
    target_spot_pct = getattr(config, "OPTION_TARGET_MOVE_PCT", 1.5)
    stop_spot_pct = getattr(config, "OPTION_STOP_MOVE_PCT", 0.7)
    target = entry + delta * spot * target_spot_pct / 100
    stop = max(entry - delta * spot * stop_spot_pct / 100,
               entry * (1 - getattr(config, "OPTION_STOP_FLOOR_PCT", 40) / 100))
    q.update({
        "target_premium_usd": target,
        "stop_premium_usd": stop,
        "target_pct_spot": target_spot_pct,
        "stop_pct_spot": stop_spot_pct,
        "premium_target_pct": (target / entry - 1) * 100,
        "premium_stop_pct": (stop / entry - 1) * 100,
    })
    # Approximate expiry break-even at expiry, excluding fees.
    strike = q["strike"]
    q["expiry_breakeven"] = strike + entry if q["option_type"] == "CALL" else strike - entry


def _mock(currency, direction):
    base = {"BTC": 100000.0, "ETH": 4000.0}[currency]
    spot = base
    strike = round(spot / (1000 if currency == "BTC" else 50)) * (1000 if currency == "BTC" else 50)
    opt = "CALL" if direction == "LONG" else "PUT"
    q = {
        "instrument_name": f"{currency}-MOCK-OPTION-{strike:.0f}-{opt[0]}",
        "option_type": opt, "strike": strike, "expiry_date": "mock", "days_to_expiry": 7.0,
        "underlying_price": spot, "mark_price_underlying": .03, "bid_price_underlying": .029,
        "ask_price_underlying": .031, "entry_price_underlying": .031, "mark_iv": 55.0,
        "bid_iv": 54.0, "ask_iv": 56.0, "delta": .50 if opt == "CALL" else -.50,
        "gamma": .00001, "theta": -.01, "vega": .02, "open_interest": 1000,
        "volume_24h": 500, "spread_pct": 6.6, "contract_size": 1, "min_trade_amount": 1,
        "tick_size": .0001, "state": "open", "entry_premium_usd": spot * .031,
        "bid_premium_usd": spot * .029, "ask_premium_usd": spot * .031,
        "mark_premium_usd": spot * .03,
    }
    _project_levels(q, direction)
    q["selection_score"] = 12.0
    q["liquidity_ok"] = True
    q["quality"] = "MOCK"
    return q


def get_option_signal(symbol: str, direction: str):
    """Return the best actionable CALL/PUT candidate for a spot signal."""
    currency = SYMBOL_TO_CCY.get(symbol)
    if not currency or direction not in ("LONG", "SHORT"):
        return None
    if config.DRY_RUN:
        return _mock(currency, direction)

    option_type = "call" if direction == "LONG" else "put"
    instruments = [i for i in _instruments(currency)
                   if i.get("is_active", True) and i.get("state") == "open"
                   and i.get("option_type") == option_type]
    if not instruments:
        return None
    idx = _get_index(currency)
    if not idx:
        return None
    spot = float(idx)
    min_days = getattr(config, "OPTION_MIN_DTE", 2.0)
    max_days = getattr(config, "OPTION_MAX_DTE", 14.0)
    candidates = []
    # Avoid an API burst: only inspect the closest strikes around spot in each expiry.
    by_exp = {}
    for inst in instruments:
        d = _days(inst["expiration_timestamp"])
        if d < min_days or d > max_days:
            continue
        by_exp.setdefault(inst["expiration_timestamp"], []).append(inst)
    for exp, arr in sorted(by_exp.items())[:getattr(config, "OPTION_MAX_EXPIRIES_TO_SCAN", 3)]:
        arr.sort(key=lambda x: abs(float(x["strike"]) - spot))
        for inst in arr[:getattr(config, "OPTION_STRIKES_PER_EXPIRY", 5)]:
            ticker = _get("ticker", {"instrument_name": inst["instrument_name"]})
            q = _build_quote(inst, ticker, spot)
            if q:
                q["selection_score"] = _candidate_score(q, spot, direction)
                q["liquidity_ok"] = (
                    q.get("spread_pct") is None or q["spread_pct"] <= getattr(config, "OPTION_HARD_MAX_SPREAD_PCT", 20.0)
                )
                if q["liquidity_ok"]:
                    candidates.append(q)
    if not candidates:
        return None
    best = max(candidates, key=lambda x: x["selection_score"])
    if best["selection_score"] < getattr(config, "OPTION_MIN_SELECTION_SCORE", 7.0):
        return None
    _project_levels(best, direction)
    best["quality"] = "LIQUID" if best.get("spread_pct", 999) <= getattr(config, "OPTION_MAX_SPREAD_PCT", 8.0) else "WIDE_SPREAD"
    best["spot_direction"] = direction
    best["underlying_symbol"] = currency
    return best


def _get_index(currency):
    r = _get("get_index_price", {"index_name": f"{currency.lower()}_usd"})
    return r.get("index_price") if r else None
