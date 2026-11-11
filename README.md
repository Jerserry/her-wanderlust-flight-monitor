# Her Wanderlust Path · Flight Monitor ✈️

A pastel flight-price monitoring dashboard for routes departing from Kuala Lumpur (KUL). It uses Google Flights results through SerpApi, stores price observations in GitHub, and can send Telegram alerts.

## Dashboard

GitHub Pages serves the dashboard from `docs/`. It includes:
- Overview cards and price trend chart (7 / 30 / 90 days / all)
- Route watchlist and target-budget status
- Flight deal radar
- Price history table
- Telegram alert guidance and system settings
- Responsive mobile layout

## Configure the monitor

Edit `config/routes.json` and commit the changes:

```json
{
  "origin": "KUL",
  "currency": "MYR",
  "travel_class": "1",
  "adults": 1,
  "departure_date": "2027-05-15",
  "return_date": "2027-05-22",
  "routes": [
    { "destination": "BKK", "budget_myr": 600 },
    { "destination": "NRT", "budget_myr": 1800 }
  ]
}
```

- `destination`: destination airport IATA code.
- `budget_myr`: your target round-trip fare in MYR.
- `travel_class`: 1 Economy, 2 Premium Economy, 3 Business, 4 First.
- The monitor does not use a fixed airline allowlist: it collects every airline / airline combination that Google Flights returns in the search results for the configured KUL route. This is broader than a manually maintained list, but it is not a guarantee that every airline operating that route appears in every search response.\n- All routes currently share the same outbound and return dates, passenger count and cabin class.

## GitHub Actions secrets

In Repository → Settings → Secrets and variables → Actions, add:

1. `SERPAPI_API_KEY` — required for fare searches.
2. `TELEGRAM_BOT_TOKEN` — optional Telegram bot token.
3. `TELEGRAM_CHAT_ID` — optional Telegram chat ID.

Never put API keys in frontend files or commit them to the repository.

## Schedule

The workflow runs daily at 00:00 UTC (08:00 Malaysia time) and can also be started manually from Actions → Daily Flight Monitor → Run workflow. It commits updated price observations to `docs/data/prices.json`.

## Data and limitations

- The browser dashboard is read-only. To add or change monitored routes, edit `config/routes.json` in GitHub and commit the change. This avoids exposing a write token or SerpApi key in the public website.
- Prices are indicative search results, not guaranteed inventory, a fare hold, or a booking. Verify the final price, taxes, baggage, fare rules and availability at checkout.
- Google Flights results can differ from what is shown in the consumer website. The project uses a third-party SerpApi service; check its current usage limits and billing.
- The monitor stores up to 1,500 observations. The history chart becomes more useful as daily scans accumulate.
- Routes share a single date range in this initial full-dashboard version. Per-route date ranges and an authenticated route editor can be added in a later backend phase.

## Deploy

Repository Settings → Pages → Deploy from a branch → `main` → `/docs`.


### Airline-by-airline price tracking

The monitor searches Google Flights without a fixed airline allowlist and saves the cheapest returned itinerary for each airline (or airline combination) on every monitored route. The dashboard shows a separate fare card per airline, and historical lows and Telegram alerts are tracked separately for each airline. An airline appears only when Google Flights returns a priced itinerary for that route and date.


### One-way and round-trip comparison

Each monitoring run now performs two Google Flights searches for every configured route:
- One-way: uses the configured departure date and no return date.
- Round-trip: uses both the configured departure and return dates.

The dashboard's Airlines page compares the cheapest returned fare per airline for both trip types. The configured route budget is applied only to round-trip fares; one-way prices are shown as reference values so they are not incorrectly compared against a round-trip budget. Telegram budget alerts remain round-trip only.

This doubles the Google Flights / SerpApi search requests per route compared with round-trip-only monitoring. Actual results depend on Google Flights returning a priced itinerary for the selected airline, route, and dates.
