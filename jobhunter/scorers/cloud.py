"""Optional cloud scorers: Google Gemini and Groq. Both have free tiers and need an API key in .env; nothing else in
jobhunter needs them. They are asked the local Ollama scorer's prompt, with more of the description by default
(max_description_chars 3000, against Ollama's 2000)."""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import requests

from jobhunter.errors import RateLimited, ScorerBusy, ScorerError, ScorerUnavailable
from jobhunter.http import retry_after_seconds
from jobhunter.models import Job, Resume, ScoreResult
from jobhunter.registry import scorer
from jobhunter.scorers.prompt import build_prompt, parse_json, to_result


# A 400 that means the key or model cannot be used (Gemini: API_KEY_INVALID; Groq: model_decommissioned).
_SETUP_ERROR = re.compile(r"API_KEY_INVALID|API key not valid|model_decommissioned|model_not_found|decommissioned",
                          re.I)


class KeyRing:
    """API keys from a comma-separated environment variable. A rate-limited key rests (for its Retry-After, else
    `cooldown_s`) while the others are used; each time every key is limited in a row the rest doubles, up to
    `max_cooldown_s`, and a success resets it."""

    def __init__(self, env_var: str, env: Mapping[str, str], cooldown_s: float = 60.0, max_cooldown_s: float = 900.0):
        self.keys = [k.strip() for k in env.get(env_var, "").split(",") if k.strip()]
        if not self.keys:
            raise ScorerUnavailable(f"no API key in the environment variable {env_var} (add it to .env)")
        self.cooldown_s, self.max_cooldown_s = cooldown_s, max_cooldown_s
        self.clock: Callable[[], float] = lambda: time.monotonic()
        self._start = 0
        self._resting_until = [0.0] * len(self.keys)
        self._streak = 0                  # rounds in a row in which every key was limited
        self._lock = threading.Lock()

    def available(self) -> list[int]:
        """Keys not resting, starting with the one that worked last."""
        with self._lock:
            now = self.clock()
            order = [(self._start + i) % len(self.keys) for i in range(len(self.keys))]
            return [i for i in order if self._resting_until[i] <= now]

    def seconds_until_free(self) -> float:
        with self._lock:
            return max(0.0, min(self._resting_until) - self.clock())

    def limited(self, index: int, retry_after: float | None) -> None:
        with self._lock:
            rest = retry_after if retry_after is not None else min(
                self.cooldown_s * 2 ** self._streak, self.max_cooldown_s)
            self._resting_until[index] = self.clock() + rest

    def all_limited(self) -> None:
        with self._lock:
            self._streak += 1

    def worked(self, index: int) -> None:
        with self._lock:
            self._start, self._streak = index, 0


class Pacer:
    """Keeps requests at least `interval_s` apart: free tiers allow a fixed number of requests a minute."""

    def __init__(self, interval_s: float, clock: Callable[[], float] = time.monotonic):
        self.interval_s, self.clock = interval_s, clock
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            start = max(now, self._next)
            self._next = start + self.interval_s
        if start > now:
            time.sleep(start - now)


