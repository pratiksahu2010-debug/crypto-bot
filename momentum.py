"""
High-quality BIG MOMENTUM detector.

Design goals:
- Prefer fresh acceleration over simply measuring a large move.
- Require participation (volume/range) and structure confirmation.
- Reject obvious exhaustion / wick-only breakouts.
- Treat a new impulse as a new signal; continuation of the same impulse is ignored
  by cooldown_manager.py using the stored price + reference level.
- Works with the existing 5m dataframe and does not require a new data provider.
"""
import logging
import math
from dataclasses import dataclass, field
from typing import Dict, Optional
import config

log = logging.getLogger("momentum")


def _g(name, default):
    return getattr(config, name, default)


def _finite(x):
    try:
        return math.isfinite(float(x))
    except Exception:
        return False


@dataclass
class MomentumResult:
    direction: str
    mode: str
    strength: float
    factors: Dict[str, bool] = field(default_factory=dict)
    price: float = 0.0
    move_pct: float = 0.0
    lookback_minutes: int = 0
    move_atr: float = 0.0
    rvol: float = 0.0
    rvol_accel: float = 0.0
    day_move_pct: Optional[float] = None
    breakout: str = ""
    vwap: float = 0.0
    rsi: float = 0.0
    rsi_slope: float = 0.0
    adx: float = 0.0
    atr: float = 0.0
    ref_level: float = 0.0
    volume: float = 0.0
    body_ratio: float = 0.0
    overextended: bool = False
    exhaustion: bool = False
    new_impulse: bool = False


