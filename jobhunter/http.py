"""HTTP client handed to boards: one session, a default timeout, polite per-host spacing, and retries with backoff."""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlparse

import requests

# Sites that refuse requests at the default pace; one spacing is shared by all their subdomains.
HOST_INTERVALS = {"linkedin.com": 3.0}
RETRY_STATUS = {429, 502, 503, 504}
BACKOFF_S = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER_S = 60.0     # a server asking for a longer wait is left alone until the next run


def retry_after_seconds(resp: Any) -> float | None:
    """Seconds from a Retry-After header (a number or an HTTP date); None when absent or unreadable."""
    value = (getattr(resp, "headers", None) or {}).get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _refused(url: str) -> requests.Response:
    """A 429 answered locally for a site this run has given up on."""
    resp = requests.Response()
    resp.status_code, resp.url, resp._content = 429, url, b""
    resp.reason = "gave up on this site for the rest of the run"
    return resp


class Http:
    def __init__(self, min_interval_s: float = 0.5, timeout_s: float = 20, session: Any = None,
                 host_intervals: dict[str, float] | None = None, max_retries: int = 3,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None):
        self.min_interval_s = min_interval_s
        self.host_intervals = dict(HOST_INTERVALS if host_intervals is None else host_intervals)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "jobhunter/0.1 (+self-hosted job search)"
        # Looked up at call time, so tests that patch time.sleep / time.monotonic still take effect.
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._clock = clock or (lambda: time.monotonic())
        self.rng = random.Random()
        self._next_allowed: dict[str, float] = {}
        self._given_up: set[str] = set()          # sites that kept refusing: not asked again in this run
        self._lock = threading.Lock()

    def _spacing(self, url: str) -> tuple[str, float]:
        """The key requests are spaced by (a whole site for HOST_INTERVALS, else the host) and the spacing."""
        host = (urlparse(url).hostname or "").lower()
        for domain, interval in self.host_intervals.items():
            if host == domain or host.endswith("." + domain):
                return domain, interval
        return host, self.min_interval_s

    def _wait_turn(self, url: str) -> None:
        key, interval = self._spacing(url)
        with self._lock:
            now = self._clock()
            start = max(now, self._next_allowed.get(key, 0.0))
            self._next_allowed[key] = start + interval
        if start > now:
            self._sleep(start - now)

    def _cool_down(self, url: str, seconds: float) -> None:
        """After a 429, every request to that site (from any thread) waits `seconds`."""
        key, _ = self._spacing(url)
        with self._lock:
            self._next_allowed[key] = max(self._next_allowed.get(key, 0.0), self._clock() + seconds)

    def _backoff(self, attempt: int) -> float:
        return BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)] * self.rng.uniform(0.75, 1.25)

    def request(self, method: str, url: str, *, retry: bool | None = None, **kw: Any) -> Any:
        """Send a request. GETs (and requests with retry=True) are retried on 429/502/503/504, dropped connections
        and timeouts, up to max_retries times with growing waits or the server's Retry-After (60 s at most)."""
        retryable = method.upper() == "GET" if retry is None else retry
        kw.setdefault("timeout", self.timeout_s)
        site = self._spacing(url)[0]
        if site in self._given_up:
            return _refused(url)
        attempt = 0
        while True:
            self._wait_turn(url)
            try:
                resp = self.session.request(method, url, **kw)
            except (requests.ConnectionError, requests.Timeout):
                if not retryable or attempt >= self.max_retries:
                    raise
                self._sleep(self._backoff(attempt))
                attempt += 1
                continue
            if resp.status_code not in RETRY_STATUS:
                return resp
            asked = retry_after_seconds(resp)
            if asked is not None and asked > MAX_RETRY_AFTER_S:
                self._given_up.add(site)          # the site wants us gone for a while: leave it until the next run
                return resp
            wait = asked if asked is not None else self._backoff(attempt)
            if resp.status_code == 429:
                self._cool_down(url, wait)
            if not retryable or attempt >= self.max_retries:
                if resp.status_code == 429 and attempt > 0:
                    self._given_up.add(site)      # still limited after every retry: waiting per job would stall the run
                return resp
            if hasattr(resp, "close"):
                resp.close()
            if resp.status_code != 429:          # a 429's wait is taken by _wait_turn through the cooldown
                self._sleep(wait)
            attempt += 1

    def get_json(self, url: str, **kw: Any) -> Any:
        resp = self.request("GET", url, **kw)
        resp.raise_for_status()
        return resp.json()
