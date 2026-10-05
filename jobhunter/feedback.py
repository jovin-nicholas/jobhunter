"""Feedback buttons on alert emails: each opens a pre-filled email to the user's own plus address, whose subject
`jh:<verdict>:<job_id>` jobhunter reads back over IMAP (inbox.py). No I/O here."""
from __future__ import annotations

import re
from urllib.parse import quote

from jobhunter.models import Job

VERDICTS = {"applied": "notify", "good": "notify", "maybe": "log", "bad": "skip"}   # verdict -> training label
BUTTONS = [("applied", "✅ Applied"), ("good", "👍 Good"), ("maybe", "🤷 Maybe"), ("bad", "👎 Bad match")]
DEFAULT_ALERT_TAG = "jobhunter"
DEFAULT_FEEDBACK_TAG = "jobhunter-feedback"
LABEL = "jobhunter/feedback"           # Gmail label read messages get; Gmail search writes it label:jobhunter-feedback
TAG_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PREFIX_RE = re.compile(r"^(?:(?:re|fwd?)\s*:\s*)+", re.I)


def plus_address(address: str, tag: str) -> str:
    """sender@example.com + tag -> sender+tag@example.com; an existing +tag is replaced."""
    local, _, domain = address.strip().rpartition("@")
    return f"{local.split('+', 1)[0]}+{tag}@{domain}"


def mailto(feedback_address: str, verdict: str, job: Job) -> str:
    """The address's + is written %2B: some mail apps read a bare + in a mailto link as a space."""
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    subject = f"jh:{verdict}:{job.id}"
    body = f"Feedback for: {' '.join(job.title.split())} at {' '.join(job.company.split())}"
    return f"mailto:{quote(feedback_address, safe='@')}?subject={quote(subject, safe='')}&body={quote(body, safe='')}"


def parse_subject(subject: str | None) -> tuple[str, str] | None:
    """(verdict, job_id) from a feedback subject, or None when it is not one."""
    text = _PREFIX_RE.sub("", (subject or "").strip())
    parts = text.split(":", 2)
    if len(parts) != 3 or parts[0].lower() != "jh":
        return None
    verdict, job_id = parts[1].strip().lower(), parts[2].strip()
    if verdict not in VERDICTS or not job_id:
        return None
    return verdict, job_id
