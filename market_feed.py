import logging, os, threading, time, json, urllib.parse
import pandas as pd
import requests
import config
from tape import PriceTape

log = logging.getLogger("market_feed")
try:
    import socketio
    SOCKETIO_AVAILABLE = True
except Exception:
    SOCKETIO_AVAILABLE = False

CS_BASE = os.getenv("COINSWITCH_BASE_URL", "https://coinswitch.co").rstrip("/")
CS_WS = "wss://ws.coinswitch.co"
CS_PATH = "/pro/realtime-rates-socket/futures/exchange_2"
CS_EXCHANGE = "EXCHANGE_2"
DELTA_BASE = "https://api.india.delta.exchange"
DELTA_WS = "wss://public-socket.india.delta.exchange"
PROVIDER_PREF = config.DATA_PROVIDER
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "400"))
CACHE_REFRESH_SECONDS = int(os.getenv("CACHE_REFRESH_SECONDS", "240"))
STALE_TICK_SECONDS = int(os.getenv("WS_STALE_SECONDS", "90"))

def empty_df():
    return pd.DataFrame(columns=["timestamp","open","high","low","close","volume"])

def base_of(s):
    s = str(s).upper().replace("/","").replace(",","")
    for q in ("USDT","USD","INR"):
        if s.endswith(q):
            return s[:-len(q)]
    return s

