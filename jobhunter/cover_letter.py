"""Cover-letter drafts for notified jobs, written by one of the text-capable scorers (off by default)."""
from __future__ import annotations

from typing import Any, Callable

from jobhunter.models import Job, Resume
from jobhunter.scorers.prompt import POSTING_END, POSTING_START, UNTRUSTED, fenced

PROMPT = """You are the candidate applying for this role. Write the body of a cover letter: 250-300 words in three
paragraphs, from the resume and the job below.
{untrusted}

1. Who you are and why this team's product or technical stack matters to you.
2. One or two concrete pieces of work from the resume that match the job's requirements, and the trade-offs you made.
3. How you work (ownership, reliability, collaboration), with an example from the resume.
End with one sentence offering to talk about how your experience fits the team.

Rules: use only facts from the resume; do not repeat its numbers; never write "I am writing to apply", "excited",
"passionate", "perfect fit", "resonates", "highly motivated" or "delighted". Return only the letter body.

RESUME:
{resume}

{start}
JOB: {title} at {company}
{description}
{end}
"""


def build_prompt(job: Job, resume: Resume, max_description_chars: int = 3000) -> str:
    return PROMPT.format(untrusted=UNTRUSTED, resume=resume.text, start=POSTING_START, end=POSTING_END,
                         title=fenced(job.title), company=fenced(job.company),
                         description=fenced(" ".join((job.description or "").split())[:max_description_chars]))


def write(writer: Any, job: Job, resume: Resume, log: Callable[[str], None]) -> str | None:
    """The letter, or None when the writer fails (the notification is then sent without one)."""
    try:
        text = writer.generate(build_prompt(job, resume)).strip()
    except Exception as e:
        log(f"cover letter failed [{job.title} at {job.company}]: {e}")
        return None
    return text or None
