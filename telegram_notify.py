"""
telegram_notify.py
-------------------
Same Bot API approach as the NSE version, adapted for crypto:
  - USD ($) instead of INR (₹)
  - Dynamic decimal precision, since BTC ($60,000+) and SHIB ($0.00002) both
    appear in the same symbol list and need very different formatting.
"""

import logging
import requests
import config
import tg_core
from momentum import format_alert as _format_momentum
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("telegram")

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def _fmt_price(p: float) -> str:
    """Dynamic precision: more decimals for sub-$1 assets, fewer for BTC-scale."""
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.6f}"
    return f"{p:.8f}"


def _send(token: str, chat_id: str, text: str, retries: int = 2) -> str:
    """Delegates to tg_core (Markdown fallback, 429 handling, message spacing)."""
    return tg_core.send(token, chat_id, text, retries)


def _fmt_options_section(option_ctx: dict) -> str:
    """Compact CALL/PUT trade candidate with the key option-risk fields."""
    if not option_ctx:
        return ""
    def fnum(v, fmt=".2f"):
        return "N/A" if v is None else format(v, fmt)

    action = "BUY CALL" if option_ctx.get("option_type") == "CALL" else "BUY PUT"
    entry = option_ctx.get("entry_premium_usd")
    target = option_ctx.get("target_premium_usd")
    stop = option_ctx.get("stop_premium_usd")
    lines = [
        "\n\n🎛 *OPTIONS TRADE SIGNAL (Deribit)*",
        f"   👉 *{action}*  | {option_ctx.get('instrument_name', 'N/A')}",
        f"   Underlying: {option_ctx.get('underlying_symbol', 'N/A')} | Spot: ${fnum(option_ctx.get('underlying_price'), ',.2f')}",
        f"   Strike: ${fnum(option_ctx.get('strike'), ',.0f')} | Expiry: {option_ctx.get('expiry_date', 'N/A')} ({fnum(option_ctx.get('days_to_expiry'), '.1f')}d)",
        f"   Entry (ask): ${fnum(option_ctx.get('ask_premium_usd'))} | Bid: ${fnum(option_ctx.get('bid_premium_usd'))} | Mark: ${fnum(option_ctx.get('mark_premium_usd'))}",
        f"   IV: {fnum(option_ctx.get('mark_iv'), '.1f')}% | Spread: {fnum(option_ctx.get('spread_pct'), '.1f')}% | OI: {fnum(option_ctx.get('open_interest'), ',.0f')}",
        f"   Δ {fnum(option_ctx.get('delta'), '+.3f')} | Γ {fnum(option_ctx.get('gamma'), '.6f')} | Θ {fnum(option_ctx.get('theta'), '.4f')} | Vega {fnum(option_ctx.get('vega'), '.4f')}",
        f"   Option quality: {option_ctx.get('quality', 'N/A')} | Selection score: {fnum(option_ctx.get('selection_score'), '.1f')}",
    ]
    if target is not None:
        lines.append(f"   🎯 Target premium: ${fnum(target)} ({fnum(option_ctx.get('premium_target_pct'), '+.1f')}%)")
    if stop is not None:
        lines.append(f"   🛑 Stop premium: ${fnum(stop)} ({fnum(option_ctx.get('premium_stop_pct'), '+.1f')}%)")
    if option_ctx.get("expiry_breakeven") is not None:
        lines.append(f"   📍 Expiry break-even: ${fnum(option_ctx.get('expiry_breakeven'), ',.2f')} (approx., before fees)")
    if option_ctx.get("days_to_expiry", 99) <= 2:
        lines.append("   ⚠️ Near expiry: theta/gamma can change rapidly; this is a short-term alert, not a hold instruction.")
    else:
        lines.append("   ⚠️ Premium is affected by spot, IV and time decay; projected levels are estimates, not guarantees.")
    return "\n".join(lines)


