# Her Wanderlust Path Flight Monitor ✈️

KUL-origin flight fare monitor with MYR budgets, price history, Telegram alerts, a GitHub Pages dashboard and daily GitHub Actions runs.

## Setup
1. Create an app at https://developers.amadeus.com/ and add repository Actions secrets `AMADEUS_CLIENT_ID` and `AMADEUS_CLIENT_SECRET`.
2. Optional Telegram secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Edit `config/routes.json` with your dates and budgets.
4. In Settings → Pages, select Deploy from a branch → `main` → `/docs`.

The workflow uses Amadeus test API by default. Test results can be limited or synthetic and are not guaranteed bookable live fares. Production access requires approval. Never put API keys in source code.

Daily run: 00:00 UTC / 08:00 Malaysia time. You can also run it manually in Actions.

The initial routes/dates are examples. API fares are indicative; recheck baggage, fees and availability before booking.
