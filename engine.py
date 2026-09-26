"""
engine.py - scan orchestration shared by all bots.

Responsibilities
  * tick(): slot-based scheduling. A slot = one SCAN_INTERVAL window shifted by this bot's
    BOT_SLOT_SECONDS. If a slot has not been scanned yet, it is scanned NOW - so a missed
    APScheduler fire, a slow boot or an external ping (/tick) can never lose a scan.
  * run_scan(): fetch -> enrich -> strict score + momentum -> rank -> dispatch with spacing.
  * radar_tick(): watches the live tick tape and immediately re-checks any symbol that is
    moving fast, between the regular scans.
  * Feed-outage protection: if most symbols return no data, that is an outage (expired
    login, exchange holiday, API down) - do NOT blame individual symbols.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import tg_core
from indicators import enrich_dataframe, MIN_CANDLES
from momentum import evaluate_momentum
from scoring import evaluate, should_alert, is_early_signal

log = logging.getLogger("engine")


@dataclass
class Candidate:
    kind: str                     # MOMENTUM | CONFIRMED | EARLY
    symbol: str
    priority: float
    strict: object = None
    mom: object = None
    also_confirmed: bool = False
    meta: dict = field(default_factory=dict)


def threaded_fetch(fn, symbols, workers=4):
    """Runs fn(symbol) concurrently; exceptions become None (=> NO_DATA)."""
    def safe(s):
        try:
            return fn(s)
        except Exception as e:
            log.warning(f"fetch failed for {s}: {e}")
            return None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(zip(symbols, ex.map(safe, symbols)))


class Engine:
    def __init__(self, storage, cooldown, notify, ops):
        self.storage, self.cooldown, self.notify, self.ops = storage, cooldown, notify, ops
        self._tz = ZoneInfo(config.TIMEZONE)
        self._scan_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.last_slot = None
        self._radar_last = {}
        self._signal_candles = set()
        self._sym_cache = (0.0, [])
        self.stats = {"scans_total": 0, "last_scan_started": None, "last_scan_seconds": None,
                      "last_scan_source": None, "last_scan_symbols": 0, "last_scan_no_data": 0,
                      "last_scan_alerts": 0, "last_scan_note": "", "radar_scans": 0}

    # ------------------------------------------------------------------ #
    # Market clock
    # ------------------------------------------------------------------ #
    def market_open_now(self) -> bool:
        now = datetime.now(self._tz)
        if getattr(config, "TRADING_DAYS", "mon-fri") == "mon-fri" and now.weekday() >= 5:
            return False
        o, c = getattr(config, "MARKET_OPEN", None), getattr(config, "MARKET_CLOSE", None)
        if not o or not c:
            return True
        open_t = datetime.strptime(o, "%H:%M").time()
        close_t = datetime.strptime(c, "%H:%M").time()
        # 3-minute grace so the final candle of the session is still scanned
        close_dt = now.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
        return open_t <= now.time() and now <= close_dt + timedelta(minutes=3)

    # ------------------------------------------------------------------ #
    # Slot scheduling
    # ------------------------------------------------------------------ #
    def _slot_id(self, now: float) -> int:
        interval = config.SCAN_INTERVAL_MINUTES * 60
        offset = config.BOT_SLOT_SECONDS + config.SCAN_BASE_DELAY_SECONDS
        return int((now - offset) // interval)

    def next_scan_in(self) -> float:
        interval = config.SCAN_INTERVAL_MINUTES * 60
        offset = config.BOT_SLOT_SECONDS + config.SCAN_BASE_DELAY_SECONDS
        now = time.time()
        return (offset - now) % interval

    def tick(self, source="scheduler", force=False, background=False):
        if not self.ops.ready():
            return {"status": "not_ready", "detail": "feed still starting (login / instrument master)"}
        if not force and not self.market_open_now():
            return {"status": "market_closed"}
        slot = self._slot_id(time.time())
        with self._state_lock:
            if not force and slot == self.last_slot:
                return {"status": "already_scanned_this_slot"}
            if self._scan_lock.locked():
                return {"status": "scan_in_progress"}
            self.last_slot = slot
        if background:
            threading.Thread(target=self.run_scan, kwargs={"source": source}, daemon=True).start()
            return {"status": "scan_started_in_background", "source": source}
        self.run_scan(source=source)
        return {"status": "scan_finished", "source": source, "stats": self.stats}

    # ------------------------------------------------------------------ #
    # Radar (tick-driven early trigger)
    # ------------------------------------------------------------------ #
    def _symbols(self):
        ts, syms = self._sym_cache
        if time.time() - ts > 60:
            syms = self.ops.symbols()
            self._sym_cache = (time.time(), syms)
        return syms

    def radar_tick(self):
        if not getattr(config, "RADAR_ENABLED", False) or not self.ops.ready() or not self.market_open_now():
            return
        if self._scan_lock.locked():
            return
        now = time.time()
        hot = []
        for sym in self._symbols():
            mv = self.ops.recent_move_pct(sym, config.RADAR_WINDOW_SECONDS)
            if mv is not None and abs(mv) >= config.RADAR_MOVE_PCT \
                    and now - self._radar_last.get(sym, 0) > config.RADAR_RECHECK_SECONDS:
                hot.append((abs(mv), sym))
        if not hot:
            return
        hot.sort(reverse=True)
        picked = [s for _, s in hot[:config.RADAR_MAX_SYMBOLS]]
        for s in picked:
            self._radar_last[s] = now
        log.info(f"[RADAR] fast movers -> {picked}")
        self.stats["radar_scans"] += 1
        self.run_scan(symbols=picked, source="radar", momentum_only=True)

    # ------------------------------------------------------------------ #
    # The scan
    # ------------------------------------------------------------------ #
    def run_scan(self, symbols=None, source="scheduler", momentum_only=False):
        if not self._scan_lock.acquire(blocking=False):
            log.info(f"[SCAN] skipped ({source}): another scan is still running")
            return
        t0 = time.time()
        try:
            symbols = symbols or self.ops.symbols()
            log.info(f"[SCAN:{source}] {len(symbols)} symbols")
            frames = self.ops.fetch_frames(symbols)
            candidates, ok, no_data, errors = [], [], [], []
            for sym in symbols:
                try:
                    status = self._analyse(sym, frames.get(sym), momentum_only, candidates)
                except Exception as e:
                    log.exception(f"analyse failed for {sym}")
                    self.storage.log_error(sym, "UNHANDLED_EXCEPTION", str(e))
                    status = "ERROR"
                (ok if status == "OK" else no_data if status == "NO_DATA" else errors if status == "ERROR" else ok).append(sym)
            self._apply_health(symbols, ok, no_data, errors, source)
            sent = self._dispatch(candidates)
            if source != "radar":
                self.stats["scans_total"] += 1
                self.stats["last_scan_started"] = datetime.now(self._tz).isoformat()
                self.stats["last_scan_seconds"] = round(time.time() - t0, 1)
                self.stats["last_scan_source"] = source
                self.stats["last_scan_symbols"] = len(symbols)
                self.stats["last_scan_no_data"] = len(no_data)
                self.stats["last_scan_alerts"] = sent
            log.info(f"[SCAN:{source}] done in {time.time() - t0:.1f}s, ok={len(ok)} no_data={len(no_data)} "
                     f"err={len(errors)} alerts_sent={sent}")
        finally:
            self._scan_lock.release()

    def _analyse(self, sym, df_raw, momentum_only, out: list) -> str:
        if df_raw is None or len(df_raw) < MIN_CANDLES:
            return "NO_DATA"

        # IMPORTANT: live LTP is used only by the radar. It is never injected into
        # OHLC used for signal calculation; enrich_dataframe removes the forming bar.

        try:
            df = enrich_dataframe(df_raw)
        except ValueError:
            return "NO_DATA"

        strict = evaluate(df)
        if strict.reject_reason == "VWAP_UNAVAILABLE":
            self.storage.log_error(sym, "VWAP_MISSING", "VWAP is N/A (zero volume) - strict alert skipped")

        mom = evaluate_momentum(df)
        cand = None
        candle_key = (sym, str(strict.candle_timestamp or df.iloc[-1]["timestamp"]))
        if mom is not None and candle_key not in self._signal_candles and self.cooldown.can_alert_momentum(sym, mom.direction, mom.price, mom.ref_level):
            same = strict.direction == mom.direction and should_alert(strict)
            cand = Candidate("MOMENTUM", sym, 1000 + mom.strength, strict=strict if same else None, mom=mom,
                             also_confirmed=same, meta={"candle_key": candle_key})
            out.append(cand)

        if momentum_only:
            return "OK"

        if should_alert(strict) and not (cand and cand.also_confirmed):
            if self.cooldown.can_alert(sym) and candle_key not in self._signal_candles:
                out.append(Candidate("CONFIRMED", sym, 500 + strict.score, strict=strict, meta={"candle_key": candle_key}))
        elif config.EARLY_SIGNAL_ENABLED and is_early_signal(strict) and self.cooldown.can_alert_early(sym) and candle_key not in self._signal_candles:
            out.append(Candidate("EARLY", sym, strict.score, strict=strict, meta={"candle_key": candle_key}))
        return "OK"

    def _apply_health(self, symbols, ok, no_data, errors, source):
        self.storage.mark_healthy(ok)
        bad = no_data + errors
        if not bad:
            return
        ratio = len(bad) / max(1, len(symbols))
        if len(symbols) >= 6 and ratio >= config.FEED_OUTAGE_RATIO:
            msg = f"{len(bad)}/{len(symbols)} symbols returned no data - treating as FEED OUTAGE (not counting per-symbol failures)"
            log.error(f"[SCAN:{source}] {msg}")
            self.storage.log_error("*", "FEED_OUTAGE", msg)
            self.stats["last_scan_note"] = msg
            try:
                self.ops.on_feed_outage()
            except Exception:
                log.exception("on_feed_outage failed")
            return
        for s in bad:
            self.storage.log_error(s, "NO_DATA", "no/insufficient candles")
            self.storage.record_failure(s, config.MAX_CONSECUTIVE_FAILS_BROKEN, config.MAX_CONSECUTIVE_FAILS_DISABLE)

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def _dispatch(self, cands) -> int:
        cands.sort(key=lambda c: c.priority, reverse=True)
        cap = getattr(config, "MAX_REGULAR_ALERTS_PER_SCAN", 3)
        sent, regular = 0, 0
        for c in cands:
            if c.kind != "MOMENTUM":
                if regular >= cap:
                    continue           # deferred: cooldown NOT started, re-evaluated next scan
                regular += 1
            if self._send(c):
                sent += 1
        return sent

    def _send(self, c: Candidate) -> bool:
        s = c.symbol
        extra = self.ops.alert_kwargs(s, (c.mom or c.strict).direction) if hasattr(self.ops, "alert_kwargs") else {}
        tok, chat, name = config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME
        try:
            if c.kind == "MOMENTUM":
                mid = self.notify.send_momentum_alert(tok, chat, name, c.mom, s, strict=c.strict, **extra)
            elif c.kind == "CONFIRMED":
                mid = self.notify.send_trade_alert(tok, chat, name, c.strict, s, **extra)
            else:
                mid = self.notify.send_early_signal(tok, chat, name, c.strict, s, **extra)
        except Exception as e:
            log.exception(f"send failed for {s}")
            mid = ""
            self.storage.log_error(s, "TELEGRAM_EXCEPTION", str(e))

        delivered = bool(mid) or config.DRY_RUN
        if not delivered:
            # previously the cooldown started anyway and the signal was lost for hours
            self.storage.log_error(s, "TELEGRAM_SEND_FAILED", f"{c.kind} not delivered; will retry next scan")
            return False

        candle_key = c.meta.get("candle_key")
        if candle_key:
            self._signal_candles.add(candle_key)
            if len(self._signal_candles) > 5000:
                self._signal_candles = set(list(self._signal_candles)[-2500:])

        r = c.strict
        if c.kind == "MOMENTUM":
            m = c.mom
            self.storage.log_alert(s, m.direction, m.price, m.vwap, m.rsi, m.adx, 0, 0, m.volume,
                                   m.mode, int(round(m.strength)), mid, signal_type="MOMENTUM")
            self.cooldown.start_momentum_cooldown(s, m.direction, m.price, m.ref_level)
            if c.also_confirmed:
                self.cooldown.start_cooldown(s)
            log.info(f"MOMENTUM ALERT: {s} {m.direction} {m.move_pct:+.2f}% strength={m.strength}")
        else:
            self.storage.log_alert(s, r.direction, r.price, r.vwap, r.rsi, r.adx, r.ema9, r.ema21, r.volume,
                                   r.confidence, r.score, mid, signal_type=c.kind)
            (self.cooldown.start_cooldown if c.kind == "CONFIRMED" else self.cooldown.start_early_cooldown)(s)
            log.info(f"{c.kind} ALERT: {s} {r.direction} score={r.score}/10")
        return True

    # ------------------------------------------------------------------ #
    # Debug helper: what does the bot "see" for a symbol right now?
    # ------------------------------------------------------------------ #
    def inspect(self, symbol):
        frames = self.ops.fetch_frames([symbol])
        df_raw = frames.get(symbol)
        if df_raw is None or len(df_raw) < MIN_CANDLES:
            return {"symbol": symbol, "error": "no/insufficient candles", "rows": 0 if df_raw is None else len(df_raw)}
        df = enrich_dataframe(df_raw)
        strict = evaluate(df)
        mom = evaluate_momentum(df)
        last = df.iloc[-1]
        return {
            "symbol": symbol, "rows": len(df), "last_candle": str(last["timestamp"]),
            "price": float(last["close"]), "vwap": None if last["vwap"] != last["vwap"] else float(last["vwap"]),
            "rsi": float(last["rsi14"]), "adx": float(last["adx14"]), "rvol": float(last["rvol"]),
            "strict": {"direction": strict.direction, "score": int(strict.score), "reject": strict.reject_reason,
                       "conditions": {k: bool(v) for k, v in strict.conditions.items()}},
            "momentum": None if mom is None else {"direction": mom.direction, "mode": mom.mode, "strength": float(mom.strength),
                                                  "move_pct": float(mom.move_pct), "factors": {k: bool(v) for k, v in mom.factors.items()}},
        }
