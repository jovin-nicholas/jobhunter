"""Data types shared by boards, filters, resumes, scorers and the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from jobhunter.fit_scale import fit_of, floor2, is_expected_label


@dataclass
class Job:
    id: str                       # unique across boards: usually the board name and its id ("dice_<id>"); some name
                                  # the site ("devto_", "top_amazon_") or the source ("getro_", "consider_")
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
    score: float | None            # 1-10 (Laya's questions method keeps its decimals); None for a probability
    model: str
    decision: str = ""            # set by the scorer chain from the thresholds, unless the scorer decided itself
    reasoning: str = ""
    matched_skills: list[str] = field(default_factory=list)
    keyword_gaps: list[str] = field(default_factory=list)
    role_type: str | None = None
    confidence: float | None = None
    probability: float | None = None   # P(good fit), for a scorer that answers yes/no
    label: str = ""                # how the result reads, when the score alone would misstate the decision


def score_label(result: ScoreResult, scales: Mapping[str, Any] | None = None) -> str:
    """How a result reads in alerts and logs: "8/10", "fit 72%" for a probability, or "n/a" for neither. An expected
    fit from a calibrated Laya checkpoint reads "fit 8.8/10 (raw 6.50)" (scaled 0-10, raw kept)."""
    scale = (scales or {}).get(result.model)
    if result.label:
        if scale is not None and result.score is not None and is_expected_label(result.label):
            return f"fit {scale.scale(result.score):.1f}/10 (raw {floor2(result.score):.2f})"
        return result.label
    if result.score is None and result.probability is None:
        return "n/a"
    if result.score is None and result.probability is not None:
        return f"fit {result.probability:.0%}"
    # Shared with jobhunter.tracker.records.fit_fields: a whole Laya score with no label is a decision placeholder
    # (fit_of), never scaled; any other unlabelled score is a real fit and is scaled like a labelled one.
    fit = fit_of(result.score, result.label, result.model)
    if scale is not None and fit is not None:
        return f"fit {scale.scale(fit):.1f}/10 (raw {floor2(fit):.2f})"
    return f"{result.score}/10"


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
    # Records a detail call that answered with no posting (null, or no title): a hiccup or a closed posting. It is
    # requested again on later runs, and remembered as gone once it has answered empty for RETRY_HOURS.
    mark_empty: Callable[[str], None] = _forget
    # The ATS boards that read discovered links this run (enabled, picked by --only, discover on); None: not known,
    # taken as every ATS board.
    discovering: frozenset[str] | set[str] | None = None
    # The run's data folder (settings.data_dir), for a board that needs a private working folder; None: not known.
    data_dir: Any = None
