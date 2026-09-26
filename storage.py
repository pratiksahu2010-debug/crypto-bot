"""
storage.py - SQLite persistence (per bot).

FIXES vs the old version
  * All timestamps are timezone-AWARE in the bot's market timezone (old code used the
    server's naive clock = UTC on Render, so "today" counts / summaries were off).
  * Symbols are no longer disabled forever by transient failures: the morning reset
    (and boot) re-enables everything, and the engine does not count failures during a
    feed-wide outage (login expired, API down, exchange holiday).
  * Momentum alerts get their own state (last time / price / direction) so a runaway move
    can re-alert on genuine escalation but not spam.
Set DATA_DIR to a Render persistent-disk mount to keep cooldowns across restarts/deploys.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    symbol TEXT PRIMARY KEY,
    sector TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    last_alert TEXT DEFAULT '',
    last_early_alert TEXT DEFAULT '',
    cooldown_hours REAL DEFAULT 2,
    alert_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ACTIVE',
    manual_reset INTEGER DEFAULT 0,
    last_momentum_alert TEXT DEFAULT '',
    last_momentum_price REAL DEFAULT 0,
    last_momentum_dir TEXT DEFAULT '',
    last_momentum_ref REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT, symbol TEXT, signal TEXT, price REAL, vwap REAL, rsi REAL, adx REAL,
    ema9 REAL, ema21 REAL, volume REAL, confidence TEXT, score INTEGER, message_id TEXT,
    signal_type TEXT DEFAULT 'CONFIRMED'
);
CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT, symbol TEXT, error_type TEXT, error_message TEXT, retry_count INTEGER
);
"""

_TZ = ZoneInfo(config.TIMEZONE)


