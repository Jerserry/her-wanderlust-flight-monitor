import unittest
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
        with self.assertRaisesRegex(RuntimeError, "No priced round-trip"):
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
        with self.assertRaises(monitor.SerpApiRateLimitError):
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
