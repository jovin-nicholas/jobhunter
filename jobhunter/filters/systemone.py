"""Questions about a job that the rule-based filters cannot answer, asked of a local System One model.

Runs after the other filters, so only jobs they kept are asked. Each question is one request whose state holds only
the parts of the job it needs (title, an opening or the requirement lines); nothing about the candidate is sent. When
the model gives no answer, the job is kept.
"""
from __future__ import annotations

from jobhunter.filters.years import _sentences, _tokens
from jobhunter.models import KEEP, FilterResult, Job, skip
from jobhunter.systemone import SystemOneClient

THRESHOLD = 0.5
OPENING_CHARS = 1500
EXCERPT_CHARS = 1500
REQUIREMENT_WORDS = {"experience", "years", "degree", "bachelor", "bachelor's", "master", "master's", "phd",
                     "enrolled", "enrollment", "enrolment", "student", "students", "pursuing", "qualification",
                     "qualifications", "requirement", "requirements", "required", "intern", "internship", "co-op",
                     "sophomore", "junior", "rising"}

SOFTWARE_ROLE = {
    "type": "noul",
    "instructions": "Is the main work of `job` writing production software as an engineer?",
    "criteria": {
        "true": "Software, backend, frontend, full-stack, mobile, data engineering, machine learning or AI engineering, "
                "platform, infrastructure, DevOps, SRE, or forward-deployed engineering where the person writes and "
                "ships code.",
        "false": "Product management, business or data analysis, data science focused on analysis, quantitative "
                 "research, finance, sales, consulting, operations, hardware, design, or a training program, even when "
                 "the job mentions coding, data or technology.",
    },
}
STUDENTS_ONLY = {
    "type": "noul",
    "instructions": "Is `job` open only to current students?",
    "criteria": {
        "true": "An internship or co-op, a fellowship for students, or a role that requires current enrollment in a "
                "degree program.",
        "false": "A full-time or contract role open to people who are not students, including new-graduate roles.",
    },
}


# Requirements a job seeker may not meet, asked only when listed in filters.systemone.skip_if. The criteria name the
# near-misses that a phrase match gets wrong: equal-opportunity text, work authorisation and sponsorship policies.
def requirement_questions(country: str) -> dict:
    return {
        "citizenship": ("requires_citizenship", {
            "type": "noul",
            "instructions": f"Do `job.requirements` require the candidate to be a citizen of {country}?",
            "criteria": {
                "true": f"Citizenship of {country} is a stated requirement, for example \"must be a U.S. citizen\" or "
                        "\"citizenship required\", including for a clearance only citizens can hold.",
                "false": "Citizenship is not required: equal-opportunity statements that mention citizens, being "
                         "authorized to work in the country, visa sponsorship policies, or no mention at all.",
            }}, "requires citizenship"),
        "clearance": ("requires_clearance", {
            "type": "noul",
            "instructions": "Do `job.requirements` require a government security clearance, held now or obtained for "
                            "the role?",
            "criteria": {
                "true": "An active or obtainable security clearance is required, such as Secret, Top Secret, TS/SCI or "
                        "a polygraph.",
                "false": "No security clearance is required. A standard background check, drug screen or reference "
                         "check does not count.",
            }}, "requires a security clearance"),
    }


ELIGIBILITY_WORDS = {"citizen", "citizens", "citizenship", "clearance", "clearances", "secret", "polygraph",
                     "authorized", "authorization", "authorised", "sponsorship", "sponsor", "eligibility", "eligible",
                     "background", "investigation", "must"}


def opening(job: Job) -> dict:
    return {"job": {"title": job.title, "company": job.company,
                    "description": (job.description or "")[:OPENING_CHARS]}}


def requirement_lines(job: Job) -> dict:
    """The title and the description's sentences about experience, degrees, enrollment or qualifications."""
    kept, size = [], 0
    for sentence in _sentences(job.description or ""):
        tokens = set(_tokens(sentence))
        if tokens & REQUIREMENT_WORDS or any(t.startswith("graduat") for t in tokens):
            line = sentence.strip()
            if size + len(line) > EXCERPT_CHARS:
                break
            kept.append(line)
            size += len(line) + 1
    text = "\n".join(kept) or (job.description or "")[:EXCERPT_CHARS]
    return {"job": {"title": job.title, "requirements": text}}


def eligibility_lines(job: Job) -> dict:
    """The title and the lines about citizenship, clearance, work authorisation or other hard requirements. Whole lines,
    not sentences: "U.S. citizen" must not be cut at its dots."""
    kept, size = [], 0
    for raw in (job.description or "").splitlines():
        tokens = set(_tokens(raw))
        if tokens & (ELIGIBILITY_WORDS | REQUIREMENT_WORDS):
            line = raw.strip()
            if size + len(line) > EXCERPT_CHARS:
                break
            kept.append(line)
            size += len(line) + 1
    return {"job": {"title": job.title, "requirements": "\n".join(kept) or (job.description or "")[:EXCERPT_CHARS]}}


class SystemOneFilter:
    name = "systemone"

    def __init__(self, model: SystemOneClient, ask_students: bool, skip_if: list[str] | None = None,
                 country: str = "the country where the job is based"):
        self.model = model
        self.ask_students = ask_students      # only when the seniority filter leaves internships out
        self.skip_if = list(skip_if or [])    # requirement checks from filters.systemone.skip_if
        self.country = country

    def check(self, job: Job) -> FilterResult:
        p = self.model.noul("software_role", opening(job), SOFTWARE_ROLE)
        if p is not None and p < THRESHOLD:
            return skip(f"not a software engineering role (model: {p:.2f})")
        if self.ask_students:
            p = self.model.noul("students_only", requirement_lines(job), STUDENTS_ONLY)
            if p is not None and p >= THRESHOLD:
                return skip(f"only for current students (model: {p:.2f})")
        if self.skip_if:
            questions, state = requirement_questions(self.country), eligibility_lines(job)
            for check in self.skip_if:
                name, question, reason = questions[check]
                p = self.model.noul(name, state, question)
                if p is not None and p >= THRESHOLD:
                    return skip(f"{reason} (model: {p:.2f})")
        return KEEP

    def report(self) -> str | None:
        return self.model.report()
