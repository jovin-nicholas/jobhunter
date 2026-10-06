"""Scores jobs with a local Ollama model (free, runs on your machine)."""
from __future__ import annotations

import re
from dataclasses import dataclass

import requests

from jobhunter.errors import RateLimited, ScorerBusy, ScorerError, ScorerUnavailable
from jobhunter.ollama_check import ollama_problem
from jobhunter.models import Job, Resume, ScoreResult
from jobhunter.registry import scorer
from jobhunter.scorers.prompt import build_prompt, parse_json, to_result

_INLINE_THINKING = re.compile(r"^\s*<think>.*?</think>\s*", re.S)


def _without_reasoning(text: str) -> str:
    """The answer without reasoning a model wrote inline instead of in Ollama's separate thinking field."""
    text = _INLINE_THINKING.sub("", text)
    if "</think>" in text and "<think>" not in text.split("</think>", 1)[0]:
        # Templates that put <think> in the prompt leave only the closing tag in the reply.
        return text.rsplit("</think>", 1)[1].lstrip()
    if text.lstrip().startswith("<think>"):
        raise ScorerError("the model's reply stopped inside its reasoning, before the answer")
    return text
_STRICT = ("CRITICAL: Your entire response must be a single JSON object. "
           "Do not include any text, explanation or markdown before or after the JSON.\n\n")


@scorer("ollama")
class OllamaScorer:
    # Cover letters read better with thinking on and some variety; thinking is slower, so they get more time.
    LETTER_DEFAULTS = {"think": True, "temperature": 0.7, "timeout_s": 600}

    @dataclass
    class Options:
        model: str
        url: str = "http://localhost:11434"
        timeout_s: int = 240
        max_description_chars: int = 2000
        # Thinking models (gemma4, qwen3) otherwise write a hidden essay first: about 4x slower, no better scores.
        think: bool = False
        temperature: float = 0.0          # the same posting always gets the same score
        num_ctx: int | None = None        # Ollama's context window; its default fits the usual prompt

    def __init__(self, options: dict):
        self.options = self.Options(**options)
        self.name = f"ollama:{self.options.model}"
        self._thinks = True             # False once Ollama says the model cannot think

    def score(self, job: Job, resume: Resume) -> ScoreResult:
        prompt = build_prompt(job, resume, self.options.max_description_chars)
        reply = self._generate(prompt)
        try:
            return to_result(parse_json(reply), self.name)
        except ScorerError:
            # Small local models sometimes wrap or break the JSON; one stricter retry fixes most of those.
            return to_result(parse_json(self._generate(_STRICT + prompt)), self.name)

    def check(self) -> str | None:
        """For check-config: why this scorer cannot run yet (Ollama stopped, model not pulled), or None."""
        return ollama_problem(self.options.url, self.options.model)

    def generate(self, prompt: str) -> str:
        """Plain text, for cover letters."""
        return self._generate(prompt, json_format=False).strip()

    def _generate(self, prompt: str, json_format: bool = True) -> str:
        url = f"{self.options.url.rstrip('/')}/api/generate"
        options = {"temperature": self.options.temperature}
        if self.options.num_ctx:
            options["num_ctx"] = self.options.num_ctx
        body = {"model": self.options.model, "prompt": prompt, "stream": False, "options": options}
        if self._thinks:
            body["think"] = self.options.think
        if json_format:
            body["format"] = "json"
        resp = self._post(url, body)
        if resp.status_code == 400 and "does not support thinking" in resp.text and "think" in body:
            # A model without thinking rejects think: true (think: false is accepted); ask again without it.
            self._thinks = False
            body = {k: v for k, v in body.items() if k != "think"}
            resp = self._post(url, body)
        if resp.status_code == 429:
            raise RateLimited("Ollama returned HTTP 429")
        if resp.status_code == 404:
            raise ScorerUnavailable(f"Ollama has no model {self.options.model!r}: run `ollama pull "
                                    f"{self.options.model}` ({resp.text[:200]})")
        if resp.status_code >= 500:
            raise ScorerBusy(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise ScorerError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            text = resp.json().get("response", "")
        except ValueError as e:
            raise ScorerError(f"Ollama sent a reply that is not JSON: {resp.text[:200]}") from e
        return _without_reasoning(text)

    def _post(self, url: str, body: dict) -> requests.Response:
        try:
            return requests.post(url, json=body, timeout=self.options.timeout_s)
        except requests.ConnectionError as e:       # also ConnectTimeout: nothing is listening
            raise ScorerUnavailable(f"Ollama not reachable at {self.options.url} ({e}); is `ollama serve` running?") from e
        except requests.Timeout as e:               # connected, but no answer within timeout_s
            raise ScorerBusy(f"Ollama did not answer within {self.options.timeout_s} s ({e})") from e
        except requests.RequestException as e:
            raise ScorerUnavailable(f"Ollama not reachable at {self.options.url} ({e}); is `ollama serve` running?") from e
