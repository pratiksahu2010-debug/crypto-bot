"""
config.py
---------
Crypto alert bot - runs 24/7 (no market-hours gate, unlike the NSE bots),
scanning every 5 minutes to match your 5-min trading timeframe.

Data source: CoinSwitch PRO futures with Delta Exchange India fallback. Binance is not used.
"""

import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

BOT_ID = "CRYPTO1"
BOT_NAME = "CRYPTO ALT-COIN ALERT BOT"
TELEGRAM_DISPLAY_NAME = "@CryptoAltAlertBot"

# ---------------------------------------------------------------------------
# Symbol universe - Binance trading pairs (base asset + USDT).
# Built directly into this file so a missing/uncommitted symbols.json can
# never silently shrink your monitored list (this bit the NSE version
# twice - fixed here from day one). bots_config/symbols.json, if present,
# OVERRIDES this list, so you can still edit symbols without touching code.
#
# NOTE: a handful of these tickers are uncertain as real Binance pairs at
# the time this was written (BDAG, VNI, AKE, ESPORTS, WLFI, LEO, LIT -
# some are unlisted/presale tokens, renamed, or exchange-specific). The
# app does NOT trust this list blindly - binance_feed.validate_symbols()
# checks every pair against Binance's live exchangeInfo at boot and
# automatically skips anything not currently tradeable, logging exactly
# which ones and why. Check the boot log after first deploy.
# ---------------------------------------------------------------------------
_BASE_TICKERS = [
    "TRX", "DOGE", "ADA", "XLM", "HBAR", "SUI", "SHIB", "CRO", "VET",
    "XEC", "GALA", "CELR", "RVN", "NEAR", "AAVE", "XMR", "MNT", "DOT",
    "LTC", "BCH", "BTC", "ETH", "SOL", "XRP", "AVAX", "BNB", "ENA",
    "ZEC", "UNI", "LINK", "MATIC", "ATOM", "FIL", "ETC", "APT", "OP",
    "ARB", "INJ", "SEI", "TIA", "PEPE", "WIF", "BONK", "FLOKI", "TON"
]
_BUILT_IN_SYMBOLS = [f"{t}USDT" for t in _BASE_TICKERS]
SYMBOLS = _BUILT_IN_SYMBOLS

# ---------------------------------------------------------------------------
# Strict rule thresholds. Crypto is materially more volatile than NSE
# large/mid-caps, so the VWAP-distance bands are widened vs. the equity
# version - 2% on a large-cap stock and 2% on SHIB in a single 5-min
# candle are not comparable events. Tune these to taste; consider
# backtesting before trusting them with capital.
# ---------------------------------------------------------------------------
RSI_LONG_MIN, RSI_LONG_MAX = 40, 65
RSI_SHORT_MIN, RSI_SHORT_MAX = 35, 60
ADX_MIN = 25
VWAP_MAX_DISTANCE_PCT = 3.0          # widened from 2.0 (equities) for crypto volatility
CONFIDENCE_HIGH_PCT = 1.0            # widened from 0.5
CONFIDENCE_MEDIUM_PCT = 2.0          # widened from 1.5
VOLUME_LOOKBACK = 20
EMA_FAST, EMA_SLOW = 9, 21
EMA_TREND = 50
RSI_PERIOD = 14
ADX_PERIOD = 14
ATR_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RVOL_MIN = 1.20
MIN_BODY_RATIO = 0.45
ATR_EXPANSION_MULT = 1.15

SCORE_ALERT_THRESHOLD = 9
EARLY_SIGNAL_ENABLED = True
EARLY_SCORE_MIN = 7            # score 8/10 = building momentum, not yet confirmed (9+)
EARLY_COOLDOWN_HOURS = 0.5      # shorter than confirmed 2h - crypto moves fast on a 5-min
                                 # timeframe, so a developing setup can re-notify sooner
COOLDOWN_HOURS = 2
CANDLE_INTERVAL = "5m"
SCAN_INTERVAL_MINUTES = 5              # every 5-min candle close (was 15 -> fast moves were seen up to 15 min late)

TIMEZONE = "UTC"
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "Asia/Kolkata")                      # crypto has no single home timezone - UTC throughout
DAILY_SUMMARY_TIME = "23:55"          # UTC
ERROR_SUMMARY_TIME = "23:58"          # UTC
DAILY_RESET_TIME = "00:00"            # UTC - cooldown reset + fresh VWAP session

