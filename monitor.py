import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config/routes.json").read_text(encoding="utf-8"))
FLEX_CONFIG_PATH = ROOT / "config/flexible_dates.json"
FLEX_CONFIG = json.loads(FLEX_CONFIG_PATH.read_text(encoding="utf-8")) if FLEX_CONFIG_PATH.exists() else {}
DATA = ROOT / "docs/data/prices.json"
SERPAPI_URL = "https://serpapi.com/search.json"
HISTORY_LIMIT = 6000
PRICE_DROP_ALERT_PERCENT = float(CONFIG.get("price_drop_alert_percent", 5))
TIMEZONE = ZoneInfo(FLEX_CONFIG.get("timezone", "Asia/Kuala_Lumpur"))
API_LIMITS_PATH = ROOT / "config/api_limits.json"
API_LIMITS = json.loads(API_LIMITS_PATH.read_text(encoding="utf-8")) if API_LIMITS_PATH.exists() else {}
REQUEST_INTERVAL_SECONDS = max(0.0, float(API_LIMITS.get("minimum_request_interval_seconds", 2)))
MAX_429_RETRIES = max(0, int(API_LIMITS.get("max_429_retries", 1)))
FALLBACK_RETRY_AFTER_SECONDS = max(1, int(API_LIMITS.get("fallback_retry_after_seconds", 60)))
MAX_RETRY_AFTER_SECONDS = max(0, int(API_LIMITS.get("max_retry_after_seconds", 90)))
LAST_REQUEST_STARTED = None


class SerpApiRateLimitError(RuntimeError):
    """Raised after a bounded 429 retry; callers should stop the current scan."""

    def __init__(self, retry_after_seconds):
        self.retry_after_seconds = retry_after_seconds
        super().__init__("SerpApi rate limit reached (HTTP 429)")


def safe_error(error):
    """Return concise, credential-safe errors without request URLs or query strings."""
    response = getattr(error, "response", None)
    if response is not None and getattr(response, "status_code", None):
        message = f"HTTP {response.status_code} {getattr(response, 'reason', '')}".strip()
    else:
        message = str(error)
    for secret_name in ("SERPAPI_API_KEY", "TELEGRAM_BOT_TOKEN", "UPSCRAPE_API_KEY"):
        secret = os.getenv(secret_name)
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"https?://\S+", "[request URL hidden]", message)
    return message[:240]


