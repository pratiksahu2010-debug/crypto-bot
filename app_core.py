"""
app_core.py - Flask app + scheduler wiring shared by all bots.

Endpoints
  /health, /status       diagnostics (status shows last scan, slot timing, feed health)
  /tick                  idempotent "scan if one is due" - point an external cron / uptime
                         pinger (cron-job.org, UptimeRobot, Render cron) at it every minute.
                         It also keeps a sleeping Render instance awake.
  /trigger[?force=true]  run a scan now (background; add &wait=true to block until finished)
  /inspect?symbol=XYZ    show the score/momentum readout for one symbol (no alert is sent)
  /telegram_test         sends a test message and returns Telegram's raw reply
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import tg_core
from engine import Engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("app")


def _hm(s):
    h, m = map(int, s.split(":"))
    return h, m


def create_app(storage, cooldown, notify, ops, extra_routes=None):
    app = Flask(__name__)
    engine = Engine(storage, cooldown, notify, ops)
    ops.engine = engine
    tz = ZoneInfo(config.TIMEZONE)
    days = getattr(config, "TRADING_DAYS", "mon-fri")

    # ------------------------------------------------------------------ #
    # Jobs
    # ------------------------------------------------------------------ #
    def job_tick():
        try:
            engine.tick(source="scheduler")
        except Exception:
            log.exception("tick failed")

    def job_radar():
        try:
            engine.radar_tick()
        except Exception:
            log.exception("radar failed")

    def job_morning_reset():
        cooldown.reset_all()
        n = storage.recover_disabled()
        log.info(f"Morning reset: re-enabled {n} symbols")
        try:
            ops.morning_reset_extra()
        except Exception:
            log.exception("morning_reset_extra failed")
        notify.send_health_check(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME, ops.symbol_count())

    def job_daily_summary():
        notify.send_daily_summary(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME,
                                  storage.count_alerts_today(), storage.top_symbols_today(),
                                  total_early=storage.count_early_signals_today())
        # momentum count as an extra line
        n = storage.count_momentum_today()
        if n:
            tg_core.send(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, f"🔥 {config.BOT_NAME}: big-momentum alerts today: {n}")

    def job_error_summary():
        total, breakdown = storage.errors_today_summary()
        notify.send_error_summary(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME, total, breakdown)

    def job_keepalive():
        url = os.environ.get("RENDER_EXTERNAL_URL")
        if url:
            try:
                requests.get(url.rstrip("/") + "/health", timeout=10)
            except Exception:
                pass

    def start_scheduler():
        sched = BackgroundScheduler(timezone=config.TIMEZONE,
                                    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120})
        sched.add_job(job_tick, IntervalTrigger(seconds=config.TICK_INTERVAL_SECONDS), id="tick")
        if getattr(config, "RADAR_ENABLED", False):
            sched.add_job(job_radar, IntervalTrigger(seconds=config.RADAR_INTERVAL_SECONDS), id="radar")
        rh, rm = _hm(getattr(config, "MORNING_RESET_TIME", getattr(config, "DAILY_RESET_TIME", "00:00")))
        sched.add_job(job_morning_reset, CronTrigger(day_of_week=days, hour=rh, minute=rm), id="morning_reset")
        sh, sm = _hm(config.DAILY_SUMMARY_TIME)
        sched.add_job(job_daily_summary, CronTrigger(day_of_week=days, hour=sh, minute=sm), id="daily_summary")
        eh, em = _hm(config.ERROR_SUMMARY_TIME)
        sched.add_job(job_error_summary, CronTrigger(day_of_week=days, hour=eh, minute=em), id="error_summary")
        sched.add_job(job_keepalive, IntervalTrigger(minutes=8), id="keepalive")
        for fn, trigger, jid in getattr(ops, "extra_jobs", lambda: [])():
            sched.add_job(fn, trigger, id=jid)
        sched.start()
        log.info(f"Scheduler started: tick every {config.TICK_INTERVAL_SECONDS}s, scan every "
                 f"{config.SCAN_INTERVAL_MINUTES} min at +{config.BOT_SLOT_SECONDS + config.SCAN_BASE_DELAY_SECONDS}s "
                 f"(slot {config.BOT_SLOT_SECONDS}s), radar={'on' if getattr(config, 'RADAR_ENABLED', False) else 'off'}")
        return sched

    def boot():
        try:
            storage.recover_disabled()
            ops.boot()          # blocks (with retries) until feed is ready; sets ops.ready()
        except Exception:
            log.exception("boot failed")

    scheduler = start_scheduler()          # scheduler first: ticks simply return not_ready until boot finishes
    threading.Thread(target=boot, daemon=True, name="boot").start()

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    @app.route("/")
    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "bot_name": config.BOT_NAME, "ready": ops.ready(), "dry_run": config.DRY_RUN,
                        "time": datetime.now(tz).isoformat()})

    @app.route("/status")
    def status():
        rows = storage.get_active_symbols()
        total_errors, breakdown = storage.errors_today_summary()
        info = {
            "bot_name": config.BOT_NAME, "ready": ops.ready(), "dry_run": config.DRY_RUN,
            "time": datetime.now(tz).isoformat(), "market_open_now": engine.market_open_now(),
            "scan_interval_min": config.SCAN_INTERVAL_MINUTES, "bot_slot_seconds": config.BOT_SLOT_SECONDS,
            "next_scan_in_seconds": round(engine.next_scan_in(), 1),
            "engine": engine.stats, "radar_enabled": getattr(config, "RADAR_ENABLED", False),
            "telegram_token_configured": bool(config.TELEGRAM_TOKEN), "telegram_chat_id_configured": bool(config.TELEGRAM_CHAT_ID),
            "active_symbols": len(rows), "broken_symbols": [r["symbol"] for r in rows if r["status"] == "BROKEN"],
            "alerts_sent_today": storage.count_alerts_today(), "early_signals_sent_today": storage.count_early_signals_today(),
            "momentum_alerts_sent_today": storage.count_momentum_today(),
            "errors_today_total": total_errors, "errors_today_by_type": breakdown,
            "most_recent_errors": storage.recent_errors(10),
        }
        info.update(ops.status())
        return jsonify(info)

    @app.route("/tick")
    def tick():
        return jsonify(engine.tick(source="external_tick", background=True))

    @app.route("/trigger")
    def trigger():
        force = request.args.get("force", "false").lower() == "true"
        wait = request.args.get("wait", "false").lower() == "true"
        res = engine.tick(source="manual_force" if force else "manual", force=force, background=not wait)
        res["forced"] = force
        return jsonify(res)

    @app.route("/inspect")
    def inspect():
        sym = request.args.get("symbol", "")
        if not sym:
            return jsonify({"error": "pass ?symbol=..."}), 400
        try:
            return jsonify(engine.inspect(sym))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/telegram_test")
    def telegram_test():
        if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
            return jsonify({"sent": False, "reason": "TELEGRAM_TOKEN or TELEGRAM_CHAT_ID env var not set"}), 400
        try:
            resp = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
                                 json={"chat_id": config.TELEGRAM_CHAT_ID, "text": f"✅ Test message from {config.BOT_NAME}"}, timeout=10)
            data = resp.json()
        except Exception as e:
            return jsonify({"sent": False, "reason": f"Request failed: {e}"}), 500
        return jsonify({"sent": bool(data.get("ok")), "http_status": resp.status_code, "telegram_response": data,
                        "chat_id_used": config.TELEGRAM_CHAT_ID})

    if extra_routes:
        extra_routes(app)
    return app, engine, scheduler