def evaluate_momentum(df) -> Optional[MomentumResult]:
    if not _g("MOMENTUM_ENABLED", True):
        return None

    n = int(_g("MOM_LOOKBACK_CANDLES", 3))
    # Need enough history for acceleration + prior structure.
    if len(df) < max(n + 24, 30):
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    base = df.iloc[-1 - n]

    price = float(last["close"])
    base_price = float(base["close"])
    if price <= 0 or base_price <= 0:
        return None

    move = price - base_price
    move_pct = move / base_price * 100.0
    if abs(move_pct) < 1e-12:
        return None

    direction = "LONG" if move > 0 else "SHORT"
    sign = 1 if direction == "LONG" else -1

    atr = float(last["atr14"]) if _finite(last["atr14"]) else 0.0
    move_atr = abs(move) / atr if atr > 0 else 0.0

    def aligned(row):
        return (float(row["close"]) - float(row["open"])) * sign > 0

    def ret_pct(a, b):
        a, b = float(a), float(b)
        return (a / b - 1.0) * 100.0 if b > 0 else 0.0

    # ---- acceleration -------------------------------------------------
    ret_now = ret_pct(last["close"], prev["close"])
    ret_prev = ret_pct(prev["close"], prev2["close"])
    acceleration = (ret_now - ret_prev) * sign

    move_recent = ret_pct(last["close"], df.iloc[-3]["close"]) * sign
    move_earlier = ret_pct(df.iloc[-3]["close"], df.iloc[-5]["close"]) * sign if len(df) >= 5 else 0.0
    accelerating = acceleration >= _g("MOM_ACCELERATION_MIN_PCT", 0.20) and move_recent > move_earlier

    # ---- participation ------------------------------------------------
    rvol = max(float(last["rvol"]), float(prev["rvol"]))
    prev_rvol = float(prev["rvol"]) if _finite(prev["rvol"]) else 0.0
    rvol_accel = rvol - prev_rvol

    last_range = float(last["high"]) - float(last["low"])
    prev_range = float(prev["high"]) - float(prev["low"])
    range_atr = max(last_range, prev_range) / atr if atr > 0 else 0.0
    range_expansion = range_atr >= _g("MOM_RANGE_ATR_MULT", 1.8)

    volume_surge = rvol >= _g("MOM_RVOL_MIN", 2.0)
    volume_accel = rvol_accel >= _g("MOM_RVOL_ACCEL_MIN", 0.35) or rvol >= _g("MOM_RVOL_STRONG", 3.0)

    # ---- candle quality ----------------------------------------------
    total_range = max(last_range, 1e-12)
    body_ratio = abs(float(last["close"]) - float(last["open"])) / total_range
    body_quality = aligned(last) and body_ratio >= _g("MOM_MIN_BODY_RATIO", 0.55)

    # ---- structure ----------------------------------------------------
    hh20 = float(last["hh20_prior"]) if _finite(last["hh20_prior"]) else math.inf
    ll20 = float(last["ll20_prior"]) if _finite(last["ll20_prior"]) else -math.inf
    day_hi = float(last["day_high_prior"]) if _finite(last["day_high_prior"]) else math.inf
    day_lo = float(last["day_low_prior"]) if _finite(last["day_low_prior"]) else -math.inf

    if direction == "LONG":
        broke20 = price > hh20
        broke_day = price > day_hi
        breakout = "20-candle HIGH" if broke20 else ("DAY HIGH" if broke_day else "")
        ref_level = max(x for x in (hh20, day_hi) if math.isfinite(x)) if (math.isfinite(hh20) or math.isfinite(day_hi)) else 0.0
        breakout_distance = (price - ref_level) / atr if ref_level > 0 and atr > 0 else 0.0
    else:
        broke20 = price < ll20
        broke_day = price < day_lo
        breakout = "20-candle LOW" if broke20 else ("DAY LOW" if broke_day else "")
        ref_level = min(x for x in (ll20, day_lo) if math.isfinite(x)) if (math.isfinite(ll20) or math.isfinite(day_lo)) else 0.0
        breakout_distance = (ref_level - price) / atr if ref_level > 0 and atr > 0 else 0.0

    clean_breakout = bool(breakout) and breakout_distance >= _g("MOM_BREAKOUT_ATR_MIN", 0.12)

    # ---- trend / momentum --------------------------------------------
    vwap = float(last["vwap"]) if _finite(last["vwap"]) else 0.0
    ema9, ema21 = float(last["ema9"]), float(last["ema21"])
    trend_ok = (price > vwap and ema9 > ema21) if direction == "LONG" else (price < vwap and ema9 < ema21)
    if vwap <= 0:
        trend_ok = (ema9 > ema21) if direction == "LONG" else (ema9 < ema21)

    rsi = float(last["rsi14"])
    prev_rsi = float(prev["rsi14"])
    rsi_slope = (rsi - prev_rsi) * sign
    rsi_ok = rsi_slope >= _g("MOM_RSI_SLOPE_MIN", 1.0) and (rsi > 50 if direction == "LONG" else rsi < 50)

    adx = float(last["adx14"])
    adx_prev = float(prev["adx14"])
    adx_rising = adx >= _g("MOM_ADX_MIN", 18.0) and adx >= adx_prev

    # ---- compression before expansion --------------------------------
    ranges = (df["high"] - df["low"]).tail(8)
    compression = False
    if len(ranges) >= 8:
        early_avg = float(ranges.iloc[:5].mean())
        late_avg = float(ranges.iloc[-3:].mean())
        compression = early_avg > 0 and late_avg <= early_avg * _g("MOM_COMPRESSION_RATIO", 0.78)

    # ---- exhaustion ---------------------------------------------------
    vwap_dist = abs(price - vwap) / vwap * 100.0 if vwap > 0 else 0.0
    huge_candle = atr > 0 and last_range / atr >= _g("MOM_EXHAUSTION_RANGE_ATR", 2.8)
    extreme_rsi = rsi >= _g("MOM_EXHAUSTION_RSI_LONG", 82.0) if direction == "LONG" else rsi <= _g("MOM_EXHAUSTION_RSI_SHORT", 18.0)
    far_vwap = vwap_dist >= _g("MOM_EXHAUSTION_VWAP_PCT", 3.0)
    exhaustion = bool(extreme_rsi and (huge_candle or far_vwap))

    mv_min = _g("MOM_MOVE_PCT", 1.5)
    move_fast = abs(move_pct) >= mv_min
    move_vs_atr = move_atr >= _g("MOM_ATR_MULT", 2.0)
    day_ref = last["prev_close"] if _finite(last["prev_close"]) else last["day_open"]
    day_move = ret_pct(price, day_ref) if _finite(day_ref) and float(day_ref) > 0 else None
    day_support = day_move is not None and day_move * sign >= _g("MOM_DAY_MOVE_PCT", 2.5)

    # A fresh impulse needs a change in speed/participation/structure, not merely time passing.
    new_impulse = bool(
        accelerating and (volume_accel or clean_breakout)
        or clean_breakout and body_quality and volume_surge
        or compression and accelerating and volume_surge
    )

    factors = {
        "fast_move_pct": move_fast,
        "move_vs_atr": move_vs_atr,
        "acceleration": accelerating,
        "volume_surge": volume_surge,
        "volume_acceleration": volume_accel,
        "clean_breakout": clean_breakout,
        "body_quality": body_quality,
        "trend_aligned": trend_ok,
        "rsi_momentum": rsi_ok,
        "adx_rising": adx_rising,
        "compression_breakout": compression and (clean_breakout or accelerating),
        "day_move": day_support,
    }

    # Quality score deliberately rewards independent evidence and penalises exhaustion.
    score = sum(bool(v) for v in factors.values())
    hard_core = (move_fast or move_vs_atr) and accelerating
    participation = volume_surge and (volume_accel or clean_breakout or range_expansion)
    structure = clean_breakout or (trend_ok and body_quality)

    # High-quality momentum requires acceleration + participation + structure + trend/RSI.
    qualified = hard_core and participation and structure and (trend_ok or rsi_ok) and not exhaustion

    # Compression breakout can fire earlier with a slightly smaller absolute move.
    compression_setup = compression and clean_breakout and volume_surge and body_quality and not exhaustion

    if qualified or compression_setup:
        mode = "BIG_MOMENTUM" if score >= _g("MOM_BIG_SCORE", 8) else "EARLY_MOMENTUM"
    elif day_move is not None and abs(day_move) >= _g("MOM_BIG_DAY_PCT", 8.0) and day_support and trend_ok and volume_surge and not exhaustion:
        mode = "DAY_RUNNER"
    else:
        return None

    strength = min(10.0, score + (1.0 if new_impulse else 0.0) + (1.0 if clean_breakout else 0.0) - (1.5 if exhaustion else 0.0))

    recent_lows = df["low"].iloc[-1 - n:]
    recent_highs = df["high"].iloc[-1 - n:]
    move_ref = float(recent_lows.min()) if direction == "LONG" else float(recent_highs.max())
    # Use the structural reference when available; it is more useful for deduplication.
    if ref_level > 0:
        move_ref = ref_level

    return MomentumResult(
        direction=direction,
        mode=mode,
        strength=round(strength, 1),
        factors=factors,
        price=price,
        move_pct=round(move_pct, 2),
        lookback_minutes=n * int(_g("CANDLE_MINUTES", 5)),
        move_atr=round(move_atr, 1),
        rvol=round(rvol, 1),
        rvol_accel=round(rvol_accel, 2),
        day_move_pct=None if day_move is None else round(day_move, 2),
        breakout=breakout,
        vwap=vwap,
        rsi=rsi,
        rsi_slope=round(rsi_slope, 1),
        adx=adx,
        atr=atr,
        ref_level=move_ref,
        volume=float(last["volume"]),
        body_ratio=round(body_ratio, 2),
        overextended=extreme_rsi or far_vwap,
        exhaustion=exhaustion,
        new_impulse=new_impulse,
    )


