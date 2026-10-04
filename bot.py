import os, json, re, csv, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
RED_THRESHOLD = 6
MAX_SENDS = 10
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

# (url, tier)  tier 1 = official, 2 = major outlet, 0 = Google News (tier set by source)
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

def parse(xml):
    root = ET.fromstring(xml)
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] in ("item", "entry"):
            title = link = ""
            for c in el:
                t = c.tag.split("}")[-1]
                if t == "title": title = (c.text or "").strip()
                elif t == "link": link = (c.text or c.attrib.get("href") or "").strip()
            if title and link: out.append((title, link))
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
    if len(parts) < 2: return title, 3
    clean, src = parts[0], parts[1].lower()
    if re.search(MUTE, src): return clean, 9
    return clean, (2 if re.search(TIER2, src) else 3)

def lean(title):
    t = title.lower()
    b, r = len(re.findall(BULL, t)), len(re.findall(BEAR, t))
    if b > r: return "BUY"
    if r > b: return "SELL"
    return "WAIT"

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

def log_row(title, tier, s, v, nq):
    try:
        with open(LOG_FILE, "a", newline="") as f:
            csv.writer(f).writerow([datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), tier, s, v,
                                    "" if nq is None else f"{nq:+.2f}", title[:150]])
    except Exception as e: print("log failed", e)

def send(hook, title, link, s, tier, red):
    line, nq = px_line()
    v = verdict(lean(title), nq)
    emb = {"title": title[:240], "url": link, "color": 0x2B2D31,
           "fields": [{"name": "NAS100 VERDICT", "value": v, "inline": False},
                      {"name": "Price, last 15 min", "value": line, "inline": False},
                      {"name": "Source", "value": f"Tier {tier}", "inline": True},
                      {"name": "Timing", "value": "OUT NOW (caught within ~5 min)", "inline": True}],
           "footer": {"text": f"impact score {s} | not financial advice"}}
    body = {"embeds": [emb]}
    if red: body["content"] = "**MARKET ALERT: USD / NAS100**"
    post(hook, body)
    log_row(title, tier, s, v, nq)

# ---------- calendar ----------
def get_calendar():
    cache = {"t": 0, "data": []}
    try: cache = json.load(open("calendar.json"))
    except Exception: pass
    if time.time() - cache.get("t", 0) > 3 * 3600:
        try:
            cache = {"t": time.time(), "data": json.loads(fetch(CAL_URL))}
        except Exception as e:
            print("calendar fetch failed, using cache", e)
            cache["t"] = time.time() - 2.5 * 3600
        json.dump(cache, open("calendar.json", "w"))
    return cache.get("data", [])

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

def calendar_alerts(seen, hook):
    if not hook: return
    now = datetime.now(timezone.utc)
    for e in get_calendar():
        if not wanted(e): continue
        imp, title = e.get("impact"), e.get("title", "")
        try: dt = datetime.fromisoformat(e["date"])
        except Exception: continue
        mins = (dt - now).total_seconds() / 60
        key = "evt:" + title + e["date"]
        if WARN_MIN <= mins <= WARN_MAX and key not in seen:
            seen[key] = 1
            ts = int(dt.timestamp())
            line, _ = px_line()
            fields = [{"name": "NEWS OUT IN", "value": f"~{round(mins)} min (<t:{ts}:R>)", "inline": False}]
            fields += [{"name": k.title(), "value": e[k], "inline": True}
                       for k in ("forecast", "previous") if e.get(k)]
            fields.append({"name": "NAS100 PLAN", "value": plan_for(title), "inline": False})
            fields.append({"name": "Price now, last 15 min", "value": line, "inline": False})
            emb = {"title": title + f" ({imp} impact)", "color": 0x2B2D31, "fields": fields}
            post(hook, {"content": f"**NEWS IN ~{round(mins)} MIN: {title}**", "embeds": [emb]})

