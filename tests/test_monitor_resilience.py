import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import requests

import monitor


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, reason="Too Many Requests"):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.reason = reason

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} {self.reason}")
            error.response = self
            raise error


class SerpApiResilienceTests(unittest.TestCase):
    def setUp(self):
        monitor.LAST_REQUEST_STARTED = None

    @patch("monitor.pace_serpapi_request")
    @patch("monitor.time.sleep")
    @patch("monitor.requests.get")
    def test_retries_one_429_then_handles_response_without_network(self, get, sleep, pace):
        get.side_effect = [
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(200, {"best_flights": [], "other_flights": []}, reason="OK"),
        ]
        with patch("monitor.reserve_weekly_retry_quota", return_value=True), \
             self.assertRaisesRegex(RuntimeError, "No priced round-trip"):
            monitor.search_fares(
                {"destination": "NRT"}, "test-key", "2026-11-18T12:00:00+08:00",
                "2026-12-01", "2026-12-08"
            )
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(0)

    @patch("monitor.pace_serpapi_request")
    @patch("monitor.time.sleep")
    @patch("monitor.requests.get")
    def test_second_429_stops_after_bounded_retry(self, get, sleep, pace):
        get.side_effect = [
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(429, headers={"Retry-After": "0"}),
        ]
        with patch("monitor.reserve_weekly_retry_quota", return_value=True), \
             self.assertRaises(monitor.SerpApiRateLimitError):
            monitor.search_fares(
                {"destination": "NRT"}, "test-key", "2026-11-18T12:00:00+08:00",
                "2026-12-01", "2026-12-08"
            )
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(0)

    def test_rate_limit_resume_cursor_skips_the_pair_that_triggered_429(self):
        self.assertEqual(monitor.cursor_after_rate_limit(4, 0, 20), 5)
        self.assertEqual(monitor.cursor_after_rate_limit(4, 1, 20), 6)
        self.assertEqual(monitor.cursor_after_rate_limit(19, 0, 20), 0)
        self.assertEqual(monitor.cursor_after_rate_limit(4, 0, 0), 0)

    def test_persisted_cooldown_survives_restart_and_expires(self):
        now = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "serpapi_cooldown.json"
            with patch.object(monitor, "SERPAPI_COOLDOWN_PATH", path):
                saved = monitor.save_serpapi_cooldown(
                    300, destination="NRT", departure_date="2026-12-01",
                    return_date="2026-12-08", now=now
                )
                self.assertEqual(saved["cooldown_seconds"], 300)
                loaded, remaining = monitor.read_serpapi_cooldown(
                    now=now + timedelta(seconds=120)
                )
                self.assertEqual(loaded["destination"], "NRT")
                self.assertEqual(remaining, 180)
                _, expired_remaining = monitor.read_serpapi_cooldown(
                    now=now + timedelta(seconds=301)
                )
                self.assertEqual(expired_remaining, 0)

    def test_short_or_zero_retry_after_uses_minimum_persistent_cooldown(self):
        now = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "serpapi_cooldown.json"
            with patch.object(monitor, "SERPAPI_COOLDOWN_PATH", path):
                saved = monitor.save_serpapi_cooldown(0, now=now)
                self.assertGreaterEqual(
                    saved["cooldown_seconds"],
                    monitor.API_LIMITS["minimum_429_cooldown_seconds"]
                )

    def test_active_persisted_cooldown_skips_search_and_preserves_old_fares(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cooldown_path = root / "serpapi_cooldown.json"
            data_path = root / "prices.json"
            with patch.object(monitor, "SERPAPI_COOLDOWN_PATH", cooldown_path):
                monitor.save_serpapi_cooldown(300, destination="NRT", now=now)
                data_path.write_text(json.dumps({
                    "history": [],
                    "results": [{
                        "destination": "NRT",
                        "trip_type": "round_trip",
                        "departure_date": "2026-12-01",
                        "return_date": "2026-12-08",
                        "price": 1234.0
                    }],
                    "currency": "MYR"
                }), encoding="utf-8")
                config = {"routes": [{"destination": "NRT"}], "origin": "KUL", "currency": "MYR"}
                flex = {
                    "mode": "flexible", "window_days": 30, "departure_step_days": 7,
                    "trip_duration_options_days": [7], "minimum_trip_days": 7,
                    "maximum_trip_days": 7
                }
                with patch.object(monitor, "DATA", data_path), \
                     patch.object(monitor, "CONFIG", config), \
                     patch.object(monitor, "FLEX_CONFIG", flex), \
                     patch.dict("os.environ", {"SERPAPI_API_KEY": "offline-test-key"}), \
                     patch("monitor.search_fares") as search:
                    with self.assertRaises(SystemExit):
                        monitor.main()
                    search.assert_not_called()
                saved = json.loads(data_path.read_text(encoding="utf-8"))
                self.assertEqual(len(saved["results"]), 1)
                self.assertTrue(saved["results"][0]["is_stale"])
                self.assertIn("限流", saved["results"][0]["stale_reason"])

    @patch("monitor.pace_serpapi_request")
    @patch("monitor.time.sleep")
    @patch("monitor.requests.get")
    def test_long_retry_after_stops_without_sleeping_or_retrying(self, get, sleep, pace):
        get.return_value = FakeResponse(429, headers={"Retry-After": "120"})
        with patch("monitor.reserve_weekly_retry_quota", return_value=True), \
             self.assertRaises(monitor.SerpApiRateLimitError) as caught:
            monitor.search_fares(
                {"destination": "NRT"}, "test-key", "2026-11-18T12:00:00+08:00",
                "2026-12-01", "2026-12-08"
            )
        self.assertEqual(caught.exception.retry_after_seconds, 120)
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_retry_after_http_date_and_invalid_header_fallback(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        response = FakeResponse(
            429, headers={"Retry-After": format_datetime(future, usegmt=True)}
        )
        seconds = monitor.retry_after_seconds(response)
        self.assertGreaterEqual(seconds, 110)
        self.assertLessEqual(seconds, 120)
        invalid = FakeResponse(429, headers={"Retry-After": "not-a-date"})
        self.assertEqual(
            monitor.retry_after_seconds(invalid),
            monitor.FALLBACK_RETRY_AFTER_SECONDS
        )

    def test_main_persists_cooldown_after_final_429(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cooldown_path = root / "serpapi_cooldown.json"
            data_path = root / "prices.json"
            config = {"routes": [{"destination": "NRT"}], "origin": "KUL", "currency": "MYR"}
            flex = {
                "mode": "flexible", "window_days": 30, "departure_step_days": 7,
                "trip_duration_options_days": [7], "minimum_trip_days": 7,
                "maximum_trip_days": 7, "search_pairs_per_destination_per_run": 1
            }
            with patch.object(monitor, "SERPAPI_COOLDOWN_PATH", cooldown_path), \
                 patch.object(monitor, "DATA", data_path), \
                 patch.object(monitor, "CONFIG", config), \
                 patch.object(monitor, "FLEX_CONFIG", flex), \
                 patch.dict("os.environ", {"SERPAPI_API_KEY": "offline-test-key"}), \
                 patch("monitor.search_fares", side_effect=monitor.SerpApiRateLimitError(300)):
                with self.assertRaises(SystemExit):
                    monitor.main()
                saved_state, remaining = monitor.read_serpapi_cooldown()
                self.assertEqual(saved_state["reason"], "HTTP 429")
                self.assertEqual(saved_state["destination"], "NRT")
                self.assertGreater(remaining, 0)

    def test_cooldown_schema_rejects_invalid_timestamp_types(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "serpapi_cooldown.json"
            path.write_text(json.dumps({
                "schema_version": 1, "provider": "SerpApi",
                "last_429_at": 123, "cooldown_until": None
            }), encoding="utf-8")
            with patch.object(monitor, "SERPAPI_COOLDOWN_PATH", path):
                with self.assertRaisesRegex(RuntimeError, "refusing API requests"):
                    monitor.read_serpapi_cooldown()

    def test_cooldown_schema_requires_timestamp_when_429_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "serpapi_cooldown.json"
            path.write_text(json.dumps({
                "schema_version": 1, "provider": "SerpApi",
                "last_429_at": "2026-11-19T00:00:00+00:00",
                "cooldown_until": None
            }), encoding="utf-8")
            with patch.object(monitor, "SERPAPI_COOLDOWN_PATH", path):
                with self.assertRaisesRegex(RuntimeError, "refusing API requests"):
                    monitor.read_serpapi_cooldown()

    def test_corrupt_cooldown_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "serpapi_cooldown.json"
            path.write_text("{invalid json", encoding="utf-8")
            with patch.object(monitor, "SERPAPI_COOLDOWN_PATH", path):
                with self.assertRaisesRegex(RuntimeError, "refusing API requests"):
                    monitor.read_serpapi_cooldown()

    def test_weekly_retry_quota_reserves_before_retry(self):
        import base64
        existing = {
            "schema_version": 1, "limit": 3,
            "week_start": (datetime.now(monitor.TIMEZONE).date() - timedelta(days=datetime.now(monitor.TIMEZONE).date().weekday())).isoformat(),
            "reservations": [{"id": "old-1"}, {"id": "old-2"}]
        }
        encoded = base64.b64encode(json.dumps(existing).encode()).decode()
        remote = {"content": encoded, "sha": "existing-sha"}
        with patch.dict("os.environ", {
            "GITHUB_TOKEN": "test-token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "test-branch",
            "GITHUB_RUN_ID": "run-3",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_WORKFLOW": "offline-test"
        }), patch("monitor.requests.get", return_value=FakeResponse(200, remote)) as get, \
             patch("monitor.requests.put", return_value=FakeResponse(200, {})) as put:
            self.assertTrue(monitor.reserve_weekly_retry_quota())
        get.assert_called_once()
        put.assert_called_once()
        body = put.call_args.kwargs["json"]
        saved = json.loads(base64.b64decode(body["content"]).decode())
        self.assertEqual(len(saved["reservations"]), 3)
        self.assertEqual(saved["reservations"][-1]["id"], "run-3-1")

    def test_weekly_retry_quota_stops_when_three_reservations_exist(self):
        existing = {
            "schema_version": 1, "limit": 3,
            "week_start": (datetime.now(monitor.TIMEZONE).date() - timedelta(days=datetime.now(monitor.TIMEZONE).date().weekday())).isoformat(),
            "reservations": [{"id": "old-1"}, {"id": "old-2"}, {"id": "old-3"}]
        }
        import base64
        remote = {"content": base64.b64encode(json.dumps(existing).encode()).decode(), "sha": "existing-sha"}
        with patch.dict("os.environ", {
            "GITHUB_TOKEN": "test-token",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "test-branch",
        }), patch("monitor.requests.get", return_value=FakeResponse(200, remote)), \
             patch("monitor.requests.put") as put:
            self.assertFalse(monitor.reserve_weekly_retry_quota())
        put.assert_not_called()

    def test_weekly_retry_quota_is_idempotent_for_same_workflow_attempt(self):
        import base64
        monday = datetime.now(monitor.TIMEZONE).date() - timedelta(days=datetime.now(monitor.TIMEZONE).date().weekday())
        existing = {
            "schema_version": 1, "limit": 3, "week_start": monday.isoformat(),
            "reservations": [{"id": "same-run-1"}]
        }
        remote = {"content": base64.b64encode(json.dumps(existing).encode()).decode(), "sha": "existing-sha"}
        with patch.dict("os.environ", {
            "GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "test-branch", "GITHUB_RUN_ID": "same-run",
            "GITHUB_RUN_ATTEMPT": "1"
        }), patch("monitor.requests.get", return_value=FakeResponse(200, remote)), \
             patch("monitor.requests.put") as put:
            self.assertTrue(monitor.reserve_weekly_retry_quota())
        put.assert_not_called()

    # Regression: a stale prior-week ledger must reset before checking the quota ceiling.
    def test_weekly_retry_quota_resets_on_new_malaysia_week(self):
        import base64
        old_monday = "2000-01-03"
        existing = {
            "schema_version": 1, "limit": 3, "week_start": old_monday.isoformat(),
            "reservations": [{"id": "old-1"}, {"id": "old-2"}, {"id": "old-3"}]
        }
        remote = {"content": base64.b64encode(json.dumps(existing).encode()).decode(), "sha": "existing-sha"}
        with patch.dict("os.environ", {
            "GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_REF_NAME": "test-branch", "GITHUB_RUN_ID": "new-week-run",
            "GITHUB_RUN_ATTEMPT": "1"
        }), patch("monitor.requests.get", return_value=FakeResponse(200, remote)), \
             patch("monitor.requests.put", return_value=FakeResponse(200, {})) as put:
            self.assertTrue(monitor.reserve_weekly_retry_quota())
        body = put.call_args.kwargs["json"]
        saved = json.loads(base64.b64decode(body["content"]).decode())
        self.assertEqual(saved["week_start"], (datetime.now(monitor.TIMEZONE).date() - timedelta(days=datetime.now(monitor.TIMEZONE).date().weekday())).isoformat())
        self.assertEqual(len(saved["reservations"]), 1)

    def test_safe_error_hides_request_url_and_credentials(self):
        error = requests.HTTPError(
            "429 Client Error for url: https://serpapi.com/search.json?api_key=secret-token"
        )
        with patch.dict("os.environ", {"SERPAPI_API_KEY": "secret-token"}):
            message = monitor.safe_error(error)
        self.assertNotIn("serpapi.com", message)
        self.assertNotIn("secret-token", message)
        self.assertIn("[request URL hidden]", message)


if __name__ == "__main__":
    unittest.main()
