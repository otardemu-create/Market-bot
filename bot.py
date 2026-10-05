import os, json, re, time, random, threading, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
import websocket  # websocket-client

ET_TZ = ZoneInfo("America/New_York")
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
ALPACA_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
RED_THRESHOLD = 6
MAX_AGE_MIN = 20
CONFIRM_PCT = 0.15

RED = os.environ.get("DISCORD_RED", "")
NEWS = os.environ.get("DISCORD_NEWS", "")

MEGA = {"NVDA","AAPL","MSFT","AMZN","META","GOOGL","GOOG","TSLA","AVGO","NFLX","COST","AMD","ASML"}
MED_OK = r"jobless|retail sales|ism|pmi|ppi|jolts|adp|speaks|testifies|minutes|sentiment|durable"
BLOCK = r"south africa|\bindia\b|indian|bitcoin|crypto|ethereum|\bbtc\b|nigeria|pakistan|kenya"
TIER2 = r"reuters|bloomberg|wall street journal|wsj|associated press|\bap\b|cnbc|financial times|marketwatch|barron|axios|politico|bbc|nikkei"
MUTE = r"motley fool|zacks|seeking alpha|investorplace|daily investor|borneo|insider monkey|tipranks|stocktwits|24/7 wall|simply wall|newsbreak"
BULL = r"rate cuts?|cuts? rates|cooler|cools|eases|beats|rally|record high|ceasefire|truce|stimulus|upgrade|tariff (pause|delay|relief)"
BEAR = r"rate hike|hikes? rates|hotter|accelerat|tariff|sanction|export controls?|chip ban|plunge|crash|tumble|sell-?off|recession|downgrade|misses|\bwar\b|missile|attack|shutdown|bank (failure|run)|yields? (jump|surge|rise|spike)"

def gnews(q):
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1h") + "&hl=en-US&gl=US&ceid=US:en"

# (url, tier, poll seconds)
FEEDS = [
    ("https://www.federalreserve.gov/feeds/press_all.xml", 1, 30),
    ("https://www.bls.gov/feed/bls_latest.rss", 1, 30),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", 2, 45),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", 2, 45),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258", 2, 45),
    ("https://feeds.content.dowjones.io/public/rss/mw_topstories", 2, 45),
    ("https://feeds.content.dowjones.io/public/rss/mw_marketpulse", 2, 45),
    (gnews("Fed OR Powell OR FOMC OR CPI OR inflation OR payrolls"), 0, 90),
    (gnews("US dollar OR DXY OR Treasury yields"), 0, 90),
    (gnews("Nasdaq 100 OR Nvidia OR Apple OR Microsoft OR Tesla stock"), 0, 90),
    (gnews("tariffs OR sanctions OR export controls OR Trump economy"), 0, 90),
    (gnews("Truth Social Trump markets"), 0, 90),
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

lock = threading.Lock()
seen = {}      # key -> time first seen
titles = {}    # normalized title -> time sent
fired = set()  # calendar / open / earnings keys
STATE = {"warm": True, "ws_err": False}

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 market-bot"})
    return urllib.request.urlopen(req, timeout=15).read()

def post(hook, body):
    if not hook: return
    for _ in range(3):
        try:
            req = urllib.request.Request(hook, json.dumps(body).encode(),
                  {"Content-Type": "application/json", "User-Agent": "market-bot"})
            urllib.request.urlopen(req, timeout=15)
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try: wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception: wait = 2
                time.sleep(min(wait, 10) + 0.5)
            else:
                print("discord error", e.code); return
        except Exception as e:
            print("discord error", e); return

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
    c = _px.get(sym)
    if c and time.time() - c[0] < 20: return c[1]
    out = []
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym) + "?interval=1m&range=1d"
        r = json.loads(fetch(url))["chart"]["result"][0]
        out = [(t, v) for t, v in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if v is not None]
    except Exception as e:
        print("price failed", sym, e)
        if c: out = c[1]
    _px[sym] = (time.time(), out)
    return out

def change(sym, since_ts=None, minutes=15):
    s = series(sym)
    if not s or time.time() - s[-1][0] > 1800: return None
    last_t, last = s[-1]
    ref = since_ts if since_ts else last_t - minutes * 60
    past = [v for t, v in s if t <= ref]
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

def classify(title, tier):
    if tier != 0: return title, tier
    parts = title.rsplit(" - ", 1)
    if len(parts) < 2: return title, 3
    clean, src = parts[0], parts[1].lower()
    if re.search(MUTE, src): return clean, 9
    return clean, (2 if re.search(TIER2, src) else 3)

def lean(title):
    t = title.lower()
    b, r = len(re.findall(BULL, t)), len(re.findall(BEAR, t))
    return "BUY" if b > r else "SELL" if r > b else "WAIT"

