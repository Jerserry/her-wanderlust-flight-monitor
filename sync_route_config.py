#!/usr/bin/env python3
"""Generate the public website route budget file from config/routes.json.

This script performs no network requests and does not call any flight API.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "config" / "routes.json"
TARGET = ROOT / "docs" / "data" / "route_config.json"


def main():
    config = json.loads(SOURCE.read_text(encoding="utf-8"))
    routes = config.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ValueError("config/routes.json must contain a non-empty routes list")

    destinations = set()
    normalized_routes = []
    budgets = {}
    for route in routes:
        destination = str(route.get("destination", "")).strip().upper()
        budget = route.get("budget_myr")
        if not destination:
            raise ValueError("Every route must have a destination")
        if destination in destinations:
            raise ValueError(f"Duplicate destination: {destination}")
        if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
            raise ValueError(f"Invalid budget_myr for {destination}: {budget!r}")
        destinations.add(destination)
        item = dict(route)
        item["destination"] = destination
        item["budget_myr"] = budget
        normalized_routes.append(item)
        budgets[destination] = budget

    specification = config.get("budget_specification", {})
    public_config = {
        "schema_version": 1,
        "origin": config.get("origin", "KUL"),
        "currency": config.get("currency", "MYR"),
        "travel_class": config.get("travel_class", "ECONOMY"),
        "adults": config.get("adults", 1),
        "trip_type": specification.get("trip_type", "round_trip"),
        "budget_specification": specification,
        "routes": normalized_routes,
        "budgets_by_destination": budgets,
    }

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(public_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Generated {TARGET.relative_to(ROOT)} with {len(normalized_routes)} routes.")


if __name__ == "__main__":
    main()
