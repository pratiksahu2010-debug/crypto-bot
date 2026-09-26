
"""
long_short_strategy.py
----------------------
ADD-ON LONG/SHORT STRATEGY MODULE

This file is intentionally independent of the existing scoring/alert logic.
It does NOT modify, replace, or monkey-patch the original bot.

Purpose:
  * Add a separate LONG-vs-SHORT decision layer.
  * Use the existing enriched OHLCV dataframe.
  * Combine trend, VWAP, EMA structure, RSI momentum, ADX, volume,
    breakout/pullback structure and ATR risk levels.
  * Produce an actionable strategy overlay while leaving the V4 engine intact.

Usage:
    from long_short_strategy import evaluate_long_short

    overlay = evaluate_long_short(df)
    print(overlay)

The module is deliberately conservative: WAIT is a valid result.
No strategy can guarantee profitable trades.
"""

from dataclasses import dataclass, asdict
from typing import Optional, Dict, List
import math

import numpy as np
import pandas as pd


@dataclass
class LongShortSignal:
    signal: str = "WAIT"                 # LONG / SHORT / WAIT
    regime: str = "NEUTRAL"
    long_score: float = 0.0              # 0-100
    short_score: float = 0.0             # 0-100
    edge: float = 0.0                    # absolute score separation
    price: float = 0.0
    atr: float = 0.0
    vwap: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema50: float = 0.0
    ema200: float = 0.0
    rsi: float = 50.0
    adx: float = 0.0
    volume_multiple: float = 0.0
    entry: float = 0.0
    stop: float = 0.0
    target1: float = 0.0
    target2: float = 0.0
    risk_reward_t1: float = 0.0
    risk_reward_t2: float = 0.0
    reasons: List[str] = None
    warnings: List[str] = None
    components: Dict[str, float] = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []
        if self.warnings is None:
            self.warnings = []
        if self.components is None:
            self.components = {}

    def to_dict(self):
        return asdict(self)


def _num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _pct(a, b):
    a, b = _num(a), _num(b)
    return ((a / b) - 1.0) * 100.0 if b > 0 else 0.0


def _ema(s, n):
    return pd.Series(s).ewm(span=n, adjust=False).mean()