def verdict(kw, nq):
    if kw == "WAIT": return "WAIT: no clear direction from the headline"
    if nq is None: return f"{kw} lean, UNCONFIRMED (no price data)"
    up, dn = nq > CONFIRM_PCT, nq < -CONFIRM_PCT
    if kw == "BUY":
        if up: return "BUY: price confirms (NQ rising)"
        if dn: return "WAIT: headline bullish but NQ falling (conflict)"
        return "BUY lean, UNCONFIRMED (NQ flat)"
    if dn: return "SELL: price confirms (NQ falling)"
    if up: return "WAIT: headline bearish but NQ rising (conflict)"
    return "SELL lean, UNCONFIRMED (NQ flat)"

def send(hook, title, link, s, tier, ts, red):
    line, nq = px_line()
    v = verdict(lean(title), nq)
    age = max(0, int((time.time() - ts) / 60))
    age_txt = "under 1 min ago" if age < 1 else f"{age} min ago"
    emb = {"title": title[:240], "color": 0x2B2D31,
           "fields": [{"name": "NAS100 VERDICT", "value": v, "inline": False},
                      {"name": "Price, last 15 min", "value": line, "inline": False},
                      {"name": "Source", "value": f"Tier {tier}", "inline": True},
                      {"name": "Published", "value": age_txt, "inline": True}],
           "footer": {"text": f"impact score {s} | not financial advice"}}
    if link.startswith("http"): emb["url"] = link
    body = {"embeds": [emb]}
    if red: body["content"] = "**MARKET ALERT: USD / NAS100**"
    post(hook, body)
    print("ALERT", s, tier, v, "|", title[:100], flush=True)

def ingest(title, link, ts, tier, key, symbols=()):
    now = time.time()
    with lock:
        if key in seen: return
        seen[key] = now
    if STATE["warm"]: return
    if ts is None or now - ts > MAX_AGE_MIN * 60 or ts - now > 600: return
    clean, tier = classify(title, tier)
    if tier == 9: return
    s = score(clean)
    if tier == 1 and s: s += 2
    if tier == 3: s = max(0, s - 2)
    if s and set(symbols) & MEGA: s += 1
    if s <= 0: return
    tkey = re.sub(r"\W+", " ", clean.lower())[:60]
    with lock:
        if tkey in titles: return
        titles[tkey] = now
    thr = RED_THRESHOLD + (2 if market_closed() and tier > 1 else 0)
    if s >= thr and RED: send(RED, clean, link, s, tier, ts, True)
    elif NEWS: send(NEWS, clean, link, s, tier, ts, False)

# ---------- sources ----------
def feed_loop(url, tier, every):
    time.sleep(random.uniform(0, 10))
    while True:
        try:
            for title, link, ts in parse(fetch(url)):
                ingest(title, link, ts, tier, link)
        except Exception as e:
            print("feed failed", url[:60], e)
        time.sleep(every)

def handle_news(m):
    ingest(m.get("headline", ""), m.get("url") or "", to_ts(m.get("created_at")), 2,
           "alp:" + str(m.get("id")), m.get("symbols") or [])

def ws_loop():
    key, sec = os.environ.get("ALPACA_KEY", ""), os.environ.get("ALPACA_SECRET", "")
    if not key or not sec:
        print("ALPACA_KEY / ALPACA_SECRET not set: real-time stream disabled"); return
    fails, warned = 0, False
    def on_open(ws): ws.send(json.dumps({"action": "auth", "key": key, "secret": sec}))
    def on_message(ws, msg):
        try: data = json.loads(msg)
        except Exception: return
        for m in (data if isinstance(data, list) else [data]):
            T = m.get("T")
            if T == "success" and m.get("msg") == "authenticated":
                ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
                print("alpaca news stream: authenticated", flush=True)
            elif T == "n":
                try: handle_news(m)
                except Exception as e: print("news handler", e)
            elif T == "error":
                print("alpaca error", m.get("code"), m.get("msg"), flush=True)
                if not STATE["ws_err"]:
                    STATE["ws_err"] = True
                    post(RED, {"content": f"**STREAM ERROR:** Alpaca news says: {m.get('msg')} (code {m.get('code')}). Check your Alpaca keys."})
    while True:
        t0 = time.time()
        try:
            app = websocket.WebSocketApp(ALPACA_URL, on_open=on_open, on_message=on_message,
                                         on_error=lambda ws, e: print("ws error", e))
            app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print("ws crashed", e)
        fails = fails + 1 if time.time() - t0 < 30 else 0
        if fails >= 5 and not warned:
            warned = True
            post(RED, {"content": "**STREAM DOWN:** real-time news feed keeps disconnecting. Using slower sources only."})
        if fails == 0: warned = False
        time.sleep(min(5 * (fails + 1), 60))

# ---------- calendar ----------
CAL = {"t": 0, "data": []}
def get_calendar():
    if time.time() - CAL["t"] > 3 * 3600:
        try:
            CAL["data"] = json.loads(fetch(CAL_URL)); CAL["t"] = time.time()
        except Exception as e:
            print("calendar failed", e); CAL["t"] = time.time() - 3 * 3600 + 1800
    return CAL["data"]

