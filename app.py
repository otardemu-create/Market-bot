import hashlib, json, logging, os, re, time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread, Lock

LOG = logging.getLogger("market-alerts")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
CALENDAR_LOOKAHEAD_HOURS = max(6, int(os.getenv("CALENDAR_LOOKAHEAD_HOURS", "48")))
PRE_ALERT_MINUTES = int(os.getenv("PRE_ALERT_MINUTES", "10"))
NEWS_LOOKBACK_MINUTES = max(30, int(os.getenv("NEWS_LOOKBACK_MINUTES", "180")))
NEWS_POLL_SECONDS = max(60, int(os.getenv("NEWS_POLL_SECONDS", "180")))
TREND_LOOKBACK_BARS = max(8, int(os.getenv("TREND_LOOKBACK_BARS", "20")))
STATE_FILE = Path(os.getenv("STATE_FILE", "data/state.json"))
FINANCE_CALENDAR_URL = "https://www.financecalendar.com/wp-json/fc/v1/calendar"
FINNHUB_BASE = "https://finnhub.io/api/v1"
NEWS_RSS_QUERY = '("Federal Reserve" OR FOMC OR Powell OR "rate cut" OR "rate hike" OR "rate decision" OR CPI OR PPI OR "nonfarm payroll" OR "jobs report" OR unemployment OR "jobless claims" OR GDP OR PCE OR "retail sales" OR "ISM" OR "Treasury yields" OR "10-year yield" OR "2-year yield" OR "US dollar" OR DXY OR "dollar index" OR "Nasdaq 100" OR Nasdaq OR "stock futures" OR "S&P 500" OR "Brent crude" OR WTI OR OPEC OR tariffs OR "trade war" OR "export controls" OR Nvidia OR Apple OR Microsoft OR Amazon OR Meta OR Google OR Tesla OR Broadcom OR AMD) when:2h'
NEWS_RSS_URL = "https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(NEWS_RSS_QUERY) + "&hl=en-US&gl=US&ceid=US:en"
TRADINGVIEW_WEBHOOK_SECRET = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "").strip()
TRADINGVIEW_STALE_SECONDS = max(120, int(os.getenv("TRADINGVIEW_STALE_SECONDS", "180")))

