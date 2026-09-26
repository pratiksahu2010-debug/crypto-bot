"""
indicators.py
-------------
Pure indicator maths + candle cleaning.

KEY FIXES vs the old version
  * Candles are normalised to tz-aware timestamps in the bot's MARKET timezone.
  * VWAP is computed PER SESSION DATE (resets every trading day) while EMA/RSI/ADX/ATR
    run over several days of history. Previously only "today's" candles were fetched, so
    the bot needed 21 candles (~105 min on 5-min bars, ~5h on 15-min bars) before it could
    produce a single signal - and every one of those scans was logged as a FAILURE, which
    eventually DISABLED every symbol.
  * Extra columns needed by the momentum module: ATR, relative volume, previous close,
    prior day-high/low and prior 20-candle high/low.
"""

import logging
import numpy as np
import pandas as pd

import config

log = logging.getLogger("indicators")

MIN_CANDLES = getattr(config, "MIN_CANDLES", 30)


# ---------------------------------------------------------------------- #
# Candle cleaning
# ---------------------------------------------------------------------- #
def normalize_timestamps(ts: pd.Series, tz: str) -> pd.Series:
    """Accepts ISO strings (with or without offset) or epoch-milliseconds."""
    if pd.api.types.is_numeric_dtype(ts):
        idx = pd.to_datetime(ts, unit="ms", utc=True, errors="coerce")
    else:
        idx = pd.to_datetime(ts, utc=True, errors="coerce")
    return idx.dt.tz_convert(tz)


