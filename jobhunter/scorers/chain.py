"""Tries scorers in order; errors and rate limits fall through to the next one."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jobhunter.errors import RateLimited, ScorerBusy, ScorerError, ScorerUnavailable
from jobhunter.models import Job, Resume, ScoreResult, decide
from jobhunter.settings import Decisions


@dataclass
class ChainOutcome:
    result: ScoreResult | None
    status: str                   # "scored", "error_429_retry", "error_unavailable" or "error_scorer"
    errors: list[str] = field(default_factory=list)
    first_failures: list[str] = field(default_factory=list)   # failures of scorers not seen failing before this run
    notes: list[str] = field(default_factory=list)            # one-off lines for the run log (a scorer switched off)


# A scorer unavailable this many jobs in a row (Ollama not running, say) is skipped for the rest of the run, rather
# than waited on for every job. A busy one (slow, or a 5xx) is running, so it is not counted, and any answer at all
# (a garbled one, a rate limit) resets the count.
MAX_UNAVAILABLE_IN_A_ROW = 3


class ScorerChain:
    def __init__(self, scorers: list[tuple[str, Any]], decisions: Decisions):
        self.scorers = scorers
        self.decisions = decisions
        self._failed: set[str] = set()      # scorers whose failure has been reported, so each is noted once a run
        self._unavailable: dict[str, int] = {}  # ScorerUnavailable in a row, per scorer
        self._off: set[str] = set()         # scorers skipped for the rest of the run

    def score(self, job: Job, resume: Resume) -> ChainOutcome:
        errors, notes, rate_limited, unavailable = [], [], False, False
        for name, scorer in self.scorers:
            if name in self._off:
                unavailable = True
                errors.append(f"{name}: skipped this run (unavailable {MAX_UNAVAILABLE_IN_A_ROW} times in a row)")
                continue
            try:
                result = scorer.score(job, resume)
            except RateLimited as e:
                rate_limited = True
                errors.append(f"{name}: rate limited ({e})")
                self._unavailable[name] = 0
                continue
            except ScorerBusy as e:
                unavailable = True
                errors.append(f"{name}: unavailable ({e})")
                continue
            except ScorerUnavailable as e:
                unavailable = True
                errors.append(f"{name}: unavailable ({e})")
                self._unavailable[name] = self._unavailable.get(name, 0) + 1
                if self._unavailable[name] >= MAX_UNAVAILABLE_IN_A_ROW:
                    self._off.add(name)
                    notes.append(f"note: {name} was unavailable {MAX_UNAVAILABLE_IN_A_ROW} times in a row ({e}); "
                                 "skipped for the rest of this run")
                continue
            except ScorerError as e:
                errors.append(f"{name}: {e}")
                self._unavailable[name] = 0
                continue
            except Exception as e:
                # A bug or an unexpected reply in one scorer must not stop the others from trying, and is not the
                # job's fault (a wrongly typed option, say): the job is tried again rather than given up on.
                unavailable = True
                errors.append(f"{name}: {type(e).__name__}: {e}")
                continue
            self._unavailable[name] = 0
            if not result.decision:
                # Every scored result is judged by the same thresholds, whatever its own idea of the decision.
                result.decision = decide(result.score, self.decisions.notify_at, self.decisions.log_at)
                least = self.decisions.min_confidence
                if result.decision == "notify" and least is not None and result.confidence is not None \
                        and result.confidence < least:
                    result.decision = "log"         # a notify the scorer is unsure of is kept for review, not sent
            # A scorer that decided itself (Laya's alert method, from its checkpoint's cut-offs) is used as given.
            # A scorer that failed before a later one answered is noted once a run, not hidden behind the fallback.
            first = [e for e in errors if e.split(":", 1)[0] not in self._failed]
            self._failed.update(e.split(":", 1)[0] for e in errors)
            return ChainOutcome(result, "scored", errors, first, notes)
        # Rate limits and outages are retried on the next run. A job every scorer failed on (a garbled answer, say)
        # is retried for 24 hours from when it was first saved, then final (the pipeline makes it error_terminal).
        status = "error_429_retry" if rate_limited else "error_unavailable" if unavailable else "error_scorer"
        return ChainOutcome(None, status, errors, notes=notes)
