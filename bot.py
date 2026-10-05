"""
MARKET NEWS + FOREX ECONOMIC CALENDAR DISCORD BOT
=================================================

ONE FILE.

Install:
    pip install requests beautifulsoup4 feedparser python-dateutil

Set your Discord webhook:

Linux/macOS:
    export DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."

Windows PowerShell:
    $env:DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."

Then:
    python market_bot.py


OPTIONAL SETTINGS:

    export POLL_SECONDS="60"
    export CALENDAR_MINUTES="180"
    export MIN_IMPACT="medium"

Impact:
    low
    medium
    high

The bot stores seen items in:
    market_bot_seen.json

so it does not repeatedly send the same news.
"""

import os
import re
import json
import time
import hashlib
import logging
import html
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import requests
import feedparser
from bs4 import BeautifulSoup


# ============================================================
# CONFIGURATION
# ============================================================

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
CALENDAR_MINUTES = int(os.getenv("CALENDAR_MINUTES", "180"))

MIN_IMPACT = os.getenv("MIN_IMPACT", "medium").lower()

SEEN_FILE = "market_bot_seen.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("market-bot")


# ============================================================
# IMPACT
# ============================================================

IMPACT_LEVELS = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}


def impact_allowed(impact: str) -> bool:
    impact = (impact or "none").lower()
    return IMPACT_LEVELS.get(impact, 0) >= IMPACT_LEVELS.get(
        MIN_IMPACT, 2
    )


# ============================================================
# STORAGE
# ============================================================

def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return set(data)

    except FileNotFoundError:
        pass

    except Exception as e:
        log.warning("Could not load seen database: %s", e)

    return set()


def save_seen(seen):
    """
    Keep database from growing forever.
    """
    try:
        items = list(seen)

        # Keep latest-ish 20,000 hashes.
        if len(items) > 20000:
            items = items[-20000:]

        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f)

    except Exception as e:
        log.warning("Could not save seen database: %s", e)


SEEN = load_seen()


# ============================================================
# UTILITIES
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def make_id(*parts):
    raw = "|".join(clean_text(x) for x in parts)

    return hashlib.sha256(
        raw.encode("utf-8", errors="ignore")
    ).hexdigest()


def get(url, timeout=20):
    response = SESSION.get(
        url,
        timeout=timeout,
        headers=HEADERS,
    )

    response.raise_for_status()

    return response


def now_utc():
    return datetime.now(timezone.utc)


def parse_datetime(value):
    """
    Handles common ISO timestamps.
    """
    if not value:
        return None

    value = str(value).strip()

    try:
        # Python ISO parser handles offsets.
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except Exception:
        return None


# ============================================================
# DISCORD
# ============================================================

def discord_send(content=None, embeds=None):
    if not DISCORD_WEBHOOK:
        log.error(
            "DISCORD_WEBHOOK is not configured."
        )
        return False

    payload = {}

    if content:
        payload["content"] = content[:1900]

    if embeds:
        payload["embeds"] = embeds[:10]

    try:
        r = SESSION.post(
            DISCORD_WEBHOOK,
            json=payload,
            timeout=20,
        )

        if r.status_code == 429:
            try:
                retry = r.json().get(
                    "retry_after",
                    5,
                )
            except Exception:
                retry = 5

            log.warning(
                "Discord rate limited. Sleeping %.1fs",
                float(retry),
            )

            time.sleep(float(retry))

            return discord_send(
                content,
                embeds,
            )

        if r.status_code >= 300:
            log.error(
                "Discord error %s: %s",
                r.status_code,
                r.text[:500],
            )
            return False

        return True

    except Exception as e:
        log.error(
            "Discord request failed: %s",
            e,
        )

        return False


def send_once(item_id, content=None, embeds=None):
    global SEEN

    if item_id in SEEN:
        return False

    success = discord_send(
        content=content,
        embeds=embeds,
    )

    if success:
        SEEN.add(item_id)

        # Save periodically.
        save_seen(SEEN)

    return success


# ============================================================
# FOREX FACTORY
# ============================================================

FOREX_FACTORY_JSON = (
    "https://nfs.faireconomy.media/"
    "ff_calendar_thisweek.json"
)


