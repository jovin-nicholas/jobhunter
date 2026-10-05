"""A job posting condensed to the parts that decide fit, in importance order: requirements, preferred,
responsibilities, then anything else. Company background, benefits, pay and legal text are dropped.

Rules only and the standard library only, so the training notebook uses this same file: a model is trained and
scored on identical inputs. When a posting is still too long, Laya cuts from the end, which is the least important
part.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SECTIONS = ("requirements", "preferred", "responsibilities", "other")
MAX_HEADING_WORDS = 8
MAX_HEADING_CHARS = 70
MAX_PHRASE_HEADING_WORDS = 6

# A heading's section: the first kind with a phrase the heading contains.
_KINDS = (
    ("drop", ("equal opportunity", "eeo", "e-verify", "accommodation", "privacy", "benefit", "perk", "what we offer",
              "compensation", "salary", "pay range", "pay transparency", "about us", "about the company",
              "who we are", "our mission", "our culture", "our values", "why join", "why work", "life at",
              "how to apply", "application process", "next steps", "disclaimer", "notice to")),
    ("preferred", ("preferred", "nice to have", "nice-to-have", "bonus", "desired", "desirable", "extra credit")),
    ("requirements", ("requirement", "qualification", "must have", "must-have", "what you bring", "what you'll bring",
                      "what we're looking for", "what we are looking for", "what you'll need", "what you need",
                      "you'll need", "about you", "who you are", "skills", "experience", "education", "minimum",
                      "basic", "required", "ideal candidate")),
    ("responsibilities", ("responsibilit", "what you'll do", "what you will do", "duties", "the role", "role overview",
                          "day to day", "day-to-day", "in this role", "job description", "position summary",
                          "overview", "what you'll work on", "the opportunity", "your impact")),
)
_PHRASES = tuple(p for _, phrases in _KINDS for p in phrases)
_TAG = re.compile(r"^#?li-|^usd$|^ci/?cd$|e-?mail|contact|corp to corp|recruit", re.I)
_BOILERPLATE = re.compile(
    r"equal opportunity|without regard to|e-verify|reasonable accommodation|protected veteran|401\(k\)|"
    r"paid time off|\bpto\b|medical, dental|dental(,| and) vision|dental insurance|"
    r"health insurance (plan|coverage|benefits)|individuals with disabilities|disability status|"
    r"regardless of disability|pay range|salary range|pay transparency|"
    r"privacy (notice|policy)|background check|drug[- ]free", re.I)
_REQUIREMENT = re.compile(
    r"\b(\d+\+? ?(years?|yrs)|years? of|bachelor|master'?s|degree|ph\.?d|required|must have|proficien|"
    r"experience (with|in))\b", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n")


def _plain(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'")


def _label(line: str) -> str:
    return _plain(line).strip().strip("*#").strip()


def _shaped(line: str) -> bool:
    """Marked as a heading by its form: a colon, capitals, or markdown / bold."""
    raw, text = line.strip(), _label(line)
    letters = [c for c in text if c.isalpha()]
    return (text.endswith(":") or raw.startswith(("#", "**"))
            or (len(letters) > 3 and all(c.isupper() for c in letters)))


def is_heading(line: str) -> bool:
    text = _label(line)
    if (not text or len(text) > MAX_HEADING_CHARS or _TAG.search(text.rstrip(":").strip())
            or sum(c.isalpha() for c in text) < 4):
        return False
    words = text.split()
    if len(words) > MAX_HEADING_WORDS or text.endswith((".", ";", ",")):
        return False
    if _shaped(line):
        return True
    low = text.lower()
    return len(words) <= MAX_PHRASE_HEADING_WORDS and (low.startswith("about ")
                                                       or any(low.startswith(p.strip()) for p in _PHRASES))


def section_kind(heading: str) -> str:
    low = _label(heading).lower().rstrip(":")
    for kind, phrases in _KINDS:
        if any(p in low + " " for p in phrases):
            return kind
    # "About Acme": company background, once "about you" and "about the role" have matched above.
    return "drop" if low.startswith("about ") else "other"


def _label_only(line: str) -> bool:
    """A heading that is just a label ("The role", "Requirements", "Nice to have"), not a bullet worth keeping."""
    low = _label(line).lower().rstrip(":")
    return len(low.split()) <= 2 or any(low in (p.strip(), p.strip() + "s") for p in _PHRASES)


@dataclass
class Posting:
    sections: dict[str, str]       # only non-empty sections, in SECTIONS order
    headings: int
    unknown_headings: int

    @property
    def empty_requirements(self) -> bool:
        return "requirements" not in self.sections


def _route(line: str, current: str, out: dict[str, list[str]]) -> None:
    if _BOILERPLATE.search(line):
        return
    target = "requirements" if current in ("responsibilities", "other") and _REQUIREMENT.search(line) else current
    out[target].append(line.strip())


def split_posting(description: str) -> Posting:
    out: dict[str, list[str]] = {s: [] for s in SECTIONS}
    lines = [l for l in (description or "").splitlines() if l.strip()]
    headings = unknown = 0
    if not any(is_heading(l) for l in lines):
        for sentence in _SENTENCE_END.split(description or ""):
            if sentence.strip():
                _route(sentence, "other", out)
    else:
        current = "other"
        for line in lines:
            if is_heading(line):
                headings += 1
                current = section_kind(line)
                unknown += current == "other"
                # A short phrase line without heading marks ("Experience with Kubernetes") may be a bullet: keep it.
                if not _shaped(line) and not _label_only(line) and current not in ("drop", "other"):
                    _route(line, current, out)
                continue
            if current != "drop":
                _route(line, current, out)
    return Posting({s: "\n".join(out[s]) for s in SECTIONS if out[s]}, headings, unknown)


def condense(job) -> dict:
    """The state's `job` field: title, company, location (when known) and the posting's sections."""
    out = {"title": job.title, "company": job.company}
    if getattr(job, "location", ""):
        out["location"] = job.location
    out.update(split_posting(getattr(job, "description", "") or "").sections)
    return out