NAS100_TICKERS = {"NVDA","AAPL","MSFT","AMZN","META","GOOGL","GOOG","TSLA","AVGO","NFLX","COST","AMD","ADBE","PEP","CSCO","INTC"}
# NEWS TRACKER SCOPE:
# Only alert when a story has a credible direct path to USD and/or NAS100.
# Generic global news, politics, commodities, crypto, and company chatter are excluded.
US_MACRO_TERMS = {
    "federal reserve","fomc","fed minutes","fed decision","fed rate decision",
    "interest rate","rate cut","rate hike","rate decision","monetary policy",
    "cpi","consumer price index","core cpi","ppi","producer price",
    "nonfarm payroll","non-farm payroll","jobs report","unemployment rate",
    "jobless claims","adp employment","wage growth","average hourly earnings",
    "gdp","gross domestic product","pce","personal consumption expenditures",
    "retail sales","ism manufacturing","ism services","pmi",
    "consumer confidence","consumer sentiment","inflation expectations",
    "treasury yield","treasury yields","10-year yield","2-year yield",
    "bond yields","us dollar","dollar index","dxy"
}
FED_SPEECH_TERMS = {"fed","federal reserve","powell","fomc","fed governor","fed chair"}
FED_POLICY_TERMS = {"rate","rates","hike","hikes","cut","cuts","inflation","policy","monetary"}
US_FISCAL_TRADE_TERMS = {
    "us tariff","u.s. tariff","us tariffs","u.s. tariffs",
    "trade war","trade agreement","trade deal","export controls",
    "us sanctions","u.s. sanctions","debt ceiling","government shutdown"
}
ENERGY_TERMS = {"oil","crude","brent","wti","opec","opec+","gasoline","natural gas"}
ENERGY_SHOCK_TERMS = {
    "surge","surges","surging","soar","soars","soaring","jump","jumps","spike","spikes",
    "plunge","plunges","plunging","drop","drops","fall","falls","falling",
    "supply disruption","supply shock","production cut","output cut","production halt",
    "supply cut","embargo","disruption"
}
MARKET_MOVE_TERMS = {
    "nasdaq plunge","nasdaq plunges","nasdaq selloff","nasdaq surge","nasdaq surges",
    "nasdaq rally","nasdaq falls","nasdaq rises","nasdaq hits record","nasdaq record high",
    "nasdaq futures rise","nasdaq futures fall","nasdaq futures surge","nasdaq futures plunge",
    "nasdaq 100 rises","nasdaq 100 falls","nasdaq 100 surge","nasdaq 100 plunge",
    "s&p 500 plunge","s&p 500 selloff","s&p 500 surge","s&p 500 rally",
    "us stocks plunge","us stocks surge","us stocks selloff",
    "stock futures plunge","stock futures surge","stock futures rise","stock futures fall"
}
COMPANY_CATALYST_TERMS = {
    "earnings","quarterly results","guidance","profit warning","revenue warning",
    "acquisition","merger","takeover","buyout","bankruptcy","chapter 11",
    "sec investigation","sec charges","antitrust","major lawsuit","accounting fraud",
    "layoffs","job cuts","ceo resigns","ceo steps down"
}
NASDAQ_COMPANIES = {
    "nvidia","apple","microsoft","amazon","meta","google","alphabet","tesla",
    "broadcom","amd","netflix","costco","adobe","pepsico","cisco","intel"
}
NASDAQ_SYMBOLS = {"NVDA","AAPL","MSFT","AMZN","META","GOOGL","GOOG","TSLA","AVGO","AMD","NFLX","COST","ADBE","PEP","CSCO","INTC"}


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
    except (FileNotFoundError,json.JSONDecodeError): return {"pre_alerted":[],"released":[],"news":[],"trend_live":[]}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True,exist_ok=True)
    for key in ("pre_alerted","released","news","trend_live"): state[key]=list(dict.fromkeys(state.get(key,[])))[-5000:]
    tmp=STATE_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(state,indent=2)); tmp.replace(STATE_FILE)

STATE=load_state()
STATE.setdefault("trend_live", [])
STATE_LOCK=Lock()

def api_get(path, params):
    if not FINNHUB_KEY: raise RuntimeError("FINNHUB_API_KEY is missing")
    params=dict(params); params["token"]=FINNHUB_KEY
    r=requests.get(f"{FINNHUB_BASE}{path}",params=params,timeout=20)
    if r.status_code == 429: raise RuntimeError("Finnhub rate limit (429)")
    if r.status_code in (401,403): raise RuntimeError(f"Finnhub authorization/plan error ({r.status_code})")
    r.raise_for_status()
    return r.json()

def discord_send(text):
    if not DISCORD_WEBHOOK_URL: raise RuntimeError("DISCORD_WEBHOOK_URL is required")
    r=requests.post(DISCORD_WEBHOOK_URL,json={"content":text[:2000],"allowed_mentions":{"parse":[]}},timeout=20)
    r.raise_for_status()

def send_once(bucket,uid,text):
    with STATE_LOCK:
        if uid in STATE[bucket]: return False
    discord_send(text)
    with STATE_LOCK:
        STATE[bucket].append(uid)
        save_state(STATE)
    return True

