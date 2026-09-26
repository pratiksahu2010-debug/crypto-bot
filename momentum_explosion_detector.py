
"""
momentum_explosion_detector.py
--------------------------------
ADD-ON ONLY. Does not modify existing V4 logic.

Designed to identify the EARLY PHASE of unusually strong directional moves.

Evidence:
  1. volume expansion / RVOL
  2. range expansion
  3. price acceleration
  4. breakout / breakdown
  5. trend alignment
  6. candle impulse
  7. open-interest confirmation when an OI column exists
  8. futures basis / premium confirmation when futures columns exist

Returns EXPLOSION_LONG / EXPLOSION_SHORT / WATCH / WAIT.

Important:
  - OI is optional. If unavailable, the detector does not fabricate it.
  - This is a signal-quality filter, not a guarantee of future price movement.
"""

from dataclasses import dataclass, asdict
from typing import List, Dict
import math
import numpy as np
import pandas as pd


@dataclass
class MomentumExplosion:
    signal: str = "WAIT"
    direction: str = "NEUTRAL"
    score: float = 0.0
    confidence: str = "LOW"
    price: float = 0.0
    rvol: float = 0.0
    range_expansion: float = 0.0
    velocity_pct: float = 0.0
    acceleration_pct: float = 0.0
    breakout_pct: float = 0.0
    oi_change_pct: float = 0.0
    futures_confirmation: bool = False
    entry_reference: float = 0.0
    atr: float = 0.0
    reasons: List[str] = None
    warnings: List[str] = None
    components: Dict[str, float] = None

    def __post_init__(self):
        self.reasons = self.reasons or []
        self.warnings = self.warnings or []
        self.components = self.components or {}

    def to_dict(self):
        return asdict(self)


def _num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _pct(a, b):
    b = _num(b)
    return ((_num(a) / b) - 1.0) * 100 if b else 0.0


def _ema(s, n):
    return pd.Series(s, dtype="float64").ewm(span=n, adjust=False).mean()


