import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config/routes.json").read_text())
DATA = ROOT / "docs/data/prices.json"
SERPAPI_URL = "https://serpapi.com/search.json"


def telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=30,
        )
        response.raise_for_status()


def main():
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        raise SystemExit("Missing SERPAPI_API_KEY GitHub Actions secret.")

    state = json.loads(DATA.read_text()) if DATA.exists() else {
        "history": [], "results": [], "currency": "MYR"
    }
    now = datetime.now(timezone.utc).isoformat()
    results = []

    for route in CONFIG["routes"]:
        destination = route["destination"]
        params = {
            "engine": "google_flights",
            "departure_id": CONFIG.get("origin", "KUL"),
            "arrival_id": destination,
            "outbound_date": CONFIG["departure_date"],
            "return_date": CONFIG["return_date"],
            "type": "1",
            "adults": CONFIG.get("adults", 1),
            "travel_class": "1",
            "currency": CONFIG.get("currency", "MYR"),
            "hl": "en",
            "api_key": api_key,
        }

        try:
            response = requests.get(SERPAPI_URL, params=params, timeout=60)
            response.raise_for_status()
            payload = response.json()

            if payload.get("error"):
                print(f"SerpApi error for {destination}: {payload['error']}")
                continue

            flights = payload.get("best_flights", []) + payload.get("other_flights", [])
            priced_flights = [
                flight for flight in flights
                if isinstance(flight.get("price"), (int, float))
            ]
            if not priced_flights:
                print(f"No priced flight results for {destination}.")
                continue

            cheapest = min(priced_flights, key=lambda flight: flight["price"])
            price = round(float(cheapest["price"]), 2)
            budget = float(route["budget_myr"])
            item = {
                "origin": CONFIG.get("origin", "KUL"),
                "destination": destination,
                "departure_date": CONFIG["departure_date"],
                "return_date": CONFIG["return_date"],
                "price": price,
                "currency": CONFIG.get("currency", "MYR"),
                "budget": budget,
                "below_budget": price <= budget,
                "checked_at": now,
                "source": "Google Flights via SerpApi",
            }

            previous = [
                entry for entry in state.get("history", [])
                if entry.get("destination") == destination
                and entry.get("departure_date") == item["departure_date"]
                and entry.get("return_date") == item["return_date"]
            ]
            state.setdefault("history", []).append(item)
            results.append(item)

            if item["below_budget"] and (
                not previous or price < min(float(entry["price"]) for entry in previous)
            ):
                telegram(
                    f"✈️ Her Wanderlust Path\n"
                    f"KUL → {destination}\n"
                    f"{item['departure_date']} 至 {item['return_date']}\n"
                    f"Google Flights 报价 RM{price:.2f}（预算 RM{budget:.2f}）\n"
                    "请再次确认实时价格、行李及费用。"
                )

            print(f"{destination}: RM{price:.2f}")
        except requests.RequestException as error:
            print(f"Request failed for {destination}: {error}")

    state.update({
        "updated_at": now,
        "currency": CONFIG.get("currency", "MYR"),
        "results": results,
    })
    DATA.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