def get_forex_factory():
    """
    Forex Factory weekly JSON calendar.

    We only request this once per polling cycle and the bot
    itself should not be configured below ~60 seconds.
    """

    events = []

    try:
        response = get(
            FOREX_FACTORY_JSON,
            timeout=20,
        )

        data = response.json()

        if not isinstance(data, list):
            log.warning(
                "Forex Factory returned unexpected data."
            )
            return events

        current = now_utc()
        future_limit = current + timedelta(
            minutes=CALENDAR_MINUTES
        )

        for event in data:

            title = clean_text(
                event.get("title")
                or event.get("event")
                or ""
            )

            currency = clean_text(
                event.get("country")
                or event.get("currency")
                or ""
            ).upper()

            impact = clean_text(
                event.get("impact")
                or ""
            ).lower()

            date_value = (
                event.get("date")
                or event.get("datetime")
                or ""
            )

            dt = parse_datetime(date_value)

            if not title:
                continue

            if not impact_allowed(impact):
                continue

            # If timestamp exists, only alert on upcoming events.
            if dt:

                if dt < current:
                    continue

                if dt > future_limit:
                    continue

                time_text = dt.astimezone(
                    timezone.utc
                ).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )

            else:
                time_text = str(date_value)

            forecast = clean_text(
                event.get("forecast")
                or ""
            )

            previous = clean_text(
                event.get("previous")
                or ""
            )

            actual = clean_text(
                event.get("actual")
                or ""
            )

            events.append({
                "source": "Forex Factory",
                "title": title,
                "currency": currency,
                "impact": impact,
                "time": time_text,
                "forecast": forecast,
                "previous": previous,
                "actual": actual,
                "url": (
                    "https://www.forexfactory.com/"
                    "calendar"
                ),
            })

    except Exception as e:
        log.error(
            "Forex Factory failed: %s",
            e,
        )

    return events


def process_forex_factory():
    events = get_forex_factory()

    for event in events:

        item_id = make_id(
            "forexfactory",
            event["title"],
            event["currency"],
            event["time"],
        )

        impact = event["impact"].upper()

        message = (
            f"**{impact} ECONOMIC EVENT**\n\n"
            f"**{event['currency']} | "
            f"{event['title']}**\n"
            f"Time: `{event['time']}`\n"
        )

        if event["forecast"]:
            message += (
                f"Forecast: `{event['forecast']}`\n"
            )

        if event["previous"]:
            message += (
                f"Previous: `{event['previous']}`\n"
            )

        if event["actual"]:
            message += (
                f"Actual: `{event['actual']}`\n"
            )

        message += (
            f"\nSource: {event['url']}"
        )

        send_once(
            item_id,
            content=message,
        )


# ============================================================
# MYFXBOOK
# ============================================================

MYFXBOOK_CALENDAR = (
    "https://www.myfxbook.com/"
    "forex-economic-calendar/USD,EUR,GBP,JPY,"
    "AUD,CAD,CHF,NZD"
)


def get_myfxbook():
    """
    Myfxbook renders its economic calendar server-side.

    We parse the visible calendar table rather than relying
    on undocumented APIs.
    """

    events = []

    try:
        response = get(
            MYFXBOOK_CALENDAR,
            timeout=25,
        )

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        # Find rows containing calendar information.
        for row in soup.find_all("tr"):

            cells = row.find_all(
                ["td", "th"]
            )

            if len(cells) < 3:
                continue

            texts = [
                clean_text(c.get_text(" ", strip=True))
                for c in cells
            ]

            row_text = " | ".join(texts)

            # Need a recognizable currency.
            currencies = [
                "USD",
                "EUR",
                "GBP",
                "JPY",
                "AUD",
                "CAD",
                "CHF",
                "NZD",
            ]

            currency = None

            for c in currencies:
                if re.search(
                    rf"\b{c}\b",
                    row_text,
                ):
                    currency = c
                    break

            if not currency:
                continue

            # Determine impact from row text.
            lower = row_text.lower()

            if "high" in lower:
                impact = "high"

            elif "medium" in lower:
                impact = "medium"

            elif "low" in lower:
                impact = "low"

            else:
                impact = "none"

            if not impact_allowed(impact):
                continue

            # Search for a useful event title.
            title = ""

            for text in texts:

                if (
                    len(text) >= 5
                    and text.lower()
                    not in {
                        "high",
                        "medium",
                        "low",
                        "none",
                        currency.lower(),
                    }
                ):
                    # Avoid obvious date/time fields.
                    if not re.fullmatch(
                        r"[\d:/\-\s]+",
                        text,
                    ):
                        title = text
                        break

            if not title:
                continue

            # Guess time/date from row.
            time_text = ""

            for text in texts:

                if re.search(
                    r"\d{1,2}:\d{2}",
                    text,
                ):
                    time_text = text
                    break

            # Try extracting previous/consensus/actual.
            numbers = []

            for text in texts:
                if (
                    text
                    and text != title
                    and text != currency
                    and text != impact
                ):
                    numbers.append(text)

            events.append({
                "source": "Myfxbook",
                "title": title,
                "currency": currency,
                "impact": impact,
                "time": time_text,
                "raw": row_text,
                "url": (
                    "https://www.myfxbook.com/"
                    "forex-economic-calendar"
                ),
            })

    except Exception as e:
        log.error(
            "Myfxbook failed: %s",
            e,
        )

    return events


