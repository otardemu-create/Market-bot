# USD + NAS100 Market Alert Bot

Always-on Python service for Discord alerts around high-impact USD macro events and market-moving NAS100 headlines.

## Alerts

- 10 minutes before a high-impact USD event.
- When an actual value appears for that event.
- Market-moving headlines involving the Fed, US macro data, yields, tariffs/politics, Nasdaq/NAS100, AI/chips, and NAS100 heavyweight tickers.

## Sources

- Finnhub economic calendar: primary calendar.
- Forex Factory weekly JSON: cross-check.
- Finnhub general/company news: headline source.

The bot intentionally does not make MyFXBook scraping a production dependency. Its HTML can change or block automated clients. A licensed/API source can be added as another cross-check provider.

Finnhub requires an API key and its economic-data product is paid. Check the current plan before production use.

## Setup

1. Create a Discord bot with BotFather.
2. Add the bot to the target chat/channel and get the chat ID.
3. Create a Finnhub API key.
4. Copy .env.example to .env and set the real values.
5. Run with Docker Compose:

    docker compose up -d --build

Or directly:

    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    python app.py

## Deployment

Run this on an always-on VPS or cloud VM. Do not use GitHub Actions for the polling loop.

The state file is persisted under data/state.json. Alert fingerprints prevent duplicate calendar and news messages across restarts.

## Discord

The service uses Discord's Bot API sendMessage endpoint.

## Latency

This is an alerting service, not an exchange-grade feed or trading execution system. Free feeds can lag. For the fastest release detection, use a licensed low-latency economic/news feed and keep this service as the notification layer.
