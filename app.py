import hashlib, json, logging, os, re, time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

LOG = logging.getLogger("market-alerts")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
POLL_SECONDS = max(15, int(os.getenv("POLL_SECONDS", "30")))
CALENDAR_LOOKAHEAD_HOURS = max(6, int(os.getenv("CALENDAR_LOOKAHEAD_HOURS", "48")))
PRE_ALERT_MINUTES = int(os.getenv("PRE_ALERT_MINUTES", "10"))
NEWS_LOOKBACK_MINUTES = max(5, int(os.getenv("NEWS_LOOKBACK_MINUTES", "20")))
TREND_LOOKBACK_BARS = max(8, int(os.getenv("TREND_LOOKBACK_BARS", "20")))
STATE_FILE = Path(os.getenv("STATE_FILE", "data/state.json"))
FOREX_FACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FINNHUB_BASE = "https://finnhub.io/api/v1"
TRADINGVIEW_WEBHOOK_SECRET = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "").strip()
TRADINGVIEW_STALE_SECONDS = max(120, int(os.getenv("TRADINGVIEW_STALE_SECONDS", "180")))

NAS100_TICKERS = {"NVDA","AAPL","MSFT","AMZN","META","GOOGL","GOOG","TSLA","AVGO","NFLX","COST","AMD","ADBE","PEP","CSCO","INTC"}
KEYWORDS = {"fed","federal reserve","fomc","powell","interest rate","rate decision","rate cut","rate hike","cpi","inflation","ppi","nfp","nonfarm payroll","non-farm payroll","jobs report","unemployment","gdp","pce","retail sales","ism","pmi","jobless claims","treasury","yield","yields","10-year","tariff","tariffs","sanction","sanctions","nasdaq","nas100","semiconductor","chip restriction","export controls","ai"}

@dataclass(frozen=True)
class Event:
    uid: str
    title: str
    time: datetime
    currency: str
    impact: str
    forecast: str = ""
    previous: str = ""
    actual: str = ""
    source: str = ""

@dataclass(frozen=True)
class News:
    uid: str
    headline: str
    url: str
    source: str
    timestamp: datetime
    symbol: str = ""

def utcnow(): return datetime.now(timezone.utc)

def parse_time(value: Any):
    if value is None: return None
    if isinstance(value, (int, float)): return datetime.fromtimestamp(value, tz=timezone.utc)
    s = str(value).strip()
    if not s: return None
    try:
        dt = datetime.fromisoformat(s.replace("Z","+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError: pass
    for fmt in ("%Y-%m-%d %H:%M:%S","%Y-%m-%dT%H:%M:%S"):
        try: return datetime.strptime(s,fmt).replace(tzinfo=timezone.utc)
        except ValueError: pass
    return None

def clean(value): return re.sub(r"\s+"," ",str(value or "")).strip()

def fingerprint(*parts):
    return hashlib.sha256("|".join(clean(p).lower() for p in parts).encode()).hexdigest()

def load_state():
    try: return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError,json.JSONDecodeError): return {"pre_alerted":[],"released":[],"news":[]}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True,exist_ok=True)
    for key in ("pre_alerted","released","news","trend_live"): state[key]=list(dict.fromkeys(state.get(key,[])))[-5000:]
    tmp=STATE_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(state,indent=2)); tmp.replace(STATE_FILE)

STATE=load_state()
STATE.setdefault("trend_live", [])

def api_get(path, params):
    if not FINNHUB_KEY: raise RuntimeError("FINNHUB_API_KEY is missing")
    params=dict(params); params["token"]=FINNHUB_KEY
    r=requests.get(f"{FINNHUB_BASE}{path}",params=params,timeout=20); r.raise_for_status(); return r.json()

def discord_send(text):
    if not DISCORD_WEBHOOK_URL: raise RuntimeError("DISCORD_WEBHOOK_URL is required")
    r=requests.post(DISCORD_WEBHOOK_URL,json={"content":text[:2000],"allowed_mentions":{"parse":[]}},timeout=20)
    r.raise_for_status()

def send_once(bucket,uid,text):
    if uid in STATE[bucket]: return False
    discord_send(text); STATE[bucket].append(uid); save_state(STATE); return True

def normalize_event(raw,source):
    currency=clean(raw.get("currency") or raw.get("country")).upper()
    impact=clean(raw.get("impact") or raw.get("importance")).lower()
    title=clean(raw.get("event") or raw.get("title") or raw.get("name"))
    dt=parse_time(raw.get("time") or raw.get("date") or raw.get("datetime"))
    if currency!="USD" or impact not in {"high","red","3"} or not title or not dt: return None
    return Event(fingerprint("calendar",title,currency,dt.isoformat()),title,dt,currency,impact,clean(raw.get("estimate") or raw.get("forecast")),clean(raw.get("prev") or raw.get("previous")),clean(raw.get("actual")),source)

FINANCE_CALENDAR_URL = "https://www.financecalendar.com/wp-json/fc/v1/calendar"

