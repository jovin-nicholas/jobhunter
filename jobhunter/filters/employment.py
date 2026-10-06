"""Skips contract and hourly-pay jobs. No board reports employment type, so the title and description are read for
wording that signals one: a title word like "contract" or "temp", an hourly rate stated in dollars, a fixed-term
phrase such as "6-month contract", or an explicit "employment type: contract" label. A skip is final, so each rule
asks for wording that names the arrangement: a bare "1099" (as in "Form 1099 processing"), a number per hour without
a currency in the description ("10,000/hr transactions") or "smart contract" never skips. In the title a number per
hour is pay ("Data Engineer 65/hr W2")."""
from __future__ import annotations

import re

from jobhunter.models import KEEP, FilterResult, Job, skip
from jobhunter.settings import EmploymentFilterSettings
from jobhunter.text_match import has_term, word_text

_NUM_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"

# Whole title words that, alone, mean the role is a contract: not "contracts" (a role about contracts, not one).
# More specific phrases are listed before the bare "contract" so a match says why, not just that "contract" occurred.
_CONTRACT_TITLE_TERMS = ("c2c", "corp to corp", "w2 contract", "contractor", "temporary", "freelance", "contract")
# "temp" alone is often temperature ("Temp Sensor Firmware Engineer"): it counts in a title only as a label, in
# brackets or after a dash or comma at the end ("Software Engineer (Temp)", "- Temp"), or before an employment word
# ("Temp Role", "Temp Position").
_TEMP_TITLE_RE = re.compile(r"\btemp\b(?=\s+(?:role|position|assignment|job|worker|contract)\b)"
                            r"|[(\[]\s*temp\s*[)\]]|[-\u2013,|]\s*temp\s*$", re.I)
# Phrases safe to match anywhere in the title or description (unlike the bare word "contract", which also turns up
# in "manage vendor contracts" or "smart contracts"). "1099" counts only in phrasing about the arrangement, never bare
# ("Form 1099 processing"); "contractor" only as a role ("as a contractor"), never "contractors we work with".
_CONTRACT_PHRASE_TERMS = ("contract role", "contract position", "contract opportunity", "contract assignment",
                          "contractor role", "contractor position", "as a contractor", "independent contractor",
                          "1099 contract", "on 1099", "1099 only", "1099 or c2c", "c2c or 1099",
                          "c2c", "corp to corp", "w2 contract", "w2 only", "only w2")
# Blockchain work, not an employment contract: masked before the bare title word "contract" is looked for.
_SMART_CONTRACT_TERMS = ("smart contracts", "smart contract")
# Skipped only when contract_to_hire is "exclude"; these alone never skip otherwise.
_CONTRACT_TO_HIRE_TERMS = ("contract to hire", "cth", "temp to perm", "temp to hire")
# The same phrases in raw text, spaced or hyphenated ("contract-to-hire"), for masking before the regexes run.
_CONTRACT_TO_HIRE_RE = re.compile(
    r"\b(?:" + "|".join(r"[\s-]+".join(map(re.escape, t.split())) for t in _CONTRACT_TO_HIRE_TERMS) + r")\b", re.I)

_MONTH_CONTRACT_RE = re.compile(rf"\b(?:\d{{1,2}}|{_NUM_WORDS})[\s-]+months?\s+contract\b", re.I)
# "Duration: 6 months", "Duration: 12+ Months with possible extension": a contract length. Not when a program word
# follows within 4 words ("Duration: 12 months onboarding program", "Duration: 6 months of paid training").
_DURATION_RE = re.compile(r"\bduration\s*[:\-]\s*(?:contract\b|\d{1,2}\+?\s*months?[\s,(\-]*contract\b"
                          r"|\d{1,2}\+?\s*months?\b(?:\s*\+)?)", re.I)
_PROGRAM_WORDS = {"program", "onboarding", "training", "rotation", "fellowship", "residency", "apprenticeship",
                  "internship"}