class CloudScorer:
    provider = ""
    Options: Any = None

    def __init__(self, options: dict, env: Mapping[str, str] | None = None):
        self.options = self.Options(**options)
        self.keys = KeyRing(self.options.api_keys_env, os.environ if env is None else env)
        self.pacer = Pacer(self.options.min_interval_s)
        self.name = f"{self.provider.lower()}:{self.options.model}"

    def score(self, job: Job, resume: Resume) -> ScoreResult:
        prompt = build_prompt(job, resume, self.options.max_description_chars)
        return to_result(parse_json(self._complete(prompt, json_mode=True)), self.name)

    def generate(self, prompt: str) -> str:
        """Plain text, for cover letters."""
        return self._complete(prompt, json_mode=False).strip()

    def _post(self, key: str, prompt: str, json_mode: bool) -> Any:
        raise NotImplementedError

    def _text(self, data: Any) -> str:
        raise NotImplementedError

    def _complete(self, prompt: str, json_mode: bool) -> str:
        indexes = self.keys.available()
        if not indexes:
            raise RateLimited(f"{self.provider}: every API key is resting after a rate limit "
                              f"({self.keys.seconds_until_free():.0f} s more)")
        rejected = 0
        for index in indexes:
            self.pacer.wait()
            try:
                resp = self._post(self.keys.keys[index], prompt, json_mode)
            except requests.ConnectionError as e:      # also ConnectTimeout
                raise ScorerUnavailable(f"{self.provider} not reachable ({type(e).__name__})") from None
            except requests.Timeout as e:              # connected, but no answer in time
                raise ScorerBusy(f"{self.provider} did not answer in time ({type(e).__name__})") from None
            except requests.RequestException as e:
                # The exception text can include the request URL; only its type is reported.
                raise ScorerUnavailable(f"{self.provider} not reachable ({type(e).__name__})") from None
            if resp.status_code == 429:
                self.keys.limited(index, retry_after_seconds(resp))
                continue
            if resp.status_code in (401, 403):
                # This key is wrong or has no access: rest it for the longest time and try the others.
                self.keys.limited(index, self.keys.max_cooldown_s)
                rejected += 1
                continue
            if resp.status_code == 404:
                raise ScorerUnavailable(f"{self.provider} has no model {self.options.model!r} (check scorers."
                                        f"{self.provider.lower()}.model): {resp.text[:200]}")
            if resp.status_code >= 500:
                raise ScorerBusy(f"{self.provider} returned HTTP {resp.status_code}")
            if resp.status_code == 400 and _SETUP_ERROR.search(resp.text or ""):
                raise ScorerUnavailable(f"{self.provider}: the API key or model cannot be used (check scorers."
                                        f"{self.provider.lower()} and .env): {resp.text[:200]}")
            if resp.status_code != 200:
                raise ScorerError(f"{self.provider} returned HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                text = self._text(resp.json())
            except (KeyError, IndexError, TypeError, ValueError) as e:
                raise ScorerError(f"{self.provider} sent a reply without text: {resp.text[:200]}") from e
            self.keys.worked(index)
            return text
        if rejected == len(indexes):
            raise ScorerUnavailable(f"{self.provider} rejected every API key in {self.options.api_keys_env} "
                                    "(check the keys in .env)")
        # Every key is limited: the job moves to the next scorer now (or is retried next run) instead of waiting here.
        self.keys.all_limited()
        raise RateLimited(f"{self.provider}: all {len(self.keys.keys)} API key(s) are rate limited")


@scorer("gemini")
class GeminiScorer(CloudScorer):
    provider = "Gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    @dataclass
    class Options:
        model: str = "gemini-3.5-flash-lite"     # 2.5 models are limited to earlier users (checked 2026-09-29)
        api_keys_env: str = "GEMINI_API_KEYS"
        min_interval_s: float = 4.0       # free tier: 15 requests a minute
        timeout_s: int = 30
        max_description_chars: int = 3000

    def _post(self, key: str, prompt: str, json_mode: bool) -> Any:
        body: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
        if json_mode:
            body["generationConfig"] = {"response_mime_type": "application/json"}
        # The key goes in a header, not the URL, so it never shows up in logs or errors.
        return requests.post(self.URL.format(model=self.options.model), headers={"x-goog-api-key": key}, json=body,
                             timeout=self.options.timeout_s)

    def _text(self, data: Any) -> str:
        return data["candidates"][0]["content"]["parts"][0]["text"]


@scorer("groq")
class GroqScorer(CloudScorer):
    provider = "Groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    @dataclass
    class Options:
        model: str = "openai/gpt-oss-120b"       # llama-4-scout was shut down on 2026-07-17
        api_keys_env: str = "GROQ_API_KEYS"
        min_interval_s: float = 2.1       # free tier: 30 requests a minute
        timeout_s: int = 30
        max_description_chars: int = 3000

    def _post(self, key: str, prompt: str, json_mode: bool) -> Any:
        body: dict[str, Any] = {"model": self.options.model, "messages": [{"role": "user", "content": prompt}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return requests.post(self.URL, headers={"Authorization": f"Bearer {key}"}, json=body,
                             timeout=self.options.timeout_s)

    def _text(self, data: Any) -> str:
        return data["choices"][0]["message"]["content"]