def finance_calendar():
    today=utcnow().date(); end=(utcnow()+timedelta(hours=CALENDAR_LOOKAHEAD_HOURS)).date()
    params={"from":today.isoformat(),"to":end.isoformat(),"impact":"high","limit":500}
    r=requests.get(FINANCE_CALENDAR_URL,params=params,timeout=20)
    r.raise_for_status()
    data=r.json()
    rows=data.get("events",data if isinstance(data,list) else [])
    events=[]
    for row in rows:
        if not isinstance(row,dict): continue
        currency=clean(row.get("currency") or row.get("country")).upper()
        if currency not in {"USD","US"}: continue
        dt=parse_time(row.get("time_utc") or row.get("scheduled_at") or row.get("datetime") or row.get("date"))
        title=clean(row.get("title") or row.get("name") or row.get("event") or row.get("eventName"))
        if not dt or not title: continue
        events.append(Event(
            fingerprint("calendar",title,currency,dt.isoformat()),
            title,dt,"USD","high",
            clean(row.get("consensus") or row.get("forecast") or row.get("estimate")),
            clean(row.get("prior") or row.get("previous") or row.get("prev")),
            clean(row.get("actual")),
            "FinanceCalendar"
        ))
    return events

def merge_events(primary,crosscheck=None):
    merged={}
    for event in primary+(crosscheck or []):
        key=fingerprint(event.title,event.currency,event.time.replace(second=0,microsecond=0))
        old=merged.get(key)
        if not old: merged[key]=event; continue
        merged[key]=Event(old.uid,old.title,old.time,old.currency,old.impact,old.forecast or event.forecast,old.previous or event.previous,old.actual or event.actual,old.source if event.source in old.source else old.source+" + "+event.source)
    return sorted(merged.values(),key=lambda x:x.time)

def event_key(event,kind): return fingerprint(kind,event.title,event.currency,event.time.isoformat())

def pre_alert(event,now):
    if event.actual: return
    delta=(event.time-now).total_seconds()
    if PRE_ALERT_MINUTES*60-45 <= delta <= PRE_ALERT_MINUTES*60+45:
        text=(f"USD HIGH-IMPACT EVENT IN {PRE_ALERT_MINUTES} MIN\n\n"
              f"{event.title}\nTime: {event.time.strftime('%H:%M UTC')}\n"
              f"Forecast: {event.forecast or 'n/a'}\nPrevious: {event.previous or 'n/a'}\n"
              f"Source: {event.source}")
        if send_once("pre_alerted",event_key(event,"pre"),text): LOG.info("Pre-alert sent: %s",event.title)


def release_alert(event):
    if not event.actual: return
    uid=event_key(event,"release")+":"+fingerprint(event.actual)
    text=(f"USD HIGH-IMPACT RELEASE\n\n{event.title}\nActual: {event.actual}\n"
          f"Forecast: {event.forecast or 'n/a'}\nPrevious: {event.previous or 'n/a'}\n"
          f"Source: {event.source}")
    if send_once("released",uid,text): LOG.info("Release alert sent: %s = %s",event.title,event.actual)


def headline_matches(headline,symbol=""):
    hay=f"{headline} {symbol}".lower()
    return symbol.upper() in NAS100_TICKERS or any(k in hay for k in KEYWORDS)

def finnhub_news():
    now=utcnow(); start=(now-timedelta(minutes=NEWS_LOOKBACK_MINUTES)).date().isoformat(); end=now.date().isoformat(); out=[]
    general=api_get("/news",{"category":"general"})
    for row in general if isinstance(general,list) else []:
        title=clean(row.get("headline")); ts=parse_time(row.get("datetime"))
        if not title or not ts or ts < now-timedelta(minutes=NEWS_LOOKBACK_MINUTES) or not headline_matches(title,clean(row.get("related"))): continue
        out.append(News(fingerprint("news",title,row.get("url")),title,clean(row.get("url")),clean(row.get("source")) or "Finnhub",ts,clean(row.get("related"))))
    for symbol in sorted(NAS100_TICKERS):
        try: rows=api_get("/company-news",{"symbol":symbol,"from":start,"to":end})
        except Exception as exc: LOG.warning("Company news failed for %s: %s",symbol,exc); continue
        for row in rows if isinstance(rows,list) else []:
            title=clean(row.get("headline")); ts=parse_time(row.get("datetime"))
            if not title or not ts or ts < now-timedelta(minutes=NEWS_LOOKBACK_MINUTES): continue
            out.append(News(fingerprint("news",title,row.get("url")),title,clean(row.get("url")),clean(row.get("source")) or "Finnhub",ts,symbol))
    return sorted({n.uid:n for n in out}.values(),key=lambda n:n.timestamp)

def classify_trend(closes):
    if len(closes) < 8:
        return "UNKNOWN", 0.0
    n=min(len(closes), TREND_LOOKBACK_BARS); y=closes[-n:]
    xbar=(n-1)/2; ybar=sum(y)/n; den=sum((i-xbar)**2 for i in range(n))
    slope=sum((i-xbar)*(v-ybar) for i,v in enumerate(y))/den if den else 0
    move=(slope*(n-1)/y[0])*100 if y[0] else 0
    return ("UP" if move>0.15 else "DOWN" if move<-0.15 else "FLAT"),move

