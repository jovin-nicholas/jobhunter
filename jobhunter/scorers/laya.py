"""Scores jobs with Laya (local, fast): base Laya from Hugging Face, or a fine-tuned checkpoint folder.

The questions and input layout match the ones fine-tuned checkpoints are trained with, so such a model sees exactly
what it learned from.
"""
from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jobhunter.errors import ScorerError, ScorerUnavailable
from jobhunter.models import Job, Resume, ScoreResult
from jobhunter.registry import scorer
from jobhunter.scorers.skills import compare_skills

MAX_DESCRIPTION_CHARS = 8000

DECISION_QUESTION = {
    "type": "choice",
    "instructions": "Should the candidate be considered for this role?",
    "criteria": {
        "notify": "Strong fit, right level, and good alignment with the candidate profile.",
        "log": "Possible fit but borderline or uncertain; could merit later review.",
        "skip": "Weak fit, wrong level, or disqualifying mismatch.",
    },
}

SCORE_QUESTION = {
    "type": "score",
    "instructions": "How well does this candidate match the job, from 1 (no match) to 10 (exceptional)?",
    "criteria": [
        "1: disqualifying mismatch (wrong role, stack or seniority)",
        "2: very weak fit",
        "3: weak fit, few matching skills",
        "4: below-average fit, significant gaps",
        "5: possible fit, meaningful gaps",
        "6: reasonable fit, some gaps",
        "7: good fit, minor gaps",
        "8: strong fit, right seniority and core skills",
        "9: very strong fit",
        "10: exceptional fit",
    ],
}

# For checkpoints that answer the decision question directly; 8/6/3 land in the default notify/log/skip bands.
DECISION_SCORES = {"notify": 8, "log": 6, "skip": 3}


def build_state(resume_text: str, job: Job) -> str:
    return (f"Candidate profile:\n{resume_text}\n\n"
            f"Job title: {job.title}\n"
            f"Company: {job.company}\n\n"
            f"Job description:\n{(job.description or '')[:MAX_DESCRIPTION_CHARS]}")


def band_score(probs: list[float], notify_at: int, log_at: int) -> int:
    """The most likely score inside the band (notify / log / skip) that holds the most probability.

    The plain average is pulled toward the middle when the model is unsure (60% on 7-8 plus some weight on 5
    averages below 7), so it rarely reaches the notify band.
    """
    scores = range(1, len(probs) + 1)
    bands = [[s for s in scores if s >= notify_at],
             [s for s in scores if log_at <= s < notify_at],
             [s for s in scores if s < log_at]]
    best = max((b for b in bands if b), key=lambda b: sum(probs[s - 1] for s in b))
    return max(best, key=lambda s: probs[s - 1])


def _read_training_info(model: str) -> tuple[str, dict[str, int]]:
    config = Path(model) / "rl_agent_config.json"
    if not config.is_file():
        return "question", {"notify_at": 7, "log_at": 5}
    training = json.loads(config.read_text(encoding="utf-8")).get("training", {})
    method = (training.get("decision_method") or {}).get("own", "question")
    thresholds = training.get("score_thresholds") or {"notify_at": 7, "log_at": 5}
    return method, {"notify_at": int(thresholds["notify_at"]), "log_at": int(thresholds["log_at"])}


def _load_agent(model: str, device: str | None) -> Any:
    try:
        from laya import Agent
    except ImportError as e:
        raise ScorerUnavailable("the laya package is not installed (pip install -r requirements-laya.txt)") from e
    try:
        return Agent(model, device=device)
    except Exception as e:
        raise ScorerUnavailable(f"could not load Laya model {model!r}: {e}") from e


def _is_folder(model: str) -> bool:
    """A path rather than a Hugging Face id ("org/name")."""
    return model.startswith(("/", "~", ".")) or model.count("/") != 1 or os.path.exists(os.path.expanduser(model))


@scorer("laya")
class LayaScorer:
    @dataclass
    class Options:
        model: str                    # fine-tuned checkpoint folder, or a Hugging Face id like convaiinnovations/laya
        device: str | None = None     # cuda / mps / cpu; default picks the best available

    def __init__(self, options: dict, agent_factory: Callable[[str, str | None], Any] | None = None):
        self.options = self.Options(**options)
        self.model = os.path.expanduser(self.options.model)
        self.method, self.thresholds = _read_training_info(self.model)
        self._factory = agent_factory or _load_agent
        self._agent = None
        self._load_error: ScorerError | None = None

    def check(self) -> str | None:
        """For check-config: why Laya cannot run yet, or None. A Hugging Face id is downloaded on first use."""
        if importlib.util.find_spec("laya") is None:
            return "the laya package is not installed; run `.venv/bin/pip install -r requirements-laya.txt`"
        if _is_folder(self.options.model) and not os.path.isdir(self.model):
            return (f"model folder {self.model} not found; use a Hugging Face id such as convaiinnovations/laya "
                    "(downloaded on the first run) or the folder of a fine-tuned Laya")
        return None

    def _get_agent(self) -> Any:
        # Loading takes seconds and ~1 GB; do it once per run, and after a failure fail fast on every later job.
        if self._load_error is not None:
            raise self._load_error
        if self._agent is None:
            try:
                self._agent = self._factory(self.model, self.options.device)
            except ScorerError as e:
                self._load_error = e
                raise
        return self._agent

    def score(self, job: Job, resume: Resume) -> ScoreResult:
        agent = self._get_agent()
        question = ({"match_score": SCORE_QUESTION} if self.method == "from_score"
                    else {"decision": DECISION_QUESTION})
        try:
            answers = agent.system_one(build_state(resume.text, job), question)["answers"]
        except Exception as e:
            raise ScorerError(f"Laya prediction failed: {e}") from e
        try:
            if self.method == "from_score":
                answer = answers["match_score"]
                probs = [float(answer["probabilities"][str(i)]) for i in range(len(answer["probabilities"]))]
                score = band_score(probs, **self.thresholds)
            else:
                answer = answers["decision"]
                score = DECISION_SCORES[answer["choice"]]
        except (KeyError, TypeError, ValueError) as e:
            raise ScorerError(f"Laya's answer has an unexpected shape ({type(e).__name__}: {e})") from e
        confidence = answer.get("answer_confidence")
        matched, gaps = compare_skills(f"{job.title} {job.description}", resume.text)
        detail = f", confidence {confidence:.2f}" if isinstance(confidence, (int, float)) else ""
        return ScoreResult(score=score, model=f"laya:{self.options.model}",
                           reasoning=f"Laya rated this {score}/10 ({self.method}{detail}).",
                           matched_skills=matched, keyword_gaps=gaps, confidence=confidence)
