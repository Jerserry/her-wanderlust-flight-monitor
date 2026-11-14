import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config/routes.json").read_text(encoding="utf-8"))
OUTPUT = ROOT / "docs/data/upscrape_latest.json"
API_URL = "https://data.upscrape.com/execute"
MAX_CREDITS = 5


def fail(message):
    raise SystemExit(message)


def main():
    api_key = os.getenv("UPSCRAPE_API_KEY")
    if not api_key:
        fail("Missing UPSCRAPE_API_KEY GitHub Actions secret.")

    destination = os.getenv("DESTINATION", "").strip().upper()
    departure_date = os.getenv("DEPARTURE_DATE", "").strip()
    if not re.fullmatch(r"[A-Z]{3}", destination):
        fail("DESTINATION must be a 3-letter IATA city/airport code.")
    try:
        date.fromisoformat(departure_date)
    except ValueError:
        fail("DEPARTURE_DATE must use YYYY-MM-DD.")

    routes = {str(r.get("destination", "")).upper(): r for r in CONFIG.get("routes", [])}
    if destination not in routes:
        fail(f"{destination} is not in config/routes.json. No API request was sent.")

    origin = str(CONFIG.get("origin", "KUL")).upper()
    currency = str(CONFIG.get("currency", "MYR")).upper()
    route = routes[destination]

    # One one-way search per manual run. Upscrape enforces the 5-credit ceiling
    # before creating a job; an over-priced request is refused without charge.
    run_id = os.getenv("GITHUB_RUN_ID", "local-manual")
    run_attempt = os.getenv("GITHUB_RUN_ATTEMPT", "1")
    idempotency_key = f"hwp-flight-{origin}-{destination}-{departure_date}-{run_id}-{run_attempt}"
    payload = {
        "capability": "tripcom.flight.search",
        "input": {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "adults": int(CONFIG.get("adults", 1)),
            "cabin_class": "economy",
            "currency": currency,
            "locale": "en-MY",
            "max_results": 0,
        },
        "max_credits": MAX_CREDITS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Prefer": "wait=30",
        "Idempotency-Key": idempotency_key,
    }

    response = requests.post(API_URL, headers=headers, json=payload, timeout=60)
    if response.status_code == 422:
        fail("Upscrape refused the request because its published cost exceeds the 5-credit ceiling. No job should have been created.")
    response.raise_for_status()
    envelope = response.json()

    if envelope.get("state") == "failed" or envelope.get("success") is False:
        fail(f"Upscrape job failed: {json.dumps(envelope.get('error', envelope), ensure_ascii=False)}")
    if envelope.get("state") != "completed" or envelope.get("success") is not True:
        # Do not submit again: a pending job must be polled by job_id/poll_url.
        fail("Upscrape did not finish within 30 seconds. It returned a pending job; inspect the job before retrying to avoid a duplicate request.")

    data = None
    for item in envelope.get("results", []):
        if isinstance(item, dict) and isinstance(item.get("data"), dict):
            data = item["data"]
            break
    if data is None:
        fail("Completed response did not contain results[].data; no dashboard data was written.")

    offers = data.get("offers", [])
    if not isinstance(offers, list):
        fail("Unexpected Upscrape response: offers is not a list.")

    budget = route.get("budget_myr")
    baggage_rule = route.get("baggage_rule", "unknown")
    normalized = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        total = offer.get("price_total")
        if not isinstance(total, (int, float)):
            continue
        # Upscrape's documented Trip.com flight response does not expose a
        # checked-baggage inclusion field. Never claim a baggage-required route
        # has met its budget until baggage is independently verified.
        normalized.append({
            "origin": offer.get("origin", origin),
            "destination": offer.get("destination", destination),
            "trip_type": "one_way",
            "departure_date": departure_date,
            "departure_time": offer.get("departure_time"),
            "arrival_time": offer.get("arrival_time"),
            "price": round(float(total), 2),
            "currency": offer.get("currency", currency),
            "budget_round_trip_myr": budget,
            "budget_comparison": "not_comparable_one_way",
            "baggage_rule": baggage_rule,
            "checked_baggage_included": None,
            "baggage_status": "unverified_not_returned_by_source",
            "airline_codes": offer.get("airline_codes", []),
            "flight_numbers": offer.get("flight_numbers", []),
            "stops": offer.get("stops"),
            "direct": offer.get("direct"),
            "duration_minutes": offer.get("duration_minutes"),
            "seats_available": offer.get("seats_available"),
            "price_fare": offer.get("price_fare"),
            "price_tax": offer.get("price_tax"),
            "source": "Trip.com via Upscrape",
            "source_url": data.get("source_url"),
            "itinerary_id": offer.get("itinerary_id"),
            "checked_at": data.get("collected_at") or datetime.now(timezone.utc).isoformat(),
            "is_demo": False,
            "is_stale": False,
        })

    normalized.sort(key=lambda row: row["price"])
    result = {
        "provider": "Upscrape",
        "capability": "tripcom.flight.search",
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "currency": currency,
        "trip_type": "one_way",
        "max_credits_per_call": MAX_CREDITS,
        "credits_charged": envelope.get("billing", {}).get("credits_charged"),
        "job_id": envelope.get("job_id"),
        "collected_at": data.get("collected_at"),
        "collection_complete": data.get("collection_complete"),
        "source_result_count": data.get("source_result_count", len(normalized)),
        "returned": len(normalized),
        "budget_round_trip_myr": budget,
        "baggage_rule": baggage_rule,
        "note": "One-way Trip.com observation only. It is not a round-trip fare and must not be compared with the round-trip budget. Checked-baggage inclusion is unverified.",
        "offers": normalized,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Upscrape success: {origin}->{destination} on {departure_date}; {len(normalized)} priced offers; credits charged={result['credits_charged']}.")
    print(f"Saved validation data to {OUTPUT.relative_to(ROOT)}. Dashboard prices.json was not changed.")


if __name__ == "__main__":
    main()
