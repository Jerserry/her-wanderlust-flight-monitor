import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config/routes.json").read_text(encoding="utf-8"))
DATA = ROOT / "docs/data/prices.json"
SERPAPI_URL = "https://serpapi.com/search.json"
HISTORY_LIMIT = 6000
PRICE_DROP_ALERT_PERCENT = float(CONFIG.get("price_drop_alert_percent", 5))


def telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets not configured; skipping notification.")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Telegram API returned ok=false"))
        return True
    except (requests.RequestException, ValueError, RuntimeError) as error:
        # A notification outage must never discard successfully collected fare data.
        print(f"WARNING Telegram notification failed: {error}")
        return False


def airline_identity(itinerary):
    segments = itinerary.get("flights", [])
    names = list(dict.fromkeys(
        str(segment.get("airline", "")).strip()
        for segment in segments
        if segment.get("airline")
    ))
    if not names and itinerary.get("airline"):
        names = [str(itinerary["airline"]).strip()]
    label = " + ".join(names) if names else "Not specified"
    return label, "|".join(names) if names else "unknown"


def search_fares(route, api_key, now, trip_type):
    destination = route["destination"].upper()
    origin = CONFIG.get("origin", "KUL").upper()
    currency = CONFIG.get("currency", "MYR")
    cabin_value = str(CONFIG.get("travel_class", "1")).strip().upper()
    cabin_class = {
        "ECONOMY": "1", "PREMIUM ECONOMY": "2",
        "BUSINESS": "3", "FIRST": "4",
    }.get(cabin_value, cabin_value)
    if cabin_class not in {"1", "2", "3", "4"}:
        raise ValueError("travel_class must be 1-4 or Economy/Premium Economy/Business/First.")

    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": CONFIG["departure_date"],
        "type": "2" if trip_type == "one_way" else "1",
        "adults": CONFIG.get("adults", 1),
        "travel_class": cabin_class,
        "currency": currency,
        "hl": CONFIG.get("language", "en"),
        "gl": CONFIG.get("country", "my"),
        "sort_by": "2",
        "api_key": api_key,
    }
    if trip_type == "round_trip":
        params["return_date"] = CONFIG["return_date"]

    response = requests.get(SERPAPI_URL, params=params, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"])

    # Collect every priced itinerary returned in both Google Flights result groups.
    flights = payload.get("best_flights", []) + payload.get("other_flights", [])
    priced = [f for f in flights if isinstance(f.get("price"), (int, float))]
    if not priced:
        raise RuntimeError(f"No priced {trip_type} itineraries returned.")

    cheapest_by_airline = {}
    for itinerary in priced:
        _, key = airline_identity(itinerary)
        current = cheapest_by_airline.get(key)
        if current is None or float(itinerary["price"]) < float(current["price"]):
            cheapest_by_airline[key] = itinerary

    results = []
    round_trip_budget = float(route["budget_myr"])
    for airline_key, itinerary in cheapest_by_airline.items():
        segments = itinerary.get("flights", [])
        first = segments[0] if segments else {}
        last = segments[-1] if segments else {}
        airline, _ = airline_identity(itinerary)
        price = round(float(itinerary["price"]), 2)
        budget = round_trip_budget if trip_type == "round_trip" else None
        results.append({
            "origin": origin,
            "destination": destination,
            "trip_type": trip_type,
            "departure_date": CONFIG["departure_date"],
            "return_date": CONFIG["return_date"] if trip_type == "round_trip" else None,
            "price": price,
            "currency": currency,
            "budget": budget,
            "below_budget": price <= budget if budget is not None else None,
            "checked_at": now,
            "source": "Google Flights via SerpApi",
            "airline": airline,
            "airline_key": airline_key,
            "airlines": list(dict.fromkeys(
                segment.get("airline", "") for segment in segments if segment.get("airline")
            )) or ([airline] if airline != "Not specified" else []),
            "flight_numbers": [segment.get("flight_number") for segment in segments if segment.get("flight_number")],
            "stops": len(itinerary.get("layovers", [])),
            "duration_minutes": itinerary.get("total_duration"),
            "departure_time": first.get("departure_airport", {}).get("time"),
            "arrival_time": last.get("arrival_airport", {}).get("time"),
            "booking_options_available": bool(itinerary.get("booking_token")),
            "is_stale": False,
        })
    return sorted(results, key=lambda item: item["price"])


def fetch_route(route, api_key, now):
    results, errors = [], []
    for trip_type in ("one_way", "round_trip"):
        try:
            results.extend(search_fares(route, api_key, now, trip_type))
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as error:
            errors.append(f"{trip_type}: {error}")
    if not results:
        raise RuntimeError("Both one-way and round-trip searches failed: " + "; ".join(errors))
    return results, errors


def main():
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        raise SystemExit("Missing SERPAPI_API_KEY GitHub Actions secret.")

    state = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {
        "history": [], "results": [], "currency": CONFIG.get("currency", "MYR")
    }
    previous_results = state.get("results", [])
    history = state.get("history", [])
    now = datetime.now(timezone.utc).isoformat()
    results, errors = [], []
    active_destinations = {
        str(route.get("destination", "")).upper()
        for route in CONFIG.get("routes", [])
        if route.get("destination")
    }

    for route in CONFIG["routes"]:
        destination = route.get("destination", "").upper()
        if not destination:
            continue
        try:
            route_results, route_errors = fetch_route(route, api_key, now)
            errors.extend(f"{destination}: {message}" for message in route_errors)
            for item in route_results:
                airline_key = item["airline_key"]
                trip_type = item["trip_type"]
                previous = [
                    entry for entry in history
                    if entry.get("destination") == destination
                    and entry.get("departure_date") == item["departure_date"]
                    and entry.get("trip_type", "round_trip") == trip_type
                    and entry.get("airline_key", entry.get("airline", "unknown")) == airline_key
                ]
                previous_low = min(
                    (float(entry["price"]) for entry in previous if entry.get("price") is not None),
                    default=None,
                )
                previous_current = next((
                    entry for entry in previous_results
                    if entry.get("destination") == destination
                    and entry.get("trip_type", "round_trip") == trip_type
                    and entry.get("airline_key", entry.get("airline", "unknown")) == airline_key
                ), None)

                old_price = float(previous_current["price"]) if previous_current and previous_current.get("price") is not None else None
                item["previous_price"] = old_price
                item["price_change"] = round(item["price"] - old_price, 2) if old_price is not None else None
                item["price_change_pct"] = round((item["price"] - old_price) / old_price * 100, 2) if old_price else None
                item["historical_low"] = round(min(previous_low, item["price"]), 2) if previous_low is not None else item["price"]
                item["is_new_historical_low"] = previous_low is None or item["price"] < previous_low

                history.append(item)
                results.append(item)

                crossed_budget = (
                    trip_type == "round_trip" and item["below_budget"] and
                    (previous_current is None or not previous_current.get("below_budget", False))
                )
                significant_drop = (
                    old_price is not None and old_price > 0 and
                    item["price_change_pct"] <= -PRICE_DROP_ALERT_PERCENT
                )
                new_low_in_budget = (
                    trip_type == "round_trip" and item["below_budget"] and item["is_new_historical_low"]
                )
                if crossed_budget or significant_drop or new_low_in_budget:
                    reasons = []
                    if crossed_budget:
                        reasons.append("首次达到目标预算")
                    if significant_drop:
                        reasons.append(f"较上次下降 {abs(item['price_change_pct']):.1f}%")
                    if new_low_in_budget:
                        reasons.append("刷新该航空公司历史最低价")
                    budget_text = f"（预算 RM{item['budget']:.2f}）" if item["budget"] is not None else ""
                    telegram(
                        f"✈️ Her Wanderlust Path Flight Alert\n"
                        f"{item['origin']} → {destination}\n"
                        f"航空公司：{item['airline']}\n"
                        f"{'来回' if trip_type == 'round_trip' else '单程'}参考价：RM{item['price']:.2f}{budget_text}\n"
                        f"提醒原因：{'；'.join(dict.fromkeys(reasons))}\n"
                        "请核实最终价格、行李与可订状态。"
                    )
                print(f"{destination} | {trip_type} | {item['airline']}: RM{item['price']:.2f}")
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as error:
            errors.append(f"{destination}: {error}")
            print(f"ERROR {destination}: {error}")

    # Preserve the last known result for a failed route/search, but clearly mark it stale.
    fresh_keys = {
        (item.get("destination"), item.get("trip_type", "round_trip"))
        for item in results
    }
    for old in previous_results:
        key = (old.get("destination"), old.get("trip_type", "round_trip"))
        if old.get("destination") in active_destinations and key not in fresh_keys:
            stale = dict(old)
            stale["is_stale"] = True
            stale["stale_reason"] = "本次搜索失败，以下为上次成功结果"
            results.append(stale)

    state["history"] = history[-HISTORY_LIMIT:]
    state.update({
        "updated_at": now,
        "currency": CONFIG.get("currency", "MYR"),
        "origin": CONFIG.get("origin", "KUL"),
        "departure_date": CONFIG["departure_date"],
        "return_date": CONFIG["return_date"],
        "searches_per_day": 4,
        "price_drop_alert_percent": PRICE_DROP_ALERT_PERCENT,
        "results": sorted(results, key=lambda item: (item.get("destination", ""), item.get("trip_type", ""), item.get("price", 0))),
        "errors": errors,
        "monitor_status": "partial" if errors and any(not item.get("is_stale") for item in results) else ("error" if errors else "success"),
    })
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors and not any(not item.get("is_stale") for item in results):
        raise SystemExit("All route checks failed. Previous results were preserved and marked stale; see logs.")


if __name__ == "__main__":
    main()