def _now() -> datetime:
    return datetime.now(_TZ)


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:          # rows written by the old version were naive server-UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class BotStorage:
    def __init__(self, db_path: str, symbols: list):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init_db()
        self._migrate()
        self._seed_symbols(symbols)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _migrate(self):
        with self._conn() as conn:
            for stmt in [
                "ALTER TABLE settings ADD COLUMN last_early_alert TEXT DEFAULT ''",
                "ALTER TABLE alert_log ADD COLUMN signal_type TEXT DEFAULT 'CONFIRMED'",
                "ALTER TABLE settings ADD COLUMN last_momentum_alert TEXT DEFAULT ''",
                "ALTER TABLE settings ADD COLUMN last_momentum_price REAL DEFAULT 0",
                "ALTER TABLE settings ADD COLUMN last_momentum_dir TEXT DEFAULT ''",
                "ALTER TABLE settings ADD COLUMN last_momentum_ref REAL DEFAULT 0",
            ]:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise

    def _seed_symbols(self, symbols: list):
        with self._conn() as conn:
            for sym in symbols:
                conn.execute("INSERT OR IGNORE INTO settings (symbol, active, status) VALUES (?, 1, 'ACTIVE')", (sym,))

    # ------------------------------------------------------------------ #
    # Settings / health
    # ------------------------------------------------------------------ #
    def get_active_symbols(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM settings WHERE active = 1 AND status != 'DISABLED'").fetchall()
            return [dict(r) for r in rows]

    def recover_disabled(self) -> int:
        """Re-enable every symbol that was auto-disabled/broken (called at boot + morning reset)."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE settings SET status='ACTIVE', active=1, fail_count=0 WHERE status IN ('DISABLED','BROKEN') OR active=0")
            return cur.rowcount

    def record_failure(self, symbol: str, broken_at: int, disable_at: int):
        with self._conn() as conn:
            conn.execute("UPDATE settings SET fail_count = fail_count + 1 WHERE symbol=?", (symbol,))
            row = conn.execute("SELECT fail_count FROM settings WHERE symbol=?", (symbol,)).fetchone()
            fails = row["fail_count"] if row else 0
            if fails >= disable_at:
                conn.execute("UPDATE settings SET status='DISABLED', active=0 WHERE symbol=?", (symbol,))
            elif fails >= broken_at:
                conn.execute("UPDATE settings SET status='BROKEN' WHERE symbol=?", (symbol,))

    def reset_fail_counts_if_healthy(self, symbol: str):
        self.mark_healthy([symbol])

    def mark_healthy(self, symbols: list):
        if not symbols:
            return
        with self._conn() as conn:
            conn.executemany(
                "UPDATE settings SET fail_count=0, status='ACTIVE' WHERE symbol=? AND status != 'DISABLED'",
                [(s,) for s in symbols])

    # ------------------------------------------------------------------ #
    # Cooldowns
    # ------------------------------------------------------------------ #
    def is_in_cooldown(self, symbol: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT last_alert, cooldown_hours, manual_reset FROM settings WHERE symbol=?",
                               (symbol,)).fetchone()
        if not row or not row["last_alert"] or row["manual_reset"]:
            return False
        return _now() < _parse(row["last_alert"]) + timedelta(hours=row["cooldown_hours"] or config.COOLDOWN_HOURS)

    def record_alert_sent(self, symbol: str):
        with self._conn() as conn:
            conn.execute("UPDATE settings SET last_alert=?, alert_count = alert_count + 1, fail_count = 0 WHERE symbol=?",
                         (_now().isoformat(), symbol))

    def is_in_early_cooldown(self, symbol: str, early_cooldown_hours: float) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT last_early_alert, manual_reset FROM settings WHERE symbol=?",
                               (symbol,)).fetchone()
        if not row or not row["last_early_alert"] or row["manual_reset"]:
            return False
        return _now() < _parse(row["last_early_alert"]) + timedelta(hours=early_cooldown_hours)

    def record_early_alert_sent(self, symbol: str):
        with self._conn() as conn:
            conn.execute("UPDATE settings SET last_early_alert=? WHERE symbol=?", (_now().isoformat(), symbol))

    def get_momentum_state(self, symbol: str):
        with self._conn() as conn:
            row = conn.execute("SELECT last_momentum_alert, last_momentum_price, last_momentum_dir, last_momentum_ref FROM settings WHERE symbol=?",
                               (symbol,)).fetchone()
        if not row or not row["last_momentum_alert"]:
            return None
        return _parse(row["last_momentum_alert"]), float(row["last_momentum_price"] or 0), row["last_momentum_dir"], float(row["last_momentum_ref"] or 0)

    def record_momentum_alert(self, symbol: str, direction: str, price: float, ref_level: float = 0.0):
        with self._conn() as conn:
            conn.execute("UPDATE settings SET last_momentum_alert=?, last_momentum_price=?, last_momentum_dir=?, last_momentum_ref=? WHERE symbol=?",
                         (_now().isoformat(), price, direction, ref_level, symbol))

    def reset_all_cooldowns(self):
        with self._conn() as conn:
            conn.execute("UPDATE settings SET last_alert='', last_early_alert='', last_momentum_alert='', "
                         "last_momentum_price=0, last_momentum_dir='', last_momentum_ref=0, manual_reset=0")

    # ------------------------------------------------------------------ #
    # AlertLog
    # ------------------------------------------------------------------ #
    def log_alert(self, symbol, signal, price, vwap, rsi, adx, ema9, ema21,
                  volume, confidence, score, message_id, signal_type="CONFIRMED"):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO alert_log (timestamp, symbol, signal, price, vwap, rsi, adx, ema9, ema21,
                   volume, confidence, score, message_id, signal_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_now().isoformat(), symbol, signal, price, vwap, rsi, adx, ema9, ema21, volume,
                 confidence, score, str(message_id), signal_type))

    def _count(self, signal_type):
        today = _now().date().isoformat()
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) c FROM alert_log WHERE timestamp LIKE ? AND signal_type=?",
                                (f"{today}%", signal_type)).fetchone()["c"]

    def count_alerts_today(self):
        return self._count("CONFIRMED")

    def count_early_signals_today(self):
        return self._count("EARLY")

    def count_momentum_today(self):
        return self._count("MOMENTUM")

    def top_symbols_today(self, limit=5):
        today = _now().date().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT symbol, COUNT(*) c FROM alert_log WHERE timestamp LIKE ? AND signal_type IN ('CONFIRMED','MOMENTUM')
                   GROUP BY symbol ORDER BY c DESC LIMIT ?""", (f"{today}%", limit)).fetchall()
            return [(r["symbol"], r["c"]) for r in rows]

    # ------------------------------------------------------------------ #
    # ErrorLog
    # ------------------------------------------------------------------ #
    def log_error(self, symbol, error_type, error_message, retry_count=0):
        with self._conn() as conn:
            conn.execute("INSERT INTO error_log (timestamp, symbol, error_type, error_message, retry_count) VALUES (?,?,?,?,?)",
                         (_now().isoformat(), symbol, error_type, str(error_message)[:500], retry_count))
            # keep the table small on a long-running instance
            conn.execute("DELETE FROM error_log WHERE id < (SELECT COALESCE(MAX(id),0) - 5000 FROM error_log)")

    def recent_errors(self, limit=10):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM error_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def errors_today_summary(self):
        today = _now().date().isoformat()
        with self._conn() as conn:
            rows = conn.execute("SELECT error_type, COUNT(*) c FROM error_log WHERE timestamp LIKE ? "
                                "GROUP BY error_type ORDER BY c DESC", (f"{today}%",)).fetchall()
            total = conn.execute("SELECT COUNT(*) c FROM error_log WHERE timestamp LIKE ?", (f"{today}%",)).fetchone()["c"]
            return total, [(r["error_type"], r["c"]) for r in rows]