def format_alert(m: MomentumResult, symbol: str, bot_name: str, cur: str, fmt, now_label: str,
                 strict=None, extra_text: str = "") -> str:
    arrow = "🚀" if m.direction == "LONG" else "🔻"
    lines = [
        f"{arrow} *{bot_name}*",
        f"🔥 *{m.mode}: {symbol}*",
        f"📈 Direction: {m.direction}",
        f"💰 Price: {cur}{fmt(m.price)}",
        f"⚡ Move: {m.move_pct:+.2f}% in {m.lookback_minutes} min ({m.move_atr:.1f}x ATR)",
        f"📊 RVOL: {m.rvol:.1f}x | RVOL accel: {m.rvol_accel:+.2f}",
        f"📊 RSI {m.rsi:.0f} ({m.rsi_slope:+.1f}) | ADX {m.adx:.0f}",
        f"🕯️ Body quality: {m.body_ratio:.0%}",
        f"🎯 Momentum strength: {m.strength}/10",
    ]
    if m.breakout:
        lines.append(f"🧱 Breakout: {m.breakout}")
    if m.day_move_pct is not None:
        lines.append(f"📆 Day move: {m.day_move_pct:+.2f}%")
    if m.vwap:
        lines.append(f"📉 VWAP: {cur}{fmt(m.vwap)}")
    if m.new_impulse:
        lines.append("✨ Fresh impulse detected")
    if m.overextended:
        lines.append("⚠️ Extended move — avoid treating extension alone as confirmation")
    if strict is not None and strict.direction == m.direction:
        lines.append(f"✅ Strict setup score: {strict.score}/10")
    if extra_text:
        lines.append(extra_text)
    lines.append(f"⏰ Time: {now_label}")
    return "\n".join(lines)