def process_myfxbook():
    events = get_myfxbook()

    # Prevent duplicate rows from parser quirks.
    local_seen = set()

    for event in events:

        key = make_id(
            event["currency"],
            event["title"],
            event["time"],
        )

        if key in local_seen:
            continue

        local_seen.add(key)

        item_id = make_id(
            "myfxbook",
            event["currency"],
            event["title"],
            event["time"],
        )

        message = (
            f"**{event['impact'].upper()} "
            f"ECONOMIC EVENT**\n\n"
            f"**{event['currency']} | "
            f"{event['title']}**\n"
        )

        if event["time"]:
            message += (
                f"Time: `{event['time']}`\n"
            )

        message += (
            f"\nSource: {event['url']}"
        )

        send_once(
            item_id,
            content=message,
        )


# ============================================================
# NEWS SOURCES
# ============================================================

DIRECT_RSS = {

    "CNBC": (
        "https://www.cnbc.com/id/"
        "100003114/device/rss/rss.html"
    ),

    "BBC Business": (
        "https://feeds.bbci.co.uk/news/"
        "business/rss.xml"
    ),

    "MarketWatch": (
        "https://feeds.marketwatch.com/"
        "marketwatch/topstories/"
    ),

    "Yahoo Finance": (
        "https://finance.yahoo.com/"
        "news/rssindex"
    ),
}


# Google News allows us to monitor publishers that don't
# reliably expose a public RSS feed.
GOOGLE_NEWS_SOURCES = {

    "Reuters": "reuters.com",

    "Bloomberg": "bloomberg.com",

    "Financial Times": "ft.com",

    "Wall Street Journal": "wsj.com",

    "Associated Press": "apnews.com",

    "Investing.com": "investing.com",

    "CNBC": "cnbc.com",

    "MarketWatch": "marketwatch.com",

    "Yahoo Finance": "finance.yahoo.com",

    "BBC Business": "bbc.com",
}


def google_news_url(domain):
    query = (
        f"site:{domain} "
        f"(forex OR stocks OR bonds OR "
        f"Federal Reserve OR ECB OR economy OR "
        f"markets OR oil OR gold OR inflation)"
    )

    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&"
        "hl=en-US&gl=US&ceid=US:en"
    )


# ============================================================
# RSS READER
# ============================================================

def read_feed(source, url):
    items = []

    try:

        feed = feedparser.parse(
            url,
            request_headers=HEADERS,
        )

        if getattr(
            feed,
            "bozo",
            False,
        ):
            log.warning(
                "%s feed returned malformed RSS",
                source,
            )

        for entry in feed.entries[:50]:

            title = clean_text(
                entry.get("title", "")
            )

            link = (
                entry.get("link")
                or ""
            )

            summary = clean_text(
                entry.get(
                    "summary",
                    "",
                )
            )

            published = clean_text(
                entry.get(
                    "published",
                    "",
                )
            )

            if not title:
                continue

            items.append({
                "source": source,
                "title": title,
                "link": link,
                "summary": summary,
                "published": published,
            })

    except Exception as e:
        log.error(
            "%s feed failed: %s",
            source,
            e,
        )

    return items


def process_news_item(item):
    source = item["source"]
    title = item["title"]
    link = item["link"]

    item_id = make_id(
        "news",
        source,
        title,
        link,
    )

    if item_id in SEEN:
        return

    summary = item.get(
        "summary",
        "",
    )

    # Don't flood Discord with huge RSS descriptions.
    summary = clean_text(summary)

    if len(summary) > 700:
        summary = summary[:700] + "..."

    embed = {
        "title": title[:256],
        "url": link,
        "description": (
            summary
            if summary
            else "New market news story."
        ),
        "footer": {
            "text": f"Market News • {source}"
        },
        "timestamp": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
    }

    if send_once(
        item_id,
        embeds=[embed],
    ):
        log.info(
            "NEWS | %s | %s",
            source,
            title,
        )


def process_all_news():
    # Direct feeds.
    for source, url in DIRECT_RSS.items():

        items = read_feed(
            source,
            url,
        )

        for item in items:
            process_news_item(item)

    # Google News publisher-specific feeds.
    for source, domain in GOOGLE_NEWS_SOURCES.items():

        url = google_news_url(domain)

        items = read_feed(
            source,
            url,
        )

        for item in items:
            process_news_item(item)


# ============================================================
# MARKET KEYWORD ALERTS
# ============================================================

# These aren't used to decide whether an article is collected.
# They are used to flag especially important stories.

