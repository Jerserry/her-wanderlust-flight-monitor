# Her Wanderlust Path · Flight Monitor ✈️

A flight-price monitoring dashboard for routes departing from Kuala Lumpur (KUL). It uses Google Flights results through SerpApi, stores price observations in GitHub, and can send Telegram alerts.

## Dashboard

GitHub Pages serves the dashboard from `docs/`. It includes:
- Overview cards and flexible-date monitoring summary (no price-trend chart)
- Route watchlist with departure / return dates and trip duration
- Flight deal radar
- Price history table
- Airline-by-airline round-trip fare comparison
- Telegram alerts and system status

## Airline directory

The Airlines view loads `docs/data/airlines.json`, a starter directory of 45 airlines that have appeared in saved Google Flights via SerpApi fare results. It supports search by airline name / IATA code / country and filters by country and airline type. The directory is based on observed search results, not a complete list of all airlines operating from KUL; airline type labels are preliminary reference classifications. Live fare counts and lowest saved round-trip fares are calculated from `docs/data/prices.json`.

## Global commercial airport database

The dashboard includes a searchable global airport directory at `docs/airports.html`. It supports search by airport IATA / ICAO, airport name, served city, city code, country and region, plus CSV export. A scheduled GitHub Actions workflow refreshes `docs/data/airports.json` daily from AirportMiles (built from OurAirports and Wikidata), CC0. The page filters out records marked as having no scheduled flights. City codes are shown only where the source provides one. Time zones are not included in this dataset.

Source: https://airportmiles.com/data

## Configure monitored routes

The current watchlist contains 34 destinations, all departing from KUL: Japan (NRT, KIX), South Korea (ICN), Hong Kong (HKG), Taiwan (TPE), China (PVG, CAN), Europe (LHR, CDG, AMS), the United States (JFK, LAX), Canada (YYZ, YVR, YUL), New Zealand (AKL, CHC, ZQN), Australia (SYD, MEL, BNE, PER), Africa (CPT, JNB, NBO, CAI), and Mexico (MEX, CUN).

Edit `config/routes.json`:

```json
{
  "origin": "KUL",
  "currency": "MYR",
  "travel_class": "ECONOMY",
  "adults": 1,
  "price_drop_alert_percent": 5,
  "routes": [
    { "destination": "NRT" },
    { "destination": "KIX" }
  ]
}
```

- The origin remains KUL and currency is MYR. `budget_myr` is optional; omit it to track and compare fares without a budget threshold.
- Flexible date settings live in config/flexible_dates.json: rolling 180-day departure window, 5–14 day trips, representative durations of 5 / 7 / 10 / 14 days, and one date pair searched per destination per run.
- Candidate departure dates are spaced 7 days apart. Each scheduled run rotates to the next date combination per destination; the return date may extend beyond the 180-day departure window by the selected trip duration.
- No airline allowlist is used. The monitor collects every priced airline / airline combination returned in Google Flights best_flights and other_flights for each selected date pair. This does not guarantee that Google Flights returns every airline operating that route.
- price_drop_alert_percent controls the Telegram alert threshold for a fall versus the previous successful scan (default 5%).
## Schedule and API usage

SerpApi runs automatically four times per week at 18:00 Malaysia time:
- Tuesday
- Wednesday
- Friday
- Saturday

GitHub Actions schedules are expressed in UTC. The corresponding cron is `0 10 * * 2,3,5,6`.

Each of the 34 destinations uses one round-trip date-pair search per run. This is approximately 34 searches per run, 136 searches per week, or about 590 searches per average 30-day month, excluding retries or any separately initiated runs. The rolling flexible-date cursor rotates the selected date pairs over time. The current configuration uses one representative round-trip date pair per destination per run.

Telegram alerts remain enabled when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured. Alerts are sent when a fare first reaches its configured budget, drops by at least 5% versus the previous successful scan, or sets a new historical low for that exact date pair and airline. Telegram delivery failures do not discard collected prices.

The scheduled SerpApi workflow is schedule-only; it does not expose a manual Run workflow trigger. GitHub Actions scheduling is best-effort, so a run may start later than the exact minute. Monitor SerpApi account usage and confirm the plan's monthly quota before relying on the schedule.

## GitHub Actions secrets

In Repository → Settings → Secrets and variables → Actions, configure:
1. `SERPAPI_API_KEY` — required for fare searches.
2. `TELEGRAM_BOT_TOKEN` — optional Telegram bot token.
3. `TELEGRAM_CHAT_ID` — optional Telegram chat ID.

Never put API keys in frontend files or commit them to the repository.

## Alerts and data reliability

- Telegram alerts are sent when a round-trip fare drops by at least the configured percentage versus the previous successful scan. Budget alerts are inactive because the monitored routes have no budget thresholds.
- A Telegram delivery error is logged but does not discard collected flight prices.
- If a route or trip-type search fails, its last successful result is retained and marked stale. A stale result must not be treated as a current fare.
- The data file records the last run time, monitor status and errors.
- The browser dashboard is read-only. Edit the route configuration in GitHub; do not expose a write token or SerpApi key in the public website.
- Prices are indicative search results, not guaranteed inventory or a booking. Verify final price, taxes, baggage, fare rules and availability at checkout.
- Google Flights results can differ from the consumer website. This project uses SerpApi; monitor your account usage and billing limits.
- Up to 6,000 price observations are retained.

## Deploy

Repository Settings → Pages → Deploy from a branch → `main` → `/docs`.
