"""Skips contract and hourly-pay jobs. No board reports employment type, so the title and description are read for
wording that signals one: a title word like "contract" or "temp", a stated hourly rate, a fixed-term phrase such as
"6-month contract", or an explicit "employment type: contract" label."""
from __future__ import annotations

import re

from jobhunter.models import KEEP, FilterResult, Job, skip
from jobhunter.settings import EmploymentFilterSettings
from jobhunter.text_match import has_term, word_text

_NUM_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"

# Whole title words that, alone, mean the role is a contract: not "contracts" (a role about contracts, not one).
# More specific phrases are listed before the bare "contract" so a match says why, not just that "contract" occurred.
_CONTRACT_TITLE_TERMS = ("c2c", "corp to corp", "1099", "w2 contract", "contractor", "temp", "temporary", "freelance",
                         "contract")
# Phrases safe to match anywhere in the title or description (unlike the bare word "contract", which also turns up
# in "manage vendor contracts" or "smart contracts").
_CONTRACT_PHRASE_TERMS = ("contract role", "contract position", "contract opportunity", "contract assignment",
                          "c2c", "corp to corp", "1099", "w2 contract", "w2 only", "only w2")
# Skipped only when contract_to_hire is "exclude"; these alone never skip otherwise.
_CONTRACT_TO_HIRE_TERMS = ("contract to hire", "cth", "temp to perm", "temp to hire")
# The same phrases in raw text, spaced or hyphenated ("contract-to-hire"), for masking before the regexes run.
_CONTRACT_TO_HIRE_RE = re.compile(
    r"\b(?:" + "|".join(r"[\s-]+".join(map(re.escape, t.split())) for t in _CONTRACT_TO_HIRE_TERMS) + r")\b", re.I)

_MONTH_CONTRACT_RE = re.compile(rf"\b(?:\d{{1,2}}|{_NUM_WORDS})[\s-]+months?\s+contract\b", re.I)
_DURATION_RE = re.compile(r"\bduration\s*[:\-]\s*(?:contract|\d{1,2}\+?\s*months?)\b", re.I)
_TYPE_RE = re.compile(r"\b(?:employment|job|position)\s*type\s*[:\-]\s*(?:contract|temporary)\b", re.I)
_CONTRACT_REGEXES = (_MONTH_CONTRACT_RE, _DURATION_RE, _TYPE_RE)

# A number tied to hour/hr: "$55/hr", "$50-60 per hour", "hourly rate of $40", "$40 hourly", "USD 40 per hour".
# Boilerplate like "the salary or hourly rate offered" has no number, so it never matches.
_NUM = r"\$?\d[\d,]*(?:\.\d+)?"
_HOURLY_RE = re.compile(
    rf"(?:usd\s*)?{_NUM}\s*(?:-\s*\$?\d[\d,]*(?:\.\d+)?\s*)?(?:/\s*|\s+per\s+)(?:hrs?|hours?)\b"
    rf"|hourly\s+rate\s+of\s+{_NUM}"
    rf"|{_NUM}\s+hourly\b",
    re.I)

_INTERN_TITLE_TERMS = ("intern", "internship", "co-op", "coop")


def _mask_cth(words: str) -> str:
    """Removes contract-to-hire phrases from already-`word_text`-normalized text, so a generic term check does not
    fire on one of their words ("contract", "temp") when contract_to_hire is "keep"."""
    for term in _CONTRACT_TO_HIRE_TERMS:
        words = words.replace(word_text(term), " ")
    return words


def _mask_cth_raw(text: str) -> str:
    """`_mask_cth` for raw text, so "employment type: contract to hire" or "6-month contract-to-hire" does not match
    the label and duration regexes either."""
    return _CONTRACT_TO_HIRE_RE.sub(" ", text)


class EmploymentFilter:
    name = "employment"

    def __init__(self, settings: EmploymentFilterSettings):
        self.s = settings

    def check(self, job: Job) -> FilterResult:
        title = word_text(job.title)
        text = word_text(f"{job.title} {job.description}")
        raw_text = f"{job.title} {job.description}".lower()

        if "contract" in self.s.exclude:
            match = self._contract_match(title, text, raw_text)
            if match:
                return skip(f"contract: {match}")

        if "hourly" in self.s.exclude:
            if self.s.hourly_internships == "keep" and any(has_term(title, t) for t in _INTERN_TITLE_TERMS):
                return KEEP
            m = _HOURLY_RE.search(raw_text)
            if m:
                return skip(f"hourly pay: {m.group(0).strip().lower()}")
        return KEEP

    def _contract_match(self, title: str, text: str, raw_text: str) -> str | None:
        if self.s.contract_to_hire == "exclude":
            for term in _CONTRACT_TO_HIRE_TERMS:
                if has_term(text, term):
                    return term
        else:
            # Kept: "contract to hire"/"temp to hire"/"temp to perm" must not trip the bare "contract"/"temp" terms
            # below just because those words are their first word.
            title = _mask_cth(title)
            text = _mask_cth(text)
            raw_text = _mask_cth_raw(raw_text)
        for term in _CONTRACT_TITLE_TERMS:
            if has_term(title, term):
                return term
        for term in _CONTRACT_PHRASE_TERMS:
            if has_term(text, term):
                return term
        for regex in _CONTRACT_REGEXES:
            m = regex.search(raw_text)
            if m:
                return m.group(0).strip().lower()
        return None
