# COMPLETE PROVIDER FIX — 2026-09-22

## Root cause
The Render log shows Binance WebSocket HTTP 451:
`Service unavailable from a restricted location`.

This is a Binance location/IP eligibility restriction. Retrying Binance mirrors does not make the Render IP eligible.

## New architecture
- **CoinSwitch PRO Futures** is primary when its API key and secret are configured.
- **Delta Exchange India** is automatic fallback.
- **Binance is removed from the critical data path.**
- The existing 5-minute EMA/VWAP/RSI/ADX/volume + BIG MOMENTUM + radar + Telegram engine is retained.
- Historical candles are cached; the bot does not hammer the historical API every scan.
- A single live WebSocket maintains ticker/candle data between scans.

CoinSwitch's current Futures API documents `EXCHANGE_2`, `BTCUSDT`-style symbols, 5-minute KLines, and a public Futures WebSocket. Delta's current India API documents `api.india.delta.exchange` and `public-socket.india.delta.exchange` for public market data.

## Render Environment
Put secrets only in Render > Environment; never put them in Git or chat.

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

CoinSwitch credentials are used for authenticated historical Futures KLines/instrument discovery. Delta public market data does not require credentials. Delta credentials are kept available for a future account/order module but this build does not place orders.

## Expected /status
After a successful boot, `/status` should show:
- `ready: true`
- `valid_symbols` greater than 0
- `data_source: coinswitch` OR `delta`
- `seconds_since_last_tick` small after the WebSocket connects
- `last_rest_error` empty unless there was a transient error

## Test sequence
1. Deploy the new files.
2. Wait for `Boot complete`.
3. Open `/health`.
4. Open `/status`.
5. Open `/inspect?symbol=BTCUSDT`.
6. Run `/trigger?force=true&wait=true`.
7. Watch Render logs for `SCAN:... ok=...`.

## Security
Do not send API secrets in this chat. Use Render environment variables. Keep trading permission disabled unless you deliberately add and test a separate execution module.
