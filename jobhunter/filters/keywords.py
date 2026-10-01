"""Skips jobs by title words, phrases, company names, or role groups (e.g. embedded: c++, rtos, firmware)."""
from __future__ import annotations

from jobhunter.models import KEEP, FilterResult, Job, skip
from jobhunter.settings import KeywordFilterSettings
from jobhunter.text_match import has_term, word_text


class KeywordFilter:
    name = "keywords"

    def __init__(self, settings: KeywordFilterSettings):
        self.s = settings

    def check(self, job: Job) -> FilterResult:
        title = word_text(job.title)
        company = word_text(job.company)
        text = word_text(f"{job.title} {job.description}")
        for term in self.s.exclude_companies:
            if has_term(company, term):
                return skip(f"excluded company: {term}")
        for term in self.s.exclude_title:
            if has_term(title, term):
                return skip(f"excluded title word: {term}")
        for phrase in self.s.exclude_phrases:
            if has_term(text, phrase):
                return skip(f"excluded phrase: {phrase}")
        for name, group in self.s.exclude_roles.items():
            hits = [t for t in dict.fromkeys(group.terms) if has_term(text, t)]
            if len(hits) >= group.min_matches:
                return skip(f"excluded role {name!r}: {', '.join(hits)}")
        return KEEP