MAX_CONSECUTIVE_FAILS_BROKEN = 3
MAX_CONSECUTIVE_FAILS_DISABLE = 5

# Point DATA_DIR at a Render persistent-disk mount to keep cooldowns/alert history across restarts
_DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
SQLITE_PATH = str(_DATA_DIR / "alerts.db")

# ---------------------------------------------------------------------------
# Telegram - set in Render's Environment tab, NEVER in this file
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ---------------------------------------------------------------------------
# Options context (Deribit) - BTC/ETH only, see deribit_options_feed.py for
# why the rest of your coin list doesn't get this treatment. Free public
# API, no key needed. Set to False to disable options context entirely
# and keep alerts spot-only, exactly as before.
# ---------------------------------------------------------------------------
OPTIONS_CONTEXT_ENABLED = True

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
PORT = int(os.environ.get("PORT", "10000"))

# Provider selection is intentionally built in so Render only needs the credentials
# that are actually secret. CoinSwitch is primary; Delta India is automatic fallback.
DATA_PROVIDER = "auto"
PRIMARY_PROVIDER = "coinswitch"
FALLBACK_PROVIDER = "delta"
COINSWITCH_API_KEY = os.environ.get("COINSWITCH_API_KEY", "").strip()
COINSWITCH_SECRET_KEY = os.environ.get("COINSWITCH_SECRET_KEY", "").strip()
# Delta public market data is used for fallback, so Delta credentials are NOT required.
DELTA_API_KEY = os.environ.get("DELTA_API_KEY", "").strip()
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "").strip()

MAX_ACTIVE_SYMBOLS = 40
SYMBOL_DISCOVERY_MODE = "provider_filtered"


# ===========================================================================
# Scan engine / big-momentum settings  (added by the bug-fix + momentum upgrade)
# ===========================================================================
MIN_CANDLES = 30                    # candles needed (multi-day history) before indicators are trusted
MAX_CANDLES_KEPT = 400
CANDLE_MINUTES = 5
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "Asia/Kolkata")
TRADING_DAYS = "mon-sun"
KLINE_LIMIT = 400                  # warm-up history for 5-minute indicators
TICK_INTERVAL_SECONDS = 10         # engine checks 'is a scan due?' every 10s (self-healing; also used by /tick)
BOT_SLOT_SECONDS = 30      # UNIQUE per bot: seconds after each 5-min candle close when THIS bot scans
SCAN_BASE_DELAY_SECONDS = 8
CANDLE_CLOSE_GRACE_SECONDS = 10        # let the exchange finalise the candle first
FEED_OUTAGE_RATIO = 0.5            # >=50% symbols without data => feed outage, not 'bad symbols'

# --- alert pacing (so signals never arrive as one simultaneous wall) ---
ALERT_SPACING_SECONDS = 8           # minimum gap between two Telegram messages from this bot
MAX_REGULAR_ALERTS_PER_SCAN = 3     # confirmed/early alerts per scan (best score first, rest re-checked next scan)
                                    # big-momentum alerts are NEVER capped

# --- BIG MOMENTUM detector (see momentum.py) ---
MOMENTUM_ENABLED = True
MOM_LOOKBACK_CANDLES = 3            # measure the move over the last 3 candles (15 min)
MOM_MOVE_PCT = 1.5                 # % move over the lookback that counts as 'fast'
MOM_ATR_MULT = 2.0                  # ...or a net move >= 2.5 x ATR
MOM_RANGE_ATR_MULT = 1.8            # candle range expansion vs ATR
MOM_RVOL_MIN = 2.0                  # volume surge vs prior-20-candle average
MOM_DAY_MOVE_PCT = 2.5              # move vs previous close counted as a supporting factor
MOM_BIG_DAY_PCT = 8.0               # 'DAY_RUNNER' mode: sustained big mover vs previous close
MOM_MIN_FACTORS = 4                 # of 8 factors must agree
# Momentum is NOT time-cooldown based. A new structural/price impulse can alert immediately.
MOM_ESCALATION_PCT = 0.75
MOM_REF_CHANGE_PCT = 0.25
MOM_ACCELERATION_MIN_PCT = 0.20
MOM_RVOL_ACCEL_MIN = 0.35
MOM_RVOL_STRONG = 3.0
MOM_MIN_BODY_RATIO = 0.55
MOM_BREAKOUT_ATR_MIN = 0.12
MOM_RSI_SLOPE_MIN = 1.0
MOM_ADX_MIN = 18.0
MOM_COMPRESSION_RATIO = 0.78
MOM_EXHAUSTION_RANGE_ATR = 2.8
MOM_EXHAUSTION_RSI_LONG = 82.0
MOM_EXHAUSTION_RSI_SHORT = 18.0
MOM_EXHAUSTION_VWAP_PCT = 3.0
MOM_BIG_SCORE = 8

