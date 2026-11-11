# Her Wanderlust Path Flight Monitor ✈️

A KUL-origin flight fare monitor using Google Flights results through SerpApi, with MYR budgets, price history, optional Telegram alerts, a GitHub Pages dashboard and daily GitHub Actions runs.

## Setup
1. Create a SerpApi account and add the repository Actions secret `SERPAPI_API_KEY`.
2. Optional Telegram secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Edit `config/routes.json` to change travel dates, destinations and MYR budgets.
4. In Settings → Pages, select Deploy from a branch → `main` → `/docs`.

## Schedule and usage
The workflow runs daily at 00:00 UTC / 08:00 Malaysia time. You can also run it manually from the Actions tab. Each destination uses one SerpApi search request per run. Check your SerpApi dashboard for current quota and usage.

The initial configuration monitors BKK and NRT only to keep test usage low. Add more destinations after confirming the results and quota.

## Important notes
- Results are indicative Google Flights search results, not a guaranteed booking price or ticket hold.
- Prices and availability can change. Recheck baggage, fees, fare conditions and availability with the airline or booking provider before purchase.
- Search coverage and results may vary by route and date.
- Never put API keys in source code or public files.
