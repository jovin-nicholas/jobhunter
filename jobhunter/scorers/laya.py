"""Scores jobs with Laya (local, fast): base Laya from Hugging Face, or a fine-tuned checkpoint folder.

The questions and input layout match the ones fine-tuned checkpoints are trained with, so such a model sees exactly
what it learned from.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jobhunter.errors import ScorerError, ScorerUnavailable
from jobhunter.models import Job, Resume, ScoreResult
from jobhunter.posting import condense
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

# Training methods this scorer knows how to answer. A checkpoint's own.method outside this set is unsupported rather
# than silently scored with the generic decision question.
SUPPORTED_METHODS = {"question", "from_score", "alert", "questions"}

# The questions method (training/laya_train.py): two gates about the job alone, then the fit score when both pass.
QUESTION_IDS = ("stack_role", "dealbreaker", "fit")
SUMMARY_RANGES = {"expected": (1.0, 10.0), "p_good": (0.0, 1.0)}   # the fit summary's scale, for its cut-offs
DEFAULT_STACK_ROLES = ("backend", "frontend", "fullstack", "ai", "data", "infra", "other")


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


def job_state(job: Job) -> dict:
    """The questions method's input for the questions about the job alone (stack role, dealbreaker)."""
    return {"job": condense(job)}


def alert_state(resume_text: str, job: Job) -> dict:
    """The alert method's input: resume first, so a long posting loses only its least important end."""
    return {"candidate": resume_text, "job": condense(job)}


def _read_questions_info(model: str) -> tuple[dict, list[str]]:
    """The questions checkpoint's questions, inputs, gates and cut-offs, and the parts it lacks (none when complete)."""
    config = Path(model) / "rl_agent_config.json"
    training = json.loads(config.read_text(encoding="utf-8")).get("training", {}) if config.is_file() else {}
    questions, inputs, gates, cutoffs = (training.get(k) for k in ("questions", "inputs", "gates", "cutoffs"))
    questions, inputs = (v if isinstance(v, dict) else {} for v in (questions, inputs))
    missing = [f"questions.{q}" for q in QUESTION_IDS if not isinstance(questions.get(q), dict)]
    for q in QUESTION_IDS:
        spec = inputs.get(q)
        if not isinstance(spec, dict) or spec.get("state") not in STATES or not isinstance(spec.get("max_len"), int):
            missing.append(f"inputs.{q}")
    if not isinstance(gates, dict) or not {"stack_roles", "dealbreaker_at"} <= set(gates):
        missing.append("gates")
    if not isinstance(cutoffs, dict) or not {"summary", "alert_at", "save_at"} <= set(cutoffs) \
            or cutoffs["summary"] not in SUMMARY_RANGES:
        missing.append("cutoffs")
    return {"questions": questions, "inputs": inputs, "gates": gates, "cutoffs": cutoffs}, missing


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def failed_gates(role: str, deal_p: float, gates: dict) -> list[str]:
    """Why the questions method skips a job without asking the fit question; empty when both gates pass."""
    failed = [] if role in gates["stack_roles"] else [f"stack role {role}"]
    return failed + ([f"likely dealbreaker ({deal_p:.0%})"] if deal_p >= gates["dealbreaker_at"] else [])


def _floor2(value: float) -> float:
    """Floored to 2 decimals for display; the tiny epsilon keeps 6.07 (stored as 6.06999...) at 6.07."""
    return math.floor(value * 100 + 1e-9) / 100


def summarise_fit(probs: list[float], summary: str) -> float:
    """`expected` = 1 + sum(i * p_i) over laya's 0-based levels (a 1-10 score); `p_good` = P(score 7-10)."""
    return 1.0 + sum(i * p for i, p in enumerate(probs)) if summary == "expected" else sum(probs[6:])


def _read_alert_info(model: str) -> dict | None:
    config = Path(model) / "rl_agent_config.json"
    if not config.is_file():
        return None
    alert = json.loads(config.read_text(encoding="utf-8")).get("training", {}).get("alert")
    if not isinstance(alert, dict) or not {"question", "alert_at", "save_at"} <= set(alert):
        return None
    return alert