def finance_calendar():
    today=utcnow().date(); end=(utcnow()+timedelta(hours=CALENDAR_LOOKAHEAD_HOURS)).date()
    params={"from":today.isoformat(),"to":end.isoformat(),"impact":"high","limit":500}
    r=requests.get(FINANCE_CALENDAR_URL,params=params,timeout=20); r.raise_for_status()
    data=r.json(); rows=data.get("events",data if isinstance(data,list) else [])
    events=[]
    for row in rows:
        if not isinstance(row,dict): continue
        currency=clean(row.get("currency") or row.get("country")).upper()
        if currency not in {"USD","US"}: continue
        dt=parse_time(row.get("time_utc") or row.get("scheduled_at") or row.get("datetime") or row.get("date"))
        title=clean(row.get("title") or row.get("name") or row.get("event") or row.get("eventName"))
        if not dt or not title: continue
        events.append(Event(fingerprint("calendar",title,currency,dt.isoformat()),title,dt,"USD","high",
            clean(row.get("consensus") or row.get("forecast") or row.get("estimate")),
            clean(row.get("prior") or row.get("previous") or row.get("prev")),
            clean(row.get("actual")),"FinanceCalendar"))
    return events

def event_key(event,kind): return fingerprint(kind,event.title,event.currency,event.time.isoformat())

def pre_alert(event,now):
    if event.actual: return
    delta=(event.time-now).total_seconds()
    if PRE_ALERT_MINUTES*60-45 <= delta <= PRE_ALERT_MINUTES*60+45:
        text=(f"USD HIGH-IMPACT EVENT IN {PRE_ALERT_MINUTES} MIN\n\n{event.title}\nTime: {event.time.strftime('%H:%M UTC')}\n"
              f"Forecast: {event.forecast or 'n/a'}\nPrevious: {event.previous or 'n/a'}\nSource: {event.source}")
        if send_once("pre_alerted",event_key(event,"pre"),text): LOG.info("Pre-alert sent: %s",event.title)

def release_alert(event):
    if not event.actual: return
    uid=event_key(event,"release")+":"+fingerprint(event.actual)
    text=(f"USD HIGH-IMPACT RELEASE\n\n{event.title}\nActual: {event.actual}\nForecast: {event.forecast or 'n/a'}\n"
          f"Previous: {event.previous or 'n/a'}\nSource: {event.source}")
    if send_once("released",uid,text): LOG.info("Release alert sent: %s = %s",event.title,event.actual)

def headline_matches(headline,symbol=""):
    """Only pass genuinely market-moving USD/NAS100 catalysts."""
    text = clean(f"{headline} {symbol}")
    hay = text.lower()

    # Tier 1: scheduled/unscheduled US macro catalysts.
    if any(term in hay for term in US_MACRO_TERMS):
        return True

    # Fed commentary only when it actually concerns rates, inflation or policy.
    if any(term in hay for term in FED_SPEECH_TERMS) and any(term in hay for term in FED_POLICY_TERMS):
        return True

    # US trade/fiscal news only when it is explicitly about a US measure.
    if any(term in hay for term in US_FISCAL_TRADE_TERMS):
        return True

    # Energy only for a genuine supply/price shock, not routine oil coverage.
    has_energy = any(re.search(rf"\\b{re.escape(term)}\\b", hay) for term in ENERGY_TERMS)
    has_energy_shock = any(term in hay for term in ENERGY_SHOCK_TERMS)
    if has_energy and has_energy_shock:
        return True

    # Broad market alerts only when an actual US/Nasdaq move is stated.
    if any(term in hay for term in MARKET_MOVE_TERMS):
        return True

    # Individual Nasdaq names only for material corporate/regulatory catalysts.
    company_hit = any(name in hay for name in NASDAQ_COMPANIES)
    symbol_hit = symbol.upper() in NASDAQ_SYMBOLS if symbol else any(
        re.search(rf"\\b{re.escape(t)}\\b", text, re.I) for t in NASDAQ_SYMBOLS
    )
    catalyst_hit = any(term in hay for term in COMPANY_CATALYST_TERMS)
    return (company_hit or symbol_hit) and catalyst_hit

