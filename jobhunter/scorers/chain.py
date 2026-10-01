"""Tries scorers in order; errors and rate limits fall through to the next one."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jobhunter.errors import RateLimited, ScorerError, ScorerUnavailable
from jobhunter.models import Job, Resume, ScoreResult, decide
from jobhunter.settings import Decisions


@dataclass
class ChainOutcome:
    result: ScoreResult | None
    status: str                   # "scored", "error_429_retry", "error_unavailable" or "error_terminal"
    errors: list[str] = field(default_factory=list)


class ScorerChain:
    def __init__(self, scorers: list[tuple[str, Any]], decisions: Decisions):
        self.scorers = scorers
        self.decisions = decisions

    def score(self, job: Job, resume: Resume) -> ChainOutcome:
        errors, rate_limited, unavailable = [], False, False
        for name, scorer in self.scorers:
            try:
                result = scorer.score(job, resume)
            except RateLimited as e:
                rate_limited = True
                errors.append(f"{name}: rate limited ({e})")
                continue
            except ScorerUnavailable as e:
                unavailable = True
                errors.append(f"{name}: unavailable ({e})")
                continue
            except ScorerError as e:
                errors.append(f"{name}: {e}")
                continue
            except Exception as e:
                # A bug or an unexpected reply in one scorer must not stop the others from trying, and is not the
                # job's fault (a wrongly typed option, say): the job is tried again rather than given up on.
                unavailable = True
                errors.append(f"{name}: {type(e).__name__}: {e}")
                continue
            # Every scorer is judged by the same thresholds, whatever its own idea of the decision.
            result.decision = decide(result.score, self.decisions.notify_at, self.decisions.log_at)
            least = self.decisions.min_confidence
            if result.decision == "notify" and least is not None and result.confidence is not None \
                    and result.confidence < least:
                result.decision = "log"         # a notify the scorer is unsure of is kept for review, not sent
            return ChainOutcome(result, "scored", errors)
        # Rate limits and outages are retried on the next run; only a job no scorer could handle is final.
        status = "error_429_retry" if rate_limited else "error_unavailable" if unavailable else "error_terminal"
        return ChainOutcome(None, status, errors)
