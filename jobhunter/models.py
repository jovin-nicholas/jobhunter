"""Data types shared by boards, filters, resumes, scorers and the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

DECISIONS = ("notify", "log", "skip")


@dataclass
class Job:
    id: str                       # prefixed with the board name, e.g. "dice_<id>"
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    posted_at: str = ""
    source: str = ""              # board name
    ats: str = ""
    description_is_snippet: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Resume:
    id: str                       # file name, e.g. "backend.pdf"
    text: str                     # the candidate's name already removed
    keywords: tuple[str, ...] = ()


@dataclass
class ScoreResult:
    score: int                    # 1-10
    model: str
    decision: str = ""            # set by the scorer chain from the configured thresholds
    reasoning: str = ""
    matched_skills: list[str] = field(default_factory=list)
    keyword_gaps: list[str] = field(default_factory=list)
    role_type: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class FilterResult:
    keep: bool
    reason: str | None = None


KEEP = FilterResult(True)


def skip(reason: str) -> FilterResult:
    return FilterResult(False, reason)


def decide(score: int, notify_at: int, log_at: int) -> str:
    if score >= notify_at:
        return "notify"
    if score >= log_at:
        return "log"
    return "skip"


def _never_known(job_id: str) -> bool:
    return False


def _forget(job_id: str) -> None:
    pass


@dataclass
class SearchContext:
    queries: list[str]
    locations: list[str]
    max_age_hours: int
    http: Any                     # jobhunter.http.Http
    log: Callable[[str], None] = print
    discovery: Any = None         # jobhunter.discovery.Discovery: ATS job links, for boards with discover: true
    is_known: Callable[[str], bool] = _never_known   # True when the store already finished this job id
    # Records a discovered posting that is too old, so later runs skip it without downloading it again.
    mark_stale: Callable[[str], None] = _forget
    # Records a discovered posting that no longer exists (404/410, or a page without the job), so it is not requested again.
    mark_gone: Callable[[str], None] = _forget