_PROGRAM_AFTER = 4
_SENTENCE_END = re.compile(r"[.!?;\n](?:\s|$)|[\n\u2022]")
_TYPE_RE = re.compile(r"\b(?:employment|job|position)\s*type\s*[:\-]\s*(?:contract|temporary)\b", re.I)
_CONTRACT_REGEXES = (_MONTH_CONTRACT_RE, _DURATION_RE, _TYPE_RE)


def _a_program_length(raw_text: str, m: re.Match) -> bool:
    """A "Duration: N months" match followed within a few words of the same sentence by a program word: a program,
    not a contract ("Duration: 6 months. Training provided" is still a contract)."""
    if m.re is not _DURATION_RE or "contract" in m.group(0).lower():
        return False
    sentence = _SENTENCE_END.split(raw_text[m.end():], maxsplit=1)[0]
    after = re.findall(r"[a-z]+", sentence.lower())[:_PROGRAM_AFTER]
    return bool(set(after) & _PROGRAM_WORDS)

# A dollar amount tied to hour/hr: "$55/hr", "$50-60 per hour", "hourly rate of $40", "$40 hourly", "USD 40 per
# hour", "40 USD per hour". The currency is required: "processes 10,000/hr transactions", "500 per hour SLA" and
# "overtime of 1.5 per hour" are not pay rates. Boilerplate like "the salary or hourly rate offered" has no number.
_NUM = r"\d[\d,]*(?:\.\d+)?"
_MONEY = rf"(?:\$\s*{_NUM}|usd\s*\$?\s*{_NUM}|{_NUM}\s*usd)"
_PER_HOUR = r"\s*(?:/\s*|\s+per\s+)(?:hrs?|hours?)\b"
# In a title a number per hour is pay even without a currency ("Data Engineer 65/hr W2"); "24/7" is not per hour.
_TITLE_HOURLY_RE = re.compile(rf"(?:\$\s*)?{_NUM}(?:\s*-\s*\$?\s*{_NUM})?{_PER_HOUR}", re.I)
_HOURLY_RE = re.compile(
    rf"{_MONEY}\s*(?:-\s*\$?\s*{_NUM}\s*(?:usd\s*)?)?{_PER_HOUR}"
    rf"|hourly\s+rate\s+of\s+{_MONEY}"
    rf"|{_MONEY}\s+hourly\b",
    re.I)

_INTERN_TITLE_TERMS = ("intern", "internship", "co-op", "coop")


def _mask_cth(words: str) -> str:
    """Removes contract-to-hire phrases from already-`word_text`-normalized text, so a generic term check does not
    fire on one of their words ("contract", "temp") when contract_to_hire is "keep"."""
    for term in _CONTRACT_TO_HIRE_TERMS:
        words = words.replace(word_text(term), " ")
    return words


def _mask_smart_contract(words: str) -> str:
    """Removes "smart contract(s)" from `word_text` text, so the title word "contract" does not fire on it."""
    for term in _SMART_CONTRACT_TERMS:
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
            match = self._contract_match(title, text, raw_text, (job.title or "").strip())
            if match:
                return skip(f"contract: {match}")

        if "hourly" in self.s.exclude:
            if self.s.hourly_internships == "keep" and any(has_term(title, t) for t in _INTERN_TITLE_TERMS):
                return KEEP
            m = _TITLE_HOURLY_RE.search(job.title or "") or _HOURLY_RE.search(raw_text)
            if m:
                return skip(f"hourly pay: {m.group(0).strip().lower()}")
        return KEEP

    def _contract_match(self, title: str, text: str, raw_text: str, raw_title: str) -> str | None:
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
            raw_title = _mask_cth_raw(raw_title).strip()
        title = _mask_smart_contract(title)
        for term in _CONTRACT_TITLE_TERMS:
            if has_term(title, term):
                return term
        if _TEMP_TITLE_RE.search(raw_title):
            return "temp"
        for term in _CONTRACT_PHRASE_TERMS:
            if has_term(text, term):
                return term
        for regex in _CONTRACT_REGEXES:
            for m in regex.finditer(raw_text):
                if not _a_program_length(raw_text, m):
                    return m.group(0).strip().lower()
        return None