def reaction_alerts(seen, hook):
    if not hook: return
    now = datetime.now(timezone.utc)
    for e in get_calendar():
        if not wanted(e): continue
        title = e.get("title", "")
        try: dt = datetime.fromisoformat(e["date"])
        except Exception: continue
        mins = (dt - now).total_seconds() / 60
        k, rk = "evt:" + title + e["date"], "rx:" + title + e["date"]
        if -20 <= mins <= -4 and k in seen and rk not in seen:
            seen[rk] = 1
            line, nq = px_line(since=int(dt.timestamp()))
            if nq is None: d = "NO PRICE DATA"
            elif nq > CONFIRM_PCT: d = "NQ UP since release"
            elif nq < -CONFIRM_PCT: d = "NQ DOWN since release"
            else: d = "NQ FLAT since release"
            emb = {"title": d, "description": line, "color": 0x2B2D31,
                   "footer": {"text": "move since the scheduled release time | not financial advice"}}
            post(hook, {"content": f"**REACTION: {title}** (released ~{round(-mins)} min ago)", "embeds": [emb]})

def extras(seen, hook):
    if not hook: return
    now = datetime.now(ET_TZ)
    if now.weekday() >= 5: return
    openT = now.replace(hour=9, minute=30, second=0, microsecond=0)
    m = (openT - now).total_seconds() / 60
    key = "open:" + now.strftime("%Y-%m-%d")
    if WARN_MIN <= m <= WARN_MAX and key not in seen:
        seen[key] = 1
        post(hook, {"content": f"**US CASH OPEN IN ~{round(m)} MIN.** NAS100 volatility window"})
    fk = os.environ.get("FINNHUB_KEY", "")
    key = "earn:" + now.strftime("%Y-%m-%d")
    if fk and now.hour >= 8 and key not in seen:
        seen[key] = 1
        d = now.strftime("%Y-%m-%d")
        try:
            data = json.loads(fetch(f"https://finnhub.io/api/v1/calendar/earnings?from={d}&to={d}&token={fk}"))
            rows = [x for x in data.get("earningsCalendar", []) if x.get("symbol") in MEGA]
            if rows:
                when = {"bmo": "before open", "amc": "after close", "dmh": "during market"}
                txt = "\n".join(f"**{x['symbol']}**: {when.get(x.get('hour'), 'today')}, EPS est {x.get('epsEstimate')}" for x in rows)
                post(hook, {"content": "**Nasdaq heavyweights reporting today**\n" + txt})
        except Exception as ex: print("earnings failed", ex)

def trim_log():
    try:
        lines = open(LOG_FILE).read().splitlines()
        if len(lines) > 1000: open(LOG_FILE, "w").write("\n".join(lines[-1000:]) + "\n")
    except Exception: pass

def main():
    red_hook = os.environ.get("DISCORD_RED", "")
    news_hook = os.environ.get("DISCORD_NEWS", "")
    first_run = not os.path.exists("seen.json")
    seen = {} if first_run else dict.fromkeys(json.load(open("seen.json")), 1)

    calendar_alerts(seen, red_hook)
    reaction_alerts(seen, red_hook)
    extras(seen, red_hook)

    closed = market_closed()
    new = []
    for url, ft in FEEDS:
        try: items = parse(fetch(url))
        except Exception as e:
            print("feed failed", url[:60], e); continue
        for title, link in items:
            if link in seen: continue
            seen[link] = 1
            clean, tier = classify(title, ft)
            if tier == 9: continue
            s = score(clean)
            if tier == 1 and s: s += 2
            if tier == 3: s = max(0, s - 2)
            if s > 0: new.append((s, clean, link, tier))
    if not first_run:
        titles_sent, sent = set(), 0
        new.sort(reverse=True)
        for s, title, link, tier in new:
            key = title.lower()[:60]
            if key in titles_sent or sent >= MAX_SENDS: continue
            titles_sent.add(key)
            thr = RED_THRESHOLD + (2 if closed and tier > 1 else 0)
            if s >= thr and red_hook: send(red_hook, title, link, s, tier, True); sent += 1
            elif news_hook: send(news_hook, title, link, s, tier, False); sent += 1
    json.dump(list(seen)[-4000:], open("seen.json", "w"))
    trim_log()

main()
