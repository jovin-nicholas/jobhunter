"""Skips jobs whose title level or required years are outside the configured range."""
from __future__ import annotations

from jobhunter.filters.years import required_years
from jobhunter.models import KEEP, FilterResult, Job, skip
from jobhunter.settings import SeniorityFilterSettings
from jobhunter.text_match import has_term, word_text

LEVEL_TERMS = {
    "intern": ["intern", "internship", "co-op", "coop"],
    "entry": ["new grad", "new graduate", "entry level", "junior", "jr", "associate", "graduate",
              "engineer i", "developer i"],
    "mid": ["mid level", "intermediate", "engineer ii", "developer ii"],
    "senior": ["senior", "sr", "lead", "engineer iii", "developer iii"],
    "staff": ["staff", "principal", "distinguished", "architect", "fellow"],
}

class SeniorityFilter:
    name = "seniority"

    def __init__(self, settings: SeniorityFilterSettings):
        self.levels = set(settings.levels)
        self.max_years = settings.max_years_required

    def check(self, job: Job) -> FilterResult:
        title = word_text(job.title)
        found = [level for level, terms in LEVEL_TERMS.items() if any(has_term(title, t) for t in terms)]
        if found and not self.levels.intersection(found):
            return skip(f"title level {'/'.join(found)} not in {', '.join(sorted(self.levels))}: {job.title}")
        if self.max_years is not None:
            years = required_years(f"{job.title} {job.description}", company=job.company)
            if years is not None and years > self.max_years:
                return skip(f"requires {years}+ years (max {self.max_years})")
        return KEEP
