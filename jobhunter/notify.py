"""Slack and email alerts for notify decisions. A failed channel is logged (without its secrets) and never stops the
run; send() says whether any channel delivered, so an alert nobody received can be tried again."""
from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText
from typing import Callable, Mapping

import requests

from jobhunter.models import Job, ScoreResult
from jobhunter.settings import NotifySettings


def _one_line(text: str) -> str:
    """Line breaks would end an email header early ("header value appears to contain an embedded header")."""
    return " ".join((text or "").split())


def _failure(e: Exception) -> str:
    """What went wrong, never the webhook URL or server text that may quote it."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return f"{type(e).__name__}" + (f" (HTTP {status})" if status else "")


def _summary(job: Job, result: ScoreResult, resume_id: str) -> str:
    return (f"{job.title} at {job.company}\n"
            f"Location: {job.location}\n"
            f"Link: {job.url}\n\n"
            f"Score: {result.score}/10 ({result.model})\n"
            f"Resume: {resume_id}\n"
            f"Why: {result.reasoning}\n"
            f"Matched skills: {', '.join(result.matched_skills) or 'none'}\n"
            f"Gaps: {', '.join(result.keyword_gaps) or 'none'}\n"
            f"Source: {job.source} | Posted: {job.posted_at or 'unknown'}")


class Notifier:
    def __init__(self, settings: NotifySettings, env: Mapping[str, str] | None = None,
                 log: Callable[[str], None] = print):
        self.settings = settings
        self.env = os.environ if env is None else env
        self.log = log

    def send(self, job: Job, result: ScoreResult, resume_id: str, cover_letter: str | None = None) -> bool:
        """True when at least one channel delivered the alert, or none is set up."""
        body = _summary(job, result, resume_id)
        if cover_letter:
            body += f"\n\nCover letter draft:\n{cover_letter}"
        delivered = []
        if self.settings.slack:
            delivered.append(self._slack(job, result, body))
        if self.settings.email:
            delivered.append(self._email(job, result, body))
        return not delivered or any(delivered)

    def _slack(self, job: Job, result: ScoreResult, body: str) -> bool:
        url = self.env.get(self.settings.slack["webhook_env"], "")
        try:
            requests.post(url, json={"text": f":dart: *New match, {result.score}/10*\n{body}"},
                          timeout=10).raise_for_status()
        except Exception as e:
            self.log(f"Slack send failed [{_one_line(job.title)}]: {_failure(e)}")
            return False
        return True

    def _email(self, job: Job, result: ScoreResult, body: str) -> bool:
        cfg = self.settings.email
        sender, password, to = (self.env.get(cfg[k], "") for k in ("from_env", "password_env", "to_env"))
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[{result.score}/10] {_one_line(job.title)} at {_one_line(job.company)} ({job.source})"
        msg["From"], msg["To"] = sender, to
        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.ehlo()
                server.starttls()
                server.login(sender, password)
                server.sendmail(sender, to, msg.as_string())
        except Exception as e:
            self.log(f"Email send failed [{_one_line(job.title)}]: {_failure(e)}")
            return False
        return True