# --- RADAR: tick-driven early trigger between scans (needs a live websocket) ---
RADAR_ENABLED = True
RADAR_INTERVAL_SECONDS = 20
RADAR_WINDOW_SECONDS = 300
RADAR_MOVE_PCT = 1.0              # % move inside the window that wakes the radar
RADAR_RECHECK_SECONDS = 240
RADAR_MAX_SYMBOLS = 12

# --- Data-source fail-over (fixes the Binance 'location' 451 block) ---
# api.binance.com blocks whole IP ranges of some cloud regions (incl. Render's default US
# region) by HTTP 451 - not fixable by retrying Binance's own mirrors. binance_feed.py now
# auto-fails-over to Bybit's public API (candles + websocket) the moment Binance stops
# answering, and quietly re-probes Binance every 30 min to switch back once unblocked.
# The durable fix is still to deploy this service in a non-US Render region (Frankfurt/
# Singapore): Render dashboard -> service -> Settings -> Region.

# --- OPTIONS TRADE SIGNAL (Deribit, BTC/ETH only - see deribit_options_feed.py) ---
# Attached automatically to every BTCUSDT/ETHUSDT alert (momentum, confirmed or early).
OPTION_STRIKE_SELECTION = "ATM"     # "ATM" (closest strike) or "OTM1" (cheaper premium, more leverage)
OPTION_TARGET_MOVE_PCT = 1.5        # expected spot move (%) used to project the option's target premium
OPTION_STOP_MOVE_PCT = 0.7          # adverse spot move (%) used to project the option's stop premium
OPTION_STOP_FLOOR_PCT = 40          # stop premium never implies more than this % loss on the option


def validate_and_report():
    """Loud boot-time diagnostic - visible in Render's Logs tab immediately."""
    print("=" * 60)
    print(f"[config] BOT: {BOT_NAME}")
    print(f"[config] DRY_RUN: {DRY_RUN}")
    print(f"[config] Scan interval: every {SCAN_INTERVAL_MINUTES} min, 24/7 (no market-hours gate)")
    checks = [("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID), ("COINSWITCH_API_KEY", COINSWITCH_API_KEY), ("COINSWITCH_SECRET_KEY", COINSWITCH_SECRET_KEY)]
    any_missing = False
    for name, value in checks:
        if value:
            print(f"[config]   {name}: SET (length {len(value)})")
        else:
            print(f"[config]   {name}: *** MISSING OR EMPTY *** - set this in Render > Environment")
            any_missing = True
    if any_missing and not DRY_RUN:
        print("[config] WARNING: Telegram env vars missing and DRY_RUN is false - alerts will fail until fixed.")
    print("=" * 60)


validate_and_report()

# --- OPTION SIGNAL ENGINE (Deribit public data; BTC/ETH only) ---
# These are selection/liquidity filters, not win-probability claims.
OPTION_INSTRUMENT_CACHE_SECONDS = 300
OPTION_MIN_DTE = 2.0
OPTION_MAX_DTE = 14.0
OPTION_MAX_EXPIRIES_TO_SCAN = 3
OPTION_STRIKES_PER_EXPIRY = 5
OPTION_MAX_SPREAD_PCT = 8.0          # preferred max bid/ask spread
OPTION_HARD_MAX_SPREAD_PCT = 20.0    # never recommend wider than this
OPTION_MIN_OPEN_INTEREST = 0.0       # set >0 if you want an OI floor
OPTION_MIN_VOLUME_24H = 0.0          # set >0 if you want a volume floor
OPTION_MIN_SELECTION_SCORE = 7.0