def rss_news():
    now=utcnow(); cutoff=now-timedelta(minutes=NEWS_LOOKBACK_MINUTES); out=[]
    r=requests.get(NEWS_RSS_URL,headers={"User-Agent":"MarketAlertBot/1.0"},timeout=20)
    r.raise_for_status()
    root=ET.fromstring(r.content)
    recent=0; matched=0
    for item in root.findall(".//item"):
        title=clean(item.findtext("title"))
        link=clean(item.findtext("link"))
        pub=clean(item.findtext("pubDate"))
        if not title or not link or not pub: continue
        try: ts=parsedate_to_datetime(pub)
        except (TypeError,ValueError): continue
        if ts.tzinfo is None: ts=ts.replace(tzinfo=timezone.utc)
        if ts < cutoff: continue
        recent += 1
        if headline_matches(title):
            matched += 1
            source=clean(item.findtext("source")) or "Google News"
            out.append(News(fingerprint("news",title,link),title,link,source,ts,""))
    LOG.info("Google News RSS: %s recent, %s NAS100/macro matches",recent,matched)
    return out

def finnhub_news():
    try:
        now=utcnow(); cutoff=now-timedelta(minutes=NEWS_LOOKBACK_MINUTES); out=[]
        general=api_get("/news",{"category":"general"})
        for row in general if isinstance(general,list) else []:
            title=clean(row.get("headline")); ts=parse_time(row.get("datetime"))
            if not title or not ts or ts < cutoff: continue
            related=clean(row.get("related"))
            if headline_matches(title,related):
                out.append(News(fingerprint("news",title,row.get("url")),title,clean(row.get("url")),clean(row.get("source")) or "Finnhub",ts,related))
        LOG.info("Finnhub fallback: %s candidate(s)",len(out))
        return out
    except Exception as exc:
        LOG.warning("Finnhub news fallback failed: %s",exc)
        return []

def collect_news():
    try:
        items=rss_news()
        if items:
            return sorted({n.uid:n for n in items}.values(),key=lambda n:n.timestamp)
        LOG.warning("Google News RSS returned no matching stories; trying Finnhub fallback")
    except Exception as exc:
        LOG.warning("Google News RSS failed: %s",exc)
    return sorted({n.uid:n for n in finnhub_news()}.values(),key=lambda n:n.timestamp)

def process_news():
    items=collect_news(); sent=0
    for item in items:
        if item.uid in STATE["news"]: continue
        text=(f"MARKET-MOVING HEADLINE\n\n{item.headline}\n"
              + (f"Ticker: {item.symbol}\n" if item.symbol else "")
              + f"Source: {item.source}\n{item.url}")
        if send_once("news",item.uid,text): sent+=1
    LOG.info("News poll complete: %s candidate(s), %s sent",len(items),sent)

def classify_trend(closes):
    if len(closes)<8: return "UNKNOWN",0.0
    n=min(len(closes),TREND_LOOKBACK_BARS); y=closes[-n:]; xbar=(n-1)/2; ybar=sum(y)/n
    den=sum((i-xbar)**2 for i in range(n))
    slope=sum((i-xbar)*(v-ybar) for i,v in enumerate(y))/den if den else 0
    move=(slope*(n-1)/y[0])*100 if y[0] else 0
    return ("UP" if move>0.15 else "DOWN" if move<-0.15 else "FLAT"),move

TV_BARS=[]

def add_tv_bar(payload):
    global TV_BARS
    bar={"ts":float(payload.get("timestamp") or payload.get("time") or time.time()),"open":float(payload["open"]),"high":float(payload["high"]),"low":float(payload["low"]),"close":float(payload["close"])}
    TV_BARS.append(bar); cutoff=bar["ts"]-86400*3; TV_BARS=[b for b in TV_BARS if b["ts"]>=cutoff][-5000:]; return bar

