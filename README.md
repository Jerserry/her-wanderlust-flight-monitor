# Her Wanderlust Path · Flight Monitor ✈️

A flight-price monitoring dashboard for routes departing from Kuala Lumpur (KUL). It uses Google Flights results through SerpApi, stores price observations in GitHub, and can send Telegram alerts.

## Dashboard

GitHub Pages serves the dashboard from `docs/`. It includes:
- Overview cards and price trend chart
- Route watchlist and target-budget status
- Flight deal radar
- Price history table
- Airline-by-airline comparison
- Telegram alerts and system status

## Configure monitored routes

Edit `config/routes.json`:

```json
{
  "origin": "KUL",
  "currency": "MYR",
  "travel_class": "ECONOMY",
  "adults": 1,
  "departure_date": "2027-05-15",
  "return_date": "2027-05-22",
  "price_drop_alert_percent": 5,
  "routes": [
    { "destination": "BKK", "budget_myr": 600 },
    { "destination": "NRT", "budget_myr": 1800 }
  ]
}
```

- The origin remains KUL.
- Every configured destination is searched in both one-way and round-trip modes.
- No airline allowlist is used. The monitor collects every priced airline / airline combination returned in the Google Flights `best_flights` and `other_flights` groups for the selected route and dates. This does not guarantee that Google Flights returns every airline operating that route.
- `price_drop_alert_percent` controls the Telegram alert threshold for a fall versus the previous successful scan (default 5%).
- All routes currently share the same dates, passenger count and cabin class.

## Schedule and API usage

The GitHub Actions workflow runs four times daily at Malaysia time:
- 00:00
- 06:00
- 12:00
- 18:00

GitHub Actions schedules are expressed in UTC. The corresponding cron is `0 22,4,10,16 * * *`.

Each route uses two SerpApi searches per run (one-way and round-trip). With two routes, this is approximately 16 searches per day or 480 searches per 30-day month, excluding manual runs and retries. Confirm that your SerpApi plan covers this usage before leaving the schedule enabled.

You can also start a manual run from Actions → Daily Flight Monitor → Run workflow. Scheduled runs can start a little later than the exact minute because GitHub Actions scheduling is best-effort.

## GitHub Actions secrets

In Repository → Settings → Secrets and variables → Actions, configure:
1. `SERPAPI_API_KEY` — required for fare searches.
2. `TELEGRAM_BOT_TOKEN` — optional Telegram bot token.
3. `TELEGRAM_CHAT_ID` — optional Telegram chat ID.

Never put API keys in frontend files or commit them to the repository.

## Alerts and data reliability

- Telegram alerts are sent when a round-trip fare first reaches its budget, sets a new historical low while within budget, or drops by at least the configured percentage versus the previous successful scan.
- A Telegram delivery error is logged but does not discard collected flight prices.
- If a route or trip-type search fails, its last successful result is retained and marked stale. A stale result must not be treated as a current fare.
- The data file records the last run time, monitor status and errors.
- The browser dashboard is read-only. Edit the route configuration in GitHub; do not expose a write token or SerpApi key in the public website.
- Prices are indicative search results, not guaranteed inventory or a booking. Verify final price, taxes, baggage, fare rules and availability at checkout.
- Google Flights results can differ from the consumer website. This project uses SerpApi; monitor your account usage and billing limits.
- Up to 6,000 price observations are retained.

## Deploy

Repository Settings → Pages → Deploy from a branch → `main` → `/docs`.
