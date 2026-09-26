import logging
from apscheduler.triggers.interval import IntervalTrigger
import app_core, config, telegram_notify
from market_feed import feed
from cooldown_manager import CooldownManager
from options_signals import get_option_signal
from engine import threaded_fetch
from ops_base import OpsBase
from storage import BotStorage

log=logging.getLogger("main")
storage=BotStorage(config.SQLITE_PATH,config.SYMBOLS)
cooldown=CooldownManager(storage)

class Ops(OpsBase):
    def boot(self):
        if config.DRY_RUN:
            feed.valid_symbols=set(config.SYMBOLS); feed.provider="mock"
        else:
            valid=feed.validate_symbols(config.SYMBOLS)
            if not valid:
                raise RuntimeError("No valid CoinSwitch/Delta market-data symbols. Check provider connectivity and API configuration.")
            feed.start_websocket()
        self._ready.set()
        log.info("Boot complete: provider=%s symbols=%d",feed.provider,len(feed.valid_symbols))
    def symbols(self):
        return [r["symbol"] for r in storage.get_active_symbols() if r["symbol"] in feed.valid_symbols]
    def fetch_frames(self,symbols):
        return threaded_fetch(feed.get_candles,symbols,workers=min(8,max(2,len(symbols))))
    def live_price(self,symbol): return feed.get_live_price(symbol)
    def recent_move_pct(self,symbol,seconds): return feed.recent_move_pct(symbol,seconds)
    def alert_kwargs(self,symbol,direction):
        if not config.OPTIONS_CONTEXT_ENABLED:return {"option_ctx":None}
        try:return {"option_ctx":get_option_signal(symbol,direction)}
        except Exception:
            log.exception("options context failed for %s",symbol); return {"option_ctx":None}
    def on_feed_outage(self): feed.on_feed_outage()
    def extra_jobs(self): return [(feed.ws_watchdog,IntervalTrigger(seconds=45),"ws_watchdog")]
    def symbol_count(self): return len(feed.valid_symbols)
    def status(self): return feed.status()

ops=Ops()
app,engine,_scheduler=app_core.create_app(storage,cooldown,telegram_notify,ops)
if __name__=="__main__": app.run(host="0.0.0.0",port=config.PORT)
