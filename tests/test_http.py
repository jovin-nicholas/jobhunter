import random
import unittest

import requests

from jobhunter.http import Http
from tests.helpers import FakeResponse


class Script:
    """A session that answers with the given responses (or raises the given exceptions) in order."""

    def __init__(self, *answers):
        self.answers, self.calls, self.headers = list(answers), [], {}

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def http_with(*answers):
    """No spacing, and a fake clock that moves only when the client sleeps, so waits are exact and instant."""
    sleeps, now = [], [0.0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    http = Http(min_interval_s=0, host_intervals={}, session=Script(*answers), sleep=sleep, clock=lambda: now[0])
    http.rng = random.Random(0)
    return http, sleeps


def status(code, **headers):
    return FakeResponse(status_code=code, text="", headers=headers)


class TestRetry(unittest.TestCase):
    def test_a_rate_limited_get_is_retried(self):
        http, sleeps = http_with(status(429), status(200))
        self.assertEqual(http.request("GET", "https://a.example/1").status_code, 200)
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(0.75 <= sleeps[0] <= 1.25, sleeps)

    def test_retry_after_is_honoured(self):
        http, sleeps = http_with(status(503, **{"Retry-After": "7"}), status(200))
        self.assertEqual(http.request("GET", "https://a.example/1").status_code, 200)
        self.assertEqual(sleeps, [7.0])

    def test_a_long_retry_after_ends_the_retries_at_once(self):
        http, sleeps = http_with(status(429, **{"Retry-After": "300"}), status(200))
        self.assertEqual(http.request("GET", "https://a.example/1").status_code, 429)
        self.assertEqual(sleeps, [])

    def test_waits_grow_and_retries_stop_after_three(self):
        http, sleeps = http_with(status(502), status(503), status(504), status(503))
        self.assertEqual(http.request("GET", "https://a.example/1").status_code, 503)
        self.assertEqual(len(sleeps), 3)
        for wait, base in zip(sleeps, (1, 2, 4)):
            self.assertTrue(0.75 * base <= wait <= 1.25 * base, sleeps)

    def test_other_errors_are_not_retried(self):
        http, sleeps = http_with(status(404), status(200))
        self.assertEqual(http.request("GET", "https://a.example/1").status_code, 404)
        self.assertEqual(sleeps, [])

    def test_posts_are_retried_only_when_marked(self):
        http, _ = http_with(status(503), status(200))
        self.assertEqual(http.request("POST", "https://a.example/1", json={}).status_code, 503)
        http, _ = http_with(status(503), status(200))
        self.assertEqual(http.request("POST", "https://a.example/1", json={}, retry=True).status_code, 200)

    def test_connection_errors_are_retried_then_raised(self):
        http, _ = http_with(requests.ConnectionError("reset"), status(200))
        self.assertEqual(http.request("GET", "https://a.example/1").status_code, 200)
        http, sleeps = http_with(*[requests.Timeout("slow")] * 4)
        with self.assertRaises(requests.Timeout):
            http.request("GET", "https://a.example/1")
        self.assertEqual(len(sleeps), 3)

    def test_a_rate_limit_slows_the_whole_host_down(self):
        # Other threads calling the same site must wait too, even when this call has no retries left.
        http, sleeps = http_with(status(429, **{"Retry-After": "5"}), status(200), status(200))
        http.max_retries = 0
        self.assertEqual(http.request("GET", "https://a.example/1").status_code, 429)
        http.request("GET", "https://b.example/1")
        self.assertEqual(sleeps, [])                     # another site: no wait
        http.request("GET", "https://a.example/2")
        self.assertEqual(sleeps, [5.0])


class TestGiveUpOnAHost(unittest.TestCase):
    def test_a_host_that_keeps_refusing_is_left_alone_for_the_rest_of_the_run(self):
        http, sleeps = http_with(*[status(429, **{"Retry-After": "60"})] * 4)
        self.assertEqual(http.request("GET", "https://www.example.com/1").status_code, 429)
        self.assertEqual(sleeps, [60.0, 60.0, 60.0])
        sleeps.clear()
        resp = http.request("GET", "https://www.example.com/2")         # no request, no waiting
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(sleeps, [])
        self.assertEqual(len(http.session.calls), 4)

    def test_a_long_retry_after_also_ends_the_run_for_that_host(self):
        http, sleeps = http_with(status(429, **{"Retry-After": "600"}), status(200))
        http.request("GET", "https://a.example/1")
        self.assertEqual(http.request("GET", "https://a.example/2").status_code, 429)
        self.assertEqual((sleeps, len(http.session.calls)), ([], 1))


if __name__ == "__main__":
    unittest.main()
