# Crypto Alt-Coin Alert Bot — India/Render provider-safe build

This version is designed for the Render/Binance HTTP 451 problem shown in the logs.

## Data providers
1. **CoinSwitch PRO Futures** — primary when API key + secret are configured.
2. **Delta Exchange India** — automatic fallback.
3. **Binance** — not used.

The alert engine remains 5-minute based and retains VWAP, EMA 9/21, RSI, ADX, volume, early signals, BIG MOMENTUM, radar and Telegram.

## Render variables

```text
DRY_RUN=false
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
DATA_PROVIDER=auto
COINSWITCH_API_KEY=...
COINSWITCH_SECRET_KEY=...
DELTA_API_KEY=...
DELTA_API_SECRET=...
```

Keep secrets only in Render Environment Variables. Do not paste them into chat or commit them.

## Endpoints

- `/health`
- `/status`
- `/inspect?symbol=BTCUSDT`
- `/trigger?force=true&wait=true`
- `/tick`
- `/telegram_test`

## Signal engine

The bot warms historical 5-minute candles, maintains a live WebSocket candle/ticker cache, and scans every 5 minutes. BIG MOMENTUM and radar are independent of the strict score so fast breakouts are not rejected merely because price is already far from VWAP or RSI is extended.

## Important

This build is an **alert/data bot**. It does not place orders using your Delta or CoinSwitch credentials.


## Minimal Render Environment

Only these four secrets are required:

- TELEGRAM_TOKEN
- TELEGRAM_CHAT_ID
- COINSWITCH_API_KEY
- COINSWITCH_SECRET_KEY

Provider behavior is built into the code: CoinSwitch PRO is primary, Delta Exchange India public market data is automatic fallback, and Binance is disabled. All strategy/radar/momentum settings have built-in defaults.

## Options signal module

The bot now includes `options_signals.py`. For BTCUSDT/ETHUSDT alerts it can select a live Deribit CALL for LONG momentum or PUT for SHORT momentum using:

- option instrument symbol
- strike and expiry / days to expiry
- bid / ask / mark premium
- bid/ask spread
- IV
- delta, gamma, theta and vega
- open interest and 24h volume
- liquidity/selection score
- approximate expiry break-even
- delta-based target and stop premium projections

The selector prefers near-ATM, liquid options with 2-14 days to expiry and rejects very wide spreads. It does not place orders.

Deribit documents `get_instruments` as the instrument-discovery endpoint and `ticker` as providing bid/ask, open interest, IV and option Greeks; the bot uses those public fields for selection and display. citeturn1view0turn2view0