TV_BARS = []

def add_tv_bar(payload):
    global TV_BARS
    bar = {
        "ts": float(payload.get("timestamp") or payload.get("time") or time.time()),
        "open": float(payload["open"]),
        "high": float(payload["high"]),
        "low": float(payload["low"]),
        "close": float(payload["close"]),
    }
    TV_BARS.append(bar)
    cutoff=bar["ts"]-86400*3
    TV_BARS=[b for b in TV_BARS if b["ts"]>=cutoff][-5000:]
    return bar

def tv_trend_report():
    now=time.time()
    fresh=[b for b in TV_BARS if b["ts"]>=now-86400]
    if not fresh:
        return "📊 NAS100.pro MARKET TREND\nStatus: WAITING FOR TRADINGVIEW DATA"
    rows=[]
    for label,seconds in [("1H",3600),("2H",7200),("3H",10800),("4H",14400)]:
        subset=[b["close"] for b in fresh if b["ts"]>=now-seconds]
        tr,move=classify_trend(subset)
        rows.append((label,tr,move))
    daily=[b["close"] for b in fresh if b["ts"]>=now-86400]
    dtrend,dmove=classify_trend(daily)
    dirs=[x[1] for x in rows+[("Daily",dtrend,dmove)] if x[1] in ("UP","DOWN")]
    overall="UP" if dirs.count("UP")>dirs.count("DOWN") else "DOWN" if dirs.count("DOWN")>dirs.count("UP") else "MIXED"
    age=max(0,int(now-fresh[-1]["ts"]))
    if age > TRADINGVIEW_STALE_SECONDS:
        overall="STALE"
    lines=[
        "📊 NAS100.pro MARKET TREND",
        f"Overall: {overall}",
        f"Daily: {dtrend}",
        f"1H: {rows[0][1]}",
        f"2H: {rows[1][1]}",
        f"3H: {rows[2][1]}",
        f"4H: {rows[3][1]}",
        f"TradingView data age: {age}s",
    ]
    return "\n".join(lines)

def process_tradingview(payload):
    try:
        add_tv_bar(payload)
        report=tv_trend_report()
        uid=f"{int(time.time()//60)}:{report}"
        if send_once("trend_live",uid,report):
            LOG.info("NAS100.pro TradingView trend sent")
    except Exception as exc:
        LOG.exception("TradingView webhook processing failed: %s",exc)

class TradingViewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?",1)[0] == "/health":
            body=b"ok"
            self.send_response(200)
            self.send_header("Content-Type","text/plain")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/tradingview/"):
            self.send_response(404)
            self.end_headers()
            return
        path_secret=self.path.split("/tradingview/",1)[1].split("?",1)[0]
        if not TRADINGVIEW_WEBHOOK_SECRET or path_secret != TRADINGVIEW_WEBHOOK_SECRET:
            self.send_response(401)
            self.end_headers()
            return
        try:
            length=int(self.headers.get("Content-Length","0"))
            payload=json.loads(self.rfile.read(length))
            if not {"open","high","low","close"}.issubset(payload):
                raise ValueError("TradingView payload missing OHLC fields")
            Thread(target=process_tradingview,args=(payload,),daemon=True).start()
            body=b"ok"
            self.send_response(200)
            self.send_header("Content-Type","text/plain")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            LOG.exception("TradingView webhook rejected: %s",exc)
            self.send_response(400)
            self.end_headers()
    def log_message(self, format, *args): return

def run_http():
    port=int(os.getenv("PORT","10000"))
    server=HTTPServer(("0.0.0.0",port),TradingViewHandler)
    Thread(target=server.serve_forever,daemon=True).start()
    LOG.info("TradingView webhook listening on port %s",port)


def process_news():
    for item in finnhub_news():
        if item.uid in STATE["news"]: continue
        text=(f"📰 MARKET-MOVING HEADLINE\n\n{item.headline}\n"
              + (f"Ticker: {item.symbol}\n" if item.symbol else "")
              + f"Source: {item.source}\n{item.url}")
        send_once("news",item.uid,text)

def validate():
    missing=[name for name,value in {"FINNHUB_API_KEY":FINNHUB_KEY,"DISCORD_WEBHOOK_URL":DISCORD_WEBHOOK_URL,"TRADINGVIEW_WEBHOOK_SECRET":TRADINGVIEW_WEBHOOK_SECRET}.items() if not value]
    if missing: raise RuntimeError("Missing environment variables: "+", ".join(missing))

def run():
    validate(); run_http(); LOG.info("Market alert bot started. Poll=%ss",POLL_SECONDS)
    while True:
        started=time.monotonic(); now=utcnow()
        try:
            events=finance_calendar()
            for event in events:
                if now-timedelta(minutes=1) <= event.time <= now+timedelta(hours=CALENDAR_LOOKAHEAD_HOURS):
                    pre_alert(event,now); release_alert(event)
            process_news()
        except Exception: LOG.exception("Polling cycle failed; retrying next cycle")
        time.sleep(max(5,POLL_SECONDS-(time.monotonic()-started)))

if __name__=="__main__": run()
