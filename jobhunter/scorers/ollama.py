"""Scores jobs with a local Ollama model (free, runs on your machine)."""
from __future__ import annotations

from dataclasses import dataclass

import requests

from jobhunter.errors import RateLimited, ScorerError, ScorerUnavailable
from jobhunter.models import Job, Resume, ScoreResult
from jobhunter.registry import scorer
from jobhunter.scorers.prompt import build_prompt, parse_json, to_result

_STRICT = ("CRITICAL: Your entire response must be a single JSON object. "
           "Do not include any text, explanation or markdown before or after the JSON.\n\n")


@scorer("ollama")
class OllamaScorer:
    @dataclass
    class Options:
        model: str
        url: str = "http://localhost:11434"
        timeout_s: int = 240
        max_description_chars: int = 2000

    def __init__(self, options: dict):
        self.options = self.Options(**options)
        self.name = f"ollama:{self.options.model}"

    def score(self, job: Job, resume: Resume) -> ScoreResult:
        prompt = build_prompt(job, resume, self.options.max_description_chars)
        reply = self._generate(prompt)
        try:
            return to_result(parse_json(reply), self.name)
        except ScorerError:
            # Small local models sometimes wrap or break the JSON; one stricter retry fixes most of those.
            return to_result(parse_json(self._generate(_STRICT + prompt)), self.name)

    def generate(self, prompt: str) -> str:
        """Plain text, for cover letters."""
        return self._generate(prompt, json_format=False).strip()

    def _generate(self, prompt: str, json_format: bool = True) -> str:
        url = f"{self.options.url.rstrip('/')}/api/generate"
        body = {"model": self.options.model, "prompt": prompt, "stream": False}
        if json_format:
            body["format"] = "json"
        try:
            resp = requests.post(url, json=body, timeout=self.options.timeout_s)
        except requests.RequestException as e:
            raise ScorerUnavailable(f"Ollama not reachable at {self.options.url} ({e}); is `ollama serve` running?") from e
        if resp.status_code == 429:
            raise RateLimited("Ollama returned HTTP 429")
        if resp.status_code == 404:
            raise ScorerUnavailable(f"Ollama has no model {self.options.model!r}: run `ollama pull "
                                    f"{self.options.model}` ({resp.text[:200]})")
        if resp.status_code >= 500:
            raise ScorerUnavailable(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise ScorerError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json().get("response", "")
        except ValueError as e:
            raise ScorerError(f"Ollama sent a reply that is not JSON: {resp.text[:200]}") from e