def plan_for(title):
    t = title.lower()
    if re.search(r"cpi|ppi|pce|non-farm|nonfarm|employment change|adp|jolts|retail sales", t):
        return "Higher than forecast = SELL. Lower than forecast = BUY."
    if re.search(r"jobless|unemployment", t):
        return "Higher than forecast = BUY. Lower = SELL."
    return "WAIT. Direction unknown until the release."

def wanted(e):
    if e.get("country") != "USD": return False
    imp, title = e.get("impact"), e.get("title", "")
    return imp == "High" or (imp == "Medium" and re.search(MED_OK, title.lower()))

def calendar_tick():
    now = datetime.now(timezone.utc)
    for e in get_calendar():
        if not wanted(e): continue
        title, imp = e.get("title", ""), e.get("impact")
        try: dt = datetime.fromisoformat(e["date"])
        except Exception: continue
        mins = (dt - now).total_seconds() / 60
        k, rk = "evt:" + title + e["date"], "rx:" + title + e["date"]
        if 0.5 <= mins <= 10.5 and k not in fired:
            fired.add(k)
            ts = int(dt.timestamp())
            line, _ = px_line()
            fields = [{"name": "NEWS OUT IN", "value": f"~{round(mins)} min (<t:{ts}:R>)", "inline": False}]
            fields += [{"name": x.title(), "value": e[x], "inline": True} for x in ("forecast", "previous") if e.get(x)]
            fields.append({"name": "NAS100 PLAN", "value": plan_for(title), "inline": False})
            fields.append({"name": "Price now, last 15 min", "value": line, "inline": False})
            post(RED, {"content": f"**NEWS IN ~{round(mins)} MIN: {title}**",
                       "embeds": [{"title": f"{title} ({imp} impact)", "color": 0x2B2D31, "fields": fields}]})
        if -15 <= mins <= -5 and rk not in fired:
            fired.add(rk)
            line, nq = px_line(since=int(dt.timestamp()))
            d = "NO PRICE DATA" if nq is None else ("NQ UP since release" if nq > CONFIRM_PCT else
                "NQ DOWN since release" if nq < -CONFIRM_PCT else "NQ FLAT since release")
            post(RED, {"content": f"**REACTION: {title}** (released ~{round(-mins)} min ago)",
                       "embeds": [{"title": d, "description": line, "color": 0x2B2D31,
                                   "footer": {"text": "move since the scheduled release time | not financial advice"}}]})

def extras_tick():
    now = datetime.now(ET_TZ)
    if now.weekday() >= 5: return
    m = (now.replace(hour=9, minute=30, second=0, microsecond=0) - now).total_seconds() / 60
    k = "open:" + now.strftime("%Y-%m-%d")
    if 0.5 <= m <= 10.5 and k not in fired:
        fired.add(k)
        post(RED, {"content": f"**US CASH OPEN IN ~{round(m)} MIN.** NAS100 volatility window"})
    fk = os.environ.get("FINNHUB_KEY", "")
    k = "earn:" + now.strftime("%Y-%m-%d")
    if fk and now.hour >= 8 and k not in fired:
        fired.add(k)
        d = now.strftime("%Y-%m-%d")
        try:
            data = json.loads(fetch(f"https://finnhub.io/api/v1/calendar/earnings?from={d}&to={d}&token={fk}"))
            rows = [x for x in data.get("earningsCalendar", []) if x.get("symbol") in MEGA]
            if rows:
                when = {"bmo": "before open", "amc": "after close", "dmh": "during market"}
                txt = "\n".join(f"**{x['symbol']}**: {when.get(x.get('hour'), 'today')}, EPS est {x.get('epsEstimate')}" for x in rows)
                post(RED, {"content": "**Nasdaq heavyweights reporting today**\n" + txt})
        except Exception as ex:
            print("earnings failed", type(ex).__name__)

def sched_loop():
    while True:
        try: calendar_tick(); extras_tick()
        except Exception as e: print("sched error", e)
        time.sleep(20)

def prune():
    cut = time.time() - 3 * 3600
    with lock:
        for d in (seen, titles):
            for k in [k for k, v in d.items() if v < cut]: del d[k]

def main():
    if not RED: print("DISCORD_RED not set"); return
    for url, tier, every in FEEDS:
        threading.Thread(target=feed_loop, args=(url, tier, every), daemon=True).start()
    threading.Thread(target=ws_loop, daemon=True).start()
    time.sleep(45)
    STATE["warm"] = False
    threading.Thread(target=sched_loop, daemon=True).start()
    post(RED, {"content": "**BOT ONLINE (fast mode).** Watching official feeds, wires and the real-time news stream."})
    print("online", flush=True)
    while True:
        time.sleep(300); prune()

if __name__ == "__main__":
    main()
