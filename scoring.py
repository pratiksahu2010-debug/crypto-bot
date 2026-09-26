"""Standardized 10-point crypto trend score using only closed candles."""
from dataclasses import dataclass, field
from typing import Optional, Dict
import config

@dataclass
class SignalResult:
    direction: Optional[str] = None
    score: int = 0
    max_score: int = 10
    confidence: Optional[str] = None
    conditions: Dict[str, bool] = field(default_factory=dict)
    price: float = 0.0
    vwap: float = 0.0
    rsi: float = 0.0
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    ema9: float = 0.0
    ema21: float = 0.0
    ema50: float = 0.0
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_hist: float = 0.0
    volume: float = 0.0
    vol_avg20: float = 0.0
    rvol: float = 0.0
    atr: float = 0.0
    vwap_distance_pct: float = 0.0
    candle_timestamp: str = ""
    reject_reason: Optional[str] = None

def _confidence_from_distance(d):
    if d <= config.CONFIDENCE_HIGH_PCT: return "HIGH"
    if d <= config.CONFIDENCE_MEDIUM_PCT: return "MEDIUM"
    return "LOW"

def evaluate(df) -> SignalResult:
    last=df.iloc[-1]; prev=df.iloc[-2]
    price=float(last.close); vwap=float(last.vwap) if last.vwap == last.vwap else None
    r=SignalResult(price=price, candle_timestamp=str(last.timestamp))
    if vwap is None or vwap <= 0:
        r.reject_reason="VWAP_UNAVAILABLE"; return r
    dist=abs(price-vwap)/vwap*100; r.vwap=vwap; r.vwap_distance_pct=round(dist,3)
    if dist > config.VWAP_MAX_DISTANCE_PCT:
        r.reject_reason="OUTSIDE_VWAP_BAND"; return r
    r.rsi=float(last.rsi14); r.adx=float(last.adx14); r.plus_di=float(last.plus_di14); r.minus_di=float(last.minus_di14)
    r.ema9=float(last.ema9); r.ema21=float(last.ema21); r.ema50=float(last.ema50)
    r.macd=float(last.macd); r.macd_signal=float(last.macd_signal); r.macd_hist=float(last.macd_hist)
    r.volume=float(last.volume); r.vol_avg20=float(last.vol_avg20); r.rvol=float(last.rvol); r.atr=float(last.atr14)
    body=float(last.body_range_ratio); rng=float(last.candle_range); atr=r.atr
    atr_expand=atr > float(prev.atr14) if float(prev.atr14 or 0)>0 else False
    bullish_structure=(price > float(last.hh5_prior) if last.hh5_prior == last.hh5_prior else False) or (price > float(last.hh20_prior) if last.hh20_prior == last.hh20_prior else False)
    bearish_structure=(price < float(last.ll5_prior) if last.ll5_prior == last.ll5_prior else False) or (price < float(last.ll20_prior) if last.ll20_prior == last.ll20_prior else False)
    long={
      "ema9_above_ema21": r.ema9>r.ema21, "ema21_above_ema50": r.ema21>r.ema50,
      "price_above_vwap": price>vwap, "rsi_bullish": r.rsi>=config.RSI_LONG_MIN and r.rsi<=config.RSI_LONG_MAX,
      "macd_hist_positive": r.macd_hist>0, "macd_hist_increasing": r.macd_hist>float(prev.macd_hist),
      "adx_direction": r.adx>=config.ADX_MIN and r.plus_di>r.minus_di,
      "rvol_participation": r.rvol>=config.RVOL_MIN,
      "bullish_structure": bullish_structure or (bool(last.bullish_candle) and body>=config.MIN_BODY_RATIO),
      "atr_expansion": atr_expand or (atr>0 and rng/atr>=config.ATR_EXPANSION_MULT),
    }
    short={
      "ema9_below_ema21": r.ema9<r.ema21, "ema21_below_ema50": r.ema21<r.ema50,
      "price_below_vwap": price<vwap, "rsi_bearish": r.rsi>=config.RSI_SHORT_MIN and r.rsi<=config.RSI_SHORT_MAX,
      "macd_hist_negative": r.macd_hist<0, "macd_hist_decreasing": r.macd_hist<float(prev.macd_hist),
      "adx_direction": r.adx>=config.ADX_MIN and r.minus_di>r.plus_di,
      "rvol_participation": r.rvol>=config.RVOL_MIN,
      "bearish_structure": bearish_structure or (bool(last.bearish_candle) and body>=config.MIN_BODY_RATIO),
      "atr_expansion": atr_expand or (atr>0 and rng/atr>=config.ATR_EXPANSION_MULT),
    }
    ls=sum(long.values()); ss=sum(short.values())
    lv=long["price_above_vwap"] and long["ema9_above_ema21"]
    sv=short["price_below_vwap"] and short["ema9_below_ema21"]
    if lv and (not sv or ls>=ss): r.direction="LONG"; r.score=ls; r.conditions=long
    elif sv: r.direction="SHORT"; r.score=ss; r.conditions=short
    else: r.reject_reason="NO_DIRECTIONAL_BIAS"; return r
    r.confidence=_confidence_from_distance(dist)
    return r

def should_alert(result):
    return result.direction is not None and result.reject_reason is None and result.score >= config.SCORE_ALERT_THRESHOLD

def is_early_signal(result):
    return result.direction is not None and result.reject_reason is None and config.EARLY_SCORE_MIN <= result.score < config.SCORE_ALERT_THRESHOLD