STATES = {"job": lambda resume_text, job: job_state(job), "candidate_job": alert_state}


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
        alert_at: float | None = None # alert and questions methods: overrides the checkpoint's alert cut-off
        save_at: float | None = None  # ... and its save-for-later cut-off (below alert_at); see docs/settings.md
        stack_roles: list[str] | None = None  # questions method only: the stack roles that pass the role gate
        dealbreaker_at: float | None = None   # questions method only: skip when P(dealbreaker) is at least this
        summary: str | None = None            # questions method only: "expected" (1-10) or "p_good" (0-1)

    def __init__(self, options: dict, agent_factory: Callable[[str, str | None], Any] | None = None):
        self.options = self.Options(**options)
        self.model = os.path.expanduser(self.options.model)
        self.method, self.thresholds = _read_training_info(self.model)
        self._factory = agent_factory or _load_agent
        self._agent = None
        self._load_error: ScorerError | None = None
        self.alert = _read_alert_info(self.model) if self.method == "alert" else None
        o = self.options
        if self.method not in ("alert", "questions") and (o.alert_at is not None or o.save_at is not None):
            raise ValueError("alert_at and save_at apply only to Laya checkpoints trained on the alert question or "
                             "the questions method")
        if self.method != "questions" and (o.stack_roles is not None or o.dealbreaker_at is not None
                                           or o.summary is not None):
            raise ValueError("stack_roles, dealbreaker_at and summary apply only to Laya checkpoints trained on the "
                             "questions method")
        self.questions, self.questions_missing = (_read_questions_info(self.model) if self.method == "questions"
                                                  else ({}, []))
        if self.method == "questions":
            self.gates, self.fit_cutoffs = self._questions_settings()
        if self.method == "alert":
            alert_at, save_at = self.cutoffs()
            if alert_at is not None and (save_at is None or not 0 < save_at < alert_at < 1):
                raise ValueError(f"save_at ({save_at}) must be below alert_at ({alert_at}), both between 0 and 1; "
                                  "set both alert_at and save_at when the checkpoint has none")

    def _questions_settings(self) -> tuple[dict, dict]:
        """The gates and cut-offs in use: the checkpoint's, with jobhunter.yaml's overrides checked and applied."""
        o, stored = self.options, self.questions
        if self.questions_missing:
            return {}, {}               # check() reports what is missing, and scoring is unavailable
        criteria = (stored["questions"]["stack_role"].get("criteria") or {})
        valid = [str(r).lower() for r in criteria] or list(DEFAULT_STACK_ROLES)
        roles = o.stack_roles if o.stack_roles is not None else stored["gates"]["stack_roles"]
        if not isinstance(roles, list) or not roles or not all(isinstance(r, str) for r in roles):
            raise ValueError(f"stack_roles must be a list of stack roles from: {', '.join(valid)}")
        unknown = [r for r in roles if r not in valid]
        if unknown:
            raise ValueError(f"unknown stack role(s) {', '.join(unknown)}; valid roles: {', '.join(valid)}")
        deal = o.dealbreaker_at if o.dealbreaker_at is not None else stored["gates"]["dealbreaker_at"]
        if not _is_number(deal) or not 0 < deal <= 1:
            raise ValueError(f"dealbreaker_at ({deal!r}) must be above 0 and at most 1")
        summary = o.summary if o.summary is not None else stored["cutoffs"]["summary"]
        if not isinstance(summary, str) or summary not in SUMMARY_RANGES:
            raise ValueError(f"summary ({summary!r}) must be one of: {', '.join(SUMMARY_RANGES)}")
        if summary != stored["cutoffs"]["summary"] and (o.alert_at is None or o.save_at is None):
            raise ValueError(f"the checkpoint's cut-offs are for its '{stored['cutoffs']['summary']}' summary; "
                             f"set both alert_at and save_at with summary '{summary}'")
        alert_at = o.alert_at if o.alert_at is not None else stored["cutoffs"]["alert_at"]
        save_at = o.save_at if o.save_at is not None else stored["cutoffs"]["save_at"]
        lo, hi = SUMMARY_RANGES[summary]
        if not (_is_number(alert_at) and _is_number(save_at) and lo <= save_at < alert_at <= hi):
            raise ValueError(f"save_at ({save_at!r}) must be below alert_at ({alert_at!r}), both between {lo:g} and "
                             f"{hi:g} for the '{summary}' summary")
        return ({"stack_roles": roles, "dealbreaker_at": deal},
                {"summary": summary, "alert_at": alert_at, "save_at": save_at})

    def check(self) -> str | None:
        """For check-config: why Laya cannot run yet, or None. A Hugging Face id is downloaded on first use."""
        if importlib.util.find_spec("laya") is None or importlib.util.find_spec("torch") is None:
            return "the laya package or PyTorch is not installed; run `.venv/bin/pip install -r requirements-laya.txt`"
        if _is_folder(self.options.model) and not os.path.isdir(self.model):
            return (f"model folder {self.model} not found; use a Hugging Face id such as convaiinnovations/laya "
                    "(downloaded on the first run) or the folder of a fine-tuned Laya")
        if self.method not in SUPPORTED_METHODS:
            return (f"{self.model}: trained with the '{self.method}' method, which jobhunter cannot score yet; "
                    "point laya at another checkpoint")
        if self.method == "alert" and self.alert is None:
            return f"{self.model}: alert checkpoint has no question or cut-offs in rl_agent_config.json"
        if self.method == "questions" and self.questions_missing:
            return self._questions_problem()
        return None

    def _questions_problem(self) -> str:
        return f"{self.model}: questions checkpoint lacks {', '.join(self.questions_missing)} in rl_agent_config.json"

    def download_note(self) -> str | None:
        """For check-config: a Hugging Face id not downloaded yet, which may also be a mistyped folder."""
        if _is_folder(self.options.model):
            return None
        hub = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or Path.home() / ".cache/huggingface") / "hub")
        if (hub / ("models--" + self.options.model.replace("/", "--"))).is_dir():
            return None
        return (f"laya: {self.options.model} is not downloaded yet; it is fetched from Hugging Face on the first run. "
                "If you meant a folder on this computer, that folder does not exist")

    def cutoffs(self) -> tuple[float | None, float | None]:
        stored = self.alert or {}
        alert_at = self.options.alert_at if self.options.alert_at is not None else stored.get("alert_at")
        save_at = self.options.save_at if self.options.save_at is not None else stored.get("save_at")
        return alert_at, save_at

    def describe(self) -> str | None:
        """For check-config: the alert or questions method's cut-offs (and gates) and where they come from."""
        if self.method == "questions":
            return self._describe_questions()
        if self.method != "alert":
            return None
        alert_at, save_at = self.cutoffs()
        if alert_at is None:
            return "laya: alert checkpoint without cut-offs; it cannot judge jobs"
        source = "overridden in jobhunter.yaml" if self.options.alert_at or self.options.save_at else "from the checkpoint"
        return f"laya: alert at {alert_at:.0%} fit or higher, save for later from {save_at:.0%} ({source})"

    def _describe_questions(self) -> str:
        if self.questions_missing:
            return "laya: questions checkpoint without gates or cut-offs; it cannot judge jobs"
        o, gates, cut = self.options, self.gates, self.fit_cutoffs

        def source(*overrides):
            return "overridden in jobhunter.yaml" if any(v is not None for v in overrides) else "from the checkpoint"

        roles = gates["stack_roles"]
        role_text = ", ".join(roles[:-1]) + f" or {roles[-1]}" if len(roles) > 1 else roles[0]
        shown = (lambda x: f"{x:.2f}") if cut["summary"] == "expected" else (lambda x: f"{x:.0%}")
        return (f"laya: questions gates: stack role {role_text}, dealbreaker below {gates['dealbreaker_at']:.0%} "
                f"({source(o.stack_roles, o.dealbreaker_at)}); fit summary {cut['summary']}, alert at "
                f"{shown(cut['alert_at'])} or higher, save for later from {shown(cut['save_at'])} "
                f"({source(o.summary, o.alert_at, o.save_at)})")

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

    def _score_alert(self, agent: Any, job: Job, resume: Resume) -> ScoreResult:
        alert_at, save_at = self.cutoffs()
        if self.alert is None or alert_at is None:
            raise ScorerUnavailable(f"{self.model}: alert checkpoint has no question or cut-offs in rl_agent_config.json")
        try:
            answer = agent.system_one(alert_state(resume.text, job), {"alert": self.alert["question"]})["answers"]["alert"]
        except Exception as e:
            raise ScorerError(f"Laya prediction failed: {e}") from e
        try:
            p = float(answer["noul"])
        except (KeyError, TypeError, ValueError) as e:
            raise ScorerError(f"Laya's alert answer has an unexpected shape ({type(e).__name__}: {e})") from e
        decision = "notify" if p >= alert_at else "log" if p >= save_at else "skip"
        matched, gaps = compare_skills(f"{job.title} {job.description}", resume.text)
        return ScoreResult(score=None, model=f"laya:{self.options.model}", decision=decision, probability=p,
                           reasoning=f"Laya: {p:.0%} fit (alert at {alert_at:.0%}, save at {save_at:.0%}).",
                           matched_skills=matched, keyword_gaps=gaps)

    def _ask(self, agent: Any, qid: str, job: Job, resume: Resume) -> dict:
        spec = self.questions["inputs"][qid]
        state = STATES[spec["state"]](resume.text, job)
        question = {qid: self.questions["questions"][qid]}
        try:
            return agent.system_one(state, question, max_len=spec["max_len"])["answers"][qid]
        except Exception as e:
            raise ScorerError(f"Laya prediction failed: {e}") from e

    def _score_questions(self, agent: Any, job: Job, resume: Resume) -> ScoreResult:
        # Matches training/laya_train.py's decide_from_answers (tests/test_laya.py checks they agree).
        matched, gaps = compare_skills(f"{job.title} {job.description}", resume.text)
        model = f"laya:{self.options.model}"
        try:
            role = str(self._ask(agent, "stack_role", job, resume)["choice"])
        except (KeyError, TypeError) as e:
            raise ScorerError(f"Laya's stack role answer has an unexpected shape ({type(e).__name__}: {e})") from e
        # A failed role gate already decides the skip, so the dealbreaker question is not asked (one call saved).
        failed = failed_gates(role, 0.0, self.gates)
        if not failed:
            try:
                deal_p = float(self._ask(agent, "dealbreaker", job, resume)["noul"])
            except (KeyError, TypeError, ValueError) as e:
                raise ScorerError(f"Laya's dealbreaker answer has an unexpected shape ({type(e).__name__}: {e})") from e
            failed = failed_gates(role, deal_p, self.gates)
        if failed:
            return ScoreResult(score=None, model=model, decision="skip", matched_skills=matched, keyword_gaps=gaps,
                               reasoning=f"Laya: skipped, {'; '.join(failed)}")
        fit = self._ask(agent, "fit", job, resume)
        try:
            probs = [float(fit["probabilities"][str(i)]) for i in range(len(fit["probabilities"]))]
        except (KeyError, TypeError, ValueError) as e:
            raise ScorerError(f"Laya's fit answer has an unexpected shape ({type(e).__name__}: {e})") from e
        cut = self.fit_cutoffs
        value = summarise_fit(probs, cut["summary"])
        decision = "notify" if value >= cut["alert_at"] else "log" if value >= cut["save_at"] else "skip"
        if cut["summary"] == "expected":
            # The stored score keeps its decimals (4, laya's own precision), so nothing is lost to rounding. The label
            # shows two, so a notify at 6.1 (above a 6.07 cut-off) does not read "6/10", which the default decisions
            # (notify at 7) would call a log.
            score = round(max(1.0, min(10.0, value)), 4)
            show = lambda v: f"{_floor2(v):.2f}"
            label = f"fit {show(value)}/10"
        else:
            score = None
            show = lambda v: f"{math.floor(v * 100 + 1e-9)}%"
            label = f"fit {show(value)}"
        # Values and cut-offs are floored to what is shown, and the side of the cut-off is said in words, so a job
        # just below a cut-off never reads as at or above it.
        alert, save = show(cut["alert_at"]), show(cut["save_at"])
        side = {"notify": f"at or above the alert cut-off {alert}",
                "log": f"below the alert cut-off {alert}, at or above the save cut-off {save}",
                "skip": f"below the save cut-off {save}"}[decision]
        return ScoreResult(score=score, model=model, decision=decision, probability=sum(probs[6:]), label=label,
                           reasoning=f"Laya: {label}, {side}; stack role {role}", matched_skills=matched,
                           keyword_gaps=gaps)

    def score(self, job: Job, resume: Resume) -> ScoreResult:
        if self.method not in SUPPORTED_METHODS:
            raise ScorerError(f"{self.model}: trained with the '{self.method}' method, which jobhunter cannot score "
                               "yet; point laya at another checkpoint")
        if self.method == "questions" and self.questions_missing:
            raise ScorerUnavailable(self._questions_problem())
        agent = self._get_agent()
        if self.method == "alert":
            return self._score_alert(agent, job, resume)
        if self.method == "questions":
            return self._score_questions(agent, job, resume)
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
