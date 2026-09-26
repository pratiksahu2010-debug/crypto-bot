"""mockdata.py - synthetic candles for DRY_RUN (no credentials / network needed).
Set MOCK_SPIKE=true to inject a momentum surge into ~1 in 5 symbols so you can test the alert path."""
import os
import zlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config


def _in_session(dt) -> bool:
    if getattr(config, "TRADING_DAYS", "mon-fri") == "mon-fri" and dt.weekday() >= 5:
        return False
    o = getattr(config, "MARKET_OPEN", None)
    c = getattr(config, "MARKET_CLOSE", None)
    if not o or not c:
        return True
    return o <= dt.strftime("%H:%M") < c


def make_candles(symbol: str, n: int = 300) -> pd.DataFrame:
    tz = ZoneInfo(config.TIMEZONE)
    step = int(getattr(config, "CANDLE_MINUTES", 5))
    now = datetime.now(tz).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % step)
    stamps, t, guard = [], now, 0
    while len(stamps) < n and guard < 200000:
        if _in_session(t):
            stamps.append(t)
        t -= timedelta(minutes=step)
        guard += 1
    stamps.reverse()
    seed = zlib.crc32(symbol.encode())
    rng = np.random.default_rng(seed)
    base = 50 + seed % 2000
    rets = rng.normal(0.0002, 0.0015, len(stamps))
    closes = base * np.cumprod(1 + rets)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * (1 + rng.uniform(0, 0.0012, len(stamps)))
    lows = np.minimum(opens, closes) * (1 - rng.uniform(0, 0.0012, len(stamps)))
    vols = rng.lognormal(10, 0.4, len(stamps))
    if os.environ.get("MOCK_SPIKE", "false").lower() == "true" and seed % 5 == 0 and len(stamps) > 5:
        for i, bump in zip((-3, -2, -1), (0.010, 0.012, 0.010)):
            opens[i] = closes[i - 1]
            closes[i] = closes[i - 1] * (1 + bump)
            highs[i] = closes[i] * 1.001
            lows[i] = opens[i] * 0.999
            vols[i] *= 4
    return pd.DataFrame({"timestamp": [s.isoformat() for s in stamps], "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": vols})
