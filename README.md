# NAS100.pro Market Alert Bot

Always-on Python service for Discord alerts around high-impact USD macro events, market-moving NAS100 news, and TradingView NAS100.pro trend updates.

## What it sends

### NAS100.pro trend
TradingView is the price/trend source. The bot does not substitute QQQ or another proxy for NAS100.pro.

Discord trend messages include:
- Overall direction: UP, DOWN, MIXED, or STALE
- 1H trend
- 2H trend
- 3H trend
- 4H trend
- Daily trend
- TradingView data age

The TradingView alert should be created on the exact NAS100.pro chart and should send OHLC JSON on each 1-minute bar close. The bot aggregates those bars into the higher-timeframe trend view.

TradingView supports alert placeholders such as `{{open}}`, `{{high}}`, `{{low}}`, `{{close}}`, and `{{time}}`.

Example alert message:

    {"symbol":"{{ticker}}","timestamp":"{{time}}","open":"{{open}}","high":"{{high}}","low":"{{low}}","close":"{{close}}"}

Use the bot's Render HTTPS webhook endpoint with its private TradingView webhook secret. Never commit the secret to Git.

### Economic calendar
- 10 minutes before a high-impact USD event
- At release when an actual value is available
- Finnhub is the primary calendar source
- Forex Factory weekly JSON is used as a cross-check
- USD + high/red impact events only

### Market-moving news
Finnhub general and company news are filtered for factors likely to move NAS100.pro, including:
- Fed/FOMC/Powell
- rates and Treasury yields
- CPI/PPI/PCE
- NFP, unemployment and jobless claims
- GDP, retail sales, ISM/PMI
- tariffs and sanctions
- Nasdaq/NAS100
- semiconductors, chips, AI and export controls
- major NAS100 constituents

## Deployment verification

Latest source changes are intended to trigger Render auto-deploy from `main`.

## Runtime

- Python 3.12
- Render web service
- Continuous polling
- HTTP webhook listener on Render's `PORT`
- `GET /health` returns 200 when the process is serving
- State stored in `data/state.json` for deduplication
- Discord webhook is used for delivery

## Required environment variables

- `FINNHUB_API_KEY`
- `DISCORD_WEBHOOK_URL`
- `TRADINGVIEW_WEBHOOK_SECRET`

Optional:
- `POLL_SECONDS` default 30
- `PRE_ALERT_MINUTES` default 10
- `CALENDAR_LOOKAHEAD_HOURS` default 48
- `NEWS_LOOKBACK_MINUTES` default 20
- `TRADINGVIEW_STALE_SECONDS` default 180
- `TREND_LOOKBACK_BARS` default 20
- `STATE_FILE` default `data/state.json`
- `LOG_LEVEL` default `INFO`

## Deployment

The production target is an always-on Render web service. GitHub Actions is intentionally not used for the polling loop.

For Render, use:
- Build: `pip install -r requirements.txt`
- Start: `python app.py`
- Bind: `0.0.0.0:$PORT`
- Health check: `/health`

## Security

Secrets belong in Render environment variables, not GitHub files.

Because API credentials and webhook credentials were exposed during setup, rotate the Finnhub API key and Discord webhook after the deployment has been verified.

## Limitations

This is an alerting and trend-monitoring service, not an exchange-grade market-data feed or trade execution engine. TradingView webhook delivery is the real-time NAS100.pro price input. Economic/news feeds can still have source-side latency.
