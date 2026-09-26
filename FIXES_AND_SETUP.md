# Fixes and setup

## Fixed on 2026-09-22
- Removed Binance from the critical data path because Render was receiving HTTP 451.
- Added CoinSwitch PRO Futures as primary market data.
- Added Delta Exchange India as automatic fallback.
- Added provider discovery and logical-symbol mapping.
- Added 400-candle 5-minute warm-up.
- Added live ticker/candle WebSocket cache.
- Historical REST is used for warm-up/recovery rather than every scan.
- Existing BIG MOMENTUM detector and radar remain enabled.
- Telegram cooldown starts only after a message is actually delivered.

## Why the old status was broken

Your status showed:
- `valid_symbols: 0`
- all requested pairs in `skipped_invalid_symbols`
- `seconds_since_last_tick: null`
- Binance WebSocket HTTP 451.

That combination means the bot never obtained a usable market feed. It was not an EMA/RSI/ADX problem.

## Provider setup

Set these in Render:
```text
DATA_PROVIDER=auto
COINSWITCH_API_KEY=...
COINSWITCH_SECRET_KEY=...
DELTA_API_KEY=...
DELTA_API_SECRET=...
```

CoinSwitch API secrets must never be pasted into this chat or committed to Git.

See `FIXED_PROVIDER_SETUP.md`.