def clean_candles(df: pd.DataFrame, tz: str = None) -> pd.DataFrame:
    tz = tz or config.TIMEZONE
    out = df.copy()
    out["timestamp"] = normalize_timestamps(out["timestamp"], tz)
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["timestamp", "open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0).clip(lower=0.0)
    out = out.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp")

    sess = getattr(config, "SESSION_FILTER", None)
    if sess:  # e.g. US: keep regular session only, so VWAP is not polluted by pre/post-market prints
        idx = out.set_index("timestamp")
        idx = idx.between_time(sess[0], sess[1], inclusive="left")
        out = idx.reset_index()
    return out.reset_index(drop=True)


def drop_unfinished_candle(df: pd.DataFrame, interval_minutes: int = None) -> pd.DataFrame:
    """Remove the currently-forming candle when timestamps represent candle start time."""
    if df.empty:
        return df
    minutes = int(interval_minutes or getattr(config, "CANDLE_MINUTES", 5))
    last_ts = df["timestamp"].iloc[-1]
    if pd.isna(last_ts):
        return df
    now = pd.Timestamp.now(tz=last_ts.tz)
    candle_end = last_ts + pd.Timedelta(minutes=minutes)
    grace = int(getattr(config, "CANDLE_CLOSE_GRACE_SECONDS", 10))
    if now < candle_end + pd.Timedelta(seconds=grace):
        return df.iloc[:-1].copy()
    return df


# ---------------------------------------------------------------------- #
# Indicators
# ---------------------------------------------------------------------- #
def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP - cumulative sums restart on every new session_date."""
    if df.empty:
        return pd.Series(dtype=float)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df["session_date"]
    cum_tpv = (tp * df["volume"]).groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return cum_tpv / cum_v


def calculate_ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    return df[column].ewm(span=period, adjust=False).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    if df.empty or len(df) < period + 1:
        return pd.Series([np.nan] * len(df), index=df.index)
    delta = df[column].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # avg_loss == 0 with gains -> RSI 100 (was silently turned into 50 before)
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    return rsi.fillna(50)


def _true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    return pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return _true_range(df).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    if df.empty or len(df) < period + 1:
        return pd.Series([np.nan] * len(df), index=df.index)
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)      # both computed from RAW moves
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr = calculate_atr(df, period)
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr.replace(0, np.nan))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().fillna(0)


def enrich_dataframe(df: pd.DataFrame, tz: str = None) -> pd.DataFrame:
    """Clean candles, discard the forming bar, then calculate indicators.

    Signal consumers therefore always see a fully closed 5-minute candle as the
    last row. Live LTP must never be injected into OHLC used for scoring.
    """
    out = clean_candles(df, tz)
    out = drop_unfinished_candle(out)
    if len(out) < MIN_CANDLES:
        raise ValueError(f"Not enough completed candles ({len(out)} < {MIN_CANDLES})")

    out = out.tail(getattr(config, "MAX_CANDLES_KEPT", 400)).reset_index(drop=True)
    out["session_date"] = out["timestamp"].dt.date

    out["vwap"] = calculate_vwap(out)
    out["ema9"] = calculate_ema(out, getattr(config, "EMA_FAST", 9))
    out["ema21"] = calculate_ema(out, getattr(config, "EMA_SLOW", 21))
    out["ema50"] = calculate_ema(out, getattr(config, "EMA_TREND", 50))
    out["rsi14"] = calculate_rsi(out, getattr(config, "RSI_PERIOD", 14))
    out["atr14"] = calculate_atr(out, getattr(config, "ATR_PERIOD", 14))

    # ADX + directional movement are exposed separately for scoring.
    up = out["high"].diff()
    down = -out["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr = out["atr14"]
    plus_di = 100 * (plus_dm.ewm(alpha=1/getattr(config, "ADX_PERIOD", 14), min_periods=getattr(config, "ADX_PERIOD", 14), adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1/getattr(config, "ADX_PERIOD", 14), min_periods=getattr(config, "ADX_PERIOD", 14), adjust=False).mean() / atr.replace(0, np.nan))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    out["plus_di14"] = plus_di
    out["minus_di14"] = minus_di
    out["adx14"] = dx.ewm(alpha=1/getattr(config, "ADX_PERIOD", 14), min_periods=getattr(config, "ADX_PERIOD", 14), adjust=False).mean().fillna(0)

    fast = getattr(config, "MACD_FAST", 12)
    slow = getattr(config, "MACD_SLOW", 26)
    sig = getattr(config, "MACD_SIGNAL", 9)
    out["macd"] = calculate_ema(out, fast) - calculate_ema(out, slow)
    out["macd_signal"] = out["macd"].ewm(span=sig, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    out["macd_hist_slope"] = out["macd_hist"].diff()

    lookback = getattr(config, "VOLUME_LOOKBACK", 20)
    out["vol_avg20"] = out["volume"].shift(1).rolling(lookback, min_periods=5).mean()
    out["rvol"] = (out["volume"] / out["vol_avg20"].replace(0, np.nan)).fillna(0.0)
    out["rvol_slope"] = out["rvol"].diff()
    out["atr_slope"] = out["atr14"].diff()

    # Candle structure.
    out["candle_range"] = (out["high"] - out["low"]).clip(lower=0)
    out["candle_body"] = (out["close"] - out["open"]).abs()
    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    out["body_range_ratio"] = (out["candle_body"] / out["candle_range"].replace(0, np.nan)).fillna(0)
    out["bullish_candle"] = out["close"] > out["open"]
    out["bearish_candle"] = out["close"] < out["open"]

    g = out.groupby("session_date")
    last_close_by_day = g["close"].last()
    out["prev_close"] = out["session_date"].map(last_close_by_day.shift(1))
    out["day_open"] = g["open"].transform("first")
    out["day_high_prior"] = g["high"].transform(lambda s: s.cummax().shift(1))
    out["day_low_prior"] = g["low"].transform(lambda s: s.cummin().shift(1))
    out["hh5_prior"] = out["high"].shift(1).rolling(5, min_periods=3).max()
    out["ll5_prior"] = out["low"].shift(1).rolling(5, min_periods=3).min()
    out["hh20_prior"] = out["high"].shift(1).rolling(20, min_periods=5).max()
    out["ll20_prior"] = out["low"].shift(1).rolling(20, min_periods=5).min()
    return out