def _atr(df, n=14):
    h = pd.to_numeric(df["high"], errors="coerce")
    l = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def _find_col(df, names):
    lower = {str(c).lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    for c in df.columns:
        cl = str(c).lower()
        if any(n.lower() in cl for n in names):
            return c
    return None


def detect_momentum_explosion(
    df,
    *,
    min_bars=60,
    trigger_score=72,
    watch_score=58,
    rvol_trigger=1.8,
    range_trigger=1.45,
):
    result = MomentumExplosion()

    if df is None or len(df) < min_bars:
        result.warnings.append(f"Need at least {min_bars} candles.")
        return result

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        result.warnings.append("Missing columns: " + ", ".join(sorted(missing)))
        return result

    x = df.copy()
    o = pd.to_numeric(x["open"], errors="coerce")
    h = pd.to_numeric(x["high"], errors="coerce")
    l = pd.to_numeric(x["low"], errors="coerce")
    c = pd.to_numeric(x["close"], errors="coerce")
    v = pd.to_numeric(x["volume"], errors="coerce").fillna(0)

    price = _num(c.iloc[-1])
    if price <= 0:
        result.warnings.append("Invalid price.")
        return result

    atr_s = _atr(x)
    atr = _num(atr_s.iloc[-1])

    # ---- 1) Relative volume expansion ----
    base_vol = _num(v.shift(1).rolling(20, min_periods=10).mean().iloc[-1])
    rvol = v.iloc[-1] / base_vol if base_vol > 0 else 0.0

    # ---- 2) Range expansion ----
    candle_range = _num(h.iloc[-1] - l.iloc[-1])
    base_range = _num((h-l).shift(1).rolling(20, min_periods=10).mean().iloc[-1])
    range_exp = candle_range / base_range if base_range > 0 else 0.0

    # ---- 3) Price velocity + acceleration ----
    ret1 = _pct(c.iloc[-1], c.iloc[-2])
    ret3 = _pct(c.iloc[-1], c.iloc[-4])
    ret5 = _pct(c.iloc[-1], c.iloc[-6])
    prev_ret1 = _pct(c.iloc[-2], c.iloc[-3])
    acceleration = ret1 - prev_ret1

    # Normalize acceleration by recent return volatility.
    returns = c.pct_change() * 100
    ret_std = _num(returns.shift(1).rolling(20, min_periods=10).std().iloc[-1])
    accel_z = acceleration / ret_std if ret_std > 0 else 0.0

    # ---- 4) Breakout / breakdown ----
    prior_high = _num(h.shift(1).rolling(20, min_periods=10).max().iloc[-1])
    prior_low = _num(l.shift(1).rolling(20, min_periods=10).min().iloc[-1])

    breakout_up = price > prior_high if prior_high else False
    breakout_dn = price < prior_low if prior_low else False

    breakout_pct_up = _pct(price, prior_high) if prior_high else 0.0
    breakout_pct_dn = _pct(price, prior_low) if prior_low else 0.0

    # ---- 5) Impulse candle ----
    body = abs(_num(c.iloc[-1] - o.iloc[-1]))
    body_ratio = body / candle_range if candle_range > 0 else 0.0
    bullish_candle = c.iloc[-1] > o.iloc[-1]
    bearish_candle = c.iloc[-1] < o.iloc[-1]

    # ---- 6) Trend alignment ----
    ema9 = _ema(c, 9)
    ema21 = _ema(c, 21)
    ema50 = _ema(c, 50)

    bull_align = price > ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1]
    bear_align = price < ema9.iloc[-1] < ema21.iloc[-1] < ema50.iloc[-1]

    # ---- 7) OI confirmation if available ----
    oi_col = _find_col(x, ["open_interest", "openinterest", "oi"])
    oi_available = oi_col is not None
    oi_change = 0.0
    oi_confirm_long = False
    oi_confirm_short = False

    if oi_available:
        oi = pd.to_numeric(x[oi_col], errors="coerce")
        if len(oi) >= 3 and _num(oi.iloc[-2]) != 0:
            oi_change = _pct(oi.iloc[-1], oi.iloc[-2])
            # Price + OI rising = long build-up confirmation.
            # Price falling + OI rising = short build-up confirmation.
            oi_confirm_long = ret1 > 0 and oi_change > 1.0
            oi_confirm_short = ret1 < 0 and oi_change > 1.0
    else:
        result.warnings.append("Open interest unavailable; OI confirmation skipped.")

    # ---- 8) Futures confirmation if columns exist ----
    fut_close_col = _find_col(x, ["futures_close", "future_close", "fut_close"])
    fut_oi_col = _find_col(x, ["futures_oi", "future_oi", "fut_oi"])

    futures_confirm = False
    fut_long = fut_short = False

    if fut_close_col:
        fc = pd.to_numeric(x[fut_close_col], errors="coerce")
        fut_move = _pct(fc.iloc[-1], fc.iloc[-2]) if len(fc) >= 2 else 0.0
        fut_long = fut_move > 0 and ret1 > 0
        fut_short = fut_move < 0 and ret1 < 0
        futures_confirm = fut_long or fut_short

    if fut_oi_col and not oi_available:
        foi = pd.to_numeric(x[fut_oi_col], errors="coerce")
        if len(foi) >= 2 and _num(foi.iloc[-2]) != 0:
            oi_change = _pct(foi.iloc[-1], foi.iloc[-2])
            oi_confirm_long = ret1 > 0 and oi_change > 1.0
            oi_confirm_short = ret1 < 0 and oi_change > 1.0

    # ---------------- scoring ----------------
    long_score = 0.0
    short_score = 0.0
    long_reasons = []
    short_reasons = []

    # Volume: 20
    if rvol >= rvol_trigger:
        if bullish_candle:
            long_score += 20; long_reasons.append(f"RVOL {rvol:.1f}x")
        if bearish_candle:
            short_score += 20; short_reasons.append(f"RVOL {rvol:.1f}x")
    elif rvol >= 1.25:
        if bullish_candle:
            long_score += 10
        if bearish_candle:
            short_score += 10

    # Range expansion: 15
    if range_exp >= range_trigger:
        if bullish_candle:
            long_score += 15; long_reasons.append(f"range expansion {range_exp:.1f}x")
        if bearish_candle:
            short_score += 15; short_reasons.append(f"range expansion {range_exp:.1f}x")
    elif range_exp >= 1.20:
        if bullish_candle: long_score += 7
        if bearish_candle: short_score += 7

    # Acceleration: 15
    if acceleration > 0 and ret1 > 0:
        long_score += 8
        if accel_z >= 1.5:
            long_score += 7
            long_reasons.append("positive price acceleration")
    if acceleration < 0 and ret1 < 0:
        short_score += 8
        if accel_z <= -1.5:
            short_score += 7
            short_reasons.append("negative price acceleration")

    # Breakout: 20
    if breakout_up:
        long_score += 20; long_reasons.append(f"20-bar breakout +{breakout_pct_up:.2f}%")
    if breakout_dn:
        short_score += 20; short_reasons.append(f"20-bar breakdown {breakout_pct_dn:.2f}%")

    # Trend alignment: 10
    if bull_align:
        long_score += 10; long_reasons.append("EMA trend alignment")
    if bear_align:
        short_score += 10; short_reasons.append("EMA trend alignment")

    # Impulse candle: 10
    if body_ratio >= 0.65:
        if bullish_candle:
            long_score += 10; long_reasons.append("impulse candle")
        if bearish_candle:
            short_score += 10; short_reasons.append("impulse candle")
    elif body_ratio >= 0.50:
        if bullish_candle: long_score += 5
        if bearish_candle: short_score += 5

    # OI: 10
    if oi_confirm_long:
        long_score += 10; long_reasons.append(f"OI +{oi_change:.1f}% with price")
    if oi_confirm_short:
        short_score += 10; short_reasons.append(f"OI +{oi_change:.1f}% with price")

    # Futures: confirmation bonus, capped within total score.
    if fut_long:
        long_score += 5; long_reasons.append("futures confirmation")
    if fut_short:
        short_score += 5; short_reasons.append("futures confirmation")

    long_score = min(100.0, long_score)
    short_score = min(100.0, short_score)

    direction = "LONG" if long_score > short_score else "SHORT" if short_score > long_score else "NEUTRAL"
    score = max(long_score, short_score)

    if direction == "LONG":
        reasons = long_reasons
    elif direction == "SHORT":
        reasons = short_reasons
    else:
        reasons = ["conflicting directional evidence"]

    # Early-warning states:
    # WATCH catches a move before every confirmation is present.
    if score >= trigger_score and abs(long_score-short_score) >= 12:
        signal = "EXPLOSION_" + direction
        confidence = "HIGH" if score >= 82 else "MEDIUM"
    elif score >= watch_score and abs(long_score-short_score) >= 8:
        signal = "WATCH"
        confidence = "MEDIUM"
    else:
        signal = "WAIT"
        confidence = "LOW"

    # Avoid calling a giant move if there is no actual directional impulse.
    if not (breakout_up or breakout_dn) and range_exp < 1.20 and rvol < 1.25:
        if signal == "EXPLOSION_" + direction:
            signal = "WATCH"
            confidence = "MEDIUM"
            result.warnings.append("Momentum is building but breakout/participation confirmation is incomplete.")

    result.signal = signal
    result.direction = direction
    result.score = round(score, 1)
    result.confidence = confidence
    result.price = price
    result.rvol = round(rvol, 2)
    result.range_expansion = round(range_exp, 2)
    result.velocity_pct = round(ret1, 3)
    result.acceleration_pct = round(acceleration, 3)
    result.breakout_pct = round(breakout_pct_up if direction == "LONG" else breakout_pct_dn, 3)
    result.oi_change_pct = round(oi_change, 2)
    result.futures_confirmation = futures_confirm
    result.entry_reference = price
    result.atr = atr
    result.reasons = reasons
    result.components = {
        "long_score": round(long_score, 1),
        "short_score": round(short_score, 1),
        "acceleration_z": round(accel_z, 2),
        "body_ratio": round(body_ratio, 3),
        "breakout_up": float(breakout_up),
        "breakout_down": float(breakout_dn),
        "oi_available": float(oi_available),
        "oi_confirm_long": float(oi_confirm_long),
        "oi_confirm_short": float(oi_confirm_short),
        "futures_confirm": float(futures_confirm),
    }
    return result


def format_explosion_alert(result, symbol=""):
    lines = [
        f"🚀 MOMENTUM EXPLOSION | {symbol}".strip(),
        f"Signal: {result.signal}",
        f"Direction: {result.direction}",
        f"Score: {result.score:.1f}/100 | Confidence: {result.confidence}",
        f"Price: {result.price}",
        f"RVOL: {result.rvol:.2f}x",
        f"Range expansion: {result.range_expansion:.2f}x",
        f"Velocity: {result.velocity_pct:+.3f}%",
        f"Acceleration: {result.acceleration_pct:+.3f}%",
        f"OI change: {result.oi_change_pct:+.2f}%",
        f"Futures confirmation: {'YES' if result.futures_confirmation else 'NO/NA'}",
    ]
    if result.reasons:
        lines.append("Evidence: " + "; ".join(result.reasons[:6]))
    if result.warnings:
        lines.append("⚠️ " + "; ".join(result.warnings[:3]))
    return "\n".join(lines)