HIGH_PRIORITY_TERMS = [
    "federal reserve",
    "fed",
    "fomc",
    "interest rate",
    "rate decision",
    "ecb",
    "bank of england",
    "boe",
    "bank of japan",
    "boj",
    "inflation",
    "cpi",
    "ppi",
    "nonfarm payroll",
    "non-farm payroll",
    "nfp",
    "unemployment",
    "gdp",
    "recession",
    "tariff",
    "sanctions",
    "war",
    "ceasefire",
    "oil",
    "crude",
    "gold",
    "bitcoin",
    "treasury",
    "bond yield",
    "default",
    "bank failure",
    "emergency",
]


def is_high_priority(title):
    lower = title.lower()

    return any(
        term in lower
        for term in HIGH_PRIORITY_TERMS
    )


# ============================================================
# OPTIONAL PRIORITY CHANNEL
# ============================================================

PRIORITY_WEBHOOK = os.getenv(
    "PRIORITY_WEBHOOK",
    "",
).strip()


def process_priority_news():
    """
    Re-read Google News and send especially important
    stories to a second Discord channel if configured.
    """

    if not PRIORITY_WEBHOOK:
        return

    original = DISCORD_WEBHOOK

    try:

        for source, domain in GOOGLE_NEWS_SOURCES.items():

            url = google_news_url(domain)

            items = read_feed(
                source,
                url,
            )

            for item in items:

                if not is_high_priority(
                    item["title"]
                ):
                    continue

                item_id = make_id(
                    "priority",
                    source,
                    item["title"],
                    item["link"],
                )

                if item_id in SEEN:
                    continue

                embed = {
                    "title": (
                        "🚨 "
                        + item["title"][:250]
                    ),
                    "url": item["link"],
                    "description": (
                        f"Source: **{source}**"
                    ),
                    "timestamp": (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    ),
                }

                # Temporarily switch webhook.
                globals()["DISCORD_WEBHOOK"] = (
                    PRIORITY_WEBHOOK
                )

                success = discord_send(
                    embeds=[embed]
                )

                globals()["DISCORD_WEBHOOK"] = (
                    original
                )

                if success:
                    SEEN.add(item_id)

    finally:
        globals()["DISCORD_WEBHOOK"] = original


# ============================================================
# STARTUP
# ============================================================

def startup_message():

    message = (
        "🟢 **MARKET BOT ONLINE**\n\n"
        f"Poll: `{POLL_SECONDS}s`\n"
        f"Calendar window: `{CALENDAR_MINUTES} min`\n"
        f"Minimum impact: `{MIN_IMPACT}`\n\n"
        "Tracking:\n"
        "• Forex Factory\n"
        "• Myfxbook\n"
        "• Reuters\n"
        "• Bloomberg\n"
        "• CNBC\n"
        "• Financial Times\n"
        "• Wall Street Journal\n"
        "• MarketWatch\n"
        "• Yahoo Finance\n"
        "• BBC Business\n"
        "• Associated Press\n"
        "• Investing.com\n"
    )

    discord_send(
        content=message
    )


# ============================================================
# MAIN LOOP
# ============================================================

def run():

    if not DISCORD_WEBHOOK:

        raise RuntimeError(
            "\n\n"
            "DISCORD_WEBHOOK is missing.\n\n"
            "Set it before starting the bot:\n\n"
            "Linux/macOS:\n"
            "export DISCORD_WEBHOOK="
            '"https://discord.com/api/webhooks/..."\n\n'
            "Windows PowerShell:\n"
            "$env:DISCORD_WEBHOOK="
            '"https://discord.com/api/webhooks/..."\n"
        )

    log.info(
        "Starting Market Bot..."
    )

    startup_message()

    # First pass immediately.
    try:
        process_forex_factory()
    except Exception as e:
        log.exception(
            "Forex Factory crashed: %s",
            e,
        )

    try:
        process_myfxbook()
    except Exception as e:
        log.exception(
            "Myfxbook crashed: %s",
            e,
        )

    try:
        process_all_news()
    except Exception as e:
        log.exception(
            "News processing crashed: %s",
            e,
        )

    # Continuous operation.
    while True:

        started = time.time()

        try:
            process_forex_factory()
        except Exception as e:
            log.exception(
                "Forex Factory error: %s",
                e,
            )

        try:
            process_myfxbook()
        except Exception as e:
            log.exception(
                "Myfxbook error: %s",
                e,
            )

        try:
            process_all_news()
        except Exception as e:
            log.exception(
                "News error: %s",
                e,
            )

        elapsed = time.time() - started

        sleep_for = max(
            5,
            POLL_SECONDS - elapsed,
        )

        log.info(
            "Cycle complete. "
            "Next cycle in %.1fs",
            sleep_for,
        )

        time.sleep(sleep_for)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:
        run()

    except KeyboardInterrupt:

        log.info(
            "Bot stopped by user."
        )

        save_seen(SEEN)

    except Exception as e:

        log.exception(
            "FATAL ERROR: %s",
            e,
        )

        save_seen(SEEN)
