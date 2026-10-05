import os, json, re, csv, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
RED_THRESHOLD = 6
MAX_SENDS = 10
MAX_AGE_MIN = 45
WARN_MIN, WARN_MAX = 1, 16
CONFIRM_PCT = 0.15
LOG_FILE = "alerts_log.csv"

MEGA = {"NVDA","AAPL","MSFT","AMZN","META","GOOGL","GOOG","TSLA","AVGO","NFLX","COST","AMD","ASML"}
MED_OK = r"jobless|retail sales|ism|pmi|ppi|jolts|adp|speaks|testifies|minutes|sentiment|durable"
BLOCK = r"south africa|\bindia\b|indian|bitcoin|crypto|ethereum|\bbtc\b|nigeria|pakistan|kenya"
TIER2 = r"reuters|bloomberg|wall street journal|wsj|associated press|\bap\b|cnbc|financial times|marketwatch|barron|axios|politico|bbc|nikkei"
MUTE = r"motley fool|zacks|seeking alpha|investorplace|benzinga|daily investor|borneo|insider monkey|tipranks|stocktwits|24/7 wall|simply wall|newsbreak"

BULL = r"rate cuts?|cuts? rates|cooler|cools|eases|beats|rally|record high|ceasefire|truce|stimulus|upgrade|tariff (pause|delay|relief)"
BEAR = r"rate hike|hikes? rates|hotter|accelerat|tariff|sanction|export controls?|chip ban|plunge|crash|tumble|sell-?off|recession|downgrade|misses|\bwar\b|missile|attack|shutdown|bank (failure|run)|yields? (jump|surge|rise|spike)"

def gnews(q):
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1h") + "&hl=en-US&gl=US&ceid=US:en"

FEEDS = [
    ("https://www.federalreserve.gov/feeds/press_all.xml", 1),
    ("https://www.bls.gov/feed/bls_latest.rss", 1),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", 2),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", 2),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258", 2),
    ("https://feeds.content.dowjones.io/public/rss/mw_topstories", 2),
    ("https://feeds.content.dowjones.io/public/rss/mw_marketpulse", 2),
    (gnews("Fed OR Powell OR FOMC OR CPI OR inflation OR payrolls"), 0),
    (gnews("US dollar OR DXY OR Treasury yields"), 0),
    (gnews("Nasdaq 100 OR Nvidia OR Apple OR Microsoft OR Tesla stock"), 0),
    (gnews("tariffs OR sanctions OR export controls OR Trump economy"), 0),
    (gnews("Truth Social Trump markets"), 0),
]

KEYWORDS = {
    r"\bfomc\b": 4, r"rate (cut|hike|decision)": 4, r"\bpowell\b": 3, r"\bfed\b|federal reserve": 2,
    r"\bcpi\b": 4, r"\bpce\b": 3, r"inflation": 2, r"nonfarm|payrolls|\bnfp\b": 4,
    r"employment situation|consumer price index|producer price index|job openings": 4,
    r"jobs report": 3, r"unemployment": 2, r"\bgdp\b": 2, r"recession": 3,
    r"tariff": 3, r"export controls?|chip ban": 3, r"sanction": 2, r"executive order": 2,
    r"treasury yield|10-year": 2, r"\bdollar\b|\bdxy\b": 2, r"emergency": 3,
    r"bank (failure|run)": 4, r"circuit breaker|trading halt": 4, r"shutdown|debt ceiling": 3,
    r"jackson hole": 3, r"nasdaq": 2, r"nvidia|nvda": 2,
    r"apple|microsoft|amazon|meta\b|tesla|alphabet|google|broadcom": 1,
    r"earnings|guidance": 1, r"plunge|crash|tumble|selloff|sell-off|soar|surge": 2,
    r"downgrade|upgrade": 1, r"\bwar\b|missile|attack|strike on": 2, r"\btrump\b": 1,
}

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 market-bot"})
    return urllib.request.urlopen(req, timeout=20).read()

def post(hook, body):
    req = urllib.request.Request(hook, json.dumps(body).encode(),
          {"Content-Type": "application/json", "User-Agent": "market-bot"})
    try: urllib.request.urlopen(req, timeout=20)
    except Exception as e: print("discord error", e)
    time.sleep(1.2)

def to_ts(s):
    s = (s or "").strip()
    if not s: return None
    try: return parsedate_to_datetime(s).timestamp()
    except Exception: pass
    try: return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception: return None

def parse(xml):
    root = ET.fromstring(xml)
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] in ("item", "entry"):
            title = link = ""; ts = None
            for c in el:
                t = c.tag.split("}")[-1].lower()
                if t == "title": title = (c.text or "").strip()
                elif t == "link": link = (c.text or c.attrib.get("href") or "").strip()
                elif t in ("pubdate", "published", "updated", "date") and ts is None: ts = to_ts(c.text)
            if title and link: out.append((title, link, ts))
    return out

# ---------- prices ----------
_px = {}
def series(sym):
    if sym in _px: return _px[sym]
    out = []
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym) + "?interval=5m&range=1d"
        r = json.loads(fetch(url))["chart"]["result"][0]
        out = [(t, c) for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if c is not None]
    except Exception as e:
        print("price failed", sym, e)
    _px[sym] = out
    return out

def change(sym, since_ts=None, minutes=15):
    s = series(sym)
    if not s or time.time() - s[-1][0] > 1800: return None
    last_t, last = s[-1]
    ref = since_ts if since_ts else last_t - minutes * 60
    past = [c for t, c in s if t <= ref]
    base = past[-1] if past else s[0][1]
    if sym == "^TNX":
        k = 0.1 if last > 20 else 1
        return (last - base) * k * 100
    return (last / base - 1) * 100

def px_line(since=None):
    nq, dx, ty = change("NQ=F", since), change("DX-Y.NYB", since), change("^TNX", since)
    def f(n, v, u="%"): return f"{n} n/a" if v is None else f"{n} {v:+.2f}{u}"
    return f("NQ", nq) + " | " + f("DXY", dx) + " | " + f("US10Y", ty, "bp"), nq

def market_closed():
    n = datetime.now(ET_TZ); d = n.weekday()
    return d == 5 or (d == 4 and n.hour >= 17) or (d == 6 and n.hour < 18)

# ---------- scoring ----------
def score(title):
    t = title.lower()
    if re.search(BLOCK, t): return 0
    return sum(w for k, w in KEYWORDS.items() if re.search(k, t))

def classify(title, feed_tier):
    if feed_tier != 0: return title, feed_tier
    parts = title.rsplit(" - ", 1)
    if len(parts) 
