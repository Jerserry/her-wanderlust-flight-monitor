import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config/routes.json").read_text(encoding="utf-8"))
DATA = ROOT / "docs/data/prices.json"
SERPAPI_URL = "https://serpapi.com/search.json"
HISTORY_LIMIT = 1500


def telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets not configured; skipping notification.")
        return
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=30,
    )
    response.raise_for_status()


def fetch_route(route, api_key, now):
    destination = route["destination"].upper()
    origin = CONFIG.get("origin", "KUL").upper()
    currency = CONFIG.get("currency", "MYR")
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": CONFIG["departure_date"],
        "return_date": CONFIG["return_date"],
        "type": "1",
        "adults": CONFIG.get("adults", 1),
        "travel_class": str(CONFIG.get("travel_class", "1")),
        "currency": currency,
        "hl": CONFIG.get("language", "en"),
        "gl": CONFIG.get("country", "my"),
        "sort_by": "2",
        "api_key": api_key,
    }
    response = requests.get(SERPAPI_URL, params=params, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"])

    flights = payload.get("best_flights", []) + payload.get("other_flights", [])
    priced = [f for f in flights if isinstance(f.get("price"), (int, float))]
    if not priced:
        raise RuntimeError("No priced itineraries returned by Google Flights.")

    cheapest = min(priced, key=lambda f: float(f["price"]))
    segments = cheapest.get("flights", [])
    first = segments[0] if segments else {}
    last = segments[-1] if segments else {}
    layovers = cheapest.get("layovers", [])
    price = round(float(cheapest["price"]), 2)
    budget = float(route["budget_myr"])

    return {
        "origin": origin,
        "destination": destination,
        "departure_date": CONFIG["departure_date"],
        "return_date": CONFIG["return_date"],
        "price": price,
        "currency": currency,
        "budget": budget,
        "below_budget": price <= budget,
        "checked_at": now,
        "source": "Google Flights via SerpApi",
        "airline": cheapest.get("airline") or first.get("airline") or "Not specified",
        "airlines": list(dict.fromkeys(
            segment.get("airline", "") for segment in segments if segment.get("airline")
        )),
        "flight_numbers": [s.get("flight_number") for s in segments if s.get("flight_number")],
        "stops": len(layovers),
        "duration_minutes": cheapest.get("total_duration"),
        "departure_time": first.get("departure_airport", {}).get("time"),
        "arrival_time": last.get("arrival_airport", {}).get("time"),
        "booking_options_available": bool(cheapest.get("booking_token")),
    }


def main():
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        raise SystemExit("Missing SERPAPI_API_KEY GitHub Actions secret.")

    state = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {
        "history": [], "results": [], "currency": CONFIG.get("currency", "MYR")
    }
    previous_results = state.get("results", [])
    now = datetime.now(timezone.utc).isoformat()
    results = []
    errors = []

    for route in CONFIG["routes"]:
        destination = route.get("destination", "").upper()
        if not destination:
            continue
        try:
            item = fetch_route(route, api_key, now)
            budget = item["budget"]
            previous = [
                entry for entry in state.get("history", [])
                if entry.get("destination") == destination
                and entry.get("departure_date") == item["departure_date"]
                and entry.get("return_date") == item["return_date"]
            ]
            previous_low = min(
                (float(entry["price"]) for entry in previous if entry.get("price") is not None),
                default=None,
            )
            previous_current = next(
                (entry for entry in previous_results if entry.get("destination") == destination),
                None,
            )
            state.setdefault("history", []).append(item)
            results.append(item)

            is_new_low = previous_low is None or item["price"] < previous_low
            crossed_budget = item["below_budget"] and (
                previous_current is None or not previous_current.get("below_budget", False)
            )
            if item["below_budget"] and (is_new_low or crossed_budget):
                reason = "达到目标预算" if crossed_budget else "刷新历史最低价"
                airline_text = ", ".join(item["airlines"]) or item["airline"]
                telegram(
                    f"✈️ Her Wanderlust Path Flight Alert\n"
                    f"{item['origin']} → {destination}\n"
                    f"{item['departure_date']} 至 {item['return_date']}\n"
                    f"最低参考价：RM{item['price']:.2f}（预算 RM{budget:.2f}）\n"
                    f"航空公司：{airline_text}\n"
                    f"提醒原因：{reason}\n"
                    "请前往航空公司或订票平台核实最终价格、行李与可订状态。"
                )
            print(f"{destination}: RM{item['price']:.2f} | budget RM{budget:.2f}")
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as error:
            message = f"{destination}: {error}"
            errors.append(message)
            print(f"ERROR {message}")

    state["history"] = state.get("history", [])[-HISTORY_LIMIT:]
    state.update({
        "updated_at": now,
        "currency": CONFIG.get("currency", "MYR"),
        "origin": CONFIG.get("origin", "KUL"),
        "departure_date": CONFIG["departure_date"],
        "return_date": CONFIG["return_date"],
        "results": results if results else previous_results,
        "errors": errors,
        "monitor_status": "partial" if errors and results else ("error" if errors else "success"),
    })
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors and not results:
        raise SystemExit("All route checks failed. Existing results were preserved; see logs.")


if __name__ == "__main__":
    main()
