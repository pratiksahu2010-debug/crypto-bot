# Crypto Alt Bot - Full Optimization

Built from the original package without deleting original files. Original `__pycache__` and `.pyc` files are retained exactly as supplied.

## Signal integrity
- Signals use completed 5-minute candles only.
- Forming candle is removed before indicators.
- Live LTP is never injected into OHLC used for scoring.
- One signal per symbol per completed candle.

## Indicators
EMA 9/21/50, session VWAP, RSI 14, MACD 12/26/9, ADX 14 with +DI/-DI, ATR 14, RVOL 20, candle structure, 5/20-bar structure and expansion metrics.

## Signals
Normal LONG/SHORT, Early Momentum, BIG MOMENTUM and options context where supported. Radar remains a trigger/wake-up mechanism; it does not make the forming candle tradable.

## Telegram
Signal candle timestamp is shown separately from Telegram send time. Display timezone defaults to Asia/Kolkata.