def _atr(df, n=14):
    h = pd.to_numeric(df["high"], errors="coerce")
    l = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    tr = pd.concat(
        [(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()],
        axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _rsi(close, n=14):
    c = pd.Series(close, dtype="float64")
    d = c.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ag = up.ewm(alpha=1.0/n, adjust=False, min_periods=n).mean()
    al = dn.ewm(alpha=1.0/n, adjust=False, min_periods=n).mean()
    rs = ag / al.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    out = out.where(~((al == 0) & (ag > 0)), 100.0)
    return out.fillna(50.0)


def _get(df, col, fallback=0.0):
    if col not in df.columns or len(df) == 0:
        return fallback
    return _num(df.iloc[-1][col], fallback)


def _previous_swing_levels(df, n=20):
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    return (
        _num(high.shift(1).rolling(n, min_periods=max(5, n//4)).max().iloc[-1]),
        _num(low.shift(1).rolling(n, min_periods=max(5, n//4)).min().iloc[-1]),
    )


def evaluate_long_short(
    df: pd.DataFrame,
    *,
    min_bars: int = 60,
    min_score: float = 68.0,
    min_edge: float = 10.0,
    atr_stop_mult: float = 1.25,
    atr_target1_mult: float = 1.50,
    atr_target2_mult: float = 2.50,
) -> LongShortSignal:
    """
    Evaluate an additive LONG/SHORT strategy layer.

    The original V4 scoring remains untouched. This function is a separate
    confirmation layer. It intentionally returns WAIT when evidence is mixed.
    """

    out = LongShortSignal()

    if df is None or len(df) < min_bars:
        out.warnings.append(f"Need at least {min_bars} candles for the add-on.")
        return out

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        out.warnings.append("Missing columns: " + ", ".join(sorted(missing)))
        return out

    x = df.copy()
    close = pd.to_numeric(x["close"], errors="coerce")
    high = pd.to_numeric(x["high"], errors="coerce")
    low = pd.to_numeric(x["low"], errors="coerce")
    volume = pd.to_numeric(x["volume"], errors="coerce").fillna(0.0)

    price = _num(close.iloc[-1])
    if price <= 0:
        out.warnings.append("Invalid price.")
        return out

    # Use existing V4 indicators when available, but calculate the additional
    # regime indicators locally so the original indicator file is untouched.
    ema9 = _get(x, "ema9", _ema(close, 9).iloc[-1])
    ema21 = _get(x, "ema21", _ema(close, 21).iloc[-1])
    ema50_series = _ema(close, 50)
    ema200_series = _ema(close, 200)
    ema50 = _num(ema50_series.iloc[-1])
    ema200 = _num(ema200_series.iloc[-1])

    rsi_series = _rsi(close, 14)
    rsi = _get(x, "rsi14", rsi_series.iloc[-1])
    rsi_prev = _num(rsi_series.iloc[-2], rsi) if len(rsi_series) >= 2 else rsi
    rsi_slope = rsi - rsi_prev

    atr_series = _atr(x, 14)
    atr = _get(x, "atr14", atr_series.iloc[-1])
    if atr <= 0:
        atr = _num(atr_series.iloc[-1])

    adx = _get(x, "adx14", 0.0)

    if "vwap" in x.columns:
        vwap = _get(x, "vwap", 0.0)
    else:
        # Session-aware VWAP fallback. If session_date is present, use it;
        # otherwise use the available dataframe as one session.
        typical = (high + low + close) / 3.0
        vol_cum = volume.cumsum().replace(0, np.nan)
        vwap_series = (typical * volume).cumsum() / vol_cum
        vwap = _num(vwap_series.iloc[-1])

    vol_avg = _num(volume.shift(1).rolling(20, min_periods=5).mean().iloc[-1])
    volume_multiple = volume.iloc[-1] / vol_avg if vol_avg > 0 else 0.0

    # Slopes normalized to price so the score is portable across instruments.
    ema9_slope_pct = _pct(ema9, _num(_ema(close, 9).iloc[-3], ema9)) if len(close) >= 3 else 0.0
    ema21_slope_pct = _pct(ema21, _num(_ema(close, 21).iloc[-3], ema21)) if len(close) >= 3 else 0.0
    ema50_slope_pct = _pct(ema50, _num(ema50_series.iloc[-3], ema50)) if len(close) >= 3 else 0.0
    ema200_slope_pct = _pct(ema200, _num(ema200_series.iloc[-3], ema200)) if len(close) >= 3 else 0.0

    vwap_dist_pct = abs(_pct(price, vwap)) if vwap > 0 else 999.0
    prior_high20, prior_low20 = _previous_swing_levels(x, 20)

    range_now = _num(high.iloc[-1] - low.iloc[-1])
    body_now = abs(_num(close.iloc[-1] - pd.to_numeric(x["open"], errors="coerce").iloc[-1]))
    body_ratio = body_now / range_now if range_now > 0 else 0.0

    # ---------------------------
    # Market regime classification
    # ---------------------------
    strong_bull = (
        price > ema21 > ema50 > ema200
        and ema21_slope_pct > 0
        and ema50_slope_pct >= 0
    )
    strong_bear = (
        price < ema21 < ema50 < ema200
        and ema21_slope_pct < 0
        and ema50_slope_pct <= 0
    )

    if strong_bull:
        regime = "BULL_TREND"
    elif strong_bear:
        regime = "BEAR_TREND"
    elif price > ema50 and ema50_slope_pct > 0:
        regime = "BULLISH"
    elif price < ema50 and ema50_slope_pct < 0:
        regime = "BEARISH"
    else:
        regime = "RANGE/TRANSITION"

    # ---------------------------
    # Independent evidence scoring
    # ---------------------------
    # The weights intentionally favor structure and participation over a
    # single oscillator. Each side gets a score out of 100.
    L = 0.0
    S = 0.0
    reasons_l, reasons_s = [], []

    # Trend structure: 30 points
    if price > vwap > 0: L += 7; reasons_l.append("price above VWAP")
    if price < vwap and vwap > 0: S += 7; reasons_s.append("price below VWAP")

    if ema9 > ema21: L += 6; reasons_l.append("9 EMA above 21 EMA")
    if ema9 < ema21: S += 6; reasons_s.append("9 EMA below 21 EMA")

    if price > ema50: L += 5; reasons_l.append("price above EMA50")
    if price < ema50: S += 5; reasons_s.append("price below EMA50")

    if price > ema200: L += 4; reasons_l.append("price above EMA200")
    if price < ema200: S += 4; reasons_s.append("price below EMA200")

    if ema21_slope_pct > 0: L += 4; reasons_l.append("21 EMA rising")
    if ema21_slope_pct < 0: S += 4; reasons_s.append("21 EMA falling")

    if ema50_slope_pct > 0: L += 4; reasons_l.append("50 EMA rising")
    if ema50_slope_pct < 0: S += 4; reasons_s.append("50 EMA falling")

    # Momentum: 22 points
    if 52 <= rsi <= 72 and rsi_slope > 0: L += 8; reasons_l.append("RSI bullish momentum")
    if 28 <= rsi <= 48 and rsi_slope < 0: S += 8; reasons_s.append("RSI bearish momentum")

    if adx >= 23:
        if price > ema21: L += 5; reasons_l.append("ADX supports trend")
        if price < ema21: S += 5; reasons_s.append("ADX supports trend")

    if rsi >= 55: L += 4
    if rsi <= 45: S += 4

    if rsi >= 75: L -= 4
    if rsi <= 25: S -= 4

    if ema9_slope_pct > 0: L += 3
    if ema9_slope_pct < 0: S += 3

    # Participation: 18 points
    if volume_multiple >= 1.20:
        if price >= _num(x["open"].iloc[-1]): L += 9; reasons_l.append("volume participation")
        if price < _num(x["open"].iloc[-1]): S += 9; reasons_s.append("volume participation")
    elif volume_multiple >= 0.90:
        if price >= _num(x["open"].iloc[-1]): L += 4
        if price < _num(x["open"].iloc[-1]): S += 4

    if body_ratio >= 0.55:
        if price >= _num(x["open"].iloc[-1]): L += 4; reasons_l.append("strong bullish candle")
        if price < _num(x["open"].iloc[-1]): S += 4; reasons_s.append("strong bearish candle")

    # Structure / trigger: 30 points
    long_break = prior_high20 > 0 and price > prior_high20 + 0.05 * max(atr, 1e-12)
    short_break = prior_low20 > 0 and price < prior_low20 - 0.05 * max(atr, 1e-12)

    if long_break:
        L += 12; reasons_l.append("20-bar breakout")
    elif prior_high20 > 0 and price > prior_high20 - 0.20 * max(atr, 1e-12) and price > ema21:
        L += 5; reasons_l.append("near 20-bar high")

    if short_break:
        S += 12; reasons_s.append("20-bar breakdown")
    elif prior_low20 > 0 and price < prior_low20 + 0.20 * max(atr, 1e-12) and price < ema21:
        S += 5; reasons_s.append("near 20-bar low")

    # Pullback continuation: trend intact + price near EMA21/VWAP.
    near_ema21 = abs(price - ema21) <= max(0.60 * atr, price * 0.002)
    near_vwap = vwap > 0 and abs(price - vwap) <= max(0.70 * atr, price * 0.0025)

    if price > ema21 and (near_ema21 or near_vwap) and rsi >= 50:
        L += 10; reasons_l.append("bullish pullback/continuation zone")
    if price < ema21 and (near_ema21 or near_vwap) and rsi <= 50:
        S += 10; reasons_s.append("bearish pullback/continuation zone")

    # Avoid rewarding extended entries as if they were fresh breakouts.
    if atr > 0 and vwap > 0:
        if _pct(price, vwap) > 2.5:
            L -= 5
            S -= 5
            out.warnings.append("Price is extended from VWAP; entry quality reduced.")

    # ---------------------------
    # Decide direction
    # ---------------------------
    L = max(0.0, min(100.0, L))
    S = max(0.0, min(100.0, S))
    edge = abs(L - S)

    signal = "WAIT"
    if max(L, S) >= min_score and edge >= min_edge:
        if L > S:
            signal = "LONG"
        elif S > L:
            signal = "SHORT"

    # Additional conflict protection.
    if signal == "LONG" and regime == "BEAR_TREND":
        signal = "WAIT"
        out.warnings.append("Long evidence conflicts with higher-timeframe bearish regime.")
    if signal == "SHORT" and regime == "BULL_TREND":
        signal = "WAIT"
        out.warnings.append("Short evidence conflicts with higher-timeframe bullish regime.")

    # Do not fire a continuation signal when ADX is extremely weak.
    if signal in ("LONG", "SHORT") and adx > 0 and adx < 15:
        signal = "WAIT"
        out.warnings.append("ADX is too weak for a trend-following entry.")

    # ---------------------------
    # ATR-based risk framework
    # ---------------------------
    entry = price
    stop = target1 = target2 = rr1 = rr2 = 0.0

    if atr > 0 and signal == "LONG":
        structural_stop = prior_low20 if prior_low20 > 0 else price - atr_stop_mult * atr
        stop = min(price - atr_stop_mult * atr, structural_stop)
        risk = price - stop
        if risk > 0:
            target1 = price + max(atr_target1_mult * atr, risk * 1.20)
            target2 = price + max(atr_target2_mult * atr, risk * 2.00)
            rr1 = (target1 - price) / risk
            rr2 = (target2 - price) / risk

    elif atr > 0 and signal == "SHORT":
        structural_stop = prior_high20 if prior_high20 > 0 else price + atr_stop_mult * atr
        stop = max(price + atr_stop_mult * atr, structural_stop)
        risk = stop - price
        if risk > 0:
            target1 = price - max(atr_target1_mult * atr, risk * 1.20)
            target2 = price - max(atr_target2_mult * atr, risk * 2.00)
            rr1 = (price - target1) / risk
            rr2 = (price - target2) / risk

    # WAIT still returns useful reference levels without pretending it is a trade.
    if signal == "WAIT" and atr > 0:
        out.warnings.append("No confirmed directional edge; WAIT is intentional.")

    reasons = reasons_l if signal == "LONG" else reasons_s if signal == "SHORT" else (
        ["LONG evidence: " + ", ".join(reasons_l[:4]) if reasons_l else "limited long evidence",
         "SHORT evidence: " + ", ".join(reasons_s[:4]) if reasons_s else "limited short evidence"]
    )

    out.signal = signal
    out.regime = regime
    out.long_score = round(L, 1)
    out.short_score = round(S, 1)
    out.edge = round(edge, 1)
    out.price = price
    out.atr = atr
    out.vwap = vwap
    out.ema_fast = ema9
    out.ema_slow = ema21
    out.ema50 = ema50
    out.ema200 = ema200
    out.rsi = rsi
    out.adx = adx
    out.volume_multiple = round(volume_multiple, 2)
    out.entry = entry
    out.stop = stop
    out.target1 = target1
    out.target2 = target2
    out.risk_reward_t1 = round(rr1, 2)
    out.risk_reward_t2 = round(rr2, 2)
    out.reasons = reasons
    out.components = {
        "vwap_distance_pct": round(vwap_dist_pct, 3),
        "ema9_slope_pct": round(ema9_slope_pct, 4),
        "ema21_slope_pct": round(ema21_slope_pct, 4),
        "ema50_slope_pct": round(ema50_slope_pct, 4),
        "ema200_slope_pct": round(ema200_slope_pct, 4),
        "rsi_slope": round(rsi_slope, 3),
        "body_ratio": round(body_ratio, 3),
        "prior_high20": round(prior_high20, 8),
        "prior_low20": round(prior_low20, 8),
        "long_breakout": float(long_break),
        "short_breakdown": float(short_break),
        "near_ema21": float(near_ema21),
        "near_vwap": float(near_vwap),
    }

    return out


def format_signal(signal: LongShortSignal, symbol: str = "") -> str:
    """Compact Telegram-friendly text for the add-on module."""
    title = f"LONG/SHORT ADD-ON | {symbol}".strip()
    lines = [
        f"🧭 {title}",
        f"Signal: {signal.signal}",
        f"Regime: {signal.regime}",
        f"Long score: {signal.long_score:.1f}/100",
        f"Short score: {signal.short_score:.1f}/100",
        f"Edge: {signal.edge:.1f}",
        f"Price: {signal.price:.8f}".rstrip("0").rstrip("."),
        f"RSI: {signal.rsi:.1f} | ADX: {signal.adx:.1f}",
        f"RVOL: {signal.volume_multiple:.2f}x",
    ]
    if signal.signal != "WAIT":
        lines += [
            f"Entry reference: {signal.entry:.8f}".rstrip("0").rstrip("."),
            f"Stop reference: {signal.stop:.8f}".rstrip("0").rstrip("."),
            f"Target 1: {signal.target1:.8f}".rstrip("0").rstrip("."),
            f"Target 2: {signal.target2:.8f}".rstrip("0").rstrip("."),
            f"R:R: {signal.risk_reward_t1:.2f} / {signal.risk_reward_t2:.2f}",
        ]
    if signal.reasons:
        lines.append("Reasons: " + "; ".join(signal.reasons[:5]))
    if signal.warnings:
        lines.append("⚠️ " + "; ".join(signal.warnings[:3]))
    return "\n".join(lines)


def evaluate_and_format(df, symbol=""):
    """Convenience helper: returns (LongShortSignal, formatted_text)."""
    result = evaluate_long_short(df)
    return result, format_signal(result, symbol)