def retry_after_seconds(response):
    raw = getattr(response, "headers", {}).get("Retry-After")
    if raw:
        try:
            return max(0, int(float(raw)))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0, int((retry_at - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return FALLBACK_RETRY_AFTER_SECONDS


def pace_serpapi_request():
    """Space requests to avoid burst limits; never parallelize SerpApi calls."""
    global LAST_REQUEST_STARTED
    now = time.monotonic()
    if LAST_REQUEST_STARTED is not None:
        wait = REQUEST_INTERVAL_SECONDS - (now - LAST_REQUEST_STARTED)
        if wait > 0:
            time.sleep(wait)
    LAST_REQUEST_STARTED = time.monotonic()


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
        print(f"WARNING Telegram notification failed: {safe_error(error)}")
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


def flexible_candidates(today):
    """Build representative round-trip date pairs inside the rolling departure window."""
    window_days = max(1, int(FLEX_CONFIG.get("window_days", 180)))
    step_days = max(1, int(FLEX_CONFIG.get("departure_step_days", 7)))
    durations = FLEX_CONFIG.get("trip_duration_options_days", [5, 7, 10, 14])
    minimum = int(FLEX_CONFIG.get("minimum_trip_days", 5))
    maximum = int(FLEX_CONFIG.get("maximum_trip_days", 14))
    durations = sorted({
        int(days) for days in durations
        if minimum <= int(days) <= maximum
    })
    if not durations:
        durations = [minimum, maximum] if minimum != maximum else [minimum]

    candidates = []
    for offset in range(1, window_days + 1, step_days):
        departure = today + timedelta(days=offset)
        for duration in durations:
            candidates.append({
                "departure_date": departure.isoformat(),
                "return_date": (departure + timedelta(days=duration)).isoformat(),
                "trip_duration_days": duration,
            })
    return candidates


def choose_candidates(route, candidates, cursors):
    destination = route["destination"].upper()
    if not candidates:
        return [], 0
    cursor = int(cursors.get(destination, 0)) % len(candidates)
    count = max(1, int(FLEX_CONFIG.get("search_pairs_per_destination_per_run", 2)))
    selected = [candidates[(cursor + index) % len(candidates)] for index in range(min(count, len(candidates)))]
    cursors[destination] = (cursor + len(selected)) % len(candidates)
    return selected, cursor


def cursor_after_rate_limit(starting_cursor, attempted_pair_index, candidate_count):
    """Resume after the date pair that triggered 429 instead of repeating it."""
    if candidate_count <= 0:
        return 0
    return (int(starting_cursor) + int(attempted_pair_index) + 1) % candidate_count


def baggage_budget_eligibility(route, baggage):
    """Evaluate baggage policy separately from the raw fare-vs-budget comparison."""
    rule = route.get("baggage_rule", "unknown")
    checked = baggage.get("checked_baggage_included")
    carry_on = baggage.get("carry_on_included")
    verified = baggage.get("baggage_status") == "verified"

    if not verified or not isinstance(checked, bool) or not isinstance(carry_on, bool):
        return None, False, "baggage_unverified"

    if rule == "must_include_checked_baggage":
        compliant = checked is True
    elif rule == "may_exclude_checked_baggage_disclose":
        notes = str(baggage.get("baggage_notes") or "").lower()
        exclusion_disclosed = any(term in notes for term in ("checked baggage excluded", "checked baggage not included", "no checked baggage", "托运行李不包含", "不含托运行李"))
        compliant = checked is True or exclusion_disclosed
    else:
        return None, True, "baggage_policy_unknown"

    if not compliant:
        return False, True, "baggage_policy_noncompliant"
    return True, True, "eligible"


def search_fares(route, api_key, now, departure_date, return_date):
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
        "outbound_date": departure_date,
        "return_date": return_date,
        "type": "1",
        "adults": CONFIG.get("adults", 1),
        "travel_class": cabin_class,
        "currency": currency,
        "hl": CONFIG.get("language", "en"),
        "gl": CONFIG.get("country", "my"),
        "sort_by": "2",
        "api_key": api_key,
    }

    response = None
    for attempt in range(MAX_429_RETRIES + 1):
        pace_serpapi_request()
        response = requests.get(SERPAPI_URL, params=params, timeout=60)
        if response.status_code != 429:
            response.raise_for_status()
            break

        wait_seconds = retry_after_seconds(response)
        if attempt >= MAX_429_RETRIES or wait_seconds > MAX_RETRY_AFTER_SECONDS:
            raise SerpApiRateLimitError(wait_seconds)
        print(f"SerpApi returned HTTP 429; waiting {wait_seconds}s before one bounded retry.")
        time.sleep(wait_seconds)

    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"])

    flights = payload.get("best_flights", []) + payload.get("other_flights", [])
    priced = [flight for flight in flights if isinstance(flight.get("price"), (int, float))]
    if not priced:
        raise RuntimeError("No priced round-trip itineraries returned.")

    cheapest_by_airline = {}
    for itinerary in priced:
        _, key = airline_identity(itinerary)
        current = cheapest_by_airline.get(key)
        if current is None or float(itinerary["price"]) < float(current["price"]):
            cheapest_by_airline[key] = itinerary

    results = []
    budget_value = route.get("budget_myr")
    budget = float(budget_value) if budget_value not in (None, "") else None
    for airline_key, itinerary in cheapest_by_airline.items():
        segments = itinerary.get("flights", [])
        first = segments[0] if segments else {}
        last = segments[-1] if segments else {}
        airline, _ = airline_identity(itinerary)
        price = round(float(itinerary["price"]), 2)
        baggage = {
            "checked_baggage_included": None,
            "carry_on_included": None,
            "baggage_status": "unverified",
        }
        baggage_compliant, baggage_verified, baggage_result_status = baggage_budget_eligibility(route, baggage)
        price_within_budget = (price <= budget) if budget is not None else None
        budget_eligible = bool(price_within_budget is True and baggage_verified and baggage_compliant is True)
        if budget is None:
            budget_status = "no_budget_configured"
        elif price_within_budget is not True:
            budget_status = "above_budget"
        elif baggage_result_status != "eligible":
            budget_status = baggage_result_status
        else:
            budget_status = "eligible"
        results.append({
            "origin": origin,
            "destination": destination,
            "trip_type": "round_trip",
            "departure_date": departure_date,
            "return_date": return_date,
            "trip_duration_days": (date.fromisoformat(return_date) - date.fromisoformat(departure_date)).days,
            "price": price,
            "currency": currency,
            "budget": budget,
            "below_budget": price_within_budget,
            "price_within_budget": price_within_budget,
            "baggage_policy_compliant": baggage_compliant,
            "budget_eligible": budget_eligible,
            "budget_status": budget_status,
            "baggage_rule": route.get("baggage_rule", "unknown"),
            "checked_baggage_included": None,
            "checked_baggage_weight_kg": None,
            "carry_on_included": None,
            "carry_on_weight_kg": None,
            "baggage_status": "unverified",
            "baggage_source": None,
            "baggage_checked_at": None,
            "baggage_notes": "SerpApi normalized itinerary data did not provide independently verifiable checked-baggage or carry-on details.",
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


def main():
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        raise SystemExit("Missing SERPAPI_API_KEY GitHub Actions secret.")

    if FLEX_CONFIG.get("mode") != "flexible":
        raise SystemExit("Flexible date mode is not enabled in config/flexible_dates.json.")

    state = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {
        "history": [], "results": [], "currency": CONFIG.get("currency", "MYR")
    }
    previous_results = state.get("results", [])
    history = state.get("history", [])
    cursors = state.get("flexible_cursor", {})
    if not isinstance(cursors, dict):
        cursors = {}

    now_dt = datetime.now(TIMEZONE)
    now = now_dt.isoformat()
    today = now_dt.date()
    candidates = flexible_candidates(today)
    results, errors = [], []
    active_destinations = {
        str(route.get("destination", "")).upper()
        for route in CONFIG.get("routes", [])
        if route.get("destination")
    }
    successful_pairs = set()
    attempted_pairs = set()
    rate_limited = False

    for route in CONFIG.get("routes", []):
        destination = str(route.get("destination", "")).upper()
        if not destination:
            continue
        selected, starting_cursor = choose_candidates(route, candidates, cursors)
        print(f"{destination}: checking {len(selected)} date pairs from candidate #{starting_cursor + 1}.")
        for pair_index, pair in enumerate(selected):
            departure_date = pair["departure_date"]
            return_date = pair["return_date"]
            pair_key = (destination, "round_trip", departure_date, return_date)
            attempted_pairs.add(pair_key)
            try:
                route_results = search_fares(
                    route, api_key, now, departure_date, return_date
                )
                successful_pairs.add(pair_key)
                for item in route_results:
                    airline_key = item["airline_key"]
                    previous = [
                        entry for entry in history
                        if entry.get("destination") == destination
                        and entry.get("departure_date") == departure_date
                        and entry.get("return_date") == return_date
                        and entry.get("trip_type", "round_trip") == "round_trip"
                        and entry.get("airline_key", entry.get("airline", "unknown")) == airline_key
                        and entry.get("price") is not None
                    ]
                    previous_low = min(
                        (float(entry["price"]) for entry in previous),
                        default=None,
                    )
                    previous_current = next((
                        entry for entry in previous_results
                        if entry.get("destination") == destination
                        and entry.get("departure_date") == departure_date
                        and entry.get("return_date") == return_date
                        and entry.get("trip_type", "round_trip") == "round_trip"
                        and entry.get("airline_key", entry.get("airline", "unknown")) == airline_key
                    ), None)

                    old_price = (
                        float(previous_current["price"])
                        if previous_current and previous_current.get("price") is not None
                        else None
                    )
                    item["previous_price"] = old_price
                    item["price_change"] = round(item["price"] - old_price, 2) if old_price is not None else None
                    item["price_change_pct"] = (
                        round((item["price"] - old_price) / old_price * 100, 2)
                        if old_price else None
                    )
                    item["historical_low"] = (
                        round(min(previous_low, item["price"]), 2)
                        if previous_low is not None else item["price"]
                    )
                    item["is_new_historical_low"] = (
                        previous_low is None or item["price"] < previous_low
                    )
                    history.append(item)
                    results.append(item)

                    crossed_budget = (
                        item["budget_eligible"]
                        and (previous_current is None or not previous_current.get("budget_eligible", False))
                    )
                    significant_drop = (
                        old_price is not None and old_price > 0
                        and item["price_change_pct"] <= -PRICE_DROP_ALERT_PERCENT
                    )
                    new_low_in_budget = (
                        item["budget_eligible"]
                        and item["is_new_historical_low"]
                    )
                    if crossed_budget or significant_drop or new_low_in_budget:
                        reasons = []
                        if crossed_budget:
                            reasons.append("首次达到目标预算")
                        if significant_drop:
                            reasons.append(f"较上次下降 {abs(item['price_change_pct']):.1f}%")
                        if new_low_in_budget:
                            reasons.append("刷新该日期组合的航空公司历史最低价")
                        telegram(
                            f"✈️ Her Wanderlust Path Flight Alert\n"
                            f"{item['origin']} → {destination}\n"
                            f"日期：{departure_date} 至 {return_date}（{item['trip_duration_days']}天）\n"
                            f"航空公司：{item['airline']}\n"
                            f"来回参考价：RM{item['price']:.2f}\n"
                            f"{('目标预算：RM' + format(item['budget'], '.2f') + chr(10)) if item['budget'] is not None else ''}"
                            f"行李状态：{item['baggage_status']}（正式预算资格：{item['budget_status']}）\n"
                            f"提醒原因：{'；'.join(dict.fromkeys(reasons))}\n"
                            "请核实最终价格、行李与可订状态。"
                        )
                    print(
                        f"{destination} | {departure_date} to {return_date} | "
                        f"{item['airline']}: RM{item['price']:.2f}"
                    )
            except SerpApiRateLimitError as error:
                wait_label = (
                    f"建议等待 {error.retry_after_seconds} 秒后再检查"
                    if error.retry_after_seconds else "请检查 SerpApi 账户额度与限流状态"
                )
                errors.append(
                    f"{destination} {departure_date}–{return_date}: SerpApi 限流（HTTP 429）；"
                    f"本轮已停止后续请求，{wait_label}。"
                )
                print(f"RATE LIMITED {destination}: HTTP 429; stopping this run to protect API quota.")
                cursors[destination] = cursor_after_rate_limit(
                    starting_cursor, pair_index, len(candidates)
                )
                rate_limited = True
                break
            except (requests.RequestException, RuntimeError, ValueError, KeyError) as error:
                errors.append(f"{destination} {departure_date}–{return_date}: {safe_error(error)}")
                print(f"ERROR {destination} {departure_date} to {return_date}: {safe_error(error)}")
        if rate_limited:
            break

    # Keep old successful fares when a date pair fails or a 429 stops the remaining scan.
    fresh_pair_keys = {
        (item.get("destination"), item.get("trip_type", "round_trip"),
         item.get("departure_date"), item.get("return_date"))
        for item in results
    }
    for old in previous_results:
        # Old one-way results belong to the previous fixed-date mode and are not shown as current fares.
        if old.get("trip_type", "round_trip") != "round_trip":
            continue
        key = (
            old.get("destination"), "round_trip",
            old.get("departure_date"), old.get("return_date")
        )
        should_preserve = (
            key not in fresh_pair_keys
            and (key in attempted_pairs or rate_limited)
            and old.get("destination") in active_destinations
        )
        if should_preserve:
            stale = dict(old)
            stale["is_stale"] = True
            stale["stale_reason"] = (
                "本轮因 SerpApi 限流停止，保留上次成功报价"
                if rate_limited and key not in attempted_pairs
                else "本次该日期组合搜索失败，以下为上次成功结果"
            )
            results.append(stale)

    state["history"] = history[-HISTORY_LIMIT:]
    has_fresh_results = any(not item.get("is_stale") for item in results)
    previous_success_time = state.get("last_successful_at") or state.get("updated_at")
    state.update({
        "last_attempted_at": now,
        "last_successful_at": now if has_fresh_results else previous_success_time,
        "updated_at": now if has_fresh_results else (state.get("updated_at") or previous_success_time),
        "currency": CONFIG.get("currency", "MYR"),
        "origin": CONFIG.get("origin", "KUL"),
        "date_mode": "flexible",
        "flexible_settings": {
            "window_days": int(FLEX_CONFIG.get("window_days", 180)),
            "minimum_trip_days": int(FLEX_CONFIG.get("minimum_trip_days", 5)),
            "maximum_trip_days": int(FLEX_CONFIG.get("maximum_trip_days", 14)),
            "trip_duration_options_days": FLEX_CONFIG.get("trip_duration_options_days", [5, 7, 10, 14]),
            "departure_step_days": int(FLEX_CONFIG.get("departure_step_days", 7)),
            "search_pairs_per_destination_per_run": int(FLEX_CONFIG.get("search_pairs_per_destination_per_run", 2)),
        },
        "flexible_cursor": cursors,
        "schedule": {
            "frequency": "weekly",
            "days": ["Tuesday", "Wednesday", "Friday", "Saturday"],
            "time": "18:00",
            "timezone": "Asia/Kuala_Lumpur",
            "cron_utc": "0 10 * * 2,3,5,6",
        },
        "runs_per_week": 4,
        "searches_per_run": (
            len(active_destinations)
            * max(1, int(FLEX_CONFIG.get("search_pairs_per_destination_per_run", 2)))
        ),
        "searches_per_week": (
            len(active_destinations)
            * max(1, int(FLEX_CONFIG.get("search_pairs_per_destination_per_run", 2)))
            * 4
        ),
        "searches_per_month_estimate": round(
            len(active_destinations)
            * max(1, int(FLEX_CONFIG.get("search_pairs_per_destination_per_run", 2)))
            * 4 * 365.25 / 12 / 7
        ),
        "searches_per_day": round(
            len(active_destinations)
            * max(1, int(FLEX_CONFIG.get("search_pairs_per_destination_per_run", 2)))
            * 4 / 7, 2
        ),
        "price_drop_alert_percent": PRICE_DROP_ALERT_PERCENT,
        "results": sorted(
            results,
            key=lambda item: (
                item.get("destination", ""), item.get("departure_date", ""),
                item.get("return_date") or "", item.get("price", 0)
            )
        ),
        "errors": errors,
        "rate_limited": rate_limited,
        "failed_route_count": len(errors),
        "monitor_status": (
            "partial" if errors and any(not item.get("is_stale") for item in results)
            else ("error" if errors else "success")
        ),
    })
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors and not any(not item.get("is_stale") for item in results):
        raise SystemExit("All flexible-date checks failed. Previous results were preserved and marked stale; see logs.")


if __name__ == "__main__":
    main()