def send_trade_alert(token, chat_id, bot_name, signal_result, symbol, option_ctx: dict = None) -> str:
    r = signal_result
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    display_symbol = symbol.replace("USDT", "/USDT")

    text = (
        f"📊 *{bot_name}*\n"
        f"🚨 TRADE ALERT: {display_symbol}\n"
        f"📈 Signal: {r.direction}\n"
        f"💰 Price: ${_fmt_price(r.price)}\n"
        f"📊 RSI: {r.rsi:.1f} ✅\n"
        f"📈 ADX: {r.adx:.1f} ✅\n"
        f"📉 VWAP: ${_fmt_price(r.vwap)} (MANDATORY ✓)\n"
        f"🔴 9 EMA: ${_fmt_price(r.ema9)}\n"
        f"🟡 21 EMA: ${_fmt_price(r.ema21)}\n"
        f"🔵 50 EMA: ${_fmt_price(getattr(r, 'ema50', 0))}\n"
        f"📈 MACD hist: {getattr(r, 'macd_hist', 0):.6f}\n"
        f"📊 Volume: {r.volume:,.0f} (Above Avg: {'YES' if r.volume > r.vol_avg20 else 'NO'})\n"
        f"📏 Distance from VWAP: {r.vwap_distance_pct:.2f}%\n"
        f"⭐ Confidence: {r.confidence}\n"
        f"🎯 Score: {r.score}/10\n"
        f"🕐 Signal candle: {getattr(r, 'candle_timestamp', 'N/A')}"
        f"⏰ Sent: {datetime.now(ZoneInfo(config.DISPLAY_TIMEZONE)).strftime('%Y-%m-%d %H:%M:%S %Z')}"
        f"{_fmt_options_section(option_ctx)}"
    )
    return _send(token, chat_id, text)


def send_early_signal(token, chat_id, bot_name, signal_result, symbol, option_ctx: dict = None) -> str:
    """
    Sent when a setup is building (score 6-7/10) but not yet at the full
    8/10 confirmation threshold - a heads-up so a fast-moving crypto trade
    isn't missed while waiting for full confirmation.
    """
    r = signal_result
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    display_symbol = symbol.replace("USDT", "/USDT")

    text = (
        f"👀 *{bot_name}*\n"
        f"⚡ EARLY MOMENTUM SIGNAL: {display_symbol}\n"
        f"📈 Building: {r.direction}\n"
        f"💰 Price: ${_fmt_price(r.price)}\n"
        f"📉 VWAP: ${_fmt_price(r.vwap)} (MANDATORY ✓)\n"
        f"📊 RSI: {r.rsi:.1f} | ADX: {r.adx:.1f}\n"
        f"🔴 9 EMA: ${_fmt_price(r.ema9)} | 🟡 21 EMA: ${_fmt_price(r.ema21)}\n"
        f"📏 Distance from VWAP: {r.vwap_distance_pct:.2f}%\n"
        f"🎯 Score: {r.score}/10 (confirmation needs 9+)\n"
        f"🔔 Not yet a confirmed trade - monitor for full setup\n"
        f"🕐 Signal candle: {getattr(r, 'candle_timestamp', 'N/A')}"
        f"⏰ Sent: {datetime.now(ZoneInfo(config.DISPLAY_TIMEZONE)).strftime('%Y-%m-%d %H:%M:%S %Z')}"
        f"{_fmt_options_section(option_ctx)}"
    )
    return _send(token, chat_id, text)


def send_daily_summary(token, chat_id, bot_name, total_alerts, top_symbols, total_early=None):
    lines = [f"📋 *{bot_name} - DAILY SUMMARY (UTC day)*", f"🚨 Confirmed alerts: {total_alerts}"]
    if total_early is not None:
        lines.append(f"👀 Early/watch signals: {total_early}")
    if top_symbols:
        lines.append("🔥 Most active symbols:")
        for sym, count in top_symbols:
            lines.append(f"   • {sym}: {count} alert(s)")
    _send(token, chat_id, "\n".join(lines))


def send_error_summary(token, chat_id, bot_name, total_errors, breakdown):
    lines = [f"⚠️ *{bot_name} - ERROR SUMMARY*", f"Total errors today: {total_errors}"]
    for err_type, count in breakdown:
        lines.append(f"   • {err_type}: {count}")
    if not breakdown:
        lines.append("No errors today ✅")
    _send(token, chat_id, "\n".join(lines))


def send_health_check(token, chat_id, bot_name, symbol_count):
    text = (
        f"🤖 *{bot_name} INITIALIZED*\n"
        f"📊 Monitoring: {symbol_count} symbols (24/7)\n"
        f"⏰ Schedule: every {config.SCAN_INTERVAL_MINUTES} min (+ live radar) | 🔥 Big-momentum detector: ON\n"
        f"📋 VWAP: MANDATORY (resets 00:00 UTC)\n"
        f"🔒 Strict Mode: score ≥ 9/10 required\n"
        f"✅ Bot is LIVE and scanning!"
    )
    _send(token, chat_id, text)


def send_momentum_alert(token, chat_id, bot_name, m, symbol, strict=None, option_ctx: dict = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = _format_momentum(m, symbol.replace("USDT", "/USDT"), bot_name, "$", _fmt_price, now, strict=strict,
                            extra_text=_fmt_options_section(option_ctx).strip("\n"))
    return _send(token, chat_id, text)