def tv_trend_report():
    now=time.time(); fresh=[b for b in TV_BARS if b["ts"]>=now-86400]
    if not fresh: return "NAS100.pro MARKET TREND\nStatus: WAITING FOR TRADINGVIEW DATA"
    rows=[]
    for label,seconds in [("1H",3600),("2H",7200),("3H",10800),("4H",14400)]:
        subset=[b["close"] for b in fresh if b["ts"]>=now-seconds]; tr,move=classify_trend(subset); rows.append((label,tr,move))
    daily=[b["close"] for b in fresh if b["ts"]>=now-86400]; dtrend,dmove=classify_trend(daily)
    dirs=[x[1] for x in rows+[("Daily",dtrend,dmove)] if x[1] in ("UP","DOWN")]
    overall="UP" if dirs.count("UP")>dirs.count("DOWN") else "DOWN" if dirs.count("DOWN")>dirs.count("UP") else "MIXED"
    age=max(0,int(now-fresh[-1]["ts"]))
    if age>TRADINGVIEW_STALE_SECONDS: overall="STALE"
    return "\n".join(["NAS100.pro MARKET TREND",f"Overall: {overall}",f"Daily: {dtrend}",f"1H: {rows[0][1]}",f"2H: {rows[1][1]}",f"3H: {rows[2][1]}",f"4H: {rows[3][1]}",f"TradingView data age: {age}s"])

def process_tradingview(payload):
    try:
        add_tv_bar(payload); report=tv_trend_report(); uid=f"{int(time.time()//60)}:{report}"
        if send_once("trend_live",uid,report): LOG.info("NAS100.pro TradingView trend sent")
    except Exception as exc: LOG.exception("TradingView webhook processing failed: %s",exc)

class TradingViewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?",1)[0]=="/health":
            body=b"ok"; self.send_response(200); self.send_header("Content-Type","text/plain"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
    def do_POST(self):
        if not self.path.startswith("/tradingview/"): self.send_response(404); self.end_headers(); return
        path_secret=self.path.split("/tradingview/",1)[1].split("?",1)[0]
        if not TRADINGVIEW_WEBHOOK_SECRET or path_secret!=TRADINGVIEW_WEBHOOK_SECRET: self.send_response(401); self.end_headers(); return
        try:
            length=int(self.headers.get("Content-Length","0")); payload=json.loads(self.rfile.read(length))
            if not {"open","high","low","close"}.issubset(payload): raise ValueError("TradingView payload missing OHLC fields")
            Thread(target=process_tradingview,args=(payload,),daemon=True).start()
            body=b"ok"; self.send_response(200); self.send_header("Content-Type","text/plain"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
        except Exception as exc:
            LOG.exception("TradingView webhook rejected: %s",exc); self.send_response(400); self.end_headers()
    def log_message(self,format,*args): return

def run_http():
    port=int(os.getenv("PORT","10000")); server=HTTPServer(("0.0.0.0",port),TradingViewHandler); Thread(target=server.serve_forever,daemon=True).start(); LOG.info("TradingView webhook listening on port %s",port)

def validate():
    missing=[name for name,value in {"FINNHUB_API_KEY":FINNHUB_KEY,"DISCORD_WEBHOOK_URL":DISCORD_WEBHOOK_URL,"TRADINGVIEW_WEBHOOK_SECRET":TRADINGVIEW_WEBHOOK_SECRET}.items() if not value]
    if missing: raise RuntimeError("Missing environment variables: "+", ".join(missing))

def run():
    validate(); run_http(); LOG.info("Market alert bot started. Poll=%ss NewsPoll=%ss",POLL_SECONDS,NEWS_POLL_SECONDS)
    last_news=0.0; last_calendar=0.0
    while True:
        started=time.monotonic(); now=utcnow()
        try:
            if time.monotonic()-last_calendar >= POLL_SECONDS:
                try:
                    events=finance_calendar(); LOG.info("Calendar poll complete: %s event(s)",len(events))
                    for event in events:
                        if now-timedelta(minutes=1)<=event.time<=now+timedelta(hours=CALENDAR_LOOKAHEAD_HOURS):
                            pre_alert(event,now); release_alert(event)
                except Exception as exc: LOG.exception("Calendar poll failed: %s",exc)
                last_calendar=time.monotonic()
            if time.monotonic()-last_news >= NEWS_POLL_SECONDS:
                try: process_news()
                except Exception as exc: LOG.exception("News poll failed: %s",exc)
                last_news=time.monotonic()
        except Exception: LOG.exception("Main loop failed; retrying")
        time.sleep(max(5,POLL_SECONDS-(time.monotonic()-started)))

if __name__=="__main__": run()
