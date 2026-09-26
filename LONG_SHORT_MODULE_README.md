
# Additive LONG/SHORT Strategy Module

This add-on was added without replacing or rewriting the existing V4 strategy files.

## What it adds

`long_short_strategy.py` provides an independent confirmation layer using:

- LONG vs SHORT evidence scoring (0-100)
- VWAP position
- 9/21 EMA structure
- locally calculated EMA50/EMA200 regime
- RSI level + RSI slope
- ADX trend strength
- relative volume participation
- candle-body quality
- 20-bar breakout/breakdown structure
- pullback/continuation detection
- over-extension protection
- ATR-based entry/stop/target references
- risk/reward calculation
- explicit WAIT state when evidence conflicts

## Important: existing logic is preserved

The original `scoring.py`, `indicators.py`, engine, feed, Telegram code and configuration are left unchanged.

That means the existing bot will behave exactly as before until this module is explicitly called.

## Activation

The existing bot needs a very small integration hook to automatically include these signals in Telegram alerts. Because the requested change was **"do not change the previous code, only add an extra module"**, this package intentionally does not modify the old files.

Example from another module:

```python
from long_short_strategy import evaluate_and_format

overlay, message = evaluate_and_format(enriched_df, symbol)
print(message)
```

For a production deployment, call the overlay after the existing indicator enrichment and before notification.

## Strategy philosophy

This is a confirmation layer, not a promise of profitability. It deliberately prefers `WAIT` over forcing a LONG/SHORT call when:

- the two sides are too close in score,
- ADX is weak,
- the higher-timeframe regime conflicts with the setup,
- or the data is insufficient.

The original V4 score remains the original score. The add-on score is separate and should not be confused with the existing 9/10 alert score.
