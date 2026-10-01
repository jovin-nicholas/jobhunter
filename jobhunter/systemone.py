"""Asks a local System One decision model (Ollama 0.35+, `/v1/systemone`) yes/no questions about a job.

One question per request: Ollama shows each question the whole schema, so questions asked together change each
other's answers. The model is deterministic, so identical requests are answered from a cache. Every failure returns
None and the caller carries on without the answer; after a few failures in a row the client stops asking for the rest
of the run, so a missing or stopped model costs seconds, not one timeout per job.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import requests

from jobhunter.settings import SystemOneSettings

KEEP_ALIVE = "5m"             # unload soon after a run, so the model does not hold memory other apps need
MAX_FAILURES = 3


class SystemOneClient:
    def __init__(self, settings: SystemOneSettings, post: Callable[..., Any] = requests.post):
        self.settings = settings
        self._post = post
        self._cache: dict[str, float] = {}
        self._failures = 0
        self._last_error: str | None = None

    def noul(self, name: str, state: dict, question: dict) -> float | None:
        """The probability that the answer to `question` about `state` is yes, or None when there is no answer."""
        if self._failures >= MAX_FAILURES:
            return None
        body = {"model": self.settings.model, "state": state, "questions": {name: question},
                "keep_alive": KEEP_ALIVE}
        key = json.dumps(body, sort_keys=True)
        if key in self._cache:
            return self._cache[key]
        url = f"{self.settings.url.rstrip('/')}/v1/systemone"
        try:
            resp = self._post(url, json=body, timeout=self.settings.timeout_s)
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}: {str(resp.text)[:200]}")
            value = resp.json()["answers"][name]["noul"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"unexpected answer {value!r}")
        except Exception as e:      # any failure: no answer, never a guessed one
            self._failures += 1
            self._last_error = f"{type(e).__name__}: {e}"
            return None
        self._failures = 0
        self._cache[key] = float(value)
        return float(value)

    def report(self) -> str | None:
        """One line for the run log when the model could not be used, else None."""
        if self._last_error is None:
            return None
        stopped = " (stopped asking for this run)" if self._failures >= MAX_FAILURES else ""
        return (f"systemone: {self.settings.model} at {self.settings.url} did not answer{stopped}: "
                f"{self._last_error}; jobs were kept and passed on to the scorers")