class MarketFeed:
    def __init__(self):
        self.provider = "none"
        self.valid_symbols = set()
        self.provider_symbols = {}
        self.tape = PriceTape()
        self.last_rest_error = ""
        self.last_switch = None
        self._cache, self._cache_ts = {}, {}
        self._lock = threading.RLock()
        self._stop = False
        self._last_ws_tick = 0.0
        self._ws_thread = None

    def _switch(self, name, reason=""):
        with self._lock:
            old = self.provider
            self.provider = name
            self.last_switch = time.time()
        if old != name:
            log.warning("[FEED] %s -> %s: %s", old, name, reason)

    def _cs_sign(self, method, path, params=None):
        key, secret = os.getenv("COINSWITCH_API_KEY","").strip(), os.getenv("COINSWITCH_SECRET_KEY","").strip()
        if not key or not secret:
            raise RuntimeError("CoinSwitch API key/secret not configured")
        from cryptography.hazmat.primitives.asymmetric import ed25519
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
        decoded = urllib.parse.unquote_plus(path)
        epoch = str(int(time.time()*1000))
        private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret))
        sig = private.sign((method.upper()+decoded+epoch).encode()).hex()
        return {
            "Content-Type":"application/json","X-AUTH-APIKEY":key,
            "X-AUTH-SIGNATURE":sig,"X-AUTH-EPOCH":epoch
        }, decoded

    def _cs_get(self, path, params=None):
        headers, final = self._cs_sign("GET", path, params)
        r = requests.get(CS_BASE+final, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(str(data["error"]))
        return data

    def _delta_get(self, path, params=None):
        r = requests.get(DELTA_BASE+path, params=params,
                         headers={"Accept":"application/json"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(str(data.get("error")))
        return data.get("result", data)

    def _cs_symbols(self, requested):
        if not os.getenv("COINSWITCH_API_KEY") or not os.getenv("COINSWITCH_SECRET_KEY"):
            return []
        try:
            payload = self._cs_get("/trade/api/v2/futures/instrument_info", {"exchange":CS_EXCHANGE})
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            available = {str(k).upper() for k,v in data.items()
                         if str((v or {}).get("status","")).upper() in ("TRADING","LIVE","ACTIVE")}
            valid = [s for s in requested if s.upper() in available]
            self.provider_symbols.update({s:s.upper() for s in valid})
            log.info("[COINSWITCH] %d/%d symbols available from instrument_info", len(valid), len(requested))
            return valid
        except requests.HTTPError as exc:
            # CoinSwitch can return 404 for instrument_info while other futures
            # market endpoints remain usable. Do not kill the bot at boot. Probe
            # only the configured symbols through the 5m kline endpoint.
            status = getattr(exc.response, "status_code", None)
            if status != 404:
                raise
            log.warning("[COINSWITCH] instrument_info returned 404; probing configured symbols via futures/klines")
            valid=[]
            now=int(time.time()*1000); start=now-35*5*60*1000
            for s in requested:
                try:
                    self._cs_get("/trade/api/v2/futures/klines", {
                        "exchange":CS_EXCHANGE,"symbol":s.upper(),"interval":"5",
                        "start_time":str(start),"end_time":str(now),"limit":"2"})
                    self.provider_symbols[s]=s.upper(); valid.append(s)
                except Exception as probe_exc:
                    log.debug("[COINSWITCH] symbol probe failed %s: %s", s, probe_exc)
            log.info("[COINSWITCH] %d/%d symbols validated by kline probe", len(valid), len(requested))
            return valid

    def _delta_symbols(self, requested):
        data = self._delta_get("/v2/products", {"page_size":200})
        products = data if isinstance(data,list) else data.get("result",[]) if isinstance(data,dict) else []
        mapped = {}
        for p in products:
            if not isinstance(p,dict): continue
            state, typ = str(p.get("state","")).lower(), str(p.get("contract_type","")).lower()
            if state and state not in ("live","trading","active"): continue
            if typ and "perpetual" not in typ: continue
            ps = str(p.get("symbol","")).upper()
            base = str(p.get("underlying_asset_symbol") or p.get("underlying_asset") or "").upper()
            if not base: base = base_of(ps)
            base = base.replace("/","").replace("-","")
            if base.endswith("USDT"): base=base[:-4]
            if base.endswith("USD"): base=base[:-3]
            mapped.setdefault(base, []).append(ps)
        valid=[]
        for s in requested:
            cands=mapped.get(base_of(s),[])
            if cands:
                cands.sort(key=lambda x:(0 if x==s.upper() else 1,len(x)))
                self.provider_symbols[s]=cands[0]; valid.append(s)
        log.info("[DELTA] %d/%d symbols mapped", len(valid), len(requested))
        return valid

    def validate_symbols(self, requested):
        requested=[str(x).upper() for x in requested]
        self.valid_symbols.clear(); self.provider_symbols.clear()
        errors=[]
        if PROVIDER_PREF in ("auto", "coinswitch") and config.COINSWITCH_API_KEY and config.COINSWITCH_SECRET_KEY:
            try:
                v=self._cs_symbols(requested)
                if v:
                    self.valid_symbols=set(v); self._switch("coinswitch","CoinSwitch PRO primary"); return v
            except Exception as e:
                errors.append("CoinSwitch: "+str(e)); log.warning("[COINSWITCH] %s",e)
        if PROVIDER_PREF in ("auto", "delta", "coinswitch"):
            try:
                v=self._delta_symbols(requested)
                if v:
                    self.valid_symbols=set(v); self._switch("delta","Delta Exchange India fallback"); return v
            except Exception as e:
                errors.append("Delta: "+str(e)); log.warning("[DELTA] %s",e)
        self.last_rest_error=" | ".join(errors) or "No provider returned valid symbols"
        log.error("[FEED] no valid symbols: %s",self.last_rest_error)
        return []

    def _parse_cs(self, payload):
        rows=payload.get("data",[]) if isinstance(payload,dict) else payload
        out=[]
        for r in rows or []:
            try:
                out.append({"timestamp":pd.to_datetime(int(r.get("start_time",r.get("t",0))),unit="ms",utc=True),
                    "open":float(r["o"]),"high":float(r["h"]),"low":float(r["l"]),
                    "close":float(r["c"]),"volume":float(r.get("volume",r.get("v",0)))})
            except Exception: pass
        return pd.DataFrame(out) if out else empty_df()

    def _history_cs(self,s):
        now=int(time.time()*1000); start=now-HISTORY_LIMIT*5*60*1000
        p=self._cs_get("/trade/api/v2/futures/klines",{
            "exchange":CS_EXCHANGE,"symbol":self.provider_symbols[s],"interval":"5",
            "start_time":str(start),"end_time":str(now),"limit":str(HISTORY_LIMIT)})
        return self._parse_cs(p)

    def _history_delta(self,s):
        now=int(time.time()); start=now-HISTORY_LIMIT*5*60
        p=self._delta_get("/v2/history/candles",{
            "resolution":"5m","symbol":self.provider_symbols[s],"start":str(start),"end":str(now)})
        out=[]
        for r in p if isinstance(p,list) else []:
            try:
                out.append({"timestamp":pd.to_datetime(int(r["time"]),unit="s",utc=True),
                    "open":float(r["open"]),"high":float(r["high"]),"low":float(r["low"]),
                    "close":float(r["close"]),"volume":float(r.get("volume",0))})
            except Exception: pass
        return pd.DataFrame(out) if out else empty_df()

    def get_candles(self,symbol,interval=None):
        if config.DRY_RUN:
            from mockdata import make_candles
            return make_candles(symbol)
        s=symbol.upper()
        with self._lock:
            cached=self._cache.get(s); age=time.time()-self._cache_ts.get(s,0)
        if cached is not None and len(cached)>=config.MIN_CANDLES and age<CACHE_REFRESH_SECONDS:
            return cached.copy()
        try:
            df=self._history_cs(s) if self.provider=="coinswitch" else self._history_delta(s)
            if len(df)>=config.MIN_CANDLES:
                df=df.sort_values("timestamp").drop_duplicates("timestamp").tail(HISTORY_LIMIT).reset_index(drop=True)
                with self._lock: self._cache[s]=df; self._cache_ts[s]=time.time()
                return df.copy()
        except Exception as e:
            self.last_rest_error=f"{s}: {e}"; log.warning("[FEED] history %s failed: %s",s,e)
        return cached.copy() if cached is not None else empty_df()

    def _merge_row(self,s,row):
        with self._lock:
            df=self._cache.get(s,empty_df()).copy()
            if df.empty: df=pd.DataFrame([row])
            else:
                mask=df["timestamp"]==row["timestamp"]
                if mask.any():
                    df.loc[mask,["open","high","low","close","volume"]]=[
                        row["open"],row["high"],row["low"],row["close"],row["volume"]]
                else: df=pd.concat([df,pd.DataFrame([row])],ignore_index=True)
            self._cache[s]=df.sort_values("timestamp").drop_duplicates("timestamp").tail(HISTORY_LIMIT).reset_index(drop=True)
            self._cache_ts[s]=time.time()

    def _start_cs_ws(self):
        if not SOCKETIO_AVAILABLE:
            log.error("[COINSWITCH WS] python-socketio missing")
            return
        def run():
            while not self._stop and self.provider=="coinswitch":
                sio=None
                try:
                    sio=socketio.Client(reconnection=False,logger=False,engineio_logger=False)
                    ns="/exchange_2"
                    @sio.on("FETCH_TICKER_INFO_CS_PRO",namespace=ns)
                    def ticker(data):
                        for sym,item in (data.items() if isinstance(data,dict) else []):
                            px=item.get("c") or item.get("p") or item.get("a") or item.get("b")
                            if px: self.tape.update(str(sym).upper(),float(px)); self._last_ws_tick=time.time()
                    @sio.on("FETCH_CANDLESTICK_CS_PRO",namespace=ns)
                    def candle(data):
                        try:
                            item=data[-1] if isinstance(data,list) else data
                            s=str(item.get("s") or item.get("symbol") or "").upper()
                            if s not in self.valid_symbols: return
                            ts=int(item.get("t") or item.get("start_time") or item.get("end_time"))
                            row={"timestamp":pd.to_datetime(ts,unit="ms",utc=True),
                                 "open":float(item["o"]),"high":float(item["h"]),"low":float(item["l"]),
                                 "close":float(item["c"]),"volume":float(item.get("v",item.get("volume",0)))}
                            self.tape.update(s,row["close"]); self._last_ws_tick=time.time(); self._merge_row(s,row)
                        except Exception: log.exception("[COINSWITCH WS] candle parse")
                    sio.connect(CS_WS,namespaces=[ns],transports=["websocket"],socketio_path=CS_PATH,wait=True,wait_timeout=20)
                    for s in sorted(self.valid_symbols):
                        sio.emit("FETCH_TICKER_INFO_CS_PRO",{"event":"subscribe","pair":s},namespace=ns)
                        sio.emit("FETCH_CANDLESTICK_CS_PRO",{"event":"subscribe","pair":f"{s}_5"},namespace=ns)
                    log.info("[COINSWITCH WS] connected for %d symbols",len(self.valid_symbols)); sio.wait()
                except Exception as e: log.warning("[COINSWITCH WS] %s",e)
                finally:
                    try:
                        if sio: sio.disconnect()
                    except Exception: pass
                if not self._stop and self.provider=="coinswitch": time.sleep(5)
        self._ws_thread=threading.Thread(target=run,daemon=True,name="coinswitch-ws"); self._ws_thread.start()

    def _start_delta_ws(self):
        try: import websocket
        except Exception: log.error("[DELTA WS] websocket-client missing"); return
        reverse={p:s for s,p in self.provider_symbols.items()}; ps=list(reverse)
        def run():
            while not self._stop and self.provider=="delta":
                def on_open(ws):
                    ws.send(json.dumps({"type":"subscribe","payload":{"channels":[
                        {"name":"ticker","symbols":ps},{"name":"candlestick_5m","symbols":ps}]}}))
                    log.info("[DELTA WS] connected for %d symbols",len(ps))
                def on_message(ws,msg):
                    try:
                        m=json.loads(msg); p=str(m.get("sy") or m.get("symbol") or "").upper()
                        s=reverse.get(p); typ=str(m.get("type",""))
                        if not s:return
                        px=m.get("p") or m.get("c")
                        if px:self.tape.update(s,float(px));self._last_ws_tick=time.time()
                        if typ=="candlestick_5m" and all(k in m for k in ("o","h","l","c")):
                            # Delta timestamps in this channel are microseconds.
                            raw=int(m.get("ts",time.time()*1_000_000)); ts=raw//1000
                            self._merge_row(s,{"timestamp":pd.to_datetime(ts,unit="ms",utc=True),
                                "open":float(m["o"]),"high":float(m["h"]),"low":float(m["l"]),
                                "close":float(m["c"]),"volume":float(m.get("v",0) or 0)})
                    except Exception: log.exception("[DELTA WS] parse")
                def on_error(ws,e): log.warning("[DELTA WS] %s",e)
                try:
                    websocket.WebSocketApp(DELTA_WS_URL,on_open=on_open,on_message=on_message,on_error=on_error).run_forever(ping_interval=25,ping_timeout=10)
                except Exception as e: log.warning("[DELTA WS] run: %s",e)
                if not self._stop and self.provider=="delta": time.sleep(5)
        self._ws_thread=threading.Thread(target=run,daemon=True,name="delta-ws"); self._ws_thread.start()

    def start_websocket(self):
        if config.DRY_RUN:return
        if self.provider=="coinswitch": self._start_cs_ws()
        elif self.provider=="delta": self._start_delta_ws()

    def ws_watchdog(self):
        if config.DRY_RUN or not self.valid_symbols:return
        if self._last_ws_tick and time.time()-self._last_ws_tick<STALE_TICK_SECONDS:return
        sample=next(iter(self.valid_symbols))
        try:
            df=self._history_cs(sample) if self.provider=="coinswitch" else self._history_delta(sample)
            if len(df)>=config.MIN_CANDLES:
                log.warning("[FEED] websocket stale but REST is healthy; retaining %s",self.provider); return
        except Exception: pass
        log.error("[FEED] provider appears unhealthy; revalidating")
        self.on_feed_outage()

    def get_live_price(self,symbol): return self.tape.latest(symbol,max_age=120)
    def recent_move_pct(self,symbol,seconds): return self.tape.move_pct(symbol,seconds)
    def on_feed_outage(self):
        old=self.provider
        self.validate_symbols(list(config.SYMBOLS))
        if self.provider!=old:self.start_websocket()

    def status(self):
        age=time.time()-self._last_ws_tick if self._last_ws_tick else None
        return {"data_source":self.provider,"primary_provider":"coinswitch","fallback_provider":"delta",
            "last_source_switch":self.last_switch,
            "valid_symbols":len(self.valid_symbols),
            "skipped_invalid_symbols":sorted(set(config.SYMBOLS)-self.valid_symbols),
            "last_rest_error":self.last_rest_error,"seconds_since_last_tick":age,
            "provider_symbols_sample":dict(list(self.provider_symbols.items())[:10]),
            "coinswitch_api_configured":bool(os.getenv("COINSWITCH_API_KEY") and os.getenv("COINSWITCH_SECRET_KEY")),
            "delta_api_configured":bool(os.getenv("DELTA_API_KEY") and os.getenv("DELTA_API_SECRET"))}

feed=MarketFeed()
